"""MASTODON side of the speed test, standard library only: libconf-style writer, config templates (the frozen sonar spec of Task L / K),
heightmap txt I/O (same file format as tmp/hm_io.py) and map building."""
import math
import os
import random

# ----------------------------------------------------------------------------- libconf writer
def _fmt(v, ind):
    pad = "  " * ind
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        s = repr(v)
        return s if ("." in s or "e" in s or "inf" in s or "nan" in s) else s + ".0"
    if isinstance(v, str):
        return '"' + v.replace("\\", "\\\\").replace('"', '\\"') + '"'
    if isinstance(v, (list, tuple)):
        return "(" + ", ".join(_fmt(x, ind) for x in v) + ")"
    if isinstance(v, dict):
        lines = ["{"]
        for k, x in v.items():
            if x is not None:
                lines.append("%s  %s = %s;" % (pad, k, _fmt(x, ind + 1)))
        lines.append(pad + "}")
        return "\n".join(lines)
    raise TypeError("cannot write %r" % (v,))


def dumps(cfg):
    return "\n".join("%s = %s;" % (k, _fmt(v, 0)) for k, v in cfg.items() if v is not None) + "\n"


def set_key(cfg, dotted, val):
    parts = dotted.split(".")
    node = cfg
    for p in parts[:-1]:
        node = node.setdefault(p, {})
    if val is None:
        node.pop(parts[-1], None)
    else:
        node[parts[-1]] = val


# ----------------------------------------------------------------------------- config template (spec "C real-aperture" of Task K/L)
DEPTH = 16.0
# frozen sonar overrides identical to tmp/L_main_test/lsim.py FROZEN (+ the base template of probe_lib.base_cfg)
FROZEN = {"sonars.projector.frequency": 520000, "sonars.projector.bandwidth": 63000, "sonars.receiver.sample_frequency": 504000, "sonars.receiver.baseband": True,
          "sonars.projector.window.model": "Rectangle", "sonars.projector.elem_height": 0.004, "sonars.receiver.elem_height": 0.004,
          "sonars.projector.num_elem_hor": 1, "sonars.receiver.num_elem_hor": 1, "sonars.projector.elem_width": None, "sonars.receiver.elem_width": None,
          "sonars.projector.hor_beamwidth": 0.37, "sonars.receiver.hor_beamwidth": 0.37, "postprocess.beamformer.type": "magnitude", "platforms.dping": 0.054,
          "environment.bottom.type": "Fine sand", "environment.bottom.model": "TR9407"}


def base_cfg(hm_path, ext, dx, num_pings, range_max, range_inc, angle_inc, altitude, threads):
    xmin, xmax, ymin, ymax = ext
    cfg = {
        "simulation": {"simulation": "MASTODON", "name": "speedtest", "type": "Imaging", "config_version": 1},
        "output": {"file": "StaveData.h5"},
        "log": {"file": "MASTLogFile.txt", "show_times": True, "verbose": True},
        "propagation": {"model": "Geometric", "angle_min": -90.0, "angle_max": 90.0, "angle_inc": float(angle_inc), "range_min": 0, "range_max": float(range_max),
                        "range_inc": float(range_inc), "bounce_surf_max": 2, "bounce_btm_max": 2, "max_target_eigenrays": 1, "write_eigenrays": False},
        "environment": {"model": "Constant",
                        "bottom": {"model": "TR9407", "dim": 0, "type": "Fine sand", "depth": DEPTH,
                                   "bathymetry": {"model": "User", "dx": float(dx), "dy": float(dx), "xmin": float(xmin), "xmax": float(xmax), "ymin": float(ymin),
                                                  "ymax": float(ymax), "outfile": "Depth.txt", "display": False, "infile": os.path.abspath(hm_path)}},
                        "ssp": {"dimension": 1, "depths": [0, DEPTH], "ssp": [1500, 1500]}},
        "sonars": {"projector": {"model": "Linear Rectangular", "name": "HB", "frequency": 200000, "bandwidth": 50000, "source_level": 120, "position": [0, 0, 0],
                                 "depression": 10, "bearing": -90, "elem_width": 0.021, "elem_height": 0.01, "num_elem_hor": 1, "num_elem_ver": 1, "signal": "LFM",
                                 "chirp_direction": "up", "pulse_length": 0.005},
                   "receiver": {"model": "Linear Rectangular", "name": "HB", "sample_frequency": 240000, "position": [0, 0, 0], "depression": 10, "bearing": -90,
                                "elem_width": 0.021, "elem_height": 0.01, "num_elem_hor": 13, "num_elem_ver": 1, "baseband": False}},
        "platforms": {"model": "Vehicle", "motion": "Linear", "bearing": 90, "dping": 0.1365, "num_pings": int(num_pings), "depth": DEPTH - float(altitude),
                      "radius": 0, "length": 0, "speed": 0.001},
        "postprocess": {"matchedfilter": True, "agc": True, "agcparams": {"power": 2}, "beamform": True,
                        "beamformer": {"output_file": "Beamformed.h5", "type": "wk", "display": False}},
        "calc": {"num_threads": int(threads)},
    }
    for k, v in FROZEN.items():
        set_key(cfg, k, v)
    return cfg


def build_profile_cfg(profile, hm_path, ext, dx, threads, extra=None):
    """profile: dict(pings, range_max (null = auto), range_inc, angle_inc, altitude). extra: dotted-key overrides from the config file."""
    H = float(profile.get("altitude", 3.0))
    rmax = profile.get("range_max")
    if rmax is None:
        rmax = round(math.sqrt(12.0 ** 2 + H ** 2) + 0.6, 2)
    cfg = base_cfg(hm_path, ext, dx, profile["pings"], rmax, profile.get("range_inc", 0.05), profile.get("angle_inc", 2.0), H, threads)
    for k, v in (extra or {}).items():
        set_key(cfg, k, v)
    return cfg


# ----------------------------------------------------------------------------- heightmap I/O (format of tmp/hm_io.py: "cols rows", "x0 dx", "y0 dy", values y-descending, x fastest)
def read_hm(path):
    with open(path) as f:
        cols, rows = (int(v) for v in f.readline().split())
        x0, dx = (float(v) for v in f.readline().split())
        y0, dy = (float(v) for v in f.readline().split())
        vals = [float(line) for line in f if line.strip()]
    if len(vals) != cols * rows:
        raise ValueError("%s: header says %dx%d=%d values, found %d" % (path, cols, rows, cols * rows, len(vals)))
    return dict(cols=cols, rows=rows, x0=x0, dx=dx, y0=y0, dy=dy, data=[vals[r * cols:(r + 1) * cols] for r in range(rows)])


def write_hm(path, hm):
    with open(path, "w") as f:
        f.write("%d %d\n%.6f %.6f\n%.6f %.6f\n" % (hm["cols"], hm["rows"], hm["x0"], hm["dx"], hm["y0"], hm["dy"]))
        zero = "0.000000\n" * hm["cols"]
        for row in hm["data"]:
            f.write(zero if row is None else "".join("%.6f\n" % v for v in row))


def synthetic_patch(size=5.0, dx=0.025, seed=1, amp=0.25):
    """Deterministic rough seafloor patch (sum of random sinusoids + white noise); stands in for an Infinigen patch."""
    n = int(round(size / dx))
    rng = random.Random(seed)
    waves = [(rng.uniform(0.3, 3.0), rng.uniform(0, 6.28), rng.uniform(0, 6.28), rng.uniform(0.2, 1.0)) for _ in range(8)]
    data = []
    for iy in range(n):
        y = (n - 1 - iy) * dx
        row = []
        for ix in range(n):
            x = ix * dx
            h = sum(a * math.sin(f * (x * math.cos(p) + y * math.sin(p)) + q) for f, p, q, a in waves) * amp / 4 + rng.gauss(0, 0.01)
            row.append(h)
        data.append(row)
    return dict(cols=n, rows=n, x0=0.0, dx=dx, y0=0.0, dy=dx, data=data)


def make_map(patch, ext, cx=10.0, taper=0.5):
    """Detrend (baseline = mean of the lowest 80 % of values), taper the edge (0.5 m) and paste the patch centred at (cx, 0) in a flat map of extent ext.
    Returns (map dict, relief_max)."""
    xmin, xmax, ymin, ymax = ext
    dx = patch["dx"]
    nx, ny = int(round((xmax - xmin) / dx)), int(round((ymax - ymin) / dx))
    flat = sorted(v for r in patch["data"] for v in r)
    base = sum(flat[:int(0.8 * len(flat))]) / max(1, int(0.8 * len(flat)))
    n_r, n_c = patch["rows"], patch["cols"]
    top = -1e9
    pr = []
    for r in range(n_r):
        row = []
        for c in range(n_c):
            d = min(c, n_c - 1 - c, r, n_r - 1 - r) * dx
            w = min(1.0, d / taper)
            v = (patch["data"][r][c] - base) * (0.5 - 0.5 * math.cos(math.pi * w))
            row.append(v)
            top = max(top, v)
        pr.append(row)
    r0 = int(round((ymax - n_r * dx / 2) / dx))
    c0 = int(round((cx - n_c * dx / 2 - xmin) / dx))
    if r0 < 0 or c0 < 0 or r0 + n_r > ny or c0 + n_c > nx:
        raise ValueError("patch (%dx%d) does not fit in map %dx%d at offset (%d,%d)" % (n_c, n_r, nx, ny, c0, r0))
    rows = [None] * ny
    for i in range(n_r):
        rows[r0 + i] = [0.0] * c0 + pr[i] + [0.0] * (nx - c0 - n_c)
    return dict(cols=nx, rows=ny, x0=xmin, dx=dx, y0=ymin, dy=dx, data=rows), top


def hm_stats(hm):
    """(top, finite, x0, dx) for st_validate.validate."""
    top, finite = -1e9, True
    for row in hm["data"]:
        if row is None:
            top = max(top, 0.0)
            continue
        for v in row:
            if v != v or v in (float("inf"), float("-inf")):
                finite = False
            elif v > top:
                top = v
    return top, finite, hm["x0"], hm["dx"]
