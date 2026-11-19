"""Fast, unbiased multi-crop heightmap extraction (runs in WSL, needs bpy). NEW file; 02_extract_heightmap.py is untouched
(its collect()/unhide_render_visible()/cast_grid() are imported and reused unchanged).

Differences to 02_extract_heightmap.py
  * ONE acceleration structure per scene, built from numpy arrays via bmesh (C-level) instead of Python lists of tuples,
    restricted to triangles near the crop windows (terrain + boulders + scatter rocks only, same filter as before).
  * Triangle edge statistics only for triangles overlapping each crop (the old script recomputed them for ALL ~21 M triangles
    for every candidate boulder: that was the 40 s .. 790 s variance).
  * K crops at random positions in the scene interior (uniform in [-R, R]^2, default R = 60 m); no boulder centring,
    boulder-free crops allowed. Explicit crops (--crop-at X Y) are added first (used to compare against the old extraction).
  * Per crop: position, relief, terrain/boulder/rock median edge, hole fraction, contains-boulder, distance to the scene camera
    and whether the crop centre is inside the camera frustum (terrain LOD is view dependent: in-view 8x finer than out-of-view).
Outputs in --out:  crop_NN/{heightmap_raw.npy,heightmap_patch.txt,unresolved_mask.npy,stats.json}, crops.json, contact_sheet.png, timing.json
"""
import argparse
import importlib.util
import json
import sys
import time
from pathlib import Path

import bpy
import bmesh  # must come after bpy
import mathutils
import numpy as np
from bpy_extras.object_utils import world_to_camera_view
from mathutils.bvhtree import BVHTree

TMP = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(TMP))
spec = importlib.util.spec_from_file_location("old_extract", TMP / "02_extract_heightmap.py")
old = importlib.util.module_from_spec(spec)
spec.loader.exec_module(old)            # defines collect(), unhide_render_visible(), cast_grid(); main is guarded
from hm_io import write_heightmap       # noqa: E402

NAMES = {0: "terrain", 1: "boulder", 2: "rock_scatter"}


def build_bvh(verts, tris):
    """BVH from numpy arrays via a temporary Mesh + bmesh (no Python-list conversion)."""
    me = bpy.data.meshes.new("hf_tmp")
    nv, nt = len(verts), len(tris)
    me.vertices.add(nv)
    me.vertices.foreach_set("co", verts.astype(np.float32).ravel())
    me.loops.add(nt * 3)
    me.loops.foreach_set("vertex_index", tris.astype(np.int32).ravel())
    me.polygons.add(nt)
    me.polygons.foreach_set("loop_start", (np.arange(nt, dtype=np.int32) * 3))
    me.polygons.foreach_set("loop_total", np.full(nt, 3, np.int32))
    me.update(calc_edges=True)
    bm = bmesh.new()
    bm.from_mesh(me)
    bvh = BVHTree.FromBMesh(bm)
    return bvh, bm, me


def tri_bboxes(verts, tris, chunk=2_000_000):
    lo = np.empty((len(tris), 2), np.float32)
    hi = np.empty((len(tris), 2), np.float32)
    for i in range(0, len(tris), chunk):
        t = verts[tris[i:i + chunk]][:, :, :2]
        lo[i:i + chunk] = t.min(1)
        hi[i:i + chunk] = t.max(1)
    return lo, hi


def overlap(lo, hi, cx, cy, half):
    return (hi[:, 0] > cx - half) & (lo[:, 0] < cx + half) & (hi[:, 1] > cy - half) & (lo[:, 1] < cy + half)


def edge_stats_idx(verts, tris, src, idx):
    out = {}
    for key, k in [("all", None)] + [(NAMES[i], i) for i in (0, 1, 2)]:
        sel = idx if k is None else idx[src[idx] == k]
        if len(sel) == 0:
            continue
        t = verts[tris[sel]]
        e = np.concatenate([np.linalg.norm(t[:, i] - t[:, (i + 1) % 3], axis=1) for i in range(3)])
        out[key] = dict(n_tris=int(len(sel)), edge_median_m=float(np.median(e)), edge_p10_m=float(np.percentile(e, 10)),
                        edge_p90_m=float(np.percentile(e, 90)), edge_max_m=float(e.max()))
    return out


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--blend", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--n-crops", type=int, default=10)
    ap.add_argument("--crop-at", nargs=2, type=float, action="append", default=[], metavar=("X", "Y"),
                    help="explicit crop centre(s), taken before the random ones")
    ap.add_argument("--size", type=float, default=5.0)
    ap.add_argument("--dx", type=float, default=0.025)
    ap.add_argument("--interior", type=float, default=60.0, help="random centres uniform in [-R, R]^2 (Blender world m)")
    ap.add_argument("--rng-seed", type=int, default=12345)
    a = ap.parse_args()
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    T = {}
    t_all = time.time()

    t0 = time.time(); bpy.ops.wm.open_mainfile(filepath=a.blend); T["open_blend_s"] = time.time() - t0
    old.unhide_render_visible()
    dg = bpy.context.evaluated_depsgraph_get()
    t0 = time.time(); verts, tris, rows, src = old.collect(dg); T["collect_s"] = time.time() - t0
    T["n_triangles_total"] = int(len(tris))
    t0 = time.time(); lo, hi = tri_bboxes(verts, tris); T["tri_bbox_s"] = time.time() - t0

    cam = bpy.context.scene.camera
    cam_xyz = np.array(cam.matrix_world.translation) if cam else np.zeros(3)
    rng = np.random.default_rng(a.rng_seed)
    centers = [np.array(c) for c in a.crop_at] + [rng.uniform(-a.interior, a.interior, 2) for _ in range(a.n_crops)]
    half = a.size / 2

    # --- single acceleration structure over the union of crop windows (+1 m margin) -------------------------------------
    t0 = time.time()
    m = np.zeros(len(tris), bool)
    for c in centers:
        m |= overlap(lo, hi, c[0], c[1], half + 1.0)
    sub = np.nonzero(m)[0]
    T["n_triangles_in_bvh"] = int(len(sub))
    stris = tris[sub]
    used, inv = np.unique(stris, return_inverse=True)
    bvh, bm, me = build_bvh(verts[used], inv.reshape(-1, 3))
    zmax = float(verts[used][:, 2].max()) + 10
    T["bvh_build_s"] = time.time() - t0

    # --- rasterize crops ---------------------------------------------------------------------------------------------------
    n = int(round(a.size / a.dx))
    summary = []
    t_ray = t_stat = 0.0
    for i, c in enumerate(centers):
        cx, cy = float(c[0]), float(c[1])
        cd = out / f"crop_{i:02d}"
        cd.mkdir(exist_ok=True)
        xs = cx - half + (np.arange(n) + 0.5) * a.dx
        ys = cy - half + (np.arange(n) + 0.5) * a.dx
        t0 = time.time()
        z0, _, _ = old.cast_grid(bvh, xs[::20], ys[::20], zmax)
        z_floor = float(np.nanmedian(z0)) - 20.0 if not np.isnan(z0).all() else -1e9
        z, n_retried, n_unres = old.cast_grid(bvh, xs, ys, zmax, z_floor=z_floor)
        t_ray += time.time() - t0
        unres = np.isnan(z) | (z < z_floor)
        np.save(cd / "unresolved_mask.npy", unres)
        if n_unres:                                   # fill unresolved cells from neighbours (as in 02)
            for _ in range(10):
                bad = np.isnan(z)
                if not bad.any():
                    break
                p = np.pad(z, 1, constant_values=np.nan)
                nb = np.stack([p[ii:ii + z.shape[0], jj:jj + z.shape[1]] for ii in range(3) for jj in range(3)])
                with np.errstate(all="ignore"):
                    z = np.where(bad, np.nanmean(nb, axis=0), z)
            z = np.nan_to_num(z, nan=float(np.nanmedian(z)))
        ref = float(np.nanmedian(z))
        rel = z - ref
        np.save(cd / "heightmap_raw.npy", z)
        write_heightmap(cd / "heightmap_patch.txt", rel, 0.0, 0.0, a.dx, reverse_y=True)
        t0 = time.time()
        idx = np.nonzero(overlap(lo, hi, cx, cy, half))[0]
        mesh = edge_stats_idx(verts, tris, src, idx)
        n_boulder = int((src[idx] == 1).sum())
        t_stat += time.time() - t0
        # view info
        zc = float(ref)
        v = mathutils.Vector((cx, cy, zc))
        cv = world_to_camera_view(bpy.context.scene, cam, v) if cam else mathutils.Vector((-1, -1, -1))
        in_frustum = bool(0 <= cv.x <= 1 and 0 <= cv.y <= 1 and cv.z > 0)
        st = dict(index=i, kind="explicit" if i < len(a.crop_at) else "random", center=[cx, cy], size_m=a.size, dx=a.dx, n=n,
                  ref_z=ref, z_min_rel=float(rel.min()), z_max_rel=float(rel.max()), relief_ptp_m=float(rel.max() - rel.min()),
                  rms_rel_m=float(rel.std()), hole_fraction=float(n_unres / z.size), cells_unresolved_filled=int(n_unres),
                  cells_retried=int(n_retried), contains_boulder=n_boulder > 0, n_boulder_tris=n_boulder, mesh=mesh,
                  terrain_edge_median_cm=(mesh["terrain"]["edge_median_m"] * 100 if "terrain" in mesh else None),
                  dist_to_camera_m=float(np.hypot(cx - cam_xyz[0], cy - cam_xyz[1])), in_camera_frustum=in_frustum)
        json.dump(st, open(cd / "stats.json", "w"), indent=1)
        summary.append(st)
    T["raycast_all_crops_s"] = t_ray
    T["crop_stats_s"] = t_stat
    T["total_s"] = time.time() - t_all
    json.dump(summary, open(out / "crops.json", "w"), indent=1)
    json.dump(T, open(out / "timing.json", "w"), indent=1)

    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    k = len(summary)
    cols = min(k, 6)
    fig, axs = plt.subplots((k + cols - 1) // cols, cols, figsize=(3.2 * cols, 3.4 * ((k + cols - 1) // cols)), squeeze=False)
    for ax in axs.ravel():
        ax.axis("off")
    for s_, ax in zip(summary, axs.ravel()):
        rel = np.load(out / f"crop_{s_['index']:02d}" / "heightmap_raw.npy")
        rel = rel - np.median(rel)
        ax.imshow(rel, origin="lower", cmap="viridis", vmin=-1, vmax=1.5)
        e = s_["terrain_edge_median_cm"]
        ax.set_title(f"#{s_['index']} {'BOULDER ' if s_['contains_boulder'] else ''}{'view ' if s_['in_camera_frustum'] else 'out '}"
                     f"d={s_['dist_to_camera_m']:.0f}m\nedge {e:.1f}cm ptp {s_['relief_ptp_m']:.2f}m", fontsize=7)
    fig.tight_layout(); fig.savefig(out / "contact_sheet.png", dpi=90)
    print(json.dumps(T, indent=1))
    for s_ in summary:
        print(f"crop {s_['index']:2d} c=({s_['center'][0]:7.1f},{s_['center'][1]:7.1f}) ptp={s_['relief_ptp_m']:.2f} edge={s_['terrain_edge_median_cm']} "
              f"holes={s_['hole_fraction']:.3f} boulder={s_['contains_boulder']} dist={s_['dist_to_camera_m']:.0f} inview={s_['in_camera_frustum']}")
