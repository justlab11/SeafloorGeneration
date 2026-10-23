#!/usr/bin/env python3
"""Helper: turn a beamformed .h5 file into raw and normalized images.

Saves raw.npy, raw.jpg, normed.npy, normed.jpg in the same folder as the beamformed file.
The .npy files are the unmodified arrays. The jpgs are drawn with physical proportions
(equal meters per pixel in range and along-track) when the geometry can be read from the
CONFIG.cfg in the same folder: columns span propagation.range_min..range_max (slant range)
and rows span the track, (platforms.num_pings - 1) * platforms.dping. Without a usable
config they fall back to one square pixel per sample, as before.

From another script:
    from visualize_beamformed import process_beamformed
    process_beamformed("test/toy_example/Beamformed.h5", section=(0, 5000, 0, 5000))

From the command line:
    python visualize_beamformed.py --beamformed-file test/toy_example/Beamformed.h5 --section 0 5000 0 5000
    python visualize_beamformed.py --beamformed-file .../Beamformed.h5 --axes          # labeled meter axes
    python visualize_beamformed.py --beamformed-file .../Beamformed.h5 --flip-track   # mirror along-track
"""
import argparse
import sys
from pathlib import Path

import h5py
import numpy as np
from matplotlib.figure import Figure

TRIM_FRAC = 0.2   # fraction of range columns dropped from the left of the normalized image


def _resolve_range(start, end, size, name):
    """An end <= 0 means 'to the end of the axis'. Returns validated (start, end)."""
    end = size if end <= 0 else min(end, size)
    if not 0 <= start < end:
        raise ValueError(f"invalid {name} range: start={start}, end={end} (axis size {size})")
    return start, end


def read_geometry(config_path):
    """Range span and track length (meters) from a MASTODON config, or None if unavailable."""
    config_path = Path(config_path)
    if not config_path.is_file():
        return None
    try:
        import libconf
        with open(config_path) as f:
            cfg = libconf.load(f)
        prop, plat = cfg["propagation"], cfg["platforms"]
        return {
            "range_min": float(prop.get("range_min", 0)),
            "range_max": float(prop["range_max"]),
            "track": (int(plat["num_pings"]) - 1) * float(plat["dping"]),
        }
    except Exception as e:   # e.g. unit strings like "2.1 km", missing keys, libconf not installed
        print(f"Could not read geometry from {config_path}: {e}")
        return None


def apply_mag(data, trim_frac=TRIM_FRAC, clip=16):
    """Divide each column by its mean, drop the first `trim_frac` of columns,
    rescale to mean 1, and clip the top at `clip`."""
    col_means = np.mean(data, axis=0)
    mag_normed = data / (col_means + 1e-10)
    a = mag_normed[:, int(trim_frac * mag_normed.shape[1]):]
    a /= a.mean()
    a = np.minimum(clip, a)
    return a


def save_image(data, path, extent=None, cmap="pink", axes=False, flip_track=False):
    """Save a 2D array as an image without touching pyplot's global state.

    extent = (left, right, bottom, top) in meters gives the image physical proportions.
    flip_track mirrors the along-track (vertical) axis; along-track then increases upward.
    """
    if flip_track:
        data = data[::-1]
        if extent is not None:
            extent = (extent[0], extent[1], extent[3], extent[2])
    fig = Figure(figsize=(6, 6))
    ax = fig.subplots(1, 1)
    ax.imshow(data, aspect="equal", cmap=cmap, extent=extent,
              interpolation="bilinear" if extent is not None else None)
    if axes and extent is not None:
        ax.set_xlabel("slant range (m)")
        ax.set_ylabel("along-track (m)")
    else:
        ax.axis("off")
    fig.tight_layout()
    fig.savefig(path, dpi=300, bbox_inches="tight", pad_inches=0.1 if axes else 0)


def process_beamformed(beamformed_file, section=(0, 0, 0, 0), config=None,
                       flip_track=False, axes=False):
    """
    Read the ping data, crop to `section`, and save raw + normalized outputs.

    section = (x_start, x_end, y_start, y_end). x indexes the first axis of the
    data (rows, along-track), y the second (columns, range). An end value of 0 means
    'to the end', so the default (0, 0, 0, 0) is the full image.

    config: path to the run's config (default: CONFIG.cfg next to the beamformed file).
    flip_track: mirror the jpgs along-track (the .npy files are never flipped).
    axes: draw labeled meter axes on the jpgs instead of a bare image.

    Returns a dict of the paths that were written.
    """
    beamformed_file = Path(beamformed_file)
    out_dir = beamformed_file.parent
    x_start, x_end, y_start, y_end = section

    with h5py.File(beamformed_file, "r") as f:
        ds = f["Sonar"]["Sensor1"]["Data1"]["PingData"]
        n_rows, n_cols = ds.shape[0], ds.shape[1]
        x_start, x_end = _resolve_range(x_start, x_end, n_rows, "x")
        y_start, y_end = _resolve_range(y_start, y_end, n_cols, "y")
        raw = ds[x_start:x_end, y_start:y_end]   # only reads the section from disk

    print(f"Section: x {x_start}:{x_end}, y {y_start}:{y_end} -> shape {raw.shape}")

    n_drop = int(TRIM_FRAC * raw.shape[1])       # columns apply_mag removes
    raw_extent = normed_extent = None
    geom = read_geometry(config if config is not None else out_dir / "CONFIG.cfg")
    if geom is not None:
        dx = (geom["range_max"] - geom["range_min"]) / n_cols   # meters per range column
        dy = geom["track"] / n_rows                             # meters per along-track row
        print(f"Pixel size: {dx * 100:.2f} cm per range column, {dy * 100:.2f} cm per row")

        def extent(col0, col1):
            return (geom["range_min"] + col0 * dx, geom["range_min"] + col1 * dx,
                    x_end * dy, x_start * dy)

        raw_extent = extent(y_start, y_end)
        normed_extent = extent(y_start + n_drop, y_end)
    else:
        print("No usable config: drawing one square pixel per sample")

    paths = {
        "raw_npy": out_dir / "raw.npy",
        "raw_jpg": out_dir / "raw.jpg",
        "normed_npy": out_dir / "normed.npy",
        "normed_jpg": out_dir / "normed.jpg",
    }

    np.save(paths["raw_npy"], raw)
    save_image(raw, paths["raw_jpg"], raw_extent, axes=axes, flip_track=flip_track)

    normed = apply_mag(raw)
    np.save(paths["normed_npy"], normed)
    save_image(normed, paths["normed_jpg"], normed_extent, axes=axes, flip_track=flip_track)

    for p in paths.values():
        print(f"Saved {p}")
    return paths


def parse_args(argv=None):
    p = argparse.ArgumentParser(
        description="Save raw and normalized images from a beamformed .h5 file.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--beamformed-file", required=True, type=Path,
                   help="path to the beamformed .h5 file")
    p.add_argument("--section", nargs=4, type=int, default=[0, 0, 0, 0],
                   metavar=("X_START", "X_END", "Y_START", "Y_END"),
                   help="crop region; an end of 0 means 'to the end' (0 0 0 0 = full image)")
    p.add_argument("--config", type=Path, default=None,
                   help="config used for the run (default: CONFIG.cfg next to the beamformed file)")
    p.add_argument("--flip-track", action="store_true",
                   help="mirror the jpgs along-track (the .npy files are not flipped)")
    p.add_argument("--axes", action="store_true",
                   help="draw labeled meter axes on the jpgs")

    args = p.parse_args(argv)
    if not args.beamformed_file.is_file():
        p.error(f"beamformed file not found: {args.beamformed_file}")
    return args


def main(argv=None):
    args = parse_args(argv)
    try:
        process_beamformed(args.beamformed_file, section=tuple(args.section), config=args.config,
                           flip_track=args.flip_track, axes=args.axes)
    except ValueError as e:
        sys.exit(f"error: {e}")
    return 0


if __name__ == "__main__":
    sys.exit(main())