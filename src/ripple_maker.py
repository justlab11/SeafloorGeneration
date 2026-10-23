#!/usr/bin/env python3
"""Generate a synthetic sand-ripple heightmap and save it as a text file.

The txt is the format MASTODON reads through environment.bottom.bathymetry.infile. Rows are
written reversed in y by default, which makes the sonar image match the plotted heightmap.

Example:
    python src/ripple_maker.py --total-x-m 50 --total-y-m 30 --cell 0.025 -o example_depthmaps/ripple_depth.txt --plot
"""
import argparse
import sys
from pathlib import Path

import numpy as np


def _smooth_noise(shape, scale, rng):
    """Smooth random field in [-1, 1], made by low-pass filtering white noise with an FFT.

    Args:
        shape: (rows, cols) of the field.
        scale: rough feature size in pixels.
        rng: numpy Generator used for the noise.
    Returns:
        2D float array of the given shape.
    """
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
    """Generate a depth map of sand ripples like those on a shallow seafloor.

    Args:
        size: (rows, cols) in pixels.
        wavelength: crest spacing in pixels.
        angle: ripple direction in radians.
        amplitude: crest-to-trough height scale.
        sinuosity: how much the crests wiggle.
        asymmetry: 0 = symmetric sine, higher = steeper lee slope.
        grain: fine sand-grain noise level.
        base_depth: mean water depth.
        seed: random seed.
    Returns:
        2D float array of depths (larger = deeper); crests are shallower, troughs deeper.
    Example:
        depth = ripple_depth_map(size=(1000, 2000), wavelength=40, seed=7)
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
    """1D window that rises smoothly 0 -> 1, stays at 1, then falls smoothly 1 -> 0.

    Smoothstep is used instead of a linear ramp so there is no slope kink where the ripples start.

    Args:
        n: length of the window.
        n_ramp: samples each ramp takes.
    Returns:
        1D float array of length n.
    Example:
        field *= edge_taper(1040, 200)[:, None]
    """
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
    """Build a full ripple heightmap in meters, flat (0) outside the ripple band.

    Args:
        total_y_m, total_x_m: size of the whole patch (rows = y, columns = x).
        ripple_y_m: y extent covered by ripples (default: all of total_y_m).
        taper_m: fade-in/out length at each y edge of the ripple band.
        cell: meters per pixel.
        wavelength_m: crest spacing.
        peak_m: largest |deviation| from flat.
        seed: random seed.
        **ripple_kwargs: angle, sinuosity, asymmetry, grain, passed to ripple_depth_map.
    Returns:
        (grid, min_x, min_y). grid has shape (rows, cols) in meters; the patch starts at x = 0
        and is centered on y = 0.
    Example:
        grid, min_x, min_y = build_heightmap(peak_m=0.05, seed=7)
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


def save_heightmap_txt(filename, grid, min_x, dx, min_y, dy, reverse_y=True):
    """Write a heightmap in the txt format MASTODON reads.

    The file has a header ("cols rows", "min_x dx", "min_y dy") and then one value per line, x
    varying fastest.

    Args:
        filename: output path.
        grid: 2D array, rows = y, columns = x, in meters.
        min_x, dx, min_y, dy: grid origin and cell size.
        reverse_y: write the rows in reverse order. MASTODON reads them reversed, so the sonar
            image then matches the grid as given. The header is unchanged, so use it with grids
            centered on y = 0. False gives the old, mirrored behavior.
    Returns:
        None; writes filename.
    Example:
        save_heightmap_txt("depthmap.txt", grid, 0.0, 0.025, -15.0, 0.025)
    """
    if reverse_y:
        grid = grid[::-1]
    rows, cols = grid.shape
    with open(filename, "w") as f:
        f.write(f"{cols} {rows}\n")
        f.write(f"{min_x:.6f} {dx:.6f}\n")
        f.write(f"{min_y:.6f} {dy:.6f}\n")
        np.savetxt(f, grid.ravel(order="C"), fmt="%.6f")   # x varies fastest


def plot_heightmap(grid, min_x, min_y, cell, image_path):
    """Plot a heightmap, save it as an image and show it.

    Args:
        grid: 2D array in meters.
        min_x, min_y: grid origin.
        cell: meters per pixel.
        image_path: where to save the jpg.
    Returns:
        None.
    Example:
        plot_heightmap(grid, 0.0, -15.0, 0.025, "ripple_depth.jpg")
    """
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

    p.add_argument("--no-reverse-y", action="store_true",
                   help="write rows in plot order instead of reversed (the sonar image then comes "
                        "out mirrored along-track)")
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
    """Command line entry point: build the heightmap, save it, optionally plot it.

    Args:
        argv: argument list (default: sys.argv).
    Returns:
        0.
    Example:
        main(["-o", "ripple_depth.txt", "--peak-m", "0.05"])
    """
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

    save_heightmap_txt(args.output, grid, min_x, args.cell, min_y, args.cell,
                       reverse_y=not args.no_reverse_y)
    print(f"Saved {args.output}: shape {grid.shape}, range {grid.min():.4f} to {grid.max():.4f}")

    if args.plot:
        image_path = Path(args.output).with_suffix(".jpg")
        plot_heightmap(grid, min_x, min_y, args.cell, image_path)

    return 0


if __name__ == "__main__":
    sys.exit(main())