"""Build the ripple heightmap variants and their preview images.

Writes, relative to where it is run:
  example_depthmaps/<set>/<NN>cm/depthmap_<window>_<dist>.txt
  example_images/<set>/<NN>cm/depthmap_<window>_<dist>.jpg
  example_depthmaps/manifest.csv

Sets:
  ripple_sizes_fixed_wl   crest-to-trough 5/10/20/50 cm, wavelength fixed at 1 m
  ripple_sizes_scaled_wl  same heights, wavelength scaled to keep steepness (height/wavelength) at 0.1
                          (10 cm is identical to the fixed case, so its files live in the fixed folder)

Heights are crest-to-trough: the highest crest minus the lowest trough in the rippled band.
Values are meters relative to the flat level (0), positive = deeper. The sonar height (5 m / 10 m)
does not change the heightmap, so those files are identical copies.

The txt rows are written reversed in y by default (reverse_y=True) because MASTODON's sonar image
comes out mirrored along-track otherwise. The preview jpg always shows the grid as designed.

Example:
    python src/make_ripple_variants.py
"""
import csv
import shutil
from pathlib import Path

import numpy as np
from matplotlib.figure import Figure

from ripple_maker import edge_taper, ripple_depth_map, save_heightmap_txt

CELL = 0.025                  # meters per pixel
TOTAL_Y, TOTAL_X = 30.0, 50.0 # full patch (m); y centered on 0, x starts at 0
RIPPLE_Y, TAPER_Y = 26.0, 5.0 # rippled band and its fade length in y (m)
HEIGHTS_CM = [5, 10, 20, 50]
STEEPNESS = 0.1               # height / wavelength for the scaled experiment
WINDOWS = [None, (0, 10), (10, 20), (20, 30), (30, 40), (40, 50)]
RAMP_M = 1.0                 # ramp length on each side of a window
DISTANCES = [5, 10]           # sonar height above floor (m), used only in file names


def smoothstep(t):
    """Smooth 0 -> 1 ramp with zero slope at both ends.

    Args:
        t: values; anything outside [0, 1] is clipped.
    Returns:
        Array of ramp values in [0, 1].
    """
    t = np.clip(t, 0.0, 1.0)
    return t * t * (3 - 2 * t)


def x_window(nx, start_m, end_m, cell=CELL, ramp_m=RAMP_M):
    """1D mask along x: 1 inside [start, end], smooth ramps just outside.

    A ramp that would fall outside the map is absent, so 0-10 and 40-50 stay full height to the
    map edge.

    Args:
        nx: number of columns.
        start_m, end_m: window edges in meters.
        cell: meters per column.
        ramp_m: ramp length on each side.
    Returns:
        1D array of length nx.
    Example:
        grid = full * x_window(2000, 10, 20)[None, :]
    """
    x = np.arange(nx) * cell
    rise = smoothstep((x - (start_m - ramp_m)) / ramp_m)
    fall = smoothstep(((end_m + ramp_m) - x) / ramp_m)
    return np.minimum(rise, fall)


def window_label(w):
    """Name of a window for file names: "nowindow" for None, otherwise "<start>-<end>".

    Args:
        w: None or a (start, end) tuple in meters.
    Returns:
        str, e.g. "10-20".
    """
    return "nowindow" if w is None else f"{w[0]}-{w[1]}"


def ripple_field(height_cm, wavelength_m, seed=7, cell=CELL):
    """Full-size ripple heightmap with no x window.

    Args:
        height_cm: crest-to-trough height of the rippled band.
        wavelength_m: crest spacing.
        seed: random seed.
        cell: meters per pixel.
    Returns:
        2D array in meters, rows = y, columns = x, whose max - min equals height_cm.
    Example:
        grid = ripple_field(20, 1.0)
    """
    ny, nx = round(TOTAL_Y / cell), round(TOTAL_X / cell)
    ny_r = round(RIPPLE_Y / cell)
    f = ripple_depth_map(size=(ny_r, nx), wavelength=wavelength_m / cell,
                         amplitude=1.0, base_depth=0.0, seed=seed)
    f -= f.mean()
    f *= edge_taper(ny_r, round(TAPER_Y / cell))[:, None]
    f *= (height_cm / 100.0) / (f.max() - f.min())   # exact crest-to-trough after the fade
    pad_top = (ny - ny_r) // 2
    return np.pad(f, ((pad_top, ny - ny_r - pad_top), (0, 0)))


def save_preview(path, grid, title, height_cm, cell=CELL):
    """Save a preview jpg of a heightmap with a symmetric color scale.

    Args:
        path: output jpg.
        grid: 2D array in meters.
        title: plot title.
        height_cm: crest-to-trough height, sets the color range to +/- half of it.
        cell: meters per pixel.
    Returns:
        None; writes path.
    """
    half = height_cm / 100.0 / 2
    ny, nx = grid.shape
    fig = Figure(figsize=(10, 5.8))
    ax = fig.subplots()
    im = ax.imshow(grid, cmap="viridis", vmin=-half, vmax=half,
                   extent=[0, nx * cell, ny * cell / 2, -ny * cell / 2])
    ax.set_xlabel("x (m)")
    ax.set_ylabel("y (m)")
    ax.set_title(title)
    fig.colorbar(im, ax=ax, label="offset from flat (m), + = deeper")
    fig.tight_layout()
    fig.savefig(path, dpi=100)


def generate_all(txt_root="example_depthmaps", img_root="example_images",
                 seed=7, duplicate_per_distance=True, heights_cm=HEIGHTS_CM, reverse_y=True):
    """Write every heightmap variant, its preview and the manifest.

    Args:
        txt_root: where the txt files and manifest.csv go.
        img_root: where the preview jpgs go.
        seed: random seed.
        duplicate_per_distance: write a copy of each file for every sonar height (5 m and 10 m).
        heights_cm: ripple heights to build.
        reverse_y: write the txt rows reversed in y (see the module docstring).
    Returns:
        None.
    Example:
        generate_all(heights_cm=[5], duplicate_per_distance=False)   # quick test
    """
    txt_root, img_root = Path(txt_root), Path(img_root)
    distances = DISTANCES if duplicate_per_distance else DISTANCES[:1]
    nx = round(TOTAL_X / CELL)
    min_y = -TOTAL_Y / 2

    experiments = {
        "ripple_sizes_fixed_wl": [(h, 1.0) for h in heights_cm],
        "ripple_sizes_scaled_wl": [(h, h / 100.0 / STEEPNESS) for h in heights_cm if h != 10],
    }
    manifest = []

    for exp, cases in experiments.items():
        for h, wl in cases:
            sub = f"{h:02d}cm"
            (txt_root / exp / sub).mkdir(parents=True, exist_ok=True)
            (img_root / exp / sub).mkdir(parents=True, exist_ok=True)
            full = ripple_field(h, wl, seed=seed)

            for w in WINDOWS:
                grid = full if w is None else full * x_window(nx, *w)[None, :]
                first_txt = first_img = None
                for d in distances:
                    name = f"depthmap_{window_label(w)}_{d}"
                    t_path = txt_root / exp / sub / f"{name}.txt"
                    i_path = img_root / exp / sub / f"{name}.jpg"
                    if first_txt is None:
                        save_heightmap_txt(t_path, grid, 0.0, CELL, min_y, CELL, reverse_y=reverse_y)
                        save_preview(i_path, grid, f"{exp}/{sub}  window {window_label(w)}  (wavelength {wl:.2f} m)", h)
                        first_txt, first_img = t_path, i_path
                    else:
                        shutil.copyfile(first_txt, t_path)   # altitude does not change the heightmap
                        shutil.copyfile(first_img, i_path)
                    manifest.append([exp, h, round(wl, 3), window_label(w), d, str(t_path), str(i_path)])
                print(f"{exp}/{sub} window {window_label(w)} done")

    # the scaled experiment's 10 cm case is the fixed one: list it so the manifest is complete
    if 10 in heights_cm:
        for row in [r for r in manifest if r[0] == "ripple_sizes_fixed_wl" and r[1] == 10]:
            manifest.append(["ripple_sizes_scaled_wl"] + row[1:])

    with open(txt_root / "manifest.csv", "w", newline="") as f:
        wr = csv.writer(f)
        wr.writerow(["experiment", "height_cm", "wavelength_m", "window", "distance_m", "txt", "jpg"])
        wr.writerows(manifest)


if __name__ == "__main__":
    generate_all()