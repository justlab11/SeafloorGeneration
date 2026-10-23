#!/usr/bin/env python3
"""Run a sweep of MASTODON experiments over ripple height, window, sonar height and ping count.

One folder per (height, window, distance):
    <output-folder>/<sweep-name>/exp_<NN>cm_<window>_<distance>/

After each run the images are renamed to {ping}_raw.jpg, {ping}_normed.jpg and {ping}_grouped.jpg,
and the big Beamformed.h5, StaveData.h5 and Depth.txt are deleted. Everything else (cfg, run.log,
raw/normed .npy) is overwritten by the next run. If a run fails, the big files are kept and its
log is saved as {ping}_failed.log. Runs whose {ping}_normed.jpg exists are skipped, so an
interrupted sweep can be rerun.

Per experiment the template is changed as follows:
    environment.bottom.bathymetry.infile  heightmap for this height/window/distance
    environment.bottom.depth              WATER_DEPTH
    environment.ssp.depths                (0, WATER_DEPTH)   (must end at the bottom depth)
    platforms.depth                       WATER_DEPTH - distance   (sonar `distance` m above the floor)
    platforms.num_pings                   each value in NUM_PINGS

Heightmaps come from make_ripple_variants.py:
    <depthmap-root>/ripple_sizes_fixed_wl/<NN>cm/depthmap_<window>_<distance>.txt
    <depthmap-root>/ripple_sizes_scaled_wl/<NN>cm/...   (use --depthmap-set scaled)

Example:
    python src/run_ripple_experiments.py --exe-location ./bin/MASTODON.exe
    python src/run_ripple_experiments.py --exe-location ./bin/MASTODON.exe --depthmap-set scaled
"""
import argparse
import shutil
import subprocess
import sys
from pathlib import Path

import libconf
from visualize_beamformed import process_beamformed

CONFIG_TEMPLATE = Path("mastodon_configs/CONFIG.cfg")

HEIGHTS_CM = [5, 10, 20, 50]                          # ripple crest-to-trough
WINDOWS = ["nowindow", "0-10", "10-20", "20-30", "30-40", "40-50"]  # ground range (x) the ripples occupy, in m
DISTANCES = [5, 10]                                   # sonar height above the floor (m)
NUM_PINGS = [222]                                     # dping 0.1365 m -> ~30 m of track, the full 30 m map
WATER_DEPTH = 20.0


def exp_name(height, window, distance):
    """Folder name of one experiment.

    Args:
        height: ripple height in cm.
        window: window label such as "10-20" or "nowindow".
        distance: sonar height above the floor in meters.
    Returns:
        str, e.g. "exp_05cm_nowindow_5".
    """
    return f"exp_{height:02d}cm_{window}_{distance}"


def depthmap_path(root, height, window, distance, depthmap_set):
    """Heightmap file for one experiment.

    The scaled set has no 10 cm folder (it is the same as the fixed one), and the _<distance> files
    are identical copies, so the lookup falls back as needed.

    Args:
        root: folder written by make_ripple_variants.py.
        height, window, distance: the experiment.
        depthmap_set: "fixed" or "scaled".
    Returns:
        Path of the file (it may not exist; the caller checks).
    Example:
        depthmap_path(Path("example_depthmaps"), 20, "10-20", 5, "fixed")
    """
    exp = "ripple_sizes_scaled_wl" if depthmap_set == "scaled" and height != 10 else "ripple_sizes_fixed_wl"
    folder = root / exp / f"{height:02d}cm"
    for d in (distance, DISTANCES[0]):
        path = folder / f"depthmap_{window}_{d}.txt"
        if path.is_file():
            return path
    return folder / f"depthmap_{window}_{distance}.txt"   # missing; reported by the caller


def parse_args(argv=None):
    """Read and check the command line.

    Args:
        argv: argument list (default: sys.argv).
    Returns:
        Namespace with exe_location, depthmap_root, depthmap_set, output_folder and sweep_name.
        Exits with an error if the executable, config template or any heightmap is missing.
    """
    p = argparse.ArgumentParser(
        description="Run a sweep of MASTODON experiments over ripple height, range window, "
                    "sonar height and ping count.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--exe-location", required=True, type=Path,
                   help="path to the executable")
    p.add_argument("--depthmap-root", type=Path, default=Path("example_depthmaps"),
                   help="folder written by make_ripple_variants.py")
    p.add_argument("--depthmap-set", choices=["fixed", "scaled"], default="fixed",
                   help="fixed 1 m wavelength, or wavelength scaled with ripple height")
    p.add_argument("--output-folder", type=Path, default=Path("experiments"),
                   help="root folder for experiments")
    p.add_argument("--sweep-name", default=None,
                   help="subfolder of the output folder (default: height+window+distance+pings, "
                        "with _scaled appended for the scaled set)")

    args = p.parse_args(argv)
    if args.sweep_name is None:
        args.sweep_name = "height+window+distance+pings" + ("_scaled" if args.depthmap_set == "scaled" else "")

    if not args.exe_location.is_file():
        p.error(f"executable not found: {args.exe_location}")
    if not CONFIG_TEMPLATE.is_file():
        p.error(f"config template not found: {CONFIG_TEMPLATE}")

    missing = [str(path) for h in HEIGHTS_CM for w in WINDOWS for d in DISTANCES
               if not (path := depthmap_path(args.depthmap_root, h, w, d, args.depthmap_set)).is_file()]
    if missing:
        p.error("heightmap files not found:\n  " + "\n  ".join(missing))
    return args


def run_one(exe, folder, config, cfg, ping):
    """Run MASTODON once for one ping count, make the images and delete the big files.

    Args:
        exe: path to the MASTODON executable.
        folder: experiment folder (also the working directory of the run).
        config: path of the config file inside folder.
        cfg: the loaded config, modified with the new num_pings.
        ping: number of pings.
    Returns:
        None. Renames the jpgs to {ping}_raw/normed/grouped.jpg. Raises if the run fails or
        Beamformed.h5 is not created.
    """
    cfg["platforms"]["num_pings"] = int(ping)
    with open(config, "w") as f:
        libconf.dump(cfg, f)

    beamformed = folder / "Beamformed.h5"
    stave = folder / "StaveData.h5"
    depth_txt = folder / "Depth.txt"   # bathymetry.outfile in the template
    beamformed.unlink(missing_ok=True)   # never pick up a stale file from the previous run
    stave.unlink(missing_ok=True)

    cmd = [str(exe), str(config)]
    with open(folder / "run.log", "w") as log:
        subprocess.run(cmd, cwd=folder, check=True,
                       stdout=log, stderr=subprocess.STDOUT)

    if not beamformed.is_file():
        raise FileNotFoundError(f"{beamformed} was not created")

    process_beamformed(beamformed)

    for kind in ("raw", "normed", "grouped"):
        (folder / f"{kind}.jpg").replace(folder / f"{ping}_{kind}.jpg")

    # only after the images exist: these files are huge and not needed any more
    beamformed.unlink(missing_ok=True)
    stave.unlink(missing_ok=True)
    depth_txt.unlink(missing_ok=True)


def main(argv=None):
    """Run the full sweep, skipping runs that already have their normed image.

    Args:
        argv: argument list (default: sys.argv).
    Returns:
        0 if every run worked, 1 if any failed.
    Example:
        main(["--exe-location", "bin/MASTODON.exe", "--depthmap-set", "scaled"])
    """
    args = parse_args(argv)

    exe = args.exe_location.resolve()
    depthmap_root = args.depthmap_root.resolve()
    root = (args.output_folder / args.sweep_name).resolve()
    root.mkdir(parents=True, exist_ok=True)

    print(f"Executable:    {exe}")
    print(f"Heightmaps:    {depthmap_root} ({args.depthmap_set})")
    print(f"Output folder: {root}")

    experiments = [(h, w, d) for h in HEIGHTS_CM for w in WINDOWS for d in DISTANCES]
    total = len(experiments) * len(NUM_PINGS)
    run_idx = 0
    failures = []

    for height, window, distance in experiments:
        name = exp_name(height, window, distance)
        folder = root / name
        pending = [n for n in NUM_PINGS if not (folder / f"{n}_normed.jpg").is_file()]
        run_idx += len(NUM_PINGS) - len(pending)   # already finished in an earlier run

        if not pending:
            print(f"{name}: all runs already done, skipping")
            continue

        folder.mkdir(parents=True, exist_ok=True)
        config = folder / CONFIG_TEMPLATE.name
        shutil.copy(CONFIG_TEMPLATE, config)

        with open(config) as f:
            cfg = libconf.load(f)
        input_txt = depthmap_path(depthmap_root, height, window, distance, args.depthmap_set)
        cfg["environment"]["bottom"]["bathymetry"]["infile"] = str(input_txt)
        cfg["environment"]["bottom"]["depth"] = WATER_DEPTH
        cfg["environment"]["ssp"]["depths"] = (0, WATER_DEPTH)   # must end at the bottom depth
        cfg["platforms"]["depth"] = WATER_DEPTH - distance        # vehicle depth below the surface

        for ping in pending:
            run_idx += 1
            print(f"[{run_idx}/{total}] {name}, num_pings={ping}")
            try:
                run_one(exe, folder, config, cfg, ping)
            except Exception as e:
                print(f"  FAILED: {e}")
                failures.append((name, ping, str(e)))
                log = folder / "run.log"
                if log.is_file():
                    shutil.copy(log, folder / f"{ping}_failed.log")   # keep it, run.log gets overwritten

    print()
    if failures:
        print(f"{len(failures)} of {total} runs failed:")
        for name, ping, err in failures:
            print(f"  {name}, num_pings={ping}: {err}")
        return 1

    print(f"Done, no failures ({total} runs checked).")
    return 0


if __name__ == "__main__":
    sys.exit(main())