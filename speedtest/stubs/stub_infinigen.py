"""Stand-in for ctrl_driver.py (generate_nature). Parses --seed --task ... --output_folder [--input_folder]; writes fake scene files."""
import os, sys, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import stub_common as sc
a = sys.argv[1:]
seed = a[a.index("--seed") + 1]; out = a[a.index("--output_folder") + 1]
tasks = []; i = a.index("--task") + 1
while i < len(a) and not a[i].startswith("-"): tasks.append(a[i]); i += 1
stage = "infinigen_fine" if "populate" in tasks else "infinigen_coarse"
if "Terrain.device='cuda'" in " ".join(a): os.environ["STUB_SLEEP"] = str(float(os.environ.get("STUB_SLEEP", 0.3)) * 0.5)   # pretend CUDA is faster
sc.behave(stage)
if stage == "infinigen_fine":
    inp = a[a.index("--input_folder") + 1]
    if not os.path.exists(os.path.join(inp, "scene.blend")): sys.stderr.write("no coarse scene in %s\n" % inp); sys.exit(4)
os.makedirs(out, exist_ok=True)
with open(os.path.join(out, "scene.blend"), "w") as f: f.write("fake blend %s %s %s\n" % (stage, seed, time.time() if sc.nondet() else 0))
print("stub infinigen done", stage)
