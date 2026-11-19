"""Shared stub behaviour for --selftest: sleep, hold memory, optionally fail, write deterministic fake outputs."""
import hashlib, os, sys, time
def behave(stage, sleep_default=0.3, mb_default=40):
    fail = [x for x in os.environ.get("STUB_FAIL", "").split(",") if x]
    sleep = float(os.environ.get("STUB_SLEEP", sleep_default)); mb = int(float(os.environ.get("STUB_MB", mb_default)))
    sys.stderr.write("stub %s starting (sleep %.2fs, %d MB)\n" % (stage, sleep, mb))
    buf = bytearray(mb * 1024 * 1024)
    for i in range(0, len(buf), 4096): buf[i] = 1          # touch pages so they are resident
    time.sleep(sleep)
    if stage in fail:
        for i in range(30): sys.stderr.write("stub failure injected %s line %d\n" % (stage, i))
        sys.exit(3)
    return len(buf)
def digest(*parts):
    h = hashlib.sha256()
    for p in parts: h.update(str(p).encode())
    return h.hexdigest()
def nondet():
    return os.environ.get("STUB_NONDET") == "1"
