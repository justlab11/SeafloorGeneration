#!/usr/bin/env python3
"""Run the ripple height x wavelength experiment and plot the contrast ratio.

The original ripple sweeps vary the ripple height with the wavelength either fixed at 1 m or scaled with
the height, so only seven of the sixteen combinations of height (5, 10, 20, 50 cm) and wavelength
(0.5, 1, 2, 5 m) exist. This script runs the combinations (every one by default, or only the missing
ones) for the five 10 m windows and both sonar heights, measures the contrast ratio of every run and
draws the plots. One folder per run:

    <output-folder>/runs/exp_<NN>cm_wl<W>m_<window>_<distance>/

Heightmaps are written once to <output-folder>/heightmaps/<NN>cm_wl<W>m/depthmap_<window>.txt and are
shared by both sonar heights. After each run only {ping}_raw.jpg, {ping}_normed.jpg, {ping}_grouped.jpg,
the config and run.log are kept; the large .h5, .npy and Depth.txt files are deleted. One row per run
goes to <output-folder>/stats.csv, and an interrupted sweep resumes where it stopped.

Contrast ratio (CR): the standard deviation of the normalized image inside the ripple window divided by
the standard deviation over the flat floor outside it (1.5 m margin on each side of the window, first and
last metre of the map left out), measured in ground range on the heightmap grid over |y| < 10 m. CR = 1
means the ripples are invisible.

Plots (written to <output-folder>/plots/):
    contrast_wavelength_grid.png     CR against window; one panel per ripple height and sonar height,
                                     one line per wavelength.
    contrast_heatmap_<window>_<d>.png  CR for one window and sonar height; ripple height by wavelength.

Example:
    python src/run_wavelength_experiments.py --exe-location ./bin/MASTODON.exe --workers 4
    python src/run_wavelength_experiments.py --exe-location ./bin/MASTODON.exe --combos missing \\
        --existing-stats tmp/ripple_stats.csv
    python src/run_wavelength_experiments.py --plot-only --output-folder experiments/wavelength
"""
import argparse
import csv
import math
import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import libconf
import numpy as np
from matplotlib.figure import Figure

import visualize_beamformed as vb
from make_ripple_variants import CELL, TOTAL_X, TOTAL_Y, ripple_field, x_window
from ripple_maker import save_heightmap_txt

CONFIG_TEMPLATE = Path("mastodon_configs/CONFIG.cfg")

HEIGHTS_CM = [5, 10, 20, 50]                      # ripple crest-to-trough
WAVELENGTHS_M = [0.5, 1.0, 2.0, 5.0]              # crest spacing
EXISTING = {(5, 0.5), (5, 1.0), (10, 1.0), (20, 1.0), (20, 2.0), (50, 1.0), (50, 5.0)}   # run by the earlier sweeps
WINDOWS = [(0, 10), (10, 20), (20, 30), (30, 40), (40, 50)]   # ground range (x) the ripples occupy, in m
DISTANCES = [5, 10]                               # sonar height above the floor (m)
WATER_DEPTH = 20.0
NUM_PINGS = 222                                   # ~30 m of track, the full 30 m map
SEED = 7
STEEPNESS = 0.1                                   # height / wavelength of the scaled sweep, to read its stats
FIELDS = ["height_cm", "wavelength_m", "window", "dist", "std_inside", "std_outside", "std_ratio"]


def window_label(window):
    """Name of a window for file names, e.g. "10-20".

    Args:
        window: (start, end) tuple in meters.
    Returns:
        str.
    """
    return f"{window[0]}-{window[1]}"


def run_name(height, wavelength, window, distance):
    """Folder name of one run.

    Args:
        height: ripple height in cm.
        wavelength: ripple wavelength in m.
        window: (start, end) tuple in meters.
        distance: sonar height above the floor in meters.
    Returns:
        str, e.g. "exp_20cm_wl2m_10-20_5".
    """
    return f"exp_{height:02d}cm_wl{wavelength:g}m_{window_label(window)}_{distance}"


def heightmap_path(root, height, wavelength, window):
    """Heightmap file of one height, wavelength and window (shared by both sonar heights).

    Args:
        root: <output-folder>/heightmaps.
        height: ripple height in cm.
        wavelength: ripple wavelength in m.
        window: (start, end) tuple in meters.
    Returns:
        Path.
    """
    return root / f"{height:02d}cm_wl{wavelength:g}m" / f"depthmap_{window_label(window)}.txt"


def write_heightmaps(root, combos):
    """Write every heightmap the sweep needs, skipping files that already exist.

    Done once, before any run starts, so parallel runs never read a file that is still being written.

    Args:
        root: <output-folder>/heightmaps.
        combos: list of (height_cm, wavelength_m).
    Returns:
        None.
    """
    nx = round(TOTAL_X / CELL)
    for h, wl in combos:
        field = None
        for window in WINDOWS:
            path = heightmap_path(root, h, wl, window)
            if path.is_file():
                continue
            if field is None:
                field = ripple_field(h, wl, seed=SEED)
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            save_heightmap_txt(tmp, field * x_window(nx, *window)[None, :], 0.0, CELL, -TOTAL_Y / 2, CELL)
            tmp.replace(path)   # a partly written file never has the final name


def contrast_ratio(folder, window):
    """Contrast ratio of a finished run, from its raw.npy, CONFIG.cfg and heightmap.

    Args:
        folder: run folder.
        window: (start, end) tuple in meters.
    Returns:
        (std_inside, std_outside, std_ratio).
    Example:
        s_in, s_out, cr = contrast_ratio(Path("runs/exp_20cm_wl1m_30-40_5"), (30, 40))
    """
    geom = vb.read_geometry(folder / "CONFIG.cfg")
    raw = np.load(folder / "raw.npy")[:, :, 0]
    n_rows, n_cols = raw.shape
    height = geom["height"]
    dr = (geom["range_max"] - geom["range_min"]) / n_cols
    y = -geom["track"] / 2 + (np.arange(n_rows) + 0.5) * (geom["track"] / n_rows)
    normed = vb.apply_mag(raw, math.ceil((height - geom["range_min"]) / dr))

    grid, ext = vb.read_depthmap(vb.depthmap_from_config(folder / "CONFIG.cfg"))
    x = (np.arange(grid.shape[1]) + 0.5) * CELL
    yy = ext[3] + (np.arange(grid.shape[0]) + 0.5) * CELL
    ground = vb.to_ground_range(normed, geom["range_min"], dr, height, x)
    row = np.interp(yy, y, np.arange(n_rows), left=np.nan, right=np.nan)
    ok = ~np.isnan(row) & (np.abs(yy) < 10)
    img = ground[np.round(row[ok]).astype(int)]

    a, b = window
    inside = (x > max(a, 1)) & (x < min(b, 48))
    outside = (x > 1) & (x < 48) & ((x < a - 1.5) | (x > b + 1.5))
    s_in, s_out = float(np.nanstd(img[:, inside])), float(np.nanstd(img[:, outside]))
    return s_in, s_out, s_in / s_out


def run_one(exe, root, height, wavelength, window, distance):
    """Run MASTODON once, make the images, measure the contrast ratio and delete the big files.

    Args:
        exe: path to the MASTODON executable.
        root: <output-folder> (absolute).
        height, wavelength, window, distance: the experiment.
    Returns:
        The row for stats.csv: [height_cm, wavelength_m, window, dist, std_inside, std_outside, std_ratio].
        Raises if the run fails; a failed run keeps its files and its log as {ping}_failed.log.
    """
    folder = root / "runs" / run_name(height, wavelength, window, distance)
    folder.mkdir(parents=True, exist_ok=True)
    config = folder / CONFIG_TEMPLATE.name
    shutil.copy(CONFIG_TEMPLATE, config)
    with open(config) as f:
        cfg = libconf.load(f)
    cfg["environment"]["bottom"]["bathymetry"]["infile"] = str(heightmap_path(root / "heightmaps", height, wavelength, window))
    cfg["environment"]["bottom"]["depth"] = WATER_DEPTH
    cfg["environment"]["ssp"]["depths"] = (0, WATER_DEPTH)   # must end at the bottom depth
    cfg["platforms"]["depth"] = WATER_DEPTH - distance        # vehicle depth below the surface
    cfg["platforms"]["num_pings"] = NUM_PINGS
    with open(config, "w") as f:
        libconf.dump(cfg, f)

    for name in ("Beamformed.h5", "StaveData.h5"):
        (folder / name).unlink(missing_ok=True)   # never pick up a stale file
    try:
        with open(folder / "run.log", "w") as log:
            subprocess.run([str(exe), str(config)], cwd=folder, check=True, stdout=log, stderr=subprocess.STDOUT)
        if not (folder / "Beamformed.h5").is_file():
            raise FileNotFoundError(f"{folder / 'Beamformed.h5'} was not created")
        vb.process_beamformed(folder / "Beamformed.h5")
        s_in, s_out, cr = contrast_ratio(folder, window)
    except Exception:
        log = folder / "run.log"
        if log.is_file():
            shutil.copy(log, folder / f"{NUM_PINGS}_failed.log")
        raise

    for kind in ("raw", "normed", "grouped"):
        (folder / f"{kind}.jpg").replace(folder / f"{NUM_PINGS}_{kind}.jpg")
    for name in ("Beamformed.h5", "StaveData.h5", "Depth.txt", "raw.npy", "normed.npy"):
        (folder / name).unlink(missing_ok=True)
    return [height, wavelength, window_label(window), distance, s_in, s_out, cr]


def read_stats(path):
    """Read a stats.csv written by this script.

    Args:
        path: csv file (missing is fine).
    Returns:
        dict (height_cm, wavelength_m, window, dist) -> std_ratio.
    """
    out = {}
    if Path(path).is_file():
        with open(path) as f:
            for r in csv.DictReader(f):
                out[(int(r["height_cm"]), float(r["wavelength_m"]), r["window"], int(r["dist"]))] = float(r["std_ratio"])
    return out


def read_existing_stats(path):
    """Read the contrast ratios of the earlier fixed/scaled-wavelength sweeps.

    Args:
        path: csv with columns set (fixed or scaled), height_cm, window, dist, std_ratio. The fixed set has a
            1 m wavelength and the scaled set a wavelength of height / STEEPNESS.
    Returns:
        dict (height_cm, wavelength_m, window, dist) -> std_ratio, without the "nowindow" runs.
    """
    out = {}
    with open(path) as f:
        for r in csv.DictReader(f):
            if r["window"] == "nowindow":
                continue
            h = int(r["height_cm"])
            wl = 1.0 if r["set"] == "fixed" else round(h / 100 / STEEPNESS, 3)
            out[(h, wl, r["window"], int(r["dist"]))] = float(r["std_ratio"])
    return out


def plot_grid(data, path):
    """Contrast ratio against window; one panel per sonar height (rows) and ripple height (columns).

    Args:
        data: dict (height_cm, wavelength_m, window, dist) -> contrast ratio.
        path: output png.
    Returns:
        None; writes path.
    """
    labels = [window_label(w) for w in WINDOWS]
    shades = dict(zip(WAVELENGTHS_M, ["#c6dbef", "#6baed6", "#2171b5", "#08306b"]))   # short -> long
    fig = Figure(figsize=(12, 7.6), layout="constrained")
    for sub, dist in zip(fig.subfigures(2, 1, hspace=0.06), DISTANCES):
        sub.suptitle(f"Sonar {dist} m above the floor", fontsize=12, fontweight="bold")
        for ax, h in zip(sub.subplots(1, len(HEIGHTS_CM), sharex=True, sharey=True), HEIGHTS_CM):
            ax.axhline(1, color="gray", ls="--", lw=1)
            for wl in WAVELENGTHS_M:
                ys = [data.get((h, wl, lab, dist), np.nan) for lab in labels]
                if not np.isnan(ys).all():
                    ax.plot(range(len(labels)), ys, "-o", color=shades[wl], lw=2, ms=4, label=f"{wl:g} m")
            ax.set_title(f"{h} cm ripples", fontsize=10)
            ax.set_xticks(range(len(labels)), labels, fontsize=8)
            ax.set_xlabel("Ripple window (m, ground range)")
            ax.spines[["top", "right"]].set_visible(False)
            ax.legend(title="wavelength", fontsize=8, title_fontsize=8, frameon=False, loc="upper left")
        sub.axes[0].set_ylabel("Contrast ratio")
    fig.savefig(path, dpi=200)


def plot_heatmap(data, window, distance, path):
    """Contrast ratio of one window and sonar height as a ripple height by wavelength table.

    Args:
        data: dict (height_cm, wavelength_m, window, dist) -> contrast ratio.
        window: window label such as "30-40".
        distance: sonar height in meters.
        path: output png.
    Returns:
        None; writes path.
    """
    table = np.array([[data.get((h, wl, window, distance), np.nan) for wl in WAVELENGTHS_M] for h in HEIGHTS_CM])
    fig = Figure(figsize=(5.6, 4.6), layout="constrained")
    ax = fig.subplots()
    im = ax.imshow(table, cmap="viridis", origin="lower", aspect="auto")
    ax.set_xticks(range(len(WAVELENGTHS_M)), [f"{w:g}" for w in WAVELENGTHS_M])
    ax.set_yticks(range(len(HEIGHTS_CM)), [str(h) for h in HEIGHTS_CM])
    ax.set_xlabel("Wavelength (m)")
    ax.set_ylabel("Ripple height (cm)")
    ax.set_title(f"Contrast ratio, window {window} m, sonar {distance} m", fontsize=10)
    for i in range(table.shape[0]):
        for j in range(table.shape[1]):
            if not np.isnan(table[i, j]):
                ax.text(j, i, f"{table[i, j]:.2f}", ha="center", va="center", fontsize=9,
                        color="white" if table[i, j] < np.nanmean(table) else "black")
    fig.colorbar(im, ax=ax, label="contrast ratio")
    fig.savefig(path, dpi=200)


def make_plots(out_folder, existing_stats, heatmap_window, more_stats=()):
    """Merge the stats and write every plot.

    Args:
        out_folder: <output-folder>.
        existing_stats: path of the earlier sweeps' csv, or None.
        heatmap_window: window label for the heatmaps.
        more_stats: stats.csv files from other machines.
    Returns:
        None.
    """
    data = read_existing_stats(existing_stats) if existing_stats else {}
    for path in (out_folder / "stats.csv", *more_stats):
        data.update(read_stats(path))   # this script's runs win if both exist
    if not data:
        print("No stats to plot.")
        return
    plots = out_folder / "plots"
    plots.mkdir(parents=True, exist_ok=True)
    plot_grid(data, plots / "contrast_wavelength_grid.png")
    for d in DISTANCES:
        plot_heatmap(data, heatmap_window, d, plots / f"contrast_heatmap_{heatmap_window}_{d}.png")
    print(f"Plots written to {plots} ({len(data)} runs)")


def parse_args(argv=None):
    """Read and check the command line.

    Args:
        argv: argument list (default: sys.argv).
    Returns:
        Namespace. Exits with an error if the executable or config template is missing.
    """
    p = argparse.ArgumentParser(
        description="Run the ripple height x wavelength experiment and plot the contrast ratio.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--exe-location", type=Path, help="path to the MASTODON executable (not needed with --plot-only)")
    p.add_argument("--output-folder", type=Path, default=Path("experiments/wavelength"),
                   help="folder for the runs, heightmaps, stats.csv and plots")
    p.add_argument("--combos", choices=["all", "missing"], default="all",
                   help="all 16 height x wavelength combinations, or only the 9 the earlier sweeps lack")
    p.add_argument("--distance", type=int, choices=DISTANCES, default=None,
                   help="only this sonar height (m), so two machines can split the sweep: 5 on one, 10 on the other")
    p.add_argument("--workers", type=int, default=1,
                   help="MASTODON runs at the same time (each needs about 8 GB of memory)")
    p.add_argument("--existing-stats", type=Path, default=None,
                   help="csv of the earlier sweeps (columns set, height_cm, window, dist, std_ratio) to add to the plots")
    p.add_argument("--more-stats", type=Path, nargs="*", default=[],
                   help="stats.csv files from other machines (same format as this script's) to add to the plots")
    p.add_argument("--heatmap-window", default="30-40", choices=[window_label(w) for w in WINDOWS],
                   help="window shown in the heatmaps")
    p.add_argument("--plot-only", action="store_true", help="skip the runs and only redraw the plots")
    p.add_argument("--dry-run", action="store_true", help="list the runs and exit")
    args = p.parse_args(argv)
    if not args.plot_only and not args.dry_run:
        if args.exe_location is None or not args.exe_location.is_file():
            p.error(f"executable not found: {args.exe_location}")
        if not CONFIG_TEMPLATE.is_file():
            p.error(f"config template not found: {CONFIG_TEMPLATE}")
    return args


def main(argv=None):
    """Run the sweep (resuming if interrupted), then plot.

    Args:
        argv: argument list (default: sys.argv).
    Returns:
        0 if every run worked, 1 if any failed.
    Example:
        main(["--exe-location", "bin/MASTODON.exe", "--workers", "4"])
    """
    args = parse_args(argv)
    out_folder = args.output_folder.resolve()

    if not args.plot_only:
        combos = [(h, wl) for h in HEIGHTS_CM for wl in WAVELENGTHS_M
                  if args.combos == "all" or (h, wl) not in EXISTING]
        done = set(read_stats(out_folder / "stats.csv"))
        wanted = [(h, wl, w, d) for h, wl in combos for w in WINDOWS for d in DISTANCES
                  if args.distance in (None, d)]
        jobs = [j for j in wanted if (j[0], j[1], window_label(j[2]), j[3]) not in done]
        print(f"{len(combos)} combinations, {len(jobs)} runs to do ({len(wanted) - len(jobs)} already done)")
        if args.dry_run:
            for h, wl, w, d in jobs:
                print(" ", run_name(h, wl, w, d))
            return 0

        out_folder.mkdir(parents=True, exist_ok=True)
        write_heightmaps(out_folder / "heightmaps", combos)
        stats = out_folder / "stats.csv"
        if not stats.is_file():
            with open(stats, "w", newline="") as f:
                csv.writer(f).writerow(FIELDS)

        exe = args.exe_location.resolve()
        failures = []
        with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
            futures = {pool.submit(run_one, exe, out_folder, *job): job for job in jobs}
            for i, fut in enumerate(as_completed(futures), 1):
                h, wl, w, d = futures[fut]
                name = run_name(h, wl, w, d)
                try:
                    row = fut.result()
                except Exception as e:
                    print(f"[{i}/{len(jobs)}] {name} FAILED: {e}", flush=True)
                    failures.append((name, str(e)))
                    continue
                with open(stats, "a", newline="") as f:   # only this thread writes the file
                    csv.writer(f).writerow(row)
                print(f"[{i}/{len(jobs)}] {name}  CR = {row[-1]:.3f}", flush=True)
        if failures:
            print(f"\n{len(failures)} of {len(jobs)} runs failed:")
            for name, err in failures:
                print(f"  {name}: {err}")

    make_plots(out_folder, args.existing_stats, args.heatmap_window, args.more_stats)
    return 1 if not args.plot_only and not args.dry_run and failures else 0


if __name__ == "__main__":
    sys.exit(main())
