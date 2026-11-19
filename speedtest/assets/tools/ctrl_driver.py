"""WSL wrapper around infinigen_examples.generate_nature (nothing in the repo or ~/ig is edited).
Adds two gin-configurable controls by monkeypatching, then runs the stock main():
  ctrl_camera.{enabled,xy,clearance,radius}   z of the nadir camera = max seabed z within `radius` of xy (+ clearance), measured on the
                                              coarse terrain BVH inside the pose proposal, so one coarse run is enough. The chosen pose is
                                              appended to $CTRL_LOG (json lines). With enabled=False the stock behaviour (and an explicit
                                              camera.camera_pose_proposal.override_loc=(x,y,z) in gin) is untouched.
  ctrl_pebbles.{scale,vol_density,scale_rand,scale_rand_axi,ground_offset,max_density,n,detail}   None = stock random draw.
                                              The stock U() draws are still consumed, so the RNG stream is unchanged.
Run from ~/ig:  python ctrl_driver.py <same args as generate_nature>
"""
import json
import logging
import os

import bpy  # noqa: F401  (must precede mathutils)
import gin
import numpy as np
from mathutils import Vector

import infinigen_examples.generate_nature as gn
import infinigen.core.placement.camera as cam_mod
import infinigen.assets.scatters.pebbles as pb
from infinigen.core import init

logger = logging.getLogger("ctrl_driver")


def _log(rec):
    if os.environ.get("CTRL_LOG"):
        with open(os.environ["CTRL_LOG"], "a") as f:
            f.write(json.dumps(rec) + "\n")


@gin.configurable("ctrl_camera")
def ctrl_camera(enabled=False, xy=(0.0, 0.0), clearance=6.0, radius=4.0, n=5, z_start=-1.0):
    return dict(enabled=enabled, xy=xy, clearance=clearance, radius=radius, n=n, z_start=z_start)


_orig_pose = cam_mod.camera_pose_proposal


def _patched_pose(scene_bvh, location_sample, *a, **kw):
    c = ctrl_camera()
    if not c["enabled"]:
        return _orig_pose(scene_bvh, location_sample, *a, **kw)
    x0, y0 = c["xy"]
    r, n = c["radius"], c["n"]
    zs = []
    for dx in np.linspace(-r, r, n):
        for dy in np.linspace(-r, r, n):
            hit = scene_bvh.ray_cast(Vector((x0 + dx, y0 + dy, c["z_start"])), Vector((0, 0, -1)))
            if hit[0] is not None:
                zs.append(hit[0].z)
    if not zs:
        logger.warning("ctrl_camera: no seabed hit below z_start at (%s,%s)", x0, y0)
        return None
    z = float(max(zs)) + c["clearance"]
    _log(dict(x=x0, y=y0, z=z, seabed_max=float(max(zs)), seabed_min=float(min(zs)), seabed_med=float(np.median(zs)),
              clearance=c["clearance"]))
    kw["override_loc"] = (x0, y0, z)
    return _orig_pose(scene_bvh=scene_bvh, location_sample=location_sample, *a, **kw)


cam_mod.camera_pose_proposal = _patched_pose


@gin.configurable("ctrl_pebbles")
def ctrl_pebbles(scale=None, vol_density=None, scale_rand=None, scale_rand_axi=None, ground_offset=None, max_density=None, n=None,
                 detail=None):
    return dict(scale=scale, vol_density=vol_density, scale_rand=scale_rand, scale_rand_axi=scale_rand_axi,
                ground_offset=ground_offset, max_density=max_density, n=n, detail=detail)


_orig_scatter = pb.scatter_instances
_KEYS = ("scale", "vol_density", "scale_rand", "scale_rand_axi", "ground_offset", "max_density")


def _scatter(*a, **kw):
    c = ctrl_pebbles()
    for k in _KEYS:
        if c[k] is not None:
            kw[k] = c[k]
    _log(dict(pebbles_effective={k: (float(kw[k]) if kw.get(k) is not None else None) for k in _KEYS},
              overridden=[k for k in _KEYS if c[k] is not None]))
    return _orig_scatter(*a, **kw)


pb.scatter_instances = _scatter
_orig_apply = pb.Pebbles.apply


def _apply(self, obj, n=5, detail=3, selection=None, **kw):
    c = ctrl_pebbles()
    n = c["n"] if c["n"] is not None else n
    detail = c["detail"] if c["detail"] is not None else detail
    return _orig_apply(self, obj, n=n, detail=detail, selection=selection, **kw)


pb.Pebbles.apply = _apply

if __name__ == "__main__":
    parser = gn.get_parser()
    args = init.parse_args_blender(parser)
    logging.getLogger("infinigen").setLevel(logging.INFO)
    logging.getLogger("infinigen.core.nodes.node_wrangler").setLevel(logging.CRITICAL)
    gn.main(args)
