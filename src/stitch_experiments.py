#!/usr/bin/env python3
"""Stitch {ping}_normed.jpg from every experiment folder into one labeled grid image.

Folders are visited in the same order as BOTTOM_TYPES in run_experiments.py, filled left to
right, then top to bottom (2 across). Each image gets its bottom type as a label on top.

Example:
    python src/stitch_experiments.py --experiments-folder experiments/ --ping 222
"""
import argparse
import math
import sys
from pathlib import Path

from matplotlib.figure import Figure
from matplotlib.image import imread

from run_experiments import BOTTOM_TYPES, slugify

NCOLS = 2             # rows are computed from the number of types (23 types -> 12 rows)

CELL_W_IN = 4.0       # width of each image in the final figure, inches
LABEL_H_IN = 0.3      # space reserved above each image for its label
GAP_X_IN = 0.25       # whitespace between columns
GAP_Y_IN = 0.2        # whitespace between one image and the next label below it
MARGIN_IN = 0.2       # border around the whole figure
DPI = 150


def load_images(root, ping):
    """Load one normed image per bottom type.

    Args:
        root: experiments folder with one subfolder per bottom type.
        ping: which num_pings result to load.
    Returns:
        List of (bottom type, image array or None), in BOTTOM_TYPES order; None (and a warning)
        where the file is missing.
    Example:
        images = load_images(Path("experiments"), 222)
    """
    images = []
    for bottom in BOTTOM_TYPES:
        path = root / slugify(bottom) / f"{ping}_normed.jpg"
        if path.is_file():
            images.append((bottom, imread(path)))
        else:
            print(f"Warning: missing {path}")
            images.append((bottom, None))
    return images


def stitch(images, output):
    """Tile the images into one labeled grid, NCOLS across, and save it.

    Args:
        images: list of (label, image array or None) from load_images; None leaves a blank cell.
        output: output image path.
    Returns:
        None. Raises FileNotFoundError if there are no images at all.
    Example:
        stitch(images, "experiments/stitched_222_normed.jpg")
    """
    ref = next((img for _, img in images if img is not None), None)
    if ref is None:
        raise FileNotFoundError("no images found to stitch")

    h, w = ref.shape[:2]
    cell_h = CELL_W_IN * h / w          # size every cell from the first image's aspect ratio

    nrows = math.ceil(len(images) / NCOLS)
    fig_w = 2 * MARGIN_IN + NCOLS * CELL_W_IN + (NCOLS - 1) * GAP_X_IN
    fig_h = 2 * MARGIN_IN + nrows * (LABEL_H_IN + cell_h) + (nrows - 1) * GAP_Y_IN
    fig = Figure(figsize=(fig_w, fig_h))

    for i, (name, img) in enumerate(images):
        if img is None:
            continue                     # leave that cell blank
        row, col = divmod(i, NCOLS)
        left = MARGIN_IN + col * (CELL_W_IN + GAP_X_IN)
        top = fig_h - MARGIN_IN - row * (LABEL_H_IN + cell_h + GAP_Y_IN)
        bottom_edge = top - LABEL_H_IN - cell_h

        ax = fig.add_axes([left / fig_w, bottom_edge / fig_h,
                           CELL_W_IN / fig_w, cell_h / fig_h])
        ax.imshow(img, aspect="equal")
        ax.axis("off")
        ax.set_title(name, fontsize=10, pad=4)

    fig.savefig(output, dpi=DPI)


def parse_args(argv=None):
    """Read and check the command line.

    Args:
        argv: argument list (default: sys.argv).
    Returns:
        Namespace with experiments_folder, ping and output (default: stitched_<ping>_normed.jpg
        inside the experiments folder).
    """
    p = argparse.ArgumentParser(
        description="Stitch one normed image per bottom type into a labeled grid.",
    )
    p.add_argument("--experiments-folder", required=True, type=Path,
                   help="root folder containing one subfolder per bottom type")
    p.add_argument("--ping", type=int, default=222,
                   help="which num_pings result to use (default: 222)")
    p.add_argument("--output", type=Path, default=None,
                   help="output image path (default: <experiments-folder>/stitched_<ping>_normed.jpg)")

    args = p.parse_args(argv)
    if not args.experiments_folder.is_dir():
        p.error(f"experiments folder not found: {args.experiments_folder}")
    if args.output is None:
        args.output = args.experiments_folder / f"stitched_{args.ping}_normed.jpg"
    return args


def main(argv=None):
    """Stitch the experiments folder's normed images into one grid.

    Args:
        argv: argument list (default: sys.argv).
    Returns:
        0 on success.
    Example:
        python src/stitch_experiments.py --experiments-folder experiments/ --ping 222
    """
    args = parse_args(argv)

    images = load_images(args.experiments_folder, args.ping)
    try:
        stitch(images, args.output)
    except FileNotFoundError as e:
        sys.exit(f"error: {e}")

    print(f"Saved {args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())