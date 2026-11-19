"""Stage 2 (runs in WSL, needs bpy): extract a true top-down (orthographic) height field from an Infinigen scene.

Method: collect world-space triangles of the seafloor geometry (terrain + boulders + scattered rocks; NOT the water
surface, sky, plants, cameras), build a BVH, and cast one vertical ray per grid cell from above (first hit = top
surface). That is exactly an orthographic height field, independent of the scene camera and of image resolution.

Modes:
  --list                       print every object / instance with its collection, vertex count and bbox
  --patch cx cy [--size 5 --dx 0.025]   rasterize a patch centred at (cx, cy) (Blender world metres)
  --auto                       pick the patch automatically (see choose_patch)
Outputs (in --out): heightmap_raw.npy (absolute z, NaN = no hit), heightmap.txt (MASTODON format, relative to the
patch's median z... see --ref), preview.png, stats.json, mesh_edges.json (mesh resolution inside the patch).
"""
import argparse
import json
import sys
import time
from pathlib import Path

import bpy
import mathutils
import numpy as np
from mathutils.bvhtree import BVHTree

sys.path.insert(0, str(Path(__file__).resolve().parent))
from hm_io import write_heightmap   # noqa: E402

EXCLUDE_NAME = ("liquid", "placeholder", "water", "ocean", "atmosphere", "sky", "cloud", "caustic", "camera", "light", "lamp", "particle", "kelp",
                "coral", "fish", "sea", "urchin", "shell", "jelly")


def want(inst_obj, parent_names):
    """Seafloor geometry filter: terrain/ground, boulders, rocks. Decided from names (see --list output)."""
    name = (inst_obj.name + " " + " ".join(parent_names)).lower()
    if inst_obj.type != "MESH" or inst_obj.original.hide_render:
        return False
    if any(k in name for k in EXCLUDE_NAME):
        return False
    return True


def unhide_render_visible():
    """Infinigen hides scatter/unique_assets/... in the VIEWPORT (hide_viewport=True) but they render. The viewport
    depsgraph skips them, so make everything that is render-visible viewport-visible before evaluating."""
    def walk(lc):
        lc.hide_viewport = False
        lc.exclude = False
        for c in lc.children:
            walk(c)
    for vl in bpy.context.scene.view_layers:
        walk(vl.layer_collection)
    for c in bpy.data.collections:
        if not c.hide_render:
            c.hide_viewport = False
    for ob in bpy.data.objects:
        if not ob.hide_render:
            ob.hide_viewport = False
            try:
                ob.hide_set(False)
            except RuntimeError:
                pass


def collect(depsgraph, verbose=False):
    verts, tris, rows, srcs = [], [], [], []
    off = 0
    for inst in depsgraph.object_instances:
        ob = inst.object
        if ob.type != "MESH":
            continue
        parents = [inst.parent.name] if inst.parent else []
        if inst.is_instance and inst.instance_object is not None:
            parents.append(inst.instance_object.name)
        colls = [c.name for c in ob.original.users_collection]
        keep = want(ob, parents + colls)
        me = ob.to_mesh() if inst.is_instance or True else None
        if me is None or len(me.vertices) == 0:
            continue
        mw = np.array(inst.matrix_world)
        co = np.empty(len(me.vertices) * 3, dtype=np.float32)
        me.vertices.foreach_get("co", co)
        co = co.reshape(-1, 3)
        wc = co @ mw[:3, :3].T + mw[:3, 3]
        me.calc_loop_triangles()
        tr = np.empty(len(me.loop_triangles) * 3, dtype=np.int32)
        me.loop_triangles.foreach_get("vertices", tr)
        rows.append(dict(name=ob.name, parents=parents, collections=colls, instance=bool(inst.is_instance), keep=keep,
                         nverts=len(wc), ntris=len(tr) // 3, bbox_min=wc.min(0).round(2).tolist(),
                         bbox_max=wc.max(0).round(2).tolist()))
        if keep:
            verts.append(wc)
            tris.append(tr.reshape(-1, 3) + off)
            cn = " ".join(colls)
            srcs.append(np.full(len(tr) // 3, 2 if cn.startswith("assets:") else 1 if "unique_assets" in cn else 0, np.int8))
            off += len(wc)
        ob.to_mesh_clear()
    if verts:
        return np.concatenate(verts), np.concatenate(tris), rows, np.concatenate(srcs)
    return np.zeros((0, 3)), np.zeros((0, 3), int), rows, np.zeros(0, np.int8)


def cast_grid(bvh, xs, ys, z_top, z_floor=None, retries=6, jitter=2e-4):
    """First hit from above for every (x, y). Rays that slip through a crack between triangles hit the terrain's
    far-below skirt (z ~ -500) or nothing; such cells (z < z_floor, if given, or no hit) are retried with a tiny xy
    jitter. Returns (z, n_retried_cells, n_unresolved_cells); unresolved cells are NaN."""
    z = np.full((len(ys), len(xs)), np.nan, dtype=np.float32)
    d = mathutils.Vector((0, 0, -1))
    rng = np.random.default_rng(0)
    def one(x, y):
        hit = bvh.ray_cast(mathutils.Vector((float(x), float(y), z_top)), d)
        return hit[0].z if hit[0] is not None else np.nan
    for iy, y in enumerate(ys):
        for ix, x in enumerate(xs):
            z[iy, ix] = one(x, y)
    n_retried = 0
    if z_floor is not None:
        bad = np.argwhere(np.isnan(z) | (z < z_floor))
        n_retried = len(bad)
        for iy, ix in bad:
            z[iy, ix] = np.nan
            for _ in range(retries):
                v = one(xs[ix] + rng.uniform(-jitter, jitter), ys[iy] + rng.uniform(-jitter, jitter))
                if not np.isnan(v) and v >= z_floor:
                    z[iy, ix] = v
                    break
    return z, n_retried, int((np.isnan(z) | (z < (z_floor if z_floor is not None else -np.inf))).sum())


def edge_stats(verts, tris, cx, cy, half, src=None):
    """Triangle edge-length stats for triangles whose xy bbox overlaps the patch; overall and per source."""
    out = {}
    names = {0: "terrain", 1: "boulder", 2: "rock_scatter"}
    for key, sel_src in [("all", None)] + ([(names[k], k) for k in (0, 1, 2)] if src is not None else []):
        t = verts[tris]
        lo, hi = t[:, :, :2].min(1), t[:, :, :2].max(1)
        sel = (hi[:, 0] > cx - half) & (lo[:, 0] < cx + half) & (hi[:, 1] > cy - half) & (lo[:, 1] < cy + half)
        if sel_src is not None:
            sel &= src == sel_src
        tt = t[sel]
        if len(tt) == 0:
            continue
        e = np.concatenate([np.linalg.norm(tt[:, i] - tt[:, (i + 1) % 3], axis=1) for i in range(3)])
        out[key] = dict(n_tris=int(sel.sum()), edge_median_m=float(np.median(e)), edge_p10_m=float(np.percentile(e, 10)),
                        edge_p90_m=float(np.percentile(e, 90)), edge_max_m=float(e.max()))
    return out


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--blend", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--patch", nargs=2, type=float)
    ap.add_argument("--auto", action="store_true")
    ap.add_argument("--size", type=float, default=5.0)
    ap.add_argument("--dx", type=float, default=0.025)
    ap.add_argument("--min-boulder-height", type=float, default=1.0, help="auto: only consider boulders at least this tall (m)")
    ap.add_argument("--ref", choices=["median", "min", "mean"], default="median",
                    help="what z is subtracted so the patch is 'relative to the flat bottom'")
    a = ap.parse_args()
    out = Path(a.out); out.mkdir(parents=True, exist_ok=True)
    T = {}
    t0 = time.time()
    bpy.ops.wm.open_mainfile(filepath=a.blend)
    T["open_blend_s"] = time.time() - t0
    unhide_render_visible()
    dg = bpy.context.evaluated_depsgraph_get()
    t0 = time.time()
    verts, tris, rows, src = collect(dg)
    T["collect_s"] = time.time() - t0
    json.dump(rows, open(out / "objects.json", "w"), indent=1)
    kept = [r for r in rows if r["keep"]]
    print(f"objects/instances: {len(rows)} total, {len(kept)} kept, {len(tris)} triangles kept")
    if a.list:
        for r in rows:
            print(("KEEP " if r["keep"] else "skip ") + json.dumps(r))
        sys.exit(0)
    # crop to windows around the candidate patch centres: the rock scatter alone is ~20M triangles, far too many for a
    # python-list BVH. A triangle is kept if its centroid is within half+1 m of any window centre.
    cam = bpy.context.scene.camera
    cam_xy = (cam.matrix_world.translation.x, cam.matrix_world.translation.y) if cam else (0, 0)
    fwd = np.array([1.0, 0.0])
    if cam:
        f = cam.matrix_world.to_quaternion() @ mathutils.Vector((0, 0, -1))
        if np.hypot(f.x, f.y) > 0.2:
            fwd = np.array([f.x, f.y]) / np.hypot(f.x, f.y)
    # fallback candidates (used only if no populated boulder qualifies): points ahead of the camera
    ahead = [np.array(cam_xy) + d * fwd for d in (8.0, 12.0, 16.0, 20.0)]
    if a.auto:
        centers = [((np.array(r["bbox_min"]) + np.array(r["bbox_max"])) / 2)[:2] for r in kept
                   if any(c.startswith("unique_assets") for c in r["collections"])] + ahead
    else:
        centers = [np.array(a.patch)]
    m = np.zeros(len(tris), bool)
    for i0 in range(0, len(tris), 2_000_000):
        t = verts[tris[i0:i0 + 2_000_000]][:, :, :2]
        lo, hi = t.min(1), t.max(1)
        for c in centers:
            h = a.size / 2 + 1.0
            m[i0:i0 + 2_000_000] |= (hi[:, 0] > c[0] - h) & (lo[:, 0] < c[0] + h) & (hi[:, 1] > c[1] - h) & (lo[:, 1] < c[1] + h)
    print(f"crop: {len(tris)} -> {int(m.sum())} triangles (xy-bbox overlap with a window; keeps big coarse terrain tris)")
    tris = tris[m]; src = src[m]
    used, inv = np.unique(tris, return_inverse=True)
    verts = verts[used]
    tris = inv.reshape(-1, 3)
    t0 = time.time()
    bvh = BVHTree.FromPolygons([tuple(v) for v in verts.tolist()], [tuple(t) for t in tris.tolist()])
    T["bvh_s"] = time.time() - t0
    selected_by = "manual"
    if a.auto:
        cands = [r for r in rows if r["keep"] and any(c.startswith("unique_assets") for c in r["collections"])]
        print(f"auto: {len(cands)} populated rock/boulder objects in unique_assets")
        zmax = float(verts[:, 2].max()) + 10

        def score_candidate(cx, cy, height, name):
            h = a.size / 2
            zc = cast_grid(bvh, cx + np.array([-h, 0, h]), cy + np.array([-h, 0, h]), zmax)[0]
            if np.isnan(zc).any():
                return None
            st = edge_stats(verts, tris, cx, cy, h, src)
            if not st:
                return None
            tm = st.get("terrain", st["all"])["edge_median_m"]
            sc = tm - 0.01 * min(height, 1.5)   # fine TERRAIN mesh, tall boulder
            print(f"  cand {name} c=({cx:.1f},{cy:.1f}) h={height:.2f} terrain_edge_med={tm*100:.2f} cm score={sc:.4f}")
            return (sc, cx, cy, st, name)

        scored = []
        for r in cands:
            lo, hi = np.array(r["bbox_min"]), np.array(r["bbox_max"])
            res_ = score_candidate((lo[0] + hi[0]) / 2, (lo[1] + hi[1]) / 2, float(hi[2] - lo[2]), r["name"])
            if res_:
                scored.append((float(hi[2] - lo[2]), res_))
        best = None
        for tier, thr in (("boulder>=%.1fm" % a.min_boulder_height, a.min_boulder_height), ("boulder>=0.3m", 0.3), ("any_boulder", 0.0)):
            pool = [x[1] for x in scored if x[0] >= thr]
            if pool:
                best, selected_by = min(pool, key=lambda t: t[0]), tier
                break
        if best is None:
            pool = [score_candidate(c[0], c[1], 0.0, f"ahead_of_camera_{i}") for i, c in enumerate(ahead)]
            pool = [p for p in pool if p]
            if pool:
                best, selected_by = min(pool, key=lambda t: t[0]), "terrain_ahead_of_camera"
        if best is None:
            sys.exit("auto patch: no candidate patch fully covered")
        _, cx, cy, st, bname = best
        print("auto patch around", bname, "| selected_by", selected_by)
    else:
        cx, cy = a.patch
    half = a.size / 2
    n = int(round(a.size / a.dx))
    xs = cx - half + (np.arange(n) + 0.5) * a.dx
    ys = cy - half + (np.arange(n) + 0.5) * a.dx
    t0 = time.time()
    z_top = float(verts[:, 2].max()) + 10
    z0, _, _ = cast_grid(bvh, xs[::20], ys[::20], z_top)
    z_floor = float(np.nanmedian(z0)) - 20.0          # anything >20 m below the patch median is the skirt
    z, n_retried, n_unresolved = cast_grid(bvh, xs, ys, z_top, z_floor=z_floor)
    T["raster_s"] = time.time() - t0
    unresolved_mask = np.isnan(z) | (z < z_floor)
    np.save(out / 'unresolved_mask.npy', unresolved_mask)
    nan_frac = float(np.isnan(z).mean())
    if n_unresolved:   # fill the few still-unresolved cells from valid neighbours (iterative 3x3 mean)
        for _ in range(10):
            bad = np.isnan(z)
            if not bad.any():
                break
            p = np.pad(z, 1, constant_values=np.nan)
            nb = np.stack([p[i:i + z.shape[0], j:j + z.shape[1]] for i in range(3) for j in range(3)])
            z = np.where(bad, np.nanmean(nb, axis=0), z)
    ref = {"median": np.nanmedian, "min": np.nanmin, "mean": np.nanmean}[a.ref](z)
    rel = np.nan_to_num(z - ref, nan=0.0)
    np.save(out / "heightmap_raw.npy", z)
    write_heightmap(out / "heightmap_patch.txt", rel, 0.0, 0.0, a.dx, reverse_y=True)  # patch-local coords, x0=y0=0
    stats = dict(selected_by=selected_by, n_populated_boulders=(len(cands) if a.auto else None), center=[cx, cy], size_m=a.size, dx=a.dx, n=n, nan_fraction=nan_frac, cells_retried=int(n_retried), cells_unresolved_filled=int(n_unresolved), ref=a.ref, ref_z=float(ref),
                 z_min_rel=float(rel.min()), z_max_rel=float(rel.max()), relief_ptp_m=float(rel.max() - rel.min()),
                 rms_rel_m=float(rel.std()), camera_xy=list(cam_xy), mesh=edge_stats(verts, tris, cx, cy, half, src), timing=T)
    json.dump(stats, open(out / "stats.json", "w"), indent=1)
    print(json.dumps(stats, indent=1))
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    fig, ax = plt.subplots(1, 2, figsize=(12, 5))
    im = ax[0].imshow(rel, origin="lower", extent=(0, a.size, 0, a.size), cmap="viridis")
    ax[0].set_title(f"height rel. to {a.ref} (m)  [Blender x right, y up]"); plt.colorbar(im, ax=ax[0])
    ax[0].set_xlabel("x (m)"); ax[0].set_ylabel("y (m)")
    gy, gx = np.gradient(rel, a.dx)
    ax[1].imshow(-(gx + gy), origin="lower", extent=(0, a.size, 0, a.size), cmap="gray")
    ax[1].set_title("slope shading (cm-scale detail)")
    fig.tight_layout(); fig.savefig(out / "preview.png", dpi=130)
