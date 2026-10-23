#!/usr/bin/env python3
"""Run a sweep of MASTODON experiments over bottom types and ping counts.

Each bottom type gets its own subfolder under --output-folder (e.g. experiments/rough_rock).
Inside it, every num_pings value is run in turn. After each run the two images are renamed
to {ping}_raw.jpg and {ping}_normed.jpg; everything else (cfg, Beamformed.h5, run.log,
raw/normed .npy) is overwritten by the next run.

Runs whose {ping}_normed.jpg already exists are skipped, so an interrupted sweep can be rerun.

Example:
    python src/run_experiments.py --exe-location ./bin/my_program --input-txt example_depthmaps/ripple_depth.txt --output-folder experiments/
"""
import argparse
import shutil
import subprocess
import sys
from pathlib import Path

import libconf
from visualize_beamformed import process_beamformed

CONFIG_TEMPLATE = Path("mastodon_configs/CONFIG.cfg")

BOTTOM_TYPES = [
    "Rough Rock",
    "Rock",
    "Cobble",
    "Sandy Gravel",
    "Very Coarse Sand",
    "Muddy Sandy Gravel",
    "Coarse Sand",
    "Gravelly Muddy Sand",
    "Medium Sand",
    "Muddy Gravel",
    "Fine Sand",
    "Muddy Sand",
    "Very Fine Sand",
    "Clayey Sand",
    "Coarse Silt",
    "Sandy Silt",
    "Medium Silt",
    "Sandy Mud",
    "Fine Silt",
    "Sandy Clay",
    "Very Fine Silt",
    "Silty Clay",
    "Clay",
]

NUM_PINGS = [13, 25, 49, 99, 111, 211, 333]


def slugify(name):
    return name.lower().replace(" ", "_")


def parse_args(argv=None):
    p = argparse.ArgumentParser(
        description="Run a sweep of MASTODON experiments over bottom types and ping counts.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--exe-location", required=True, type=Path,
                   help="path to the executable")
    p.add_argument("--input-txt", required=True, type=Path,
                   help="path to the input txt file")
    p.add_argument("--output-folder", required=True, type=Path,
                   help="root folder for all experiments (one subfolder per bottom type)")

    args = p.parse_args(argv)

    if not args.exe_location.is_file():
        p.error(f"executable not found: {args.exe_location}")
    if not args.input_txt.is_file():
        p.error(f"input file not found: {args.input_txt}")
    if not CONFIG_TEMPLATE.is_file():
        p.error(f"config template not found: {CONFIG_TEMPLATE}")
    return args


def run_one(exe, folder, config, cfg, ping):
    """Set num_pings, run the exe, make the images, and rename the jpgs."""
    cfg["platforms"]["num_pings"] = int(ping)
    with open(config, "w") as f:
        libconf.dump(cfg, f)

    beamformed = folder / "Beamformed.h5"
    beamformed.unlink(missing_ok=True)   # never pick up a stale file from the previous run

    cmd = [str(exe), str(config)]
    with open(folder / "run.log", "w") as log:
        subprocess.run(cmd, cwd=folder, check=True,
                       stdout=log, stderr=subprocess.STDOUT)

    if not beamformed.is_file():
        raise FileNotFoundError(f"{beamformed} was not created")

    process_beamformed(beamformed)

    for kind in ("raw", "normed"):
        (folder / f"{kind}.jpg").replace(folder / f"{ping}_{kind}.jpg")


def main(argv=None):
    args = parse_args(argv)

    exe = args.exe_location.resolve()
    input_txt = args.input_txt.resolve()
    root = args.output_folder.resolve()
    root.mkdir(parents=True, exist_ok=True)

    print(f"Executable:    {exe}")
    print(f"Input txt:     {input_txt}")
    print(f"Output folder: {root}")

    total = len(BOTTOM_TYPES) * len(NUM_PINGS)
    run_idx = 0
    failures = []

    for bottom in BOTTOM_TYPES:
        folder = root / slugify(bottom)
        pending = [n for n in NUM_PINGS if not (folder / f"{n}_normed.jpg").is_file()]
        run_idx += len(NUM_PINGS) - len(pending)   # already finished in an earlier run

        if not pending:
            print(f"{bottom}: all runs already done, skipping")
            continue

        folder.mkdir(parents=True, exist_ok=True)
        config = folder / CONFIG_TEMPLATE.name
        shutil.copy(CONFIG_TEMPLATE, config)

        with open(config) as f:
            cfg = libconf.load(f)
        cfg["environment"]["bottom"]["bathymetry"]["infile"] = str(input_txt)
        cfg["environment"]["bottom"]["type"] = bottom

        for ping in pending:
            run_idx += 1
            print(f"[{run_idx}/{total}] {bottom}, num_pings={ping}")
            try:
                run_one(exe, folder, config, cfg, ping)
            except Exception as e:
                print(f"  FAILED: {e}")
                failures.append((bottom, ping, str(e)))
                log = folder / "run.log"
                if log.is_file():
                    shutil.copy(log, folder / f"{ping}_failed.log")   # keep it, run.log gets overwritten

    print()
    if failures:
        print(f"{len(failures)} of {total} runs failed:")
        for bottom, ping, err in failures:
            print(f"  {bottom}, num_pings={ping}: {err}")
        return 1

    print(f"Done, no failures ({total} runs checked).")
    return 0


if __name__ == "__main__":
    sys.exit(main())