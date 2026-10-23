#!/usr/bin/env python3
"""Run a sweep of MASTODON experiments over ripple height, range window, sonar height and ping count.

One folder per (height, window, distance):
    <output-folder>/<sweep-name>/exp_<NN>cm_<window>_<distance>/

Inside each folder every num_pings value is run in turn. After each run the two images are
renamed to {ping}_raw.jpg and {ping}_normed.jpg, and the big Beamformed.h5, StaveData.h5 and
Depth.txt are deleted. Everything else (cfg, run.log, raw/normed .npy) is overwritten by the
next run. If a run fails, those big files are kept (for debugging) and its log is saved as
{ping}_failed.log.

Runs whose {ping}_normed.jpg already exists are skipped, so an interrupted sweep can be rerun.

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
    python src/run_ripple_experiments.py --exe-location ./bin/my_program
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
WINDOWS = ["0-10", "10-20", "20-30", "30-40", "40-50"]  # ground range (x) the ripples occupy, in m
DISTANCES = [5, 10]                                   # sonar height above the floor (m)
NUM_PINGS = [111, 222]                                # dping 0.1365 m -> ~15 m and ~30 m of track
WATER_DEPTH = 20.0


def exp_name(height, window, distance):
    return f"exp_{height:02d}cm_{window}_{distance}"


def depthmap_path(root, height, window, distance, depthmap_set):
    """Heightmap for this experiment. The scaled set has no 10 cm folder (it is the same as the
    fixed one), and the _<distance> files are identical copies, so fall back as needed."""
    exp = "ripple_sizes_scaled_wl" if depthmap_set == "scaled" and height != 10 else "ripple_sizes_fixed_wl"
    folder = root / exp / f"{height:02d}cm"
    for d in (distance, DISTANCES[0]):
        path = folder / f"depthmap_{window}_{d}.txt"
        if path.is_file():
            return path
    return folder / f"depthmap_{window}_{distance}.txt"   # missing; reported by the caller


def parse_args(argv=None):
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
    """Set num_pings, run the exe, make the images, rename the jpgs, delete the big files."""
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

    for kind in ("raw", "normed"):
        (folder / f"{kind}.jpg").replace(folder / f"{ping}_{kind}.jpg")

    # only after the images exist: these files are huge and not needed any more
    beamformed.unlink(missing_ok=True)
    stave.unlink(missing_ok=True)
    depth_txt.unlink(missing_ok=True)


def main(argv=None):
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