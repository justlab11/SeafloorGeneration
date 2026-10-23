#!/usr/bin/env python3
"""Turn a beamformed .h5 file into raw, normalized and grouped images.

Writes raw.npy/.jpg, normed.npy/.jpg and grouped.jpg next to the .h5 file. The jpgs have physical
proportions when the run's CONFIG.cfg is in the same folder. normed divides each range column by
its mean (columns shorter than the sonar height are left at 0). grouped.jpg stacks the heightmap,
raw and normed on one ground-range axis.

Example:
    from visualize_beamformed import process_beamformed
    process_beamformed("experiments/clay/Beamformed.h5")

    python src/visualize_beamformed.py --beamformed-file experiments/clay/Beamformed.h5
    python src/visualize_beamformed.py --beamformed-file .../Beamformed.h5 --section 0 5000 0 5000
    (also: --axes, --flip-track, --no-group, --slant-range, --depthmap PATH)
"""
import argparse
import math
import sys
from pathlib import Path

import h5py
import numpy as np
from matplotlib.figure import Figure

GROUND_STEP_M = 0.0125   # ground-range pixel size of the sonar panels in grouped.jpg


def _resolve_range(start, end, size, name):
    """Check a crop range against an axis; an end <= 0 means "to the end".

    Args:
        start, end: requested range.
        size: length of the axis.
        name: axis name, used in the error message.
    Returns:
        (start, end) clamped to the axis. Raises ValueError if the range is empty.
    """
    end = size if end <= 0 else min(end, size)
    if not 0 <= start < end:
        raise ValueError(f"invalid {name} range: start={start}, end={end} (axis size {size})")
    return start, end


def read_geometry(config_path):
    """Read the numbers the images need (range, track length, sonar height) from a MASTODON config.

    Args:
        config_path: the run's CONFIG.cfg.
    Returns:
        dict with range_min, range_max, track (m), num_pings, bottom_type and, when readable,
        water_depth and height (bottom depth - platform depth, m). None if the config is missing
        or unreadable.
    Example:
        geom = read_geometry("experiments/clay/CONFIG.cfg")
        geom["track"]   # (num_pings - 1) * dping
    """
    config_path = Path(config_path)
    if not config_path.is_file():
        return None
    try:
        import libconf
        with open(config_path) as f:
            cfg = libconf.load(f)
        prop, plat = cfg["propagation"], cfg["platforms"]
        geom = {
            "range_min": float(prop.get("range_min", 0)),
            "range_max": float(prop["range_max"]),
            "track": (int(plat["num_pings"]) - 1) * float(plat["dping"]),
        }
    except Exception as e:   # e.g. unit strings like "2.1 km", missing keys, libconf not installed
        print(f"Could not read geometry from {config_path}: {e}")
        return None
    try:
        geom["water_depth"] = float(cfg["environment"]["bottom"]["depth"])
        geom["height"] = geom["water_depth"] - float(plat["depth"])
    except Exception as e:
        print(f"Could not read the sonar height from {config_path}: {e}")
    geom["num_pings"] = int(plat["num_pings"])
    geom["bottom_type"] = str(cfg["environment"]["bottom"].get("type", "unknown"))
    return geom


def run_title(depthmap_path, geom):
    """Build the two-line title for grouped.jpg.

    Args:
        depthmap_path: heightmap txt used for the run, or None. Files laid out by
            make_ripple_variants.py give the ripple height, wavelength (from manifest.csv) and
            window; any other file is shown by name.
        geom: dict from read_geometry.
    Returns:
        The title string. Line 1 describes the heightmap, line 2 the bottom type, water depth,
        sonar height and ping count.
    Example:
        run_title(Path("example_depthmaps/ripple_sizes_fixed_wl/05cm/depthmap_nowindow_5.txt"), geom)
    """
    first = f"heightmap {Path(depthmap_path).name}" if depthmap_path else "no heightmap"
    if depthmap_path:
        p = Path(depthmap_path)
        parts = p.stem.split("_")
        if p.parent.name.endswith("cm") and len(parts) == 3 and parts[0] == "depthmap":
            window = "no window" if parts[1] == "nowindow" else f"window x = {parts[1].replace('-', ' to ')} m"
            wavelength = ""
            manifest = p.parent.parent.parent / "manifest.csv"
            try:
                import csv
                with open(manifest, newline="") as f:
                    for row in csv.DictReader(f):
                        if Path(row["txt"].replace("\\", "/")).parts[-3:] == p.parts[-3:]:
                            wavelength = f", {float(row['wavelength_m']):g} m wavelength"
                            break
            except Exception:
                pass
            first = (f"Sand ripples {int(p.parent.name[:-2])} cm crest-to-trough{wavelength}, "
                     f"{window}")
    bits = []
    if "bottom_type" in geom:
        bits.append(f"bottom: {geom['bottom_type']}")
    if "height" in geom:
        bits.append(f"water {geom['water_depth']:g} m deep, sonar {geom['height']:g} m above floor")
    bits.append(f"{geom['num_pings']} pings ({geom['track']:.1f} m track)")
    return f"{first}\n{'  |  '.join(bits)}"


# The original version: always dropped the first 20% of the range columns (10 m of a 50 m range),
# a rough guess at where the dead near-range ended. Kept for reference.
#
# def apply_mag(data, trim_frac=0.2, clip=16):
#     """Divide each column by its mean, drop the first `trim_frac` of columns,
#     rescale to mean 1, and clip the top at `clip`."""
#     col_means = np.mean(data, axis=0)
#     mag_normed = data / (col_means + 1e-10)
#     a = mag_normed[:, int(trim_frac * mag_normed.shape[1]):]
#     a /= a.mean()
#     a = np.minimum(clip, a)
#     return a


def apply_mag(data, first_col=0, clip=16):
    """Normalize each range column by its mean so only the texture is left.

    Args:
        data: array of raw amplitudes, pings x range samples (a trailing size-1 axis is fine).
        first_col: first column with a bottom echo (slant range >= sonar height). Earlier columns
            hold only noise and are left at 0.
        clip: upper limit for the normalized values.
    Returns:
        float32 array with the same shape as data; the normalized columns have mean 1.
    Example:
        normed = apply_mag(raw, first_col=1600)
    """
    out = np.zeros(data.shape, dtype=np.float32)
    valid = data[:, first_col:]
    a = valid / (valid.mean(axis=0) + 1e-10)
    a /= a.mean()
    out[:, first_col:] = a
    return np.minimum(clip, out)


def to_ground_range(img, r_left, dr, height, x_centers):
    """Resample slant-range columns onto a ground-range axis, x = sqrt(r^2 - height^2).

    Columns are averaged down to about GROUND_STEP_M first so the speckle is not aliased.

    Args:
        img: array, rows x slant-range columns.
        r_left: slant range at the left edge of img (m).
        dr: width of one column (m).
        height: sonar height above the floor (m).
        x_centers: ground ranges (m) to sample at.
    Returns:
        float32 array, rows x len(x_centers); NaN where there is no data.
    Example:
        x = (np.arange(4000) + 0.5) * 0.0125
        ground = to_ground_range(raw, 0.0, 50 / 16000, 5.0, x)
    """
    k = max(1, int(GROUND_STEP_M / dr))
    n = img.shape[1] // k * k
    img = img[:, :n].reshape(img.shape[0], n // k, k).mean(axis=2)
    dr = dr * k
    pos = (np.sqrt(x_centers ** 2 + height ** 2) - r_left) / dr - 0.5
    ok = (pos >= 0) & (pos <= img.shape[1] - 1)
    i0 = np.floor(pos[ok]).astype(int)
    i1 = np.minimum(i0 + 1, img.shape[1] - 1)
    f = (pos[ok] - i0).astype(np.float32)
    out = np.full((img.shape[0], len(x_centers)), np.nan, dtype=np.float32)
    out[:, ok] = img[:, i0] * (1 - f) + img[:, i1] * f
    return out


def save_image(data, path, extent=None, cmap="pink", axes=False, flip_track=False):
    """Save a 2D array as a jpg without touching pyplot's global state.

    Args:
        data: 2D array.
        path: output file.
        extent: (left, right, bottom, top) in meters for physical proportions; None = square pixels.
        cmap: matplotlib colormap name.
        axes: draw labeled meter axes instead of a bare image (needs extent).
        flip_track: mirror the along-track (vertical) axis.
    Returns:
        None; writes path.
    Example:
        save_image(raw, "raw.jpg", extent=(0, 50, 30.2, 0), axes=True)
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


def read_depthmap(path):
    """Load a heightmap txt for plotting.

    Args:
        path: txt with the header "cols rows", "min_x dx", "min_y dy", then one value per line
            (x fastest).
    Returns:
        (grid, extent). grid is [rows, cols] in meters with the rows reversed, which is how MASTODON
        reads them, so it lines up with the sonar images. extent = (x0, x1, y_bottom, y_top).
    Example:
        grid, extent = read_depthmap("example_depthmaps/ripple_sizes_fixed_wl/05cm/depthmap_nowindow_5.txt")
    """
    with open(path) as f:
        cols, rows = (int(v) for v in f.readline().split())
        min_x, dx = (float(v) for v in f.readline().split())
        min_y, dy = (float(v) for v in f.readline().split())
        grid = np.loadtxt(f).reshape(rows, cols)[::-1]
    return grid, (min_x, min_x + cols * dx, min_y + rows * dy, min_y)


def depthmap_from_config(config_path):
    """Find the heightmap a run used.

    Args:
        config_path: the run's CONFIG.cfg.
    Returns:
        Path in environment.bottom.bathymetry.infile if that file exists, else None.
    Example:
        depthmap_from_config("experiments/clay/CONFIG.cfg")
    """
    try:
        import libconf
        with open(config_path) as f:
            cfg = libconf.load(f)
        path = Path(cfg["environment"]["bottom"]["bathymetry"]["infile"])
        return path if path.is_file() else None
    except Exception as e:
        print(f"Could not find the heightmap from {config_path}: {e}")
        return None


def save_grouped(path, raw, normed, geom, sonar_extent, depthmap=None, flip_track=False,
                 ground_range=True, title=None, m_per_inch=5.0):
    """Save one stacked figure: heightmap, raw and normed, each with a colorbar.

    The panels share the x axis and the along-track axis (centered on 0) and the same meters per
    inch. Grey marks places with no data. The sonar panels use the full min-to-max color range,
    like raw.jpg and normed.jpg.

    Args:
        path: output jpg.
        raw, normed: sonar arrays, pings x range samples.
        geom: dict from read_geometry (needs range_max, and height for ground range).
        sonar_extent: (range_left, range_right, track_bottom, track_top) in meters, along-track
            centered on 0.
        depthmap: (grid, extent) from read_depthmap, or None to leave the heightmap out.
        flip_track: mirror the along-track axis.
        ground_range: draw the sonar panels in ground range (needs the sonar height) instead of
            slant range.
        title: figure title, see run_title.
        m_per_inch: figure scale.
    Returns:
        None; writes path.
    Example:
        save_grouped("grouped.jpg", raw, normed, geom, extent, read_depthmap(txt),
                     title=run_title(txt, geom))
    """
    height = geom.get("height")
    ground = ground_range and height is not None
    r_left, r_right, track_bottom, track_top = sonar_extent
    dr = (r_right - r_left) / raw.shape[1]

    panels = []   # (title, data, (left, right), cmap, vmin, vmax, (top, bottom), colorbar label)
    half = max(abs(track_bottom), abs(track_top))
    if ground:
        x_left = 0.0
        son_right = math.sqrt(r_right ** 2 - height ** 2)   # last ground range with data
        x = (np.arange(round(son_right / GROUND_STEP_M)) + 0.5) * GROUND_STEP_M
    else:
        x_left, son_right = r_left, r_right
    x_right = son_right
    if depthmap is not None:
        grid, ext = depthmap
        peak = np.abs(grid).max() or 1.0
        panels.append(("heightmap (as simulated)", grid, (ext[0], ext[1]), "viridis", -peak, peak,
                       (ext[3], ext[2]), "offset from flat floor (m)"))
        half = max(half, abs(ext[2]), abs(ext[3]))
        x_left, x_right = min(x_left, ext[0]), max(x_right, ext[1])
    rng = "ground" if ground else "slant"
    for name, img, label in (("raw", raw, "raw amplitude (a.u.)"),
                             ("normed", normed, "relative to column mean (1 = average)")):
        if ground:
            img = to_ground_range(img, r_left, dr, height, x)
            span = (0.0, son_right)
        else:
            span = (r_left, r_right)
        panels.append((f"{name} ({rng} range)", img, span, "pink", None, None,
                       (track_top, track_bottom), label))

    fig_w = (x_right - x_left) / m_per_inch + 2.2
    fig_h = len(panels) * (2 * half / m_per_inch + 0.7) + (1.3 if title else 0.4)
    fig = Figure(figsize=(fig_w, fig_h), layout="constrained")
    gs = fig.add_gridspec(len(panels), 2, width_ratios=[1, 0.025])
    first = None
    for i, (name, data, (left, right), cmap, vmin, vmax, (top, bottom), label) in enumerate(panels):
        ax = fig.add_subplot(gs[i, 0], sharex=first, sharey=first)
        first = first or ax
        ax.set_facecolor("0.85")
        im = ax.imshow(data, extent=(left, right, bottom, top), cmap=cmap, vmin=vmin, vmax=vmax,
                       aspect="auto", interpolation="bilinear")
        ax.set_title(name, fontsize=10)
        ax.set_ylabel("along-track (m)")
        if i < len(panels) - 1:
            ax.tick_params(labelbottom=False)
        fig.colorbar(im, cax=fig.add_subplot(gs[i, 1]), label=label)
    ax.set_xlabel("ground range x (m)" if ground else "range (m); heightmap x is ground range")
    first.set_xlim(x_left, x_right)
    first.set_ylim((-half, half) if flip_track else (half, -half))
    if title:
        fig.suptitle(title, fontsize=11)
    fig.savefig(path, dpi=150)


def process_beamformed(beamformed_file, section=(0, 0, 0, 0), config=None,
                       flip_track=False, axes=False, depthmap="auto", group=True,
                       ground_range=True):
    """Make the raw, normalized and grouped images from a beamformed .h5 file.

    Args:
        beamformed_file: path to Beamformed.h5. Outputs are written next to it.
        section: (x_start, x_end, y_start, y_end) crop. x indexes rows (along-track), y indexes
            columns (range). An end of 0 means "to the end"; the default is everything.
        config: the run's config (default: CONFIG.cfg next to the .h5).
        flip_track: mirror the jpgs along-track (the .npy files are never flipped).
        axes: labeled meter axes on raw.jpg and normed.jpg.
        depthmap: heightmap txt for grouped.jpg; "auto" reads it from the config, None leaves it out.
        group: also write grouped.jpg (needs a readable config).
        ground_range: draw the sonar panels of grouped.jpg in ground range instead of slant range.
    Returns:
        dict of written paths: raw_npy, raw_jpg, normed_npy, normed_jpg, and grouped_jpg if made.
    Example:
        paths = process_beamformed("experiments/clay/Beamformed.h5")
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

    first_col = 0                                # columns before this have no bottom echo yet
    raw_extent = None
    geom = read_geometry(config if config is not None else out_dir / "CONFIG.cfg")
    if geom is not None:
        dx = (geom["range_max"] - geom["range_min"]) / n_cols   # meters per range column
        dy = geom["track"] / n_rows                             # meters per along-track row
        print(f"Pixel size: {dx * 100:.2f} cm per range column, {dy * 100:.2f} cm per row")
        raw_extent = (geom["range_min"] + y_start * dx, geom["range_min"] + y_end * dx,
                      x_end * dy, x_start * dy)
        if "height" in geom:
            first_col = min(raw.shape[1], max(0, math.ceil((geom["height"] - geom["range_min"]) / dx)
                                              - y_start))
        else:
            print("No sonar height: normalizing every column")
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

    normed = apply_mag(raw, first_col)
    np.save(paths["normed_npy"], normed)
    save_image(normed, paths["normed_jpg"], raw_extent, axes=axes, flip_track=flip_track)

    if group and geom is not None:
        if isinstance(depthmap, str) and depthmap == "auto":
            depthmap = depthmap_from_config(config if config is not None else out_dir / "CONFIG.cfg")
        dm = read_depthmap(depthmap) if depthmap is not None else None
        # along-track is centered on 0 (the vehicle track is centered on y = 0)
        sonar_extent = (raw_extent[0], raw_extent[1], raw_extent[2] - geom["track"] / 2,
                        raw_extent[3] - geom["track"] / 2)
        paths["grouped_jpg"] = out_dir / "grouped.jpg"
        save_grouped(paths["grouped_jpg"], raw, normed, geom, sonar_extent, dm, flip_track,
                     ground_range=ground_range, title=run_title(depthmap, geom))
    elif group:
        print("No usable config: skipping grouped.jpg")

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
    p.add_argument("--depthmap", type=Path, default=None,
                   help="heightmap txt for the grouped image (default: the config's bathymetry.infile)")
    p.add_argument("--no-group", action="store_true",
                   help="skip grouped.jpg (heightmap, raw and normalized stacked)")
    p.add_argument("--slant-range", action="store_true",
                   help="keep the sonar panels of grouped.jpg in slant range instead of ground range")

    args = p.parse_args(argv)
    if not args.beamformed_file.is_file():
        p.error(f"beamformed file not found: {args.beamformed_file}")
    return args


def main(argv=None):
    args = parse_args(argv)
    try:
        process_beamformed(args.beamformed_file, section=tuple(args.section), config=args.config,
                           flip_track=args.flip_track, axes=args.axes,
                           depthmap=args.depthmap if args.depthmap else "auto",
                           group=not args.no_group, ground_range=not args.slant_range)
    except ValueError as e:
        sys.exit(f"error: {e}")
    return 0


if __name__ == "__main__":
    sys.exit(main())