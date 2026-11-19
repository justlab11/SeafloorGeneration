"""Dependency-free (numpy only) MASTODON heightmap txt I/O; used on both the WSL and Windows side."""
import numpy as np


def write_heightmap(path, grid, x0, y0, dx, dy=None, reverse_y=True):
    """Write a MASTODON user-bathymetry txt.

    grid is indexed [iy, ix] with y ASCENDING (row 0 = y0). Header is "cols rows", "x0 dx", "y0 dy",
    then one value per line, x fastest. reverse_y=True writes the rows top-to-bottom reversed, which
    is the convention SeafloorGeneration's ripple_maker uses (verified in test_orientation.py).
    """
    dy = dy or dx
    rows, cols = grid.shape
    out = grid[::-1] if reverse_y else grid
    with open(path, "w") as f:
        f.write(f"{cols} {rows}\n{x0:.6f} {dx:.6f}\n{y0:.6f} {dy:.6f}\n")
        np.savetxt(f, out.reshape(-1), fmt="%.6f")


def read_heightmap(path, reverse_y=True):
    """Inverse of write_heightmap -> (grid[iy,ix] y ascending, x0, y0, dx, dy)."""
    with open(path) as f:
        cols, rows = (int(v) for v in f.readline().split())
        x0, dx = (float(v) for v in f.readline().split())
        y0, dy = (float(v) for v in f.readline().split())
        g = np.loadtxt(f).reshape(rows, cols)
    return (g[::-1] if reverse_y else g), x0, y0, dx, dy
