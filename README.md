# SeafloorGeneration
## Description
This repo is meant to store the entire workflow behind the seafloor generation script. It goes from the environent generation, to the depth map generation, and finally to the sonar generation. Each step is documented below with details on how to run each part.

---

## Infinigen for Environment Generation
We use Infinigen ([Home Page](https://infinigen.org/), [Github](https://github.com/princeton-vl/infinigen), [ArXiV](https://arxiv.org/abs/2406.11824)) to generate realistic environments which we use as pseudo-seafloors. Infinigen is perfect for our goals because it meets a few criteria:

1. It creates large-scale ($100\times100m^2$) scenes set in several unique environments with default plants and animals for each region. 
2. It allows for customization of each environment directly from the command line for rapid generation.

### Notes on Running Infinigen
First, pay attention to what OS you're on, each OS has varying degrees of support found [here](https://github.com/princeton-vl/infinigen/blob/main/docs/Installation.md). Infinigen uses __.gin__ files which can be a bit tricky so here's the [Infinigen documentation](https://github.com/princeton-vl/infinigen/tree/main#documentation). These are quite convenient to use since you can directly include class parameters and function parameters directly in the file. This allows for rapid customization and setting adjustments. I'll provide a few tables to help you navigate what I found to be the most useful parts.

#### Functions & Classes
| Name | Useful Reason | Location |
|---------|--------|-------|
| scene (function) | Covers chances of certain deformities + overall seed | infinigen/terrain/scene.py |
| compose_nature (function) | Covers chances of each fauna/flora individually | infinigen_examples/generate_nature.py |
| Terrain (class) | Sets the size of the environment + population bounds (where inside environment plants/animals can spawn) | infinigen/terrain/core.py |

#### Gin Files
| Name | Useful Reason |
|---------|--------|
| base.gin | Covers all details around how blender renders the environment. I haven't looked into this enough so I do this as a separate step, but it's likely a better way to do it in the future |
| monocular.gin | Very small, but useful for aligning the camera properly |
| base_nature.gin | Covers all details around how the nature is generated |

It is also worth mentioning that there might be some gin files added to this repo for reproducing anything I've done and providing an easier point of entry into Infinigen. There are default environment examples given in the Infinigen repo found in __infinigen_examples/configs_nature/scene_types__.

#### Running Infinigen
I continue to use the [Hello World: Nature](https://github.com/princeton-vl/infinigen/blob/main/docs/HelloWorld.md) script without issues. The [manage_jobs](https://github.com/princeton-vl/infinigen/blob/main/docs/ConfiguringInfinigen.md) version is more touchy on a Mac and seems to fail on occassion. It might be needed for better batch processing, but I just used a bash script (looping the __infinigen_examples.generate_nature__ command with different seeds/gin files) to get a similar result. Mostly, I just ran these commands on repeat, but you don't even need the second command depending on your goals. Just remember to switch out the gin files with your settings and you're good to go.

```python
mkdir outputs

# Generate a scene layout
python -m infinigen_examples.generate_nature --seed 0 --task coarse -g desert.gin simple.gin --output_folder outputs/hello_world/coarse

# Populate unique assets
python -m infinigen_examples.generate_nature --seed 0 --task populate fine_terrain -g desert.gin simple.gin --input_folder outputs/hello_world/coarse --output_folder outputs/hello_world/fine
```

---

## Blender for Depth Maps
If I was better with Infinigen, this step probably wouldn't exist. I'm pretty sure since Infinigen already uses Blender you can set this up directly in Infinigen. There's really three ways to go about this:

1. Use Blender to set the camera up high and face the camera down. Then collect the depth map, save it and post-process it into the correct form using python. <- __I did this one__
2. Figure out how to adjust the __.gin__ files to directly do (1) in the Infinigen command.
3. Convert the __.blend__ file into a __.stl__ file and that might be all you need for the next step (I haven't tried it).

To handle the Blender method (1) isn't too bad; I made a nice python script to handle it for you. You can run it headless so it's even less annoying, but it does take a bit to run and it could crash if your GPU sucks. If you want to do it manually to get a better sense of what's going on, I'll include some pictures to show exactly what values I used and why I used them.

<details>
<summary>Blender Steps for Building Depth Map</summary>

## ___This  is if you need to do it yourself for whatever reason. There is a python script to do this for you so you don't need to worry about this most likely.___

### 1. Camera Rig
Ensure that the camera rig is set to position (0, 0, 0) and rotation (0, 0, 0). The main camera is translated off of whatever these values are, so they act as an annoying offset if you're not paying attention.

![Rig Settings](/example_images/1_camera_rig.jpg)

### 2. Camera Settings
These are all of the settings I used for the camera. 
- Starting with the rendering side of things. I switched to EEVEE because it works better for the depth map stuff and it's faster.

![Camera Scene Settings](/example_images/2_camera_scene.jpg)

- The scene settings. Since this is $100\times100m^2$ and we want a resolution of $2.5cm$, we do $100/0.025 = 4000$ to get the resolution of $4000\times4000$. We also switch to EXR on the bottom to avoid accidentally normalizing the values.

![Camera Resolution Settings](/example_images/3_camera_res.jpg)

- The layer settings. Here we're just adding in a depth layer so we can calculate the depth.

![Camera Layer Settings](/example_images/4_camera_layers.jpg)

- The location settings. We want the location high in sky and at the center of our scene. It's arbitrarily set to 300, but it doesn't matter as long as it's above the scene. It faces $180\degree$ down to focus on the floor.

![Camera Location Settings](/example_images/5_camera_loc.jpg)

- The lens settings. These are extremely important. Orthographic lets us focus on the entire camera view and doesn't blur the edges I think. the scale is set to 100 here to focus on the entire scene.

![Camera Lens Settings](/example_images/6_camera_lens.jpg)

### 3. Composite Settings
In the `Compositing` tab you'll have to rebuild this node setup:

![Composite Nodes](/example_images/7_composite_nodes.jpg)

### 4. Render 
Go to the `Render` tab and hit `Render Image`.

</details>

With the __.exr__ file, we now take subsections of that space. We typically do $50\times30m^2$ patches so that we have a long stretch of land to run the sonar simulation across. Since the space is $4000\times4000px$ at $2.5cm$ resolution, that works out to subsections of size $2000\times1200px$ ($50/0.025 and 30/0.025$). We have done rescaling in the past since mountains that are 10m in height aren't really a thing in water, but that's really only necessary for some environments. Finally, we pass the depth map into a script to properly convert the depth map into a readable format for the next step. This format is a __.txt__ file with the rows and columns stacked.

---

## MASTODON for Sonar Scans
For this last portion, we use the Navy's open source version of [MASTODON](https://github.com/Sonar-Sim/MASTODON) to simulate a side-scan sonar pass over a heightmap. How the simulator itself works is covered in its own documentation (the `docs/html` folder that ships with it, starting at `QuickStart.html`), so this section only covers what we change and the scripts built around it.

### Setup
```bash
python -m venv venv
venv\Scripts\activate          # Windows; use `source venv/bin/activate` elsewhere
pip install -r requirements.txt
```
You also need the [MASTODON](https://github.com/Sonar-Sim/MASTODON) executable. Every script takes its path as `--exe-location`. Note: this was tested on Windows; the Linux distribution might have minor differences.

### What we change in the config
[`mastodon_configs/CONFIG.cfg`](mastodon_configs/CONFIG.cfg) is our template. The scripts copy it into each output folder, edit the keys below with `libconf`, and run MASTODON on the copy (so each run folder keeps the exact config it used). Everything else stays fixed: a 200 kHz LFM chirp (50 kHz bandwidth), a starboard-looking sonar at 10° depression, a 13-element receive array, 50 m of slant range and the `wk` beamformer. [`CONFIG_ripple.cfg`](mastodon_configs/CONFIG_ripple.cfg) is the same file with the ripple heightmap's path filled in (an absolute path from the author's machine, so edit it before use).

| Key | What we do with it |
|---|---|
| `environment.bottom.bathymetry.infile` | Path to the heightmap `.txt` (the `TXT FILE GOES HERE` placeholder in the template). |
| `environment.bottom.bathymetry.dx / dy / xmin / xmax / ymin / ymax` | The patch: $50\times30m^2$ at $2.5cm$ (x from 0 to 50, y from -15 to 15). Keep these matching the heightmap's header if you change the patch size. |
| `environment.bottom.type` | Sediment type from MASTODON's TR-9407 bottom model (e.g. `Fine sand`). `run_experiments.py` sweeps all 23. |
| `environment.bottom.depth`, `environment.ssp.depths` | Water depth in meters. The sound-speed depths must end at the bottom depth, i.e. `(0, <bottom depth>)`. |
| `platforms.depth` | Sonar depth in meters. The sonar's height above the floor is `bottom.depth - platforms.depth`. |
| `platforms.num_pings` | Number of pings. The track is `(num_pings - 1) * dping` long and is centered on y = 0. |
| `platforms.dping` | Distance between pings, 0.1365 m, which is half the length of the 13 × 0.021 m receive array. Recompute it if the array changes. |
| `propagation.range_max` | Slant range covered by the image (50 m). |

**Choosing `num_pings`:** with the track centered on y = 0, the $30m$ wide heightmap fits about 222 pings ($30.2m$ of track). 111 pings covers the middle $15m$, and anything above 222 runs off the map, which shows up as dark bands in the image.

### Heightmap format
```
2000 1200               <- columns (x), rows (y)
0.000000 0.025000       <- min_x, dx
-15.000000 0.025000     <- min_y, dy
<one value per line, x varying fastest, meters relative to the flat bottom>
```
MASTODON reads the rows in reverse y order. We found this by comparing a simulated image with its map: a file written top-to-bottom in plot order gives a sonar image that is mirrored along-track. `ripple_maker.py` and `make_ripple_variants.py` therefore write their rows reversed by default (`reverse_y=True`) so the image matches the plotted map. Maps from `exr2txt.ipynb` are written in plain order, so reverse their rows before saving, or add `--flip-track` when viewing.

![Example heightmap](/example_images/ripple_depth.jpg)

### Running
```bash
# one heightmap, one run  (results/ripple/ gets everything)
python src/run_mastodon.py --exe-location path/to/MASTODON.exe --input-txt example_depthmaps/ripple_depth.txt --output-folder results/ripple

# sweep over 23 bottom types and 7 ping counts on one heightmap
python src/run_experiments.py --exe-location path/to/MASTODON.exe --input-txt example_depthmaps/ripple_depth.txt --output-folder experiments/

# build the ripple heightmaps once, then sweep over them (add --depthmap-set scaled for the scaled-wavelength set)
python src/make_ripple_variants.py
python src/run_ripple_experiments.py --exe-location path/to/MASTODON.exe

# tile one normed image per bottom type into a labeled grid
python src/stitch_experiments.py --experiments-folder experiments/ --ping 222
```
The sweeps skip runs that already have their `{ping}_normed.jpg`, so an interrupted sweep can be rerun with the same command. A ripple run takes a few minutes, and each sweep set is 48 runs.

### What a run produces
Each run folder holds `CONFIG.cfg` (the config used), `run.log`, `StaveData.h5` (per-element data) and `Beamformed.h5` (MASTODON's image), plus the images made by `visualize_beamformed.py`:

- `raw.npy` / `raw.jpg`: the beamformed amplitudes. Rows are pings along the track and columns are slant range.
- `normed.npy` / `normed.jpg`: every range column divided by its own mean and clipped at 16, which removes the fall-off with range and leaves the texture. Columns closer than the sonar's height have no bottom echo yet, so they are left black.
- `grouped.jpg`: the heightmap, raw and normed stacked on one shared ground-range axis and one along-track axis, with the experiment named in the title.

![Example grouped image](/example_images/grouped_example.jpg)

The sweeps rename the jpgs to `{ping}_raw.jpg`, `{ping}_normed.jpg` and `{ping}_grouped.jpg`, and the ripple sweep deletes the large `.h5` files after each successful run (`raw.npy` is kept, so images can be remade).

### What each file does
Files in bold are useful beyond the toy ripple case; the rest exist to build and visualize the ripple examples.

| File | What it does | Why it exists |
|---|---|---|
| [`src/exr2txt.ipynb`](src/exr2txt.ipynb) | **Converts the Blender `.exr` depth render into a MASTODON `.txt` heightmap.** Cuts a random $50\times30m^2$ patch at $2.5cm$ and has a small viewer for checking the result. | The bridge from the Blender step above to MASTODON. Not used by the ripple examples, but needed for any Infinigen/Blender environment. |
| [`src/visualize_beamformed.py`](src/visualize_beamformed.py) | **Turns `Beamformed.h5` into `raw`, `normed` and `grouped` images and arrays.** Also a command line tool: `--section` crops, `--axes` labels the jpgs, `--flip-track` mirrors along-track, `--slant-range` keeps the grouped plot in slant range, `--no-group` skips it. | Every run script calls it, and it works on any beamformed file. |
| [`src/ripple_maker.py`](src/ripple_maker.py) | Generates one synthetic sand-ripple heightmap ($50\times30m^2$, 1 m wavelength, ±10 cm by default). | A toy case that needs no Blender or Infinigen. It made `example_depthmaps/ripple_depth.txt`. |
| [`src/make_ripple_variants.py`](src/make_ripple_variants.py) | Builds the heightmaps for the ripple sweep: heights of 5/10/20/50 cm, fixed or scaled wavelength, six x-windows (none, 0-10, ..., 40-50 m, with 1 m ramps) and a copy for each sonar height, plus previews and `manifest.csv`. | Lets us ask how ripple size and extent show up in the image. About 84 files of 20 MB each. |
| [`src/run_mastodon.py`](src/run_mastodon.py) | One run: copy the template, set the heightmap, run MASTODON, make the images. | The simplest starting point for a new heightmap. |
| [`src/run_experiments.py`](src/run_experiments.py) | Sweep over 23 bottom types and 7 ping counts on one heightmap. | Compares how sediment type changes the image. |
| [`src/run_ripple_experiments.py`](src/run_ripple_experiments.py) | Sweep over ripple height, window and sonar height (5 m or 10 m above a 20 m floor) at 222 pings: 48 runs per set. | The ripple study; resumable, and deletes the big files as it goes. |
| [`src/stitch_experiments.py`](src/stitch_experiments.py) | Tiles one normed image per bottom type from a `run_experiments.py` sweep into one labeled grid. | Side-by-side comparison of the bottom types. |

![Window example](/example_images/window_example.jpg)
*A 20 cm ripple heightmap windowed to x = 10-20 m (from `make_ripple_variants.py`).*
