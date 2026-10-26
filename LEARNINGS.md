# What we have learned about MASTODON runs

Rules of thumb from our own runs, kept separate from the [README](README.md) so they can be moved there later. The numbers come from two experiments:

- **Material + pings** (`experiments/material+pings`): one rippled heightmap (`ripple_depth.txt`, ripples up to about 10 cm from flat), 23 bottom types $\times$ 7 ping counts from 13 to 333 (the 211-ping runs were a mistake and are left out below), sonar 3 m above the floor (16 m water, 13 m sonar depth).
- **Ripple sweeps** (`experiments/height+window+distance+pings`, plus `_scaled`): ripple height $\times$ x-window $\times$ sonar height (5 m / 10 m above a 20 m floor) at 222 pings. Both sets finished: 48 experiments each (4 heights × 6 windows × 2 sonar heights), 96 in total, with no failures.

"Raw level" below is the mean raw amplitude over 15 to 40 m of slant range, in the arbitrary units MASTODON writes. Only ratios between runs mean anything. These are single runs on a single heightmap and seed, so treat small differences as noise.

## 1. Ping count and the track

The image's range axis is always 50 m (16,000 columns), but the along-track axis is set by `num_pings`:

- The track is `(num_pings - 1) * dping` meters long and **centered on y = 0**. The image is `13 * num_pings` rows tall (13 rows per ping, one per receive element), so rows are 1.05 cm apart.
- `dping = 0.1365 m` is half the receive array (13 elements $\times$ 0.021 m $\div$ 2), so it only needs to change if the array does.
- To cover a track of length `L`: `num_pings = 1 + L / dping`. For the $30m$ map that is 222.

| `num_pings` | Track (m) | Image rows | Along-track extent | Fits the 30 m map? |
|---|---|---|---|---|
| 13 | 1.6 | 169 | ±0.8 m | yes, a thin strip |
| 25 | 3.3 | 325 | ±1.6 m | yes |
| 49 | 6.6 | 637 | ±3.3 m | yes |
| 99 | 13.4 | 1287 | ±6.7 m | yes |
| 111 | 15.0 | 1443 | ±7.5 m | yes, middle half |
| 222 | 30.2 | 2886 | ±15.1 m | just covers it |
| 333 | 45.3 | 4329 | ±22.7 m | no: 7.7 m past each edge |

- **Why low ping counts look "thin":** the image is not a different picture, it is a shorter slice. 13 pings gives a strip 1.6 m tall and 50 m wide (the `13_normed.jpg` files are 22 px tall and 1710 px wide), and only a few ripple crests cross it. Every extra ping adds 13 rows at the end of the track.
- **Past the map edge there is no bottom return.** In the 333-ping runs the mean normalized brightness drops about tenfold at |y| = 15 m (from about 1.5 to 0.16), which is the dark band above and below the ripples.
- **Cost grows with pings.** One 333-ping run took 4 min 45 s: 3 min 44 s of simulation (about 0.68 s per ping), about 45 s of beamforming, and MASTODON reported about 21.8 GB of memory used. `StaveData.h5` was 1.1 GB and `Beamformed.h5` 0.55 GB. Memory grows with the ping count at about 66 MB per ping (14.7 GB for a 222-ping run, 21.8 GB for 333). With two 222-ping sweeps running side by side each run took about 5 min (4 min 42 s to 4 min 55 s of simulation).

## 2. Bottom type (`environment.bottom.type`)

Raw level relative to fine sand, the nadir echo relative to the brightest range column, and how well the *normalized* image follows the ripples (absolute correlation with the heightmap's x-slope, higher = more ripple structure):

| Bottom type | Raw level ($\times$ fine sand) | Nadir / peak | Ripple correlation |
|---|---|---|---|
| Rough Rock | 12.32 | 0.04 | 0.38 |
| Rock | 6.12 | 0.05 | 0.42 |
| Cobble | 3.72 | 0.05 | 0.42 |
| Sandy Gravel | 2.50 | 0.04 | 0.41 |
| Very Coarse Sand | 2.27 | 0.05 | 0.41 |
| Muddy Sandy Gravel | 2.05 | 0.05 | 0.42 |
| Coarse Sand | 1.85 | 0.06 | 0.42 |
| Gravelly Muddy Sand | 1.66 | 0.07 | 0.42 |
| Medium Sand | 1.38 | 0.08 | 0.42 |
| Muddy Gravel | 1.16 | 0.09 | 0.42 |
| Fine Sand | 1.00 | 0.09 | 0.42 |
| Muddy Sand | 0.91 | 0.09 | 0.42 |
| Very Fine Sand | 0.85 | 0.09 | 0.42 |
| Clayey Sand | 0.83 | 0.08 | 0.42 |
| Coarse Silt | 0.81 | 0.08 | 0.41 |
| Sandy Silt | 0.79 | 0.07 | 0.41 |
| Medium Silt | 0.53 | 0.08 | 0.42 |
| Sandy Mud | 0.52 | 0.08 | 0.42 |
| Fine Silt | 0.52 | 0.08 | 0.42 |
| Sandy Clay | 0.52 | 0.08 | 0.42 |
| Very Fine Silt | 0.51 | 0.08 | 0.42 |
| Silty Clay | 0.51 | 0.08 | 0.42 |
| Clay | 0.50 | 0.08 | 0.42 |

- **The bottom type changes how bright the image is, not what it looks like.** Raw level spans about 25$\times$ from clay (1.6e+10) to rough rock (4.0e+11), and follows grain size and hardness: rock, cobble and gravels are brightest, sands are in the middle and the silts and clays are darkest.
- **The soft sediments are indistinguishable.** Clay, silty clay, sandy clay, fine and very fine silt, medium silt and sandy mud all land within 5% of each other.
- **Normalizing removes the difference.** Each range column is divided by its own mean, so the ripple correlation is 0.41 to 0.42 for every type except rough rock (0.38). The stitched `normed` grids therefore look alike. **To compare materials, use the raw images with one shared color scale.**
- **The falloff with range is the same for every material** (about 1.4$\times$ brighter at 10 to 15 m than at 15 to 40 m, and 0.56$\times$ at 40 to 48 m). It comes from the geometry and the gain correction, not the sediment. Rough rock is slightly flatter.
- **Hard bottoms hide the nadir line.** The nadir echo is about 0.04 to 0.06 of the peak for rock, cobble, sandy gravel and the coarse sands, and rises to about 0.09 for fine sands and muds, probably because the hard bottoms scatter much more strongly off-nadir (not tested).
- **Do not take the absolute levels as physical.** MASTODON's log notes that 200 kHz is outside the validated range of the TR-9407 bottom model.

## 3. Sonar height

The sonar's height above the floor is `bottom.depth - platforms.depth`.

- **Nothing returns before slant range equals the height.** The first thing in each image is a sharp nadir echo at exactly `r = height`; before it the signal sits near the noise floor, about four orders of magnitude below the signal. `visualize_beamformed.py` leaves those columns black.
- **A good image only starts well after the nadir.** The mean signal reaches half of its peak at a slant range of about 5.7 to 6.2 m with the sonar 3 m up, 9.4 to 10.2 m at 5 m, and 17.1 to 18.9 m at 10 m. The higher the sonar, the more near range is dim.
- **The nadir line stands out more from higher up:** averaged over the windows, its echo is 0.20 to 0.25 of the peak at 5 m and 0.33 to 0.41 at 10 m. It gets weaker as the ripples get taller (5 cm to 50 cm: 0.25 to 0.20 at 5 m, 0.41 to 0.33 at 10 m), probably because taller ripples scatter more energy off-nadir (not tested).
- **Ripples are harder to see from higher up.** For windows from x = 10 m outward, the contrast from 10 m up was lower than from 5 m up in all 32 of 32 comparisons (on average 16% lower, from 1% to 24%); see section 5. In the x = 0 to 10 m window the two heights are within about 0.1 of each other. This fits grazing angles being steeper from higher up, but we have not tested that on its own.

## 4. Ripple height

Whole-map contrast of the normalized image (its standard deviation), with the correlation with the heightmap's x-slope in brackets:

| Set, sonar height | 5 cm | 10 cm | 20 cm | 50 cm |
|---|---|---|---|---|
| fixed 1 m wavelength, 5 m | 0.79 (0.33) | 0.91 (0.43) | 1.06 (0.45) | 1.31 (0.35) |
| fixed 1 m wavelength, 10 m | 0.68 (0.21) | 0.74 (0.34) | 0.85 (0.43) | 1.11 (0.41) |
| scaled wavelength, 5 m | 0.90 (0.47) | 0.91 (0.43) | 0.97 (0.34) | 1.18 (0.13) |
| scaled wavelength, 10 m | 0.72 (0.34) | 0.74 (0.34) | 0.79 (0.30) | 0.97 (0.06) |

- **Contrast rises steadily with ripple height.** From 5 cm to 50 cm the standard deviation goes from 0.79 to 1.31 with the sonar 5 m up and from 0.68 to 1.11 from 10 m up (fixed wavelength). The taller ripples are always easier to see.
- **The correlation is a poor measure for the tallest ripples.** It peaks at 20 cm (0.45) and drops at 50 cm (0.35), and in the scaled set, where the 50 cm ripples have a 5 m wavelength, it falls to 0.13 even though the contrast is the highest. A likely reason is that big ripples cast shadows that the linear slope correlation does not capture (not tested). Use the contrast numbers for comparisons.
- The 10 cm map is the same file in both sets, so its two rows agree.

## 5. Windows

Each cell is the contrast ratio: the standard deviation of the normalized image inside the window divided by the standard deviation over the flat floor outside it (1.00 = ripples invisible). The whole-map column is the plain standard deviation. Windows have 1 m ramps on each side.

**Fixed 1 m wavelength**

| Ripple height | Sonar height | whole map (std) | x = 0-10 m | 10-20 m | 20-30 m | 30-40 m | 40-50 m |
|---|---|---|---|---|---|---|---|
| 5 cm | 5 m | 0.79 | 0.90 | 1.04 | 1.16 | 1.26 | 1.43 |
| 5 cm | 10 m | 0.68 | 0.87 | 1.02 | 1.05 | 1.10 | 1.12 |
| 10 cm | 5 m | 0.91 | 0.95 | 1.11 | 1.29 | 1.56 | 1.72 |
| 10 cm | 10 m | 0.74 | 0.96 | 1.03 | 1.08 | 1.22 | 1.32 |
| 20 cm | 5 m | 1.06 | 1.10 | 1.29 | 1.57 | 1.89 | 1.92 |
| 20 cm | 10 m | 0.85 | 1.13 | 1.11 | 1.19 | 1.50 | 1.56 |
| 50 cm | 5 m | 1.31 | 1.57 | 1.71 | 1.94 | 2.24 | 2.19 |
| 50 cm | 10 m | 1.11 | 1.47 | 1.69 | 1.63 | 1.89 | 1.80 |

**Scaled wavelength** (wavelength = height ÷ 0.1: 0.5 m for 5 cm, 2 m for 20 cm, 5 m for 50 cm; the 10 cm rows are the same file as above)

| Ripple height | Sonar height | whole map (std) | x = 0-10 m | 10-20 m | 20-30 m | 30-40 m | 40-50 m |
|---|---|---|---|---|---|---|---|
| 5 cm | 5 m | 0.90 | 0.93 | 1.10 | 1.33 | 1.59 | 1.62 |
| 5 cm | 10 m | 0.72 | 0.93 | 1.03 | 1.09 | 1.23 | 1.23 |
| 10 cm | 5 m | 0.91 | 0.95 | 1.11 | 1.29 | 1.56 | 1.72 |
| 10 cm | 10 m | 0.74 | 0.96 | 1.03 | 1.08 | 1.22 | 1.32 |
| 20 cm | 5 m | 0.97 | 1.08 | 1.16 | 1.38 | 1.65 | 1.84 |
| 20 cm | 10 m | 0.79 | 1.10 | 1.06 | 1.13 | 1.31 | 1.45 |
| 50 cm | 5 m | 1.18 | 1.35 | 1.55 | 1.82 | 2.01 | 1.97 |
| 50 cm | 10 m | 0.97 | 1.29 | 1.42 | 1.53 | 1.66 | 1.51 |

- **Contrast generally grows with ground range for every ripple size** (with a few small dips, mostly from 10 m up). With the sonar 5 m up (fixed wavelength) the ratio goes from 0.90 to 1.43 for 5 cm ripples, 1.10 to 1.92 for 20 cm and 1.57 to 2.19 for 50 cm, flattening off past x = 30 m for the tallest. A likely reason (not tested on its own) is that the grazing angle is steeper close to the sonar, which gives less relief shading.
- **Whether the closest window (x = 0 to 10 m) shows anything depends on ripple size.** It is invisible (ratio below 1) for 5 and 10 cm ripples, only just visible for 20 cm (1.10 and 1.13) and clear for 50 cm (1.47 to 1.57).
- **Higher sonar, less contrast:** for windows from x = 10 m outward, 10 m up was lower than 5 m up in every one of the 32 comparisons (16% lower on average). Example, 20 cm ripples at x = 30 to 40 m: 1.89 from 5 m up, 1.50 from 10 m up.
- **Steeper ripples show more.** At a given height the shorter wavelength has more contrast (x = 30 to 40 m, 5 m up, fixed vs scaled): 5 cm 1.26 vs 1.59 (the scaled one is the shorter, 0.5 m), 20 cm 1.89 vs 1.65 and 50 cm 2.24 vs 2.01 (the fixed one is the shorter, 1 m). Steepness (height ÷ wavelength) explains much of it: the 0.1-steepness ripples of 5, 10 and 20 cm land close together (1.59, 1.56, 1.65), but the 50 cm ones are higher (2.01), so height matters too.

## Practical rules of thumb

1. Pick `num_pings` from the track you want: `1 + L / 0.1365`. Keep the heightmap's y range at least as long as the track (222 pings for $30m$).
2. A thin image means few pings, not a failed run.
3. Do not read the normalized image for material differences; use raw with one color scale.
4. Choose the sonar height with the range you care about in mind: the image is only usable from about twice the height outward, and the nadir line sits at `r = height`.
5. Maps written in plain row order give a sonar image mirrored along-track (see the README); the ripple scripts already write them reversed.
6. Memory is about 66 MB per ping (14.7 GB at 222 pings), so check the machine before running several sweeps in parallel.
7. Runs are repeatable. The 10 cm map is in both sweeps, and its two raw images differ in only 0.0008% of their values, by at most 4e-10 of the peak, so a difference between two runs is the setting you changed. (MASTODON's log shows a fixed bathymetry seed of 314159.)
8. For ripples, compare the contrast (standard deviation, or the ratio to the flat floor), not the correlation with the slope, which breaks down for the tallest ripples.
9. A ripple field close to the sonar (x < 10 m) is hard to see unless the ripples are very tall (clear at 50 cm in our runs, only just visible at 20 cm), and every window gets harder to see from a higher sonar.

## How the numbers were measured

- **Raw level:** mean raw amplitude over 15 to 40 m of slant range on the rows with |y| < 10 m. For the material runs, the arrays come from the 333-ping run kept in each folder, and the normalized image was recomputed from `raw.npy` with the current `apply_mag` (the jpgs from that sweep used the older 20% trim).
- **Ripple correlation:** absolute correlation between the normalized image (converted to ground range) and the x-slope of the heightmap, on rows with |y| < 10 to 11 m and ground range beyond 4 m (material runs) or inside the window (ripple runs).
- **Contrast ratio:** standard deviation of the normalized image inside the window over the same quantity on the flat part of the map outside the window and its ramps.
- Ripple-sweep numbers use each experiment's saved `raw.npy` and heightmap, recomputed with the current `apply_mag`. There is one run per experiment, so differences of a few hundredths are within what we would call noise.
- The analysis scripts are throwaway files in `tmp/` and are not in the repo.
