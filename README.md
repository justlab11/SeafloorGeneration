# SeafloorGeneration
## Description
This repo is meant to store the entire workflow behind the seafloor generation script. It goes from the environent generation, to the depth map generation, and finally to the sonar generation. Each step is documented below with details on how to run each part.

---

## Infinigen for Environment Generation
We use Infinigen ([Home Page](https://infinigen.org/), [Github](https://github.com/princeton-vl/infinigen), [ArXiV](https://arxiv.org/abs/2406.11824)) to generate realistic environments which we use as pseudo-seafloors. Infinigen is perfect for our goals because it meets a few criteria:

1. It creates large-scale ($100\times100m^2$) scenes set in several unique environments with default fauna and flora for each region. 
2. It allows for customization of each environment directly from the command line for rapid generation.
3. 

### Notes on Running Infinigen
First, pay attention to what machine you're on, the machines have varying degrees of support found [here](https://github.com/princeton-vl/infinigen/blob/main/docs/Installation.md). Infinigen uses __gin__ files which can be a bit tricky so here's the [Infinigen documentation](https://github.com/princeton-vl/infinigen/tree/main#documentation). These are quite convenient to use since you can directly include class parameters and function parameters directly in the file. This allows for rapid customization and setting adjustments. I'll provide a few tables to help you navigate what I found to be the most useful parts.

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

### 1. Camera Rig
Ensure that the camera rig is set to position 0,0,0 and rotation 0,0,0. The main camera is translated off of whatever these values are, so they act as an annoying offset if you're not paying attention.

![Rig Setup](/example_images/1_camera_rig.jpg)

### 2. Camera Settings
These are all of the settings I used for the camera. 
- Starting with the rendering side of things. I switched to EEVEE because it works better for the depth map stuff and it's faster.

![Rig Settings](/example_images/2_camera_scene.jpg)

- The scene settings. Since this is $100\times100m^2$ and we want a resolution of $2.5cm$, we do $100/0.025 = 4000$ to get the resolution of $4000\times4000$. We also switch to EXR on the bottom to avoid accidentally normalizing the values.

![Camera Resolution Settings](/example_images/3_camera_res.jpg)

- The layer settings. Here we're just adding in a depth layer so we can calculate the depth.

![Camera Layer Settings](/example_images/4_camera_layers.jpg)

- The location settings. We want the location high in sky and at the center of our scene. It's arbitrarily set to 300, but it doesn't matter as long as it's above the scene. It faces $180\degree$ down to focus on the floor.

![Camera Location Settings](/example_images/5_camera_loc.jpg)

- The lens settings. These are extremely important. Orthographic lets us focus on the entire camera view and doesn't blur the edges I think. the scale is set to 100 here focus on the entire scene.

![Rig Setup](/example_images/6_camera_lens.jpg)

</details>

### 3. Composite Settings
In the `Compositing` tab you'll have to rebuild this node setup:

