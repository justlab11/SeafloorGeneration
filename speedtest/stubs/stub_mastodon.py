"""Stand-in for the MASTODON executable: argv[1] = config path. Like the real one it works in the config's folder, writes MASTLogFile.txt,
StaveData.h5 and Beamformed.h5; memory and time scale with num_pings x range_max."""
import hashlib, os, re, sys, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import stub_common as sc
cfgp = os.path.abspath(sys.argv[1]); os.chdir(os.path.dirname(cfgp)); txt = open(cfgp).read()
pings = int(re.search(r"num_pings = (\d+)", txt).group(1)); rmax = float(re.search(r"range_max = ([\d.]+)", txt).group(1))
work = pings * rmax / 500.0
os.environ.setdefault("STUB_SLEEP", str(0.2 + work * 0.5)); os.environ.setdefault("STUB_MB", str(int(20 + work * 150)))
for need in ("MASTODON_FOLDER",):
    if not os.environ.get(need): sys.stderr.write("warning: %s not set\n" % need)
sc.behave("mastodon", 0.3, 40)
open("MASTLogFile.txt", "w").write("stub log\npings %d\n" % pings)
open("StaveData.h5", "wb").write(os.urandom(2 * 1024 * 1024))
open("Beamformed.h5", "wb").write((hashlib.sha256(txt.encode()).digest() * 64) + (os.urandom(32) if sc.nondet() else b""))
print("stub mastodon done")
