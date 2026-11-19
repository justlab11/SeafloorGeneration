# Speed test: Infinigen -> heightmap -> MASTODON

Standard-library Python (psutil is used if importable, otherwise `/proc`). Written and tested on Windows against stubs; **never executed against the real Linux MASTODON or Infinigen**. Copy the whole `speedtest/` folder to the Linux machine.

## Quick start on the target
```
cd speedtest
python3 -m unittest discover -s tests          # parsers/helpers (needs nothing else)
python3 speedtest.py --selftest                # whole flow against stubs, a few seconds
$EDITOR speedtest_config.json                  # fix every value that has a "__GUESS" sibling (list: --list-guesses)
python3 speedtest.py --check                   # pass/fail table, runs nothing heavy
python3 speedtest.py                           # full run; add --resume after an interruption
python3 speedtest.py --redact                  # same, report without paths / user / host names
```
Useful options: `--only a,b` / `--skip a,b` (stages: hardware, setup_gin, infinigen_coarse, infinigen_fine, extract, mastodon_small, mastodon_prod, cuda_terrain, concurrency, determinism, e2e), `--repeats N` (end-to-end repeats, default 5), `--reference FILE`, `--set key.sub=value` (JSON value, e.g. `--set concurrency.levels=[1,2]`), `--out DIR`.

Outputs in `output_dir` (default `./speedtest_out`): `stages/<stage>.json` (written the moment a stage ends), `speedtest.log` (timestamps), `logs/` and `work/*/logs/` (stdout/stderr of every command), `speedtest_report.md` and `.json`. `--redact` only affects the report files; logs keep raw paths. Failed commands appear in the report with the command line and the last 20 stderr lines.

## What is measured
| stage | what |
|---|---|
| hardware | cores, RAM, GPU (`nvidia-smi`), free disk |
| setup_gin | copies `assets/gin/*.gin` into the Infinigen checkout's `configs_nature/` (gin resolves `-g` files by stem from there; the earlier scripts did the same). Set `infinigen.install_gin=false` if you installed them yourself |
| infinigen_coarse / infinigen_fine | `ctrl_driver.py --task coarse`, then `--task populate fine_terrain` (timed separately; wide-camera Task-L gin chain; seed 1) |
| extract | `extract_patch.py` (9 patches of 5 x 5 m at 2.5 cm, from the camera nadir; needs bpy, so it runs in the Infinigen python) |
| mastodon_small | 20 pings, range about 12.9 m, 3 repeats |
| mastodon_prod | 111 pings, 30 m range, 520 kHz / 63 kHz baseband, real-aperture "magnitude" beamformer (Task L/K spec). Old machine: about 76 s and 8.6 GB |
| cuda_terrain | only if a GPU is visible: coarse + fine with `Terrain.device='cuda'` vs CPU, same seed |
| concurrency | N = 1, 2, 4, 8 simultaneous production-size MASTODON runs; a level is skipped if predicted free RAM (from the single-run peak) would fall below 15 %, and a running level is killed if free RAM drops below 15 %; ends there |
| determinism | same seed twice: MASTODON small config (SHA-256 of `Beamformed.h5`, plus the fraction of differing bytes), Infinigen (first run vs a second full run: `patch_00` heightmap, max abs difference). `--reference` below |
| e2e | one evaluation = Infinigen coarse + fine + extraction + 9 MASTODON small runs (`e2e.parallel` at a time, `auto` = best level of the concurrency stage), new seed each; repeats 5; **evaluations per hour = 3600 / mean wall** (failed evaluations are excluded and counted) |

MASTODON configs are generated from the template in `st_mastodon.py` (identical to Task L's `make_cfg`, verified key by key), validated with `st_validate.py` (a stdlib port of `validate_config.py`, rules R1-R9 unchanged), written, run, and `StaveData.h5` is deleted after every run (also after failures). If the validator finds an ERROR the run is skipped and the error is reported, unless `mastodon.allow_validation_errors` is true.

Heightmap fed to MASTODON: the extracted `patch_00`; else the reference heightmap (config `paths.reference_heightmap` or `--reference`, if it is not a `.json`); else a synthetic rough patch (the report says which). The patch is detrended, tapered and pasted at x = 10 m into a flat map (as in Task L). That means MASTODON stages still run when Infinigen fails.

`--reference`: a heightmap txt (compared numerically with the extracted patch) or the `speedtest_report.json` of another machine (determinism hashes compared; Infinigen patch SHA and MASTODON output SHA).

## Safety
- Do not start the MASTODON binary without a config argument: it opens a file dialog (seen on Windows).
- Disk: every stage is skipped if free disk is below `limits.min_free_disk_gb` (20). Scene folders can be several GB; they are deleted after e2e and determinism runs unless `limits.keep_work` is true.
- Only `setup_gin` writes into the Infinigen checkout (three `.gin` files). Nothing else outside `output_dir`.

## Assumptions (record)
1. The Infinigen env has `bpy`, `gin`, `infinigen` importable by the interpreter in `paths.python`; `ctrl_driver.py` is run with the checkout as working directory (as in `run_scene.sh`); `LD_LIBRARY_PATH` includes `$CONDA_PREFIX/lib`.
2. The infinigen gin chain is `under_water.gin simple.gin no_assets.gin ctrlL_base.gin ctrlL_cam_wide.gin ctrlL_cfg_speedtest.gin` (Task L default config, copied from `tmp/L_main_test/configs`; `ctrlL_cam_wide.gin` already contains `pixels_per_cube = 0.9`).
3. `extract_patch.py` finds `overnight/fast_extract.py`, `02_extract_heightmap.py` and `hm_io.py` relative to itself, hence the `assets/tools/` layout (copies of the dev files; originals untouched).
4. MASTODON v0.22.6 changes into the config's folder itself, writes `StaveData.h5`, `Beamformed.h5`, `MASTLogFile.txt` there, exits 0 on some silent failures (hence the log scan for "Error!" and the missing-output check). `MASTODON_FOLDER` = install root (docs QuickStart). `BELLHOP_PATH` is only for Bellhop models; the Geometric model used here needs none.
5. The shipped `bin/MASTODON` has no executable bit in the copy we have; the ELF needs libX11, libGLX, libOpenGL, libgomp, libstdc++ (read from its dynamic section). On a headless server missing those, `--check` fails the ldd row.
6. RAM is "available" memory (MemAvailable, or free+buffers+cached) as a percentage of total; peak RSS is the sum over the process tree, sampled every 0.2 s, so very short spikes can be missed.
7. Determinism of the MASTODON HDF5 is judged by bytes (no HDF5 reader in the stdlib); differing HDF5 metadata could make byte hashes differ even with identical data; the report gives the differing byte fraction so a tiny fraction can be read as metadata only.

## GUESS values (likeliest to fail on Linux)
Run `python3 speedtest.py --list-guesses`. In `speedtest_config.json` each has a sibling `<key>__GUESS` with the reason:
`paths.mastodon_exe`, `paths.mastodon_root`, `paths.mastodon_wrapper` (xvfb-run if headless), `paths.infinigen_dir`, `paths.python`, `paths.conda_prefix`, `env.mastodon.LD_LIBRARY_PATH`, `env.mastodon.BELLHOP_PATH`, `infinigen.gin_install_dir`, `infinigen.cuda_device_args` (`Terrain.device='cuda'`, unverified and needs a CUDA-built terrain lib), `mastodon.profiles.prod.map` (the old production map size is unknown; peak RAM will differ from 8.6 GB if it was finer), `mastodon.profiles.prod.threads`, `concurrency.threads_policy`.

## Files
`speedtest.py` (orchestration, checks, report, selftest), `st_platform.py` (OS-specific: spawn, `/proc`, ldd, nvidia-smi, ELF; pure parsers), `st_mastodon.py` (config template and writer, heightmap I/O), `st_validate.py` (validator port), `speedtest_config.json`, `README.md`, `assets/gin/*.gin` (3), `assets/tools/` (`ctrl_driver.py`, `I_controlled_scene/extract_patch.py`, `overnight/fast_extract.py`, `02_extract_heightmap.py`, `hm_io.py`), `stubs/` (`stub_common.py`, `stub_infinigen.py`, `stub_extract.py`, `stub_mastodon.py`, `stub_nvidia.py`), `tests/test_units.py`.
