#!/usr/bin/env python3
"""Boilerplate script: takes an executable, an input txt file, and an output folder.

Example:
    python src/run_mastodon.py --exe-location ./bin/my_program --input-txt example_depthmaps/ripple_depth.txt --output-folder results/
"""
import argparse
import sys
from pathlib import Path
import shutil
import libconf
import subprocess
from visualize_beamformed import process_beamformed


def parse_args(argv=None):
    p = argparse.ArgumentParser(
        description="TODO: describe what this script does.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--exe-location", required=True, type=Path,
                   help="path to the executable")
    p.add_argument("--input-txt", required=True, type=Path,
                   help="path to the input txt file")
    p.add_argument("--output-folder", required=True, type=Path,
                   help="folder for outputs (created if it doesn't exist)")

    args = p.parse_args(argv)

    if not args.exe_location.is_file():
        p.error(f"executable not found: {args.exe_location}")
    if not args.input_txt.is_file():
        p.error(f"input file not found: {args.input_txt}")
    return args


def main(argv=None):
    args = parse_args(argv)

    exe = args.exe_location.resolve()
    input_txt = args.input_txt.resolve()
    folder = args.output_folder.resolve()
    config_template = Path("mastodon_configs/CONFIG.cfg")

    folder.mkdir(parents=True, exist_ok=True)
    config = folder / config_template.name

    print(f"Executable:    {exe}")
    print(f"Input txt:     {input_txt}")
    print(f"Output folder: {folder}")

    shutil.copy(config_template, config)

    with open(config) as f:
        cfg = libconf.load(f)

    cfg["environment"]["bottom"]["bathymetry"]["infile"] = str(input_txt)

    with open(config, "w") as f:
        libconf.dump(cfg, f)

    cmd = [str(exe), str(config)]
    with open(folder / "run.log", "w") as log:
        subprocess.run(cmd, cwd=folder, check=True,
                       stdout=log, stderr=subprocess.STDOUT)

    beamformed_file = folder / "Beamformed.h5"
    if not beamformed_file.is_file():
        print(f"Error: expected {beamformed_file} but it wasn't created, check run.log")
        return 1

    process_beamformed(beamformed_file)

    return 0

if __name__ == "__main__":
    sys.exit(main())


