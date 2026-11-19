"""Patch-only heightmap extraction (WSL, needs bpy). Reuses helpers from tmp/overnight/fast_extract.py (imported, not edited).
One acceleration structure over the crop windows only; usage:
  python extract_patch.py --blend runs/X/fine/scene.blend --out results/X [--center X Y | --from-camera] [--size 5] [--dx 0.025] [--extra DX DY ...]
--from-camera: centre = scene camera xy (nadir camera => the point straight below it).
Outputs in --out: patch_NN/{heightmap_raw.npy,heightmap_patch.txt,stats.json}, patches.json, timing.json
"""
import argparse
import importlib.util
import json
import sys
import time
from pathlib import Path

import bpy
import mathutils
import numpy as np
from bpy_extras.object_utils import world_to_camera_view

TMP = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(TMP))
sp = importlib.util.spec_from_file_location("fast_extract", TMP / "overnight" / "fast_extract.py")
fx = importlib.util.module_from_spec(sp)
sp.loader.exec_module(fx)               # defines build_bvh, tri_bboxes, overlap, edge_stats_idx; main is guarded
old = fx.old
from hm_io import write_heightmap       # noqa: E402



def collect_filtered(depsgraph, windows):
    """Same as old.collect (02_extract_heightmap.py) but skips instances whose world xy-bbox misses every window BEFORE calling to_mesh()
    (pebble scenes can have thousands of 78k-triangle instances; the unfiltered version is OOM-killed). windows = [(x0,y0,x1,y1)]."""
    verts, tris, rows, srcs = [], [], [], []
    off = 0
    for inst in depsgraph.object_instances:
        ob = inst.object
        if ob.type != "MESH":
            continue
        mw = np.array(inst.matrix_world)
        bb = np.array([list(c) for c in ob.bound_box]) @ mw[:3, :3].T + mw[:3, 3]
        lo_, hi_ = bb.min(0), bb.max(0)
        if not any(hi_[0] > w[0] and lo_[0] < w[2] and hi_[1] > w[1] and lo_[1] < w[3] for w in windows):
            continue
        parents = [inst.parent.name] if inst.parent else []
        if inst.is_instance and inst.instance_object is not None:
            parents.append(inst.instance_object.name)
        colls = [c.name for c in ob.original.users_collection]
        keep = old.want(ob, parents + colls)
        me = ob.to_mesh()
        if me is None or len(me.vertices) == 0:
            continue
        co = np.empty(len(me.vertices) * 3, dtype=np.float32)
        me.vertices.foreach_get("co", co)
        wc = co.reshape(-1, 3) @ mw[:3, :3].T + mw[:3, 3]
        me.calc_loop_triangles()
        tr = np.empty(len(me.loop_triangles) * 3, dtype=np.int32)
        me.loop_triangles.foreach_get("vertices", tr)
        rows.append(dict(name=ob.name, parents=parents, collections=colls, instance=bool(inst.is_instance), keep=keep, nverts=len(wc),
                         ntris=len(tr) // 3, bbox_min=wc.min(0).round(2).tolist(), bbox_max=wc.max(0).round(2).tolist()))
        if keep:
            verts.append(wc); tris.append(tr.reshape(-1, 3) + off)
            cn = " ".join(colls)
            srcs.append(np.full(len(tr) // 3, 2 if cn.startswith("assets:") else 1 if "unique_assets" in cn else 0, np.int8))
            off += len(wc)
        ob.to_mesh_clear()
    if verts:
        return np.concatenate(verts), np.concatenate(tris), rows, np.concatenate(srcs)
    return np.zeros((0, 3)), np.zeros((0, 3), int), rows, np.zeros(0, np.int8)


ap = argparse.ArgumentParser()
ap.add_argument("--blend", required=True)
ap.add_argument("--out", required=True)
ap.add_argument("--center", nargs=2, type=float, default=None)
ap.add_argument("--from-camera", action="store_true")
ap.add_argument("--extra", nargs=2, type=float, action="append", default=[], help="additional crop centres as offsets (dx dy) from the first")
ap.add_argument("--size", type=float, default=5.0)
ap.add_argument("--dx", type=float, default=0.025)
a = ap.parse_args()
out = Path(a.out)
out.mkdir(parents=True, exist_ok=True)
T = {}
t_all = time.time()
t0 = time.time(); bpy.ops.wm.open_mainfile(filepath=a.blend); T["open_blend_s"] = time.time() - t0
old.unhide_render_visible()
dg = bpy.context.evaluated_depsgraph_get()
cam = bpy.context.scene.camera
cam_xyz = np.array(cam.matrix_world.translation) if cam else np.full(3, np.nan)
c0 = np.array(a.center) if a.center else cam_xyz[:2]
centers = [c0] + [c0 + np.array(e) for e in a.extra]
half = a.size / 2
windows = [(c[0] - half - 1.0, c[1] - half - 1.0, c[0] + half + 1.0, c[1] + half + 1.0) for c in centers]
t0 = time.time(); verts, tris, rows, src = collect_filtered(dg, windows); T["collect_s"] = time.time() - t0
T["n_triangles_total"] = int(len(tris))
lo, hi = fx.tri_bboxes(verts, tris)
t0 = time.time()
m = np.zeros(len(tris), bool)
for c in centers:
    m |= fx.overlap(lo, hi, c[0], c[1], half + 1.0)
sub = np.nonzero(m)[0]
used, inv = np.unique(tris[sub], return_inverse=True)
bvh, bm, me = fx.build_bvh(verts[used], inv.reshape(-1, 3))
zmax = float(verts[used][:, 2].max()) + 10
T["bvh_build_s"] = time.time() - t0
n = int(round(a.size / a.dx))
res = []
for i, c in enumerate(centers):
    cx, cy = float(c[0]), float(c[1])
    cd = out / f"patch_{i:02d}"
    cd.mkdir(exist_ok=True)
    xs = cx - half + (np.arange(n) + 0.5) * a.dx
    ys = cy - half + (np.arange(n) + 0.5) * a.dx
    t0 = time.time()
    z0, _, _ = old.cast_grid(bvh, xs[::20], ys[::20], zmax)
    z_floor = float(np.nanmedian(z0)) - 20.0 if not np.isnan(z0).all() else -1e9
    z, n_retried, n_unres = old.cast_grid(bvh, xs, ys, zmax, z_floor=z_floor)
    T[f"raycast_{i}_s"] = time.time() - t0
    unres = np.isnan(z) | (z < z_floor)
    np.save(cd / "unresolved_mask.npy", unres)
    if n_unres:
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
    np.save(cd / "heightmap_raw.npy", z)
    write_heightmap(cd / "heightmap_patch.txt", z - ref, 0.0, 0.0, a.dx, reverse_y=True)
    idx = np.nonzero(fx.overlap(lo, hi, cx, cy, half))[0]
    mesh = fx.edge_stats_idx(verts, tris, src, idx)
    # object-level counts from the instance rows (boulders live in unique_assets, scattered pebbles are instances in assets:*)
    n_b = n_b_centre = n_p = n_p_centre = 0
    boulder_names = []
    for r in rows:
        if not r["keep"]:
            continue
        bmn, bmx = r["bbox_min"], r["bbox_max"]
        overl = bmx[0] > cx - half and bmn[0] < cx + half and bmx[1] > cy - half and bmn[1] < cy + half
        ctr = abs((bmn[0] + bmx[0]) / 2 - cx) < half and abs((bmn[1] + bmx[1]) / 2 - cy) < half
        cn = " ".join(r["collections"])
        if "unique_assets" in cn:
            n_b += overl; n_b_centre += ctr
            if overl:
                boulder_names.append(dict(name=r["name"], bbox_min=bmn, bbox_max=bmx))
        elif cn.startswith("assets:"):
            n_p += overl; n_p_centre += ctr
    corners = [mathutils.Vector((cx + sx * half, cy + sy * half, ref)) for sx in (-1, 1) for sy in (-1, 1)]
    cvs = [world_to_camera_view(bpy.context.scene, cam, v) for v in corners] if cam else []
    in_frustum = bool(cvs) and all(0 <= v.x <= 1 and 0 <= v.y <= 1 and v.z > 0 for v in cvs)
    rel = z - ref
    st = dict(index=i, center=[cx, cy], size_m=a.size, dx=a.dx, n=n, ref_z=ref, camera_xyz=cam_xyz.tolist(),
              camera_height_above_ref=float(cam_xyz[2] - ref) if cam else None, patch_in_frustum=in_frustum,
              relief_ptp_m=float(rel.max() - rel.min()), rms_rel_m=float(rel.std()), hole_fraction=float(n_unres / z.size),
              n_boulders_overlap=int(n_b), n_boulders_centre=int(n_b_centre), n_pebbles_overlap=int(n_p), n_pebbles_centre=int(n_p_centre),
              mesh=mesh, terrain_edge_median_cm=(mesh["terrain"]["edge_median_m"] * 100 if "terrain" in mesh else None),
              terrain_edge_p90_cm=(mesh["terrain"]["edge_p90_m"] * 100 if "terrain" in mesh else None), boulders=boulder_names)
    json.dump(st, open(cd / "stats.json", "w"), indent=1)
    res.append(st)
T["total_s"] = time.time() - t_all
json.dump(res, open(out / "patches.json", "w"), indent=1)
json.dump(T, open(out / "timing.json", "w"), indent=1)
for s in res:
    print(f"patch {s['index']} c=({s['center'][0]:.2f},{s['center'][1]:.2f}) ptp={s['relief_ptp_m']:.2f} rms={s['rms_rel_m']:.3f} "
          f"edge={s['terrain_edge_median_cm']} holes={s['hole_fraction']:.4f} boulders={s['n_boulders_overlap']} pebbles={s['n_pebbles_overlap']} "
          f"inview={s['patch_in_frustum']} camH={s['camera_height_above_ref']}")
print(json.dumps(T))
