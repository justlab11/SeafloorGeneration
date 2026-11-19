"""Stand-in for extract_patch.py: writes patch_00..08/heightmap_patch.txt (40x40, derived from the blend file content), patches.json."""
import hashlib, json, os, random, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import stub_common as sc
a = sys.argv[1:]; blend = a[a.index("--blend") + 1]; out = a[a.index("--out") + 1]
sc.behave("extract", 0.2, 30)
if not os.path.exists(blend): sys.stderr.write("blend not found %s\n" % blend); sys.exit(4)
seed = int(hashlib.sha256(open(blend, "rb").read()).hexdigest()[:8], 16)
for k in range(9):
    d = os.path.join(out, "patch_%02d" % k); os.makedirs(d, exist_ok=True); r = random.Random(seed + k); n = 40
    with open(os.path.join(d, "heightmap_patch.txt"), "w") as f:
        f.write("%d %d\n0.000000 0.025000\n0.000000 0.025000\n" % (n, n)); f.write("".join("%.6f\n" % (0.2 * r.random()) for _ in range(n * n)))
json.dump([dict(index=k, hole_fraction=0.0, patch_in_frustum=True) for k in range(9)], open(os.path.join(out, "patches.json"), "w"))
print("stub extract done")
