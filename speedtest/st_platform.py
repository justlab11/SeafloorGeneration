"""OS-specific layer of the speed test: process spawning, /proc reading, RAM/CPU/GPU discovery, ldd/ELF parsing.
Everything that parses text is a pure function (parse_*) so it can be unit-tested against recorded sample output (tests/test_units.py).
Standard library only; psutil is used when importable, /proc otherwise."""
import os
import platform
import re
import shutil
import signal
import struct
import subprocess
import sys
import time

try:
    import psutil  # optional
except Exception:  # noqa
    psutil = None

IS_WIN = os.name == "nt"


# ----------------------------------------------------------------------------- pure parsers (unit-tested)
def parse_meminfo(text):
    """/proc/meminfo -> {key: bytes}."""
    out = {}
    for line in text.splitlines():
        m = re.match(r"^(\w[\w()]*):\s+(\d+)(?:\s*(kB|KB))?", line)
        if m:
            out[m.group(1)] = int(m.group(2)) * (1024 if m.group(3) else 1)
    return out


def meminfo_free_pct(text):
    """available / total in percent from /proc/meminfo text (MemAvailable, else MemFree+Buffers+Cached)."""
    d = parse_meminfo(text)
    total = d.get("MemTotal")
    if not total:
        return None
    avail = d.get("MemAvailable")
    if avail is None:
        avail = d.get("MemFree", 0) + d.get("Buffers", 0) + d.get("Cached", 0)
    return 100.0 * avail / total


def parse_cpuinfo(text):
    """/proc/cpuinfo -> (model name, logical cores)."""
    model, n = None, 0
    for line in text.splitlines():
        if line.startswith("processor"):
            n += 1
        elif line.lower().startswith("model name") and model is None:
            model = line.split(":", 1)[1].strip()
    return model, n


def parse_status_kb(text, key="VmRSS"):
    """/proc/PID/status -> bytes of `key` (VmRSS, VmHWM) or None."""
    m = re.search(r"^%s:\s+(\d+)\s*kB" % re.escape(key), text, re.M)
    return int(m.group(1)) * 1024 if m else None


def parse_stat_ppid(text):
    """/proc/PID/stat -> (pid, ppid); the command name may contain spaces/parentheses."""
    head, _, rest = text.rpartition(")")
    pid = int(head.split("(", 1)[0])
    fields = rest.split()
    return pid, int(fields[1])


def parse_nvidia_smi_csv(text):
    """`nvidia-smi --query-gpu=name,memory.total,driver_version --format=csv,noheader,nounits` -> [dict]."""
    gpus = []
    for line in text.strip().splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) >= 3 and parts[0]:
            try:
                mem = float(parts[1])
            except ValueError:
                mem = None
            gpus.append(dict(name=parts[0], mem_mb=mem, driver=parts[2]))
    return gpus


def parse_ldd(text):
    """ldd output -> (resolved libs, missing libs)."""
    found, missing = [], []
    for line in text.splitlines():
        line = line.strip()
        if "=> not found" in line:
            missing.append(line.split("=>")[0].strip())
        elif "=>" in line:
            found.append(line.split("=>")[0].strip())
    return found, missing


def parse_elf_needed(data):
    """Names of the DT_NEEDED shared libraries of an ELF64 little-endian file given its bytes; None if not such an ELF."""
    try:
        if data[:4] != b"\x7fELF" or data[4] != 2 or data[5] != 1:
            return None
        shoff = struct.unpack_from("<Q", data, 0x28)[0]
        shentsize, shnum = struct.unpack_from("<HH", data, 0x3A)
        secs = []
        for i in range(shnum):
            _n, typ, _fl, _ad, off, size, link, _inf, _al, _es = struct.unpack_from("<IIQQQQIIQQ", data, shoff + i * shentsize)
            secs.append((typ, off, size, link))
        dyn = [s for s in secs if s[0] == 6]
        if not dyn:
            return []
        typ, off, size, link = dyn[0]
        stroff = secs[link][1]
        need = []
        for i in range(0, size, 16):
            tag, val = struct.unpack_from("<qQ", data, off + i)
            if tag == 1:
                s = stroff + val
                need.append(data[s:data.index(b"\0", s)].decode())
            elif tag == 0:
                break
        return need
    except Exception:  # noqa
        return None


def tail_lines(path, n=20):
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - 65536))
            data = f.read().decode("utf-8", "replace")
        return data.splitlines()[-n:]
    except Exception as e:  # noqa
        return ["<could not read %s: %r>" % (path, e)]


# ----------------------------------------------------------------------------- system information
def _read(path):
    with open(path, "r", errors="replace") as f:
        return f.read()


class SysInfo:
    """Real machine information. Tests/selftest subclass it (FakeSys) to script RAM pressure."""

    def total_ram(self):
        if psutil:
            return psutil.virtual_memory().total
        return parse_meminfo(_read("/proc/meminfo")).get("MemTotal")

    def free_pct(self, active=0):
        """available RAM in percent of total. `active` = number of running jobs (unused here, used by fakes)."""
        try:
            if psutil:
                v = psutil.virtual_memory()
                return 100.0 * v.available / v.total
            return meminfo_free_pct(_read("/proc/meminfo"))
        except Exception:  # noqa
            return None

    def cpu_count(self):
        return os.cpu_count() or 1

    def cpu_model(self):
        try:
            if os.path.exists("/proc/cpuinfo"):
                return parse_cpuinfo(_read("/proc/cpuinfo"))[0]
        except Exception:  # noqa
            pass
        return platform.processor() or platform.machine()

    def disk_free(self, path):
        try:
            return shutil.disk_usage(path).free
        except Exception:  # noqa
            return None


def tree_rss(pid):
    """Resident set (bytes) of `pid` plus all its descendants; None if unknown."""
    try:
        if psutil:
            p = psutil.Process(pid)
            total = p.memory_info().rss
            for c in p.children(recursive=True):
                try:
                    total += c.memory_info().rss
                except Exception:  # noqa
                    pass
            return total
        if not os.path.isdir("/proc"):
            return None
        kids = {}
        for d in os.listdir("/proc"):
            if d.isdigit():
                try:
                    cpid, ppid = parse_stat_ppid(_read("/proc/%s/stat" % d))
                    kids.setdefault(ppid, []).append(cpid)
                except Exception:  # noqa
                    pass
        stack, total = [pid], 0
        while stack:
            q = stack.pop()
            stack.extend(kids.get(q, []))
            try:
                total += parse_status_kb(_read("/proc/%d/status" % q), "VmRSS") or 0
            except Exception:  # noqa
                pass
        return total
    except Exception:  # noqa
        return None


def spawn(argv, env, cwd, out_path, err_path):
    """Start argv in its own process group/session with stdout/stderr redirected to files."""
    fo, fe = open(out_path, "wb"), open(err_path, "wb")
    kw = {}
    if IS_WIN:
        kw["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    else:
        kw["start_new_session"] = True
    try:
        p = subprocess.Popen(argv, env=env, cwd=cwd, stdin=subprocess.DEVNULL, stdout=fo, stderr=fe, **kw)
    finally:
        fo.close()
        fe.close()
    return p


def kill_tree(proc):
    try:
        if psutil:
            try:
                ch = psutil.Process(proc.pid).children(recursive=True)
            except Exception:  # noqa
                ch = []
            for c in ch:
                try:
                    c.kill()
                except Exception:  # noqa
                    pass
        elif not IS_WIN:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        proc.kill()
    except Exception:  # noqa
        pass


def run_quick(argv, timeout=30, env=None, cwd=None):
    """Run a short helper command; returns (rc, stdout, stderr) or (None, '', error text)."""
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, env=env, cwd=cwd)
        return r.returncode, r.stdout, r.stderr
    except Exception as e:  # noqa
        return None, "", repr(e)


def gpu_info(nvidia_cmd):
    rc, out, err = run_quick(nvidia_cmd, 20)
    if rc == 0:
        return parse_nvidia_smi_csv(out)
    return []


def host_summary(sysinfo, nvidia_cmd):
    return dict(platform=platform.platform(), python=sys.version.split()[0], cpu_model=sysinfo.cpu_model(), logical_cores=sysinfo.cpu_count(),
                ram_gb=round((sysinfo.total_ram() or 0) / 2 ** 30, 2), gpus=gpu_info(nvidia_cmd), psutil=bool(psutil))


def sleep(s):
    time.sleep(s)
