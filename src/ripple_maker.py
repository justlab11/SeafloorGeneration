#!/usr/bin/env python3
"""Generate a synthetic sand-ripple heightmap and save it as a text file.

Example:
    python src/make_ripple_heightmap.py --total-x-m 50 --total-y-m 30 --cell 0.025 -o example_depthmaps/ripple_depth.txt
"""
import argparse
import sys
from pathlib import Path

import numpy as np


def _smooth_noise(shape, scale, rng):
    """Smooth random field in [-1, 1], made by low-pass filtering white noise via FFT."""
    h, w = shape
    white = rng.standard_normal(shape)
    fy = np.fft.fftfreq(h)[:, None]
    fx = np.fft.fftfreq(w)[None, :]
    # Gaussian low-pass: features are roughly `scale` pixels across
    lowpass = np.exp(-(fx**2 + fy**2) * (np.pi * scale) ** 2 * 2)
    field = np.fft.ifft2(np.fft.fft2(white) * lowpass).real
    return field / (np.abs(field).max() + 1e-12)


def ripple_depth_map(
    size=(512, 512),
    wavelength=40.0,     # distance between ripple crests, in pixels
    angle=0.35,          # ripple direction in radians
    amplitude=1.0,       # crest-to-trough height scale
    sinuosity=12.0,      # how much crests wiggle (in wavelengths-ish units)
    asymmetry=0.3,       # 0 = symmetric sine, higher = steeper lee slope
    grain=0.03,          # fine sand-grain noise
    base_depth=10.0,     # mean water depth
    seed=None,
):
    """
    Generate a depth map of sand ripples like those on a shallow seafloor.

    Returns a 2D float array of depth values (larger = deeper). Ripple crests
    are shallower (smaller depth) and troughs are deeper.
    """
    rng = np.random.default_rng(seed)
    h, w = size
    y, x = np.mgrid[0:h, 0:w].astype(float)

    # Coordinate across the ripples (perpendicular to crest lines)
    across = x * np.cos(angle) + y * np.sin(angle)

    # Warp the phase with smooth noise so crests meander and bifurcate
    warp = _smooth_noise(size, scale=wavelength * 2.5, rng=rng)
    # Second, shorter-scale warp varies crest spacing locally. It is added to the phase
    # (not used to rescale the wavelength) so spacing never drifts over long distances.
    spacing = _smooth_noise(size, wavelength * 4, rng)
    phase = 2 * np.pi * (across / wavelength + sinuosity * 0.1 * warp + 0.6 * spacing)

    # Asymmetric ripple profile (steep lee side, gentle stoss side)
    profile = np.sin(phase) + asymmetry * np.sin(2 * phase - np.pi / 2)

    # Ripple height varies across the field, with some patches nearly flat
    height_mod = 0.65 + 0.35 * _smooth_noise(size, wavelength * 5, rng)
    ripples = amplitude * height_mod * profile

    # Gentle large-scale bed undulation plus fine grain
    swell = 0.8 * amplitude * _smooth_noise(size, wavelength * 8, rng)
    sand = grain * amplitude * rng.standard_normal(size)

    height = ripples + swell + sand
    return base_depth - height  # high ground = shallower


def edge_taper(n, n_ramp):
    """1D window of length n: smooth 0->1 over n_ramp samples, flat at 1, smooth 1->0 at the end.
    Smoothstep is used instead of a linear ramp so there is no slope kink where the ripples start."""
    t = np.clip(np.arange(n) / max(n_ramp, 1), 0.0, 1.0)
    up = t * t * (3 - 2 * t)
    return np.minimum(up, up[::-1])


def build_heightmap(
    total_y_m=30.0,        # full exported patch, y direction (rows)
    total_x_m=50.0,        # full exported patch, x direction (columns)
    ripple_y_m=None,       # y extent covered by ripples (default: all of total_y_m)
    taper_m=5.0,           # length of the fade-in / fade-out at each y edge of the ripple band
    cell=0.025,            # meters per pixel
    wavelength_m=1.0,      # crest spacing in meters
    peak_m=0.1,            # largest |deviation| from flat in meters
    seed=None,
    **ripple_kwargs,       # angle, sinuosity, asymmetry, grain ... passed to ripple_depth_map
):
    """
    Returns (grid, min_x, min_y). grid has shape (rows=y, cols=x), values in meters,
    zero outside the ripple band. The patch is centered on y=0 and starts at x=0.
    """
    if ripple_y_m is None:
        ripple_y_m = total_y_m

    ny, nx = round(total_y_m / cell), round(total_x_m / cell)
    ny_r = round(ripple_y_m / cell)
    if ny_r > ny:
        raise ValueError("ripple_y_m cannot be larger than total_y_m")

    field = ripple_depth_map(
        size=(ny_r, nx),
        wavelength=wavelength_m / cell,
        amplitude=1.0,
        base_depth=0.0,
        seed=seed,
        **ripple_kwargs,
    )
    field -= field.mean()                          # flat level = 0 so the taper blends cleanly
    field *= peak_m / np.abs(field).max()          # scale to meters

    field *= edge_taper(ny_r, round(taper_m / cell))[:, None]

    pad_top = (ny - ny_r) // 2
    grid = np.pad(field, ((pad_top, ny - ny_r - pad_top), (0, 0)))
    return grid, 0.0, -total_y_m / 2


def save_heightmap_txt(filename, grid, min_x, dx, min_y, dy):
    rows, cols = grid.shape
    with open(filename, "w") as f:
        f.write(f"{cols} {rows}\n")
        f.write(f"{min_x:.6f} {dx:.6f}\n")
        f.write(f"{min_y:.6f} {dy:.6f}\n")
        np.savetxt(f, grid.ravel(order="C"), fmt="%.6f")   # x varies fastest


def plot_heightmap(grid, min_x, min_y, cell, image_path):
    import matplotlib.pyplot as plt

    rows, cols = grid.shape
    plt.imshow(
        grid,
        cmap="viridis",
        extent=[min_x, min_x + cols * cell, min_y + rows * cell, min_y],
    )
    plt.xlabel("x (m)")
    plt.ylabel("y (m)")
    plt.colorbar(label="m")
    plt.savefig(image_path, dpi=150, bbox_inches="tight")
    print(f"Saved {image_path}")
    plt.show()


def parse_args(argv=None):
    p = argparse.ArgumentParser(
        description="Generate a synthetic sand-ripple heightmap and save it as a text file.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )

    # Main options
    p.add_argument("--total-y-m", type=float, default=30.0,
                   help="full exported patch size in y (rows), meters")
    p.add_argument("--total-x-m", type=float, default=50.0,
                   help="full exported patch size in x (columns), meters")
    p.add_argument("--cell", type=float, default=0.025, help="meters per pixel")
    p.add_argument("-o", "--output", default="ripple_depth.txt", help="output file path")

    # Secondary options
    p.add_argument("--ripple-y-m", type=float, default=None,
                   help="y extent covered by ripples; rest is flat zero padding "
                        "(default: same as --total-y-m)")
    p.add_argument("--taper-m", type=float, default=5.0,
                   help="fade-in/out length at each y edge of the ripple band, meters")
    p.add_argument("--wavelength-m", type=float, default=1.0, help="crest spacing, meters")
    p.add_argument("--peak-m", type=float, default=0.1,
                   help="largest absolute deviation from flat, meters")
    p.add_argument("--seed", type=int, default=7, help="random seed")

    # Ripple shape options
    p.add_argument("--angle", type=float, default=0.35, help="ripple direction, radians")
    p.add_argument("--sinuosity", type=float, default=12.0, help="how much crests wiggle")
    p.add_argument("--asymmetry", type=float, default=0.3,
                   help="0 = symmetric sine, higher = steeper lee slope")
    p.add_argument("--grain", type=float, default=0.03, help="fine sand-grain noise level")

    p.add_argument("--plot", action="store_true", help="show a plot of the result and save it as a .jpg next to the output file")

    args = p.parse_args(argv)

    if args.cell <= 0:
        p.error("--cell must be positive")
    if args.total_x_m <= 0 or args.total_y_m <= 0:
        p.error("--total-x-m and --total-y-m must be positive")
    if args.ripple_y_m is not None and args.ripple_y_m > args.total_y_m:
        p.error("--ripple-y-m cannot be larger than --total-y-m")
    return args


def main(argv=None):
    args = parse_args(argv)

    grid, min_x, min_y = build_heightmap(
        total_y_m=args.total_y_m,
        total_x_m=args.total_x_m,
        ripple_y_m=args.ripple_y_m,
        taper_m=args.taper_m,
        cell=args.cell,
        wavelength_m=args.wavelength_m,
        peak_m=args.peak_m,
        seed=args.seed,
        angle=args.angle,
        sinuosity=args.sinuosity,
        asymmetry=args.asymmetry,
        grain=args.grain,
    )

    save_heightmap_txt(args.output, grid, min_x, args.cell, min_y, args.cell)
    print(f"Saved {args.output}: shape {grid.shape}, range {grid.min():.4f} to {grid.max():.4f}")

    if args.plot:
        image_path = Path(args.output).with_suffix(".jpg")
        plot_heightmap(grid, min_x, min_y, args.cell, image_path)

    return 0


if __name__ == "__main__":
    sys.exit(main())