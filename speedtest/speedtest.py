#!/usr/bin/env python3
"""Speed test for the Infinigen -> heightmap -> MASTODON pipeline. Standard library only (psutil optional).

  python speedtest.py --check                 # verify the setup, run nothing heavy
  python speedtest.py                         # full run (resumable: --resume)
  python speedtest.py --selftest              # full flow against stubs (no Infinigen / MASTODON needed)
  python speedtest.py --list-guesses          # every config value marked GUESS

Every stage is wrapped: a failure logs the command and the last 20 lines of stderr, is stored in the stage JSON, and the run continues.
Per-stage JSON goes to <output_dir>/stages/<stage>.json immediately; the final report is speedtest_report.md/.json in <output_dir>."""
import argparse
import copy
import getpass
import hashlib
import json
import os
import re
import shlex
import shutil
import socket
import statistics
import sys
import tempfile
import time
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import st_mastodon as mm      # noqa: E402
import st_platform as plat    # noqa: E402
import st_validate            # noqa: E402

STAGES = ["hardware", "setup_gin", "infinigen_coarse", "infinigen_fine", "extract", "mastodon_small", "mastodon_prod", "cuda_terrain", "concurrency", "determinism", "e2e"]


# ============================================================================= config
def strip_guesses(node, prefix="", out=None):
    """Remove 'X__GUESS' keys (any depth); returns (clean copy, {dotted key: reason})."""
    out = {} if out is None else out
    if isinstance(node, dict):
        clean = {}
        for k, v in node.items():
            if k.endswith("__GUESS"):
                out[(prefix + "." + k[:-7]).lstrip(".")] = v
            elif k == "_comment":
                continue
            else:
                clean[k] = strip_guesses(v, prefix + "." + k, out)[0]
        return clean, out
    return node, out


def set_dotted(cfg, dotted, text):
    parts = dotted.split(".")
    node = cfg
    for p in parts[:-1]:
        node = node[p]
    try:
        val = json.loads(text)
    except ValueError:
        val = text
    node[parts[-1]] = val


def load_config(path, sets=()):
    with open(path) as f:
        raw = json.load(f)
    cfg, guesses = strip_guesses(raw)
    for s in sets:
        k, _, v = s.partition("=")
        set_dotted(cfg, k, v)
    return cfg, guesses


class Safe(dict):
    def __missing__(self, key):
        raise KeyError("unknown placeholder {%s}" % key)


def expand_env_refs(s, env):
    s = re.sub(r"\$\{?(\w+)\}?", lambda m: env.get(m.group(1), ""), s)
    return ":".join(p for p in s.split(":") if p) if ":" in s and "/" in s else s


def fill_argv(argv, vars_):
    """'{name}' is formatted; an element that is exactly '{*name}' expands a list-valued variable in place."""
    out = []
    for el in argv:
        m = re.fullmatch(r"\{\*(\w+)\}", el)
        if m:
            out.extend(str(x) for x in vars_.get(m.group(1), []))
        else:
            out.append(os.path.expanduser(el.format_map(Safe(vars_))))
    return out


# ============================================================================= context / logging
class Ctx:
    def __init__(self, cfg, guesses, args, sysinfo=None):
        self.cfg, self.guesses, self.args = cfg, guesses, args
        self.sys = sysinfo or plat.SysInfo()
        self.out = os.path.abspath(os.path.expanduser(args.out or cfg["paths"]["output_dir"]))
        for d in ("stages", "logs", "work", "maps"):
            os.makedirs(os.path.join(self.out, d), exist_ok=True)
        self.results = {}
        self.t0 = time.time()
        self.hm_cache = {}
        p = {k: v for k, v in cfg["paths"].items()}
        self.vars = dict(assets=os.path.join(HERE, "assets"), **{k: (os.path.expanduser(v) if isinstance(v, str) else v) for k, v in p.items()})
        for k in ("ctrl_driver", "extract_script"):
            self.vars[k] = self.vars[k].format_map(Safe(self.vars))
        for k in ("mastodon_exe", "python", "mastodon_root", "infinigen_dir", "conda_prefix"):
            v = self.vars.get(k)
            if isinstance(v, str):
                v = os.path.expanduser(v)
                # relative paths would break because every command runs in its own working directory; a bare name (no separator) is left for PATH lookup
                self.vars[k] = os.path.abspath(v) if (os.sep in v or "/" in v) else v
        self.vars.update(gin_stems=cfg["infinigen"]["gin_stems"], extract_args=cfg["infinigen"]["extract_args"], device_args=[])
        self.logf = open(os.path.join(self.out, "speedtest.log"), "a", buffering=1)

    def log(self, msg):
        line = "%s %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg)
        print(line, flush=True)
        self.logf.write(line + "\n")

    def v(self, **extra):
        d = dict(self.vars)
        d.update(extra)
        return d

    def env(self, which, **extra):
        env = dict(os.environ)
        vars_ = self.v(**extra)
        for k, val in self.cfg["env"].get(which, {}).items():
            if val is None:
                env.pop(k, None)
            else:
                env[k] = expand_env_refs(os.path.expanduser(str(val).format_map(Safe(vars_))), env)
        return env

    def free_disk_gb(self):
        b = self.sys.disk_free(self.out)
        return None if b is None else b / 2 ** 30


# ============================================================================= process runner
def run_jobs(ctx, jobs, abort_free_pct=None):
    """Run jobs (dicts: name, argv, env, cwd, logdir, timeout) concurrently, sampling RSS of each process tree and system free RAM.
    Returns (list of result dicts in job order, group dict)."""
    dt = ctx.cfg["limits"]["sample_interval_s"]
    live, results = [], []
    t_start = time.time()
    for j in jobs:
        os.makedirs(j["logdir"], exist_ok=True)
        fo, fe = [os.path.join(j["logdir"], j["name"] + e) for e in (".out.log", ".err.log")]
        rec = dict(name=j["name"], cmd=" ".join(shlex.quote(a) for a in j["argv"]), cwd=j["cwd"], stdout_log=fo, stderr_log=fe, rc=None, timeout=False,
                   start_error=None, peak_rss_mb=0.0, wall_s=0.0, started=False)
        try:
            p = plat.spawn(j["argv"], j["env"], j["cwd"], fo, fe)
            rec["wall_s"] = None
            rec["started"] = True
            live.append(dict(job=j, rec=rec, p=p, t0=time.time()))
        except Exception as e:  # noqa
            rec["start_error"] = repr(e)
        results.append(rec)
    group = dict(min_free_pct=None, aborted=False, peak_total_rss_mb=0.0, n_started=len(live))
    time.sleep(min(dt, 0.1))
    while any(l["p"].poll() is None for l in live):
        running = [l for l in live if l["p"].poll() is None]
        free = ctx.sys.free_pct(len(running))
        if free is not None:
            group["min_free_pct"] = free if group["min_free_pct"] is None else min(group["min_free_pct"], free)
        tot = 0
        for l in running:
            rss = plat.tree_rss(l["p"].pid)
            if rss:
                l["rec"]["peak_rss_mb"] = max(l["rec"]["peak_rss_mb"], rss / 2 ** 20)
                tot += rss
            if time.time() - l["t0"] > l["job"]["timeout"]:
                l["rec"]["timeout"] = True
                plat.kill_tree(l["p"])
        group["peak_total_rss_mb"] = max(group["peak_total_rss_mb"], tot / 2 ** 20)
        if abort_free_pct is not None and free is not None and free < abort_free_pct:
            group["aborted"] = True
            for l in running:
                plat.kill_tree(l["p"])
            ctx.log("  ABORT: free RAM %.1f%% < %.1f%%, killing %d jobs" % (free, abort_free_pct, len(running)))
        for l in live:
            if l["rec"]["wall_s"] is None and l["p"].poll() is not None:
                l["rec"]["wall_s"] = time.time() - l["t0"]
        time.sleep(dt)
    for l in live:
        l["p"].wait()
        l["rec"]["rc"] = l["p"].returncode
        if l["rec"]["wall_s"] is None:
            l["rec"]["wall_s"] = time.time() - l["t0"]
    group["wall_s"] = time.time() - t_start
    for r in results:
        ok = r["rc"] == 0 and not r["timeout"] and not r["start_error"]
        r["ok"] = ok
        if not ok:
            r["stderr_tail"] = ["COULD NOT START: %s" % r["start_error"], "check that the executable exists (absolute path?), has the exec bit (chmod +x) and is the right architecture"] if r["start_error"] else plat.tail_lines(r["stderr_log"], 20)
            if not r["start_error"] and not any(x.strip() for x in r["stderr_tail"]):
                r["stdout_tail"] = plat.tail_lines(r["stdout_log"], 20)
            ctx.log("  FAILED %s rc=%s timeout=%s\n    cmd: %s\n    last stderr lines:\n      %s" % (r["name"], r["rc"], r["timeout"], r["cmd"], "\n      ".join(r["stderr_tail"])))
    return results, group


def run_one(ctx, name, argv, env, cwd, logdir, timeout, abort_free_pct=None):
    res, grp = run_jobs(ctx, [dict(name=name, argv=argv, env=env, cwd=cwd, logdir=logdir, timeout=timeout)], abort_free_pct)
    r = res[0]
    r["min_free_pct"] = grp["min_free_pct"]
    return r


# ============================================================================= Infinigen stages
def gin_install(ctx):
    ic = ctx.cfg["infinigen"]
    if not ic["install_gin"]:
        return dict(status="skipped", reason="install_gin=false")
    dst = ic["gin_install_dir"].format_map(Safe(ctx.vars))
    dst = os.path.expanduser(dst)
    src = os.path.join(HERE, "assets", "gin")
    copied = []
    os.makedirs(dst, exist_ok=True)
    for f in sorted(os.listdir(src)):
        if f.endswith(".gin"):
            shutil.copy2(os.path.join(src, f), os.path.join(dst, f))
            copied.append(f)
    return dict(status="ok", dest=dst, copied=copied)


def ig_job(ctx, which, wd, seed, device_args=(), tag=""):
    ic = ctx.cfg["infinigen"]
    v = ctx.v(outdir=wd, seed=seed, device_args=list(device_args))
    os.makedirs(os.path.join(wd, "logs"), exist_ok=True)
    return dict(name=which + tag, argv=fill_argv(ic[which + "_cmd"], v), env=ctx.env("infinigen", outdir=wd, seed=seed), cwd=os.path.expanduser(ic["cwd"].format_map(Safe(v))),
                logdir=os.path.join(wd, "logs"), timeout=ic["timeout_s"])


def timed(ctx, job, label):
    ctx.log("run %s: %s" % (label, " ".join(job["argv"])[:300]))
    r = run_one(ctx, job["name"], job["argv"], job["env"], job["cwd"], job["logdir"], job["timeout"])
    ctx.log("  -> rc=%s wall %.1fs peak RSS %.0f MB" % (r["rc"], r["wall_s"], r["peak_rss_mb"]))
    return r


def short(r):
    keys = ("name", "ok", "rc", "wall_s", "peak_rss_mb", "min_free_pct", "timeout", "cmd", "stderr_tail", "stdout_tail", "start_error")
    return {k: r[k] for k in keys if k in r}


def dir_size(path):
    tot = 0
    for root, _d, files in os.walk(path):
        for f in files:
            try:
                tot += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return tot


def st_infinigen_coarse(ctx, device_args=(), wd=None, seed=None, tag=""):
    seed = ctx.cfg["infinigen"]["seed"] if seed is None else seed
    wd = wd or os.path.join(ctx.out, "work", "scene_main")
    shutil.rmtree(os.path.join(wd, "coarse"), ignore_errors=True)
    r = timed(ctx, ig_job(ctx, "coarse", wd, seed, device_args, tag), "infinigen coarse seed=%s" % seed)
    ok = r["ok"] and os.path.exists(os.path.join(wd, "coarse", "scene.blend"))
    if r["ok"] and not ok:
        r["note"] = "exit 0 but coarse/scene.blend missing"
    return dict(status="ok" if ok else "failed", wd=wd, seed=seed, run=short(r), coarse_dir_mb=round(dir_size(os.path.join(wd, "coarse")) / 2 ** 20, 1))


def st_infinigen_fine(ctx, device_args=(), wd=None, seed=None, tag=""):
    seed = ctx.cfg["infinigen"]["seed"] if seed is None else seed
    wd = wd or os.path.join(ctx.out, "work", "scene_main")
    if not os.path.exists(os.path.join(wd, "coarse", "scene.blend")):
        return dict(status="skipped", reason="no coarse scene (stage infinigen_coarse failed or was skipped)")
    shutil.rmtree(os.path.join(wd, "fine"), ignore_errors=True)
    r = timed(ctx, ig_job(ctx, "fine", wd, seed, device_args, tag), "infinigen populate+fine_terrain seed=%s" % seed)
    ok = r["ok"] and os.path.exists(os.path.join(wd, "fine", "scene.blend"))
    if r["ok"] and not ok:
        r["note"] = "exit 0 but fine/scene.blend missing"
    return dict(status="ok" if ok else "failed", wd=wd, seed=seed, run=short(r), fine_dir_mb=round(dir_size(os.path.join(wd, "fine")) / 2 ** 20, 1))


def st_extract(ctx, wd=None, seed=None, tag=""):
    seed = ctx.cfg["infinigen"]["seed"] if seed is None else seed
    wd = wd or os.path.join(ctx.out, "work", "scene_main")
    if not os.path.exists(os.path.join(wd, "fine", "scene.blend")):
        return dict(status="skipped", reason="no fine scene (stage infinigen_fine failed or was skipped)")
    shutil.rmtree(os.path.join(wd, "patches"), ignore_errors=True)
    r = timed(ctx, ig_job(ctx, "extract", wd, seed, (), tag), "heightmap extraction")
    pdir = os.path.join(wd, "patches")
    patches = sorted(d for d in os.listdir(pdir) if d.startswith("patch_")) if os.path.isdir(pdir) else []
    hm0 = os.path.join(pdir, "patch_00", "heightmap_patch.txt")
    ok = r["ok"] and os.path.exists(hm0)
    return dict(status="ok" if ok else "failed", wd=wd, seed=seed, run=short(r), n_patches=len(patches), patch0=hm0 if ok else None, patch0_sha256=sha256_file(hm0) if ok else None)


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# ============================================================================= MASTODON stages
def get_heightmap(ctx):
    """patch dict + source label: extracted patch_00 > reference heightmap (config/CLI, if not a .json) > synthetic."""
    if "patch" in ctx.hm_cache:
        return ctx.hm_cache["patch"]
    ex = ctx.results.get("extract", {})
    cands = []
    if ex.get("status") == "ok" and ex.get("patch0"):
        cands.append(("extracted:" + ex["patch0"], ex["patch0"]))
    ref = ctx.args.reference or ctx.cfg["paths"].get("reference_heightmap")
    if ref and not str(ref).lower().endswith(".json"):
        cands.append(("reference:" + str(ref), os.path.expanduser(ref)))
    for label, path in cands:
        try:
            ctx.hm_cache["patch"] = (mm.read_hm(path), label)
            return ctx.hm_cache["patch"]
        except Exception as e:  # noqa
            ctx.log("  could not read heightmap %s: %r" % (path, e))
    ctx.hm_cache["patch"] = (mm.synthetic_patch(), "synthetic (no Infinigen patch / reference available)")
    return ctx.hm_cache["patch"]


def map_for(ctx, profile_name):
    """Build (once) the padded map file of a profile; returns dict(path, ext, dx, stats)."""
    if profile_name in ctx.hm_cache:
        return ctx.hm_cache[profile_name]
    prof = ctx.cfg["mastodon"]["profiles"][profile_name]
    patch, label = get_heightmap(ctx)
    m = prof["map"]
    ext = (m["xmin"], m["xmax"], m["ymin"], m["ymax"])
    t = time.time()
    hm, top = mm.make_map(patch, ext, ctx.cfg["mastodon"]["patch_center_x"])
    path = os.path.join(ctx.out, "maps", "%s_%s.txt" % (profile_name, hashlib.sha1(label.encode()).hexdigest()[:6]))
    mm.write_hm(path, hm)
    info = dict(path=path, ext=ext, dx=hm["dx"], stats=mm.hm_stats(hm), source=label, relief_max=top, build_s=round(time.time() - t, 2), shape=[hm["rows"], hm["cols"]])
    ctx.hm_cache[profile_name] = info
    return info


def mastodon_prepare(ctx, profile_name, wd, threads=None, tag=""):
    prof = ctx.cfg["mastodon"]["profiles"][profile_name]
    mp = map_for(ctx, profile_name)
    os.makedirs(wd, exist_ok=True)
    cfg = mm.build_profile_cfg(prof, mp["path"], mp["ext"], mp["dx"], threads or prof["threads"], ctx.cfg["mastodon"]["extra_overrides"])
    probs = st_validate.validate(cfg, mp["stats"])
    errors = [p for p in probs if p[0] == "ERROR"]
    cfgp = os.path.join(wd, "CONFIG.cfg")
    with open(cfgp, "w") as f:
        f.write(mm.dumps(cfg))
    prep = dict(wd=wd, cfgp=cfgp, validation=dict(errors=errors, warnings=[p for p in probs if p[0] == "WARN"]), ok_to_run=(not errors) or ctx.cfg["mastodon"]["allow_validation_errors"],
                map=dict(source=mp["source"], shape=mp["shape"], relief_max=mp["relief_max"]), threads=threads or prof["threads"])
    mc = ctx.cfg["mastodon"]
    v = ctx.v(config=cfgp, outdir=wd)
    prep["job"] = dict(name="mastodon_" + profile_name + tag, argv=fill_argv(mc["cmd"], v), env=ctx.env("mastodon"), cwd=wd, logdir=os.path.join(wd, "logs"), timeout=mc["timeout_s"])
    return prep


def mastodon_finish(prep, res, keep_beamformed=False):
    """Post-run: delete StaveData.h5 (always), inspect log/output, hash Beamformed.h5."""
    wd = prep["wd"]
    out = dict(run=short(res), validation_errors=prep["validation"]["errors"], validation_warnings=len(prep["validation"]["warnings"]), map=prep["map"], threads=prep["threads"])
    sd = os.path.join(wd, "StaveData.h5")
    out["stave_mb"] = round(os.path.getsize(sd) / 2 ** 20, 1) if os.path.exists(sd) else None
    for f in (sd,):
        try:
            os.remove(f)
        except OSError:
            pass
    bf = os.path.join(wd, "Beamformed.h5")
    out["beamformed_mb"] = round(os.path.getsize(bf) / 2 ** 20, 2) if os.path.exists(bf) else None
    out["beamformed_sha256"] = sha256_file(bf) if os.path.exists(bf) else None
    lg = os.path.join(wd, "MASTLogFile.txt")
    errs = []
    if os.path.exists(lg):
        errs = [l.strip() for l in plat.tail_lines(lg, 400) if "Error!" in l][:5]
    out["log_errors"] = errs
    out["status"] = "ok" if (res["ok"] and bf and os.path.exists(bf) and not errs) else "failed"
    if out["status"] == "failed" and res["ok"]:
        out["note"] = "exit 0 but %s" % ("log shows errors" if errs else "no Beamformed.h5 (MASTODON exits 0 on silent failures, see validator rules)")
    if not keep_beamformed:
        try:
            os.remove(bf)
        except OSError:
            pass
    return out


def run_mastodon(ctx, profile, wd, threads=None, tag="", keep_beamformed=False):
    prep = mastodon_prepare(ctx, profile, wd, threads, tag)
    if not prep["ok_to_run"]:
        return dict(status="failed", note="validation errors, not run", validation_errors=prep["validation"]["errors"])
    ctx.log("run mastodon %s (%s pings) in %s" % (profile, ctx.cfg["mastodon"]["profiles"][profile]["pings"], wd))
    res = run_one(ctx, prep["job"]["name"], prep["job"]["argv"], prep["job"]["env"], prep["job"]["cwd"], prep["job"]["logdir"], prep["job"]["timeout"])
    out = mastodon_finish(prep, res, keep_beamformed)
    ctx.log("  -> %s rc=%s wall %.1fs peak RSS %.0f MB" % (out["status"], res["rc"], res["wall_s"], res["peak_rss_mb"]))
    return out


def st_mastodon_small(ctx):
    runs = []
    for i in range(ctx.cfg["mastodon"]["small_repeats"]):
        runs.append(run_mastodon(ctx, "small", os.path.join(ctx.out, "work", "mastodon_small_%d" % i), tag="_%d" % i))
    walls = [r["run"]["wall_s"] for r in runs if r.get("status") == "ok"]
    return dict(status="ok" if walls and len(walls) == len(runs) else "failed", runs=runs, wall_mean_s=statistics.mean(walls) if walls else None,
                wall_min_s=min(walls) if walls else None, peak_rss_mb=max([r["run"]["peak_rss_mb"] for r in runs if "run" in r] or [0]), heightmap_source=map_for(ctx, "small")["source"])


def st_mastodon_prod(ctx):
    r = run_mastodon(ctx, "prod", os.path.join(ctx.out, "work", "mastodon_prod"))
    r["heightmap_source"] = map_for(ctx, "prod")["source"]
    r["wall_s"] = r.get("run", {}).get("wall_s")
    r["peak_rss_mb"] = r.get("run", {}).get("peak_rss_mb")
    return r


# ============================================================================= CUDA terrain
def st_cuda(ctx):
    if not ctx.cfg["cuda_terrain"]["enabled"]:
        return dict(status="skipped", reason="disabled in config")
    gpus = ctx.results.get("hardware", {}).get("host", {}).get("gpus") or plat.gpu_info(ctx.cfg["paths"]["nvidia_smi"])
    if not gpus:
        return dict(status="skipped", reason="no GPU visible (nvidia-smi returned nothing)")
    dev = ctx.cfg["infinigen"]["cuda_device_args"]
    wd = os.path.join(ctx.out, "work", "scene_cuda")
    res = dict(status="ok", gpus=gpus, device_args=dev)
    c = st_infinigen_coarse(ctx, dev, wd, tag="_cuda")
    res["coarse"] = c
    f = st_infinigen_fine(ctx, dev, wd, tag="_cuda") if c["status"] == "ok" else dict(status="skipped", reason="cuda coarse failed")
    res["fine"] = f
    cpu_c, cpu_f = ctx.results.get("infinigen_coarse", {}), ctx.results.get("infinigen_fine", {})
    if cpu_c.get("status") != "ok" or cpu_f.get("status") != "ok":     # no CPU baseline available: measure it now with the same seed
        wd2 = os.path.join(ctx.out, "work", "scene_cpu_ref")
        cpu_c = st_infinigen_coarse(ctx, (), wd2, tag="_cpuref")
        cpu_f = st_infinigen_fine(ctx, (), wd2, tag="_cpuref") if cpu_c["status"] == "ok" else cpu_f
        res["cpu_baseline_measured_here"] = True
    w = lambda d: d.get("run", {}).get("wall_s") if d.get("status") == "ok" else None
    res.update(cpu_coarse_s=w(cpu_c), cpu_fine_s=w(cpu_f), cuda_coarse_s=w(c), cuda_fine_s=w(f))
    if res["cpu_coarse_s"] and res["cuda_coarse_s"] and res["cpu_fine_s"] and res["cuda_fine_s"]:
        res["speedup_total"] = (res["cpu_coarse_s"] + res["cpu_fine_s"]) / (res["cuda_coarse_s"] + res["cuda_fine_s"])
    else:
        res["status"] = "failed" if c["status"] != "ok" or f["status"] != "ok" else "partial"
    if not ctx.cfg["limits"]["keep_work"]:
        shutil.rmtree(wd, ignore_errors=True)
    return res


# ============================================================================= concurrency scaling
def st_concurrency(ctx):
    cc = ctx.cfg["concurrency"]
    prof = cc["workload"]
    floor = cc["min_free_ram_pct"]
    cores = ctx.sys.cpu_count()
    single = ctx.results.get("mastodon_prod", {}) if prof == "prod" else ctx.results.get("mastodon_small", {})
    peak1 = (single.get("peak_rss_mb") or 0) * 2 ** 20
    total = ctx.sys.total_ram() or 0
    levels, stopped = [], None
    base_wall = None
    for n in cc["levels"]:
        free_now = ctx.sys.free_pct(0)
        pred = (100.0 * (peak1 * n) / total) if (peak1 and total) else 0
        if free_now is not None and peak1 and free_now - pred < floor:
            stopped = "predicted free RAM %.1f%% after %d jobs (%.0f MB each) < %d%%" % (free_now - pred, n, peak1 / 2 ** 20, floor)
            ctx.log("  concurrency: stop before level %d: %s" % (n, stopped))
            levels.append(dict(n=n, skipped=True, reason=stopped))
            break
        threads = max(1, cores // n) if cc["threads_policy"] == "divide" else None
        wd = os.path.join(ctx.out, "work", "conc_%d" % n)
        preps = [mastodon_prepare(ctx, prof, os.path.join(wd, "j%d" % i), threads, tag="_c%d_%d" % (n, i)) for i in range(n)]
        if any(not p["ok_to_run"] for p in preps):
            levels.append(dict(n=n, skipped=True, reason="validation errors"))
            break
        ctx.log("concurrency level %d (threads/job %s)" % (n, threads or "profile"))
        t = time.time()
        res, grp = run_jobs(ctx, [p["job"] for p in preps], abort_free_pct=floor)
        outs = [mastodon_finish(p, r) for p, r in zip(preps, res)]
        wall = grp["wall_s"]
        okn = sum(1 for o in outs if o["status"] == "ok")
        lv = dict(n=n, threads_per_job=threads, wall_s=wall, jobs_ok=okn, jobs_per_hour=3600.0 * okn / wall if wall else None, per_job_wall_mean_s=statistics.mean([r["wall_s"] for r in res]),
                  peak_total_rss_mb=grp["peak_total_rss_mb"], min_free_pct=grp["min_free_pct"], aborted_low_ram=grp["aborted"], failed=[short(r) for r in res if not r["ok"]][:2])
        if n == cc["levels"][0] or base_wall is None:
            base_wall = wall if okn == n else base_wall
        if base_wall and okn == n and wall:
            lv["speedup_vs_first_level"] = (base_wall * n / cc["levels"][0]) / wall if cc["levels"][0] else None
        levels.append(lv)
        shutil.rmtree(wd, ignore_errors=True)
        if grp["aborted"]:
            stopped = "free RAM fell below %d%% during level %d (jobs killed)" % (floor, n)
            break
    best = max([l for l in levels if l.get("jobs_per_hour") and not l.get("aborted_low_ram")], key=lambda l: l["jobs_per_hour"], default=None)
    return dict(status="ok" if levels and any(l.get("jobs_ok") for l in levels) else "failed", workload=prof, levels=levels, stopped=stopped, best_level=best["n"] if best else None,
                cores=cores, single_peak_rss_mb=peak1 / 2 ** 20 if peak1 else None)


# ============================================================================= determinism
def compare_files(a, b):
    """-> dict(identical, size_a, size_b, differing_byte_fraction)"""
    sa, sb = os.path.getsize(a), os.path.getsize(b)
    if sha256_file(a) == sha256_file(b):
        return dict(identical=True, size_a=sa, size_b=sb, differing_byte_fraction=0.0)
    diff = 0
    with open(a, "rb") as fa, open(b, "rb") as fb:
        while True:
            x, y = fa.read(1 << 20), fb.read(1 << 20)
            if not x and not y:
                break
            diff += sum(1 for p, q in zip(x, y) if p != q) + abs(len(x) - len(y))
    return dict(identical=False, size_a=sa, size_b=sb, differing_byte_fraction=diff / max(sa, sb, 1))


def compare_heightmaps(pa, pb, tol):
    a, b = mm.read_hm(pa), mm.read_hm(pb)
    if (a["cols"], a["rows"]) != (b["cols"], b["rows"]):
        return dict(identical=False, note="shape differs %sx%s vs %sx%s" % (a["cols"], a["rows"], b["cols"], b["rows"]))
    mx = max(abs(x - y) for ra, rb in zip(a["data"], b["data"]) for x, y in zip(ra, rb))
    return dict(identical=mx == 0, max_abs_diff=mx, within_tol=mx <= tol, tol=tol)


def st_determinism(ctx):
    dc = ctx.cfg["determinism"]
    res = dict(status="ok", hashes={})
    if dc["mastodon"]:
        a = run_mastodon(ctx, "small", os.path.join(ctx.out, "work", "det_mast_a"), tag="_deta", keep_beamformed=True)
        b = run_mastodon(ctx, "small", os.path.join(ctx.out, "work", "det_mast_b"), tag="_detb", keep_beamformed=True)
        if a.get("status") == "ok" and b.get("status") == "ok":
            res["mastodon"] = compare_files(os.path.join(ctx.out, "work", "det_mast_a", "Beamformed.h5"), os.path.join(ctx.out, "work", "det_mast_b", "Beamformed.h5"))
            res["hashes"]["mastodon_beamformed_sha256"] = a["beamformed_sha256"]
        else:
            res["mastodon"] = dict(status="failed", note="a MASTODON run failed", a=a.get("status"), b=b.get("status"))
            res["status"] = "partial"
        for d in ("det_mast_a", "det_mast_b"):
            shutil.rmtree(os.path.join(ctx.out, "work", d), ignore_errors=True)
    ex = ctx.results.get("extract", {})
    if dc["infinigen"]:
        if ex.get("status") != "ok":
            res["infinigen"] = dict(status="skipped", reason="stage extract not ok (no first run to compare against)")
            res["status"] = "partial"
        else:
            wd = os.path.join(ctx.out, "work", "scene_det")
            seed = ex["seed"]
            c = st_infinigen_coarse(ctx, (), wd, seed, "_det")
            f = st_infinigen_fine(ctx, (), wd, seed, "_det") if c["status"] == "ok" else dict(status="skipped")
            e = st_extract(ctx, wd, seed, "_det") if f["status"] == "ok" else dict(status="skipped")
            if e["status"] == "ok":
                res["infinigen"] = dict(same_seed=seed, patch0_sha256_a=ex["patch0_sha256"], patch0_sha256_b=e["patch0_sha256"],
                                        **compare_heightmaps(ex["patch0"], e["patch0"], dc["float_tolerance"]))
                res["hashes"]["infinigen_patch0_sha256"] = ex["patch0_sha256"]
            else:
                res["infinigen"] = dict(status="failed", note="second Infinigen run failed", coarse=c["status"], fine=f["status"], extract=e["status"])
                res["status"] = "partial"
            if not ctx.cfg["limits"]["keep_work"]:
                shutil.rmtree(wd, ignore_errors=True)
    ref = ctx.args.reference or ctx.cfg["paths"].get("reference_heightmap")
    if ref:
        ref = os.path.expanduser(ref)
        try:
            if ref.lower().endswith(".json"):
                with open(ref) as f:
                    other = json.load(f)
                oh = (other.get("stages", {}).get("determinism", {}) or {}).get("hashes", {})
                res["reference"] = dict(file=ref, compared={k: ("match" if oh.get(k) == v else ("missing in reference" if k not in oh else "MISMATCH")) for k, v in res["hashes"].items()})
            elif ex.get("status") == "ok":
                res["reference"] = dict(file=ref, **compare_heightmaps(ref, ex["patch0"], dc["float_tolerance"]))
            else:
                res["reference"] = dict(file=ref, note="no extracted patch to compare")
        except Exception as e:  # noqa
            res["reference"] = dict(file=ref, error=repr(e))
    if "mastodon" not in res and "infinigen" not in res:
        res["status"] = "skipped"
    return res


# ============================================================================= end-to-end
def one_eval(ctx, i, par):
    ec = ctx.cfg["e2e"]
    seed = ctx.cfg["infinigen"]["seed"] + ec["seed_offset"] + i
    wd = os.path.join(ctx.out, "work", "e2e_%d" % i)
    shutil.rmtree(wd, ignore_errors=True)
    rec = dict(i=i, seed=seed, stages={}, ok=False)
    t0 = time.time()
    c = st_infinigen_coarse(ctx, (), wd, seed, "_e%d" % i)
    rec["stages"]["coarse_s"] = c.get("run", {}).get("wall_s")
    f = st_infinigen_fine(ctx, (), wd, seed, "_e%d" % i) if c["status"] == "ok" else dict(status="skipped")
    rec["stages"]["fine_s"] = f.get("run", {}).get("wall_s")
    e = st_extract(ctx, wd, seed, "_e%d" % i) if f["status"] == "ok" else dict(status="skipped")
    rec["stages"]["extract_s"] = e.get("run", {}).get("wall_s")
    if e["status"] != "ok":
        rec["failed_at"] = [n for n, d in (("coarse", c), ("fine", f), ("extract", e)) if d["status"] != "ok"][0]
        rec["failure"] = [d for d in (c, f, e) if d["status"] == "failed"][0:1]
        rec["wall_s"] = time.time() - t0
        return rec
    patch = mm.read_hm(e["patch0"])
    ctx.hm_cache.pop("patch", None)
    ctx.hm_cache["patch"] = (patch, "e2e:%s" % e["patch0"])
    for k in [k for k in ctx.hm_cache if k not in ("patch",)]:
        ctx.hm_cache.pop(k)
    n = ec["mastodon_runs"]
    preps = [mastodon_prepare(ctx, ec["profile"], os.path.join(wd, "m%d" % j), None, "_e%d_%d" % (i, j)) for j in range(n)]
    t1 = time.time()
    outs = []
    for k in range(0, n, par):
        batch = preps[k:k + par]
        res, _g = run_jobs(ctx, [p["job"] for p in batch], abort_free_pct=ctx.cfg["concurrency"]["min_free_ram_pct"])
        outs += [mastodon_finish(p, r) for p, r in zip(batch, res)]
    rec["stages"]["mastodon_total_s"] = time.time() - t1
    rec["mastodon_ok"] = sum(1 for o in outs if o["status"] == "ok")
    rec["mastodon_failed"] = [o for o in outs if o["status"] != "ok"][:1]
    rec["ok"] = rec["mastodon_ok"] == n
    rec["wall_s"] = time.time() - t0
    ctx.hm_cache.clear()
    if not ctx.cfg["limits"]["keep_work"]:
        shutil.rmtree(wd, ignore_errors=True)
    return rec


def st_e2e(ctx, prior=None):
    ec = ctx.cfg["e2e"]
    reps = ctx.args.repeats or ec["repeats"]
    par = ec["parallel"]
    if par == "auto":
        par = ctx.results.get("concurrency", {}).get("best_level") or 1
    par = max(1, int(par))
    evals = list((prior or {}).get("evals", []))
    path = os.path.join(ctx.out, "stages", "e2e.json")
    for i in range(len(evals), reps):
        ctx.log("e2e evaluation %d/%d (mastodon parallel %d)" % (i + 1, reps, par))
        evals.append(one_eval(ctx, i, par))
        write_json(path, dict(status="partial", evals=evals, parallel=par, repeats_requested=reps))
    good = [e for e in evals if e["ok"]]
    walls = [e["wall_s"] for e in good]
    out = dict(status="ok" if len(good) == reps else ("partial" if good else "failed"), evals=evals, parallel=par, repeats_requested=reps, n_ok=len(good),
               wall_mean_s=statistics.mean(walls) if walls else None, wall_median_s=statistics.median(walls) if walls else None,
               wall_stdev_s=statistics.stdev(walls) if len(walls) > 1 else 0.0,
               evals_per_hour=3600.0 / statistics.mean(walls) if walls else None,
               definition="one evaluation = Infinigen coarse + populate/fine_terrain + heightmap extraction (9 patches) + %d MASTODON '%s' runs (%d at a time), distinct seed each" % (ec["mastodon_runs"], ec["profile"], par))
    return out


# ============================================================================= hardware / checks
def st_hardware(ctx):
    host = plat.host_summary(ctx.sys, fill_argv(ctx.cfg["paths"]["nvidia_smi"], ctx.v()))
    host["free_ram_pct_now"] = ctx.sys.free_pct(0)
    host["free_disk_gb"] = ctx.free_disk_gb()
    return dict(status="ok", host=host, python_exe=sys.executable)


def check_items(ctx):
    items = []
    add = lambda name, status, detail="": items.append((name, status, detail))
    c = ctx.cfg
    v = ctx.vars
    add("python >= 3.6", "PASS" if sys.version_info >= (3, 6) else "FAIL", sys.version.split()[0])
    add("psutil importable", "PASS" if plat.psutil else "WARN", "yes" if plat.psutil else "no: /proc will be read instead (Linux only)")
    for imp in ("json", "hashlib", "subprocess", "shutil"):
        try:
            __import__(imp)
            add("import " + imp, "PASS")
        except Exception as e:  # noqa
            add("import " + imp, "FAIL", repr(e))
    for f in ("assets/tools/ctrl_driver.py", "assets/tools/I_controlled_scene/extract_patch.py", "assets/tools/overnight/fast_extract.py", "assets/tools/02_extract_heightmap.py", "assets/tools/hm_io.py",
              "assets/gin/ctrlL_base.gin", "assets/gin/ctrlL_cam_wide.gin", "assets/gin/ctrlL_cfg_speedtest.gin"):
        add("asset " + f, "PASS" if os.path.exists(os.path.join(HERE, f)) else "FAIL")
    # output dir
    try:
        p = os.path.join(ctx.out, ".write_test")
        with open(p, "w") as f:
            f.write("x")
        os.remove(p)
        add("output dir writable", "PASS", ctx.out)
    except Exception as e:  # noqa
        add("output dir writable", "FAIL", repr(e))
    free = ctx.free_disk_gb()
    need = c["limits"]["min_free_disk_gb"]
    add("disk free >= %d GB" % need, "PASS" if free is not None and free >= need else "FAIL", "%.1f GB free" % free if free is not None else "unknown")
    # MASTODON
    exe = os.path.expanduser(v["mastodon_exe"])
    ok_exe = os.path.isfile(exe)
    add("MASTODON executable exists", "PASS" if ok_exe else "FAIL", exe)
    if ok_exe:
        with open(exe, "rb") as f:
            data = f.read(1 << 20)
        need_libs = plat.parse_elf_needed(open(exe, "rb").read()) if os.path.getsize(exe) < 200 * 2 ** 20 else None
        add("MASTODON is an ELF x86-64", "PASS" if data[:4] == b"\x7fELF" else ("WARN" if plat.IS_WIN else "FAIL"), "magic %r" % data[:4])
        if not plat.IS_WIN:
            add("MASTODON executable bit", "PASS" if os.access(exe, os.X_OK) else "FAIL", "" if os.access(exe, os.X_OK) else "run: chmod +x %s" % exe)
        env = ctx.env("mastodon")
        if shutil.which("ldd") and need_libs is not None and not plat.IS_WIN:
            rc, out, err = plat.run_quick(["ldd", exe], 30, env)
            found, missing = plat.parse_ldd(out)
            add("MASTODON shared libraries (ldd)", "PASS" if rc == 0 and not missing else "FAIL", "missing: %s" % ", ".join(missing) if missing else "%d resolved" % len(found))
        else:
            import ctypes.util
            miss = [l for l in (need_libs or []) if not l.startswith("ld-linux") and not ctypes.util.find_library(l.split(".so")[0].replace("lib", "", 1))]
            add("MASTODON shared libraries", "WARN", "ldd not available; DT_NEEDED=%s; not found by find_library: %s" % (need_libs, miss))
    add("MASTODON_FOLDER exists", "PASS" if os.path.isdir(os.path.expanduser(v["mastodon_root"])) else "FAIL", v["mastodon_root"])
    add("MASTODON systems/ in root", "PASS" if os.path.isdir(os.path.join(os.path.expanduser(v["mastodon_root"]), "systems")) else "WARN", "expected at <root>/systems")
    bp = c["env"]["mastodon"].get("BELLHOP_PATH")
    add("BELLHOP_PATH", "PASS" if not bp or os.path.isdir(os.path.expanduser(str(bp))) else "WARN", "unset (Geometric model needs none)" if not bp else str(bp))
    add("X display (binary links libX11/GLX)", "WARN" if not os.environ.get("DISPLAY") and not plat.IS_WIN else "PASS", "DISPLAY=%r; if MASTODON fails on a headless box set paths.mastodon_wrapper to xvfb-run" % os.environ.get("DISPLAY"))
    # Infinigen
    ig = ctx.vars["infinigen_dir"]
    add("Infinigen checkout", "PASS" if os.path.isdir(os.path.join(ig, "src", "infinigen_examples")) else "FAIL", ig)
    py = os.path.expanduser(v["python"])
    add("Infinigen python exists", "PASS" if os.path.exists(py) else "FAIL", py)
    gd = os.path.expanduser(c["infinigen"]["gin_install_dir"].format_map(Safe(v)))
    add("gin dir exists / writable", "PASS" if os.path.isdir(gd) and os.access(gd, os.W_OK) else "FAIL", gd)
    if os.path.exists(py):
        rc, out, err = plat.run_quick([py, "-c", "import bpy, gin, numpy; import infinigen; print('imports ok')"], 240, ctx.env("infinigen", outdir=ctx.out), ig)
        add("Infinigen imports (bpy gin infinigen)", "PASS" if rc == 0 else "FAIL", (out.strip() or err.strip().splitlines()[-1:] or [""])[-1] if rc == 0 else " | ".join(err.strip().splitlines()[-3:]))
    # GPU
    gpus = plat.gpu_info(fill_argv(c["paths"]["nvidia_smi"], ctx.v()))
    add("GPU visible (nvidia-smi)", "PASS" if gpus else "WARN", "; ".join("%s %s MB" % (g["name"], g["mem_mb"]) for g in gpus) if gpus else "none: CUDA terrain stage will be skipped")
    ram = ctx.sys.total_ram()
    add("RAM / cores", "PASS", "%.1f GB, %d logical cores" % ((ram or 0) / 2 ** 30, ctx.sys.cpu_count()))
    return items


def print_check(items):
    w = max(len(i[0]) for i in items)
    for n, s, d in items:
        print("%-5s %-*s  %s" % (s, w, n, d))
    nf = sum(1 for i in items if i[1] == "FAIL")
    print("-- %d checks, %d FAIL, %d WARN" % (len(items), nf, sum(1 for i in items if i[1] == "WARN")))
    return nf


# ============================================================================= runner, report
def write_json(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f, indent=1, default=str)
    os.replace(tmp, path)


def run_stage(ctx, name, fn, only, skip, resume):
    path = os.path.join(ctx.out, "stages", name + ".json")
    if only and name not in only or name in skip:
        ctx.log("stage %s: not selected" % name)
        return
    prior = None
    if resume and os.path.exists(path):
        try:
            prior = json.load(open(path))
        except Exception:  # noqa
            prior = None
        if prior and prior.get("status") in ("ok", "skipped"):
            ctx.results[name] = prior
            ctx.log("stage %s: finished earlier (%s), resumed" % (name, prior["status"]))
            return
    free = ctx.free_disk_gb()
    need = ctx.cfg["limits"]["min_free_disk_gb"]
    if name not in ("hardware", "setup_gin") and free is not None and free < need:
        res = dict(status="skipped", reason="free disk %.1f GB < %d GB" % (free, need))
    else:
        ctx.log("=== stage %s ===" % name)
        t = time.time()
        try:
            res = fn(ctx, prior) if name == "e2e" else fn(ctx)
        except Exception as e:  # noqa
            res = dict(status="failed", exception=repr(e), traceback=traceback.format_exc().splitlines()[-20:])
            ctx.log("stage %s raised %r\n%s" % (name, e, "\n".join(res["traceback"])))
        res["stage_wall_s"] = time.time() - t
    res["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    ctx.results[name] = res
    write_json(path, res)
    ctx.log("stage %s: %s" % (name, res.get("status")))


def make_stage_table(ctx):
    return [("hardware", st_hardware), ("setup_gin", lambda c: gin_install(c)), ("infinigen_coarse", lambda c: st_infinigen_coarse(c)), ("infinigen_fine", lambda c: st_infinigen_fine(c)),
            ("extract", lambda c: st_extract(c)), ("mastodon_small", st_mastodon_small), ("mastodon_prod", st_mastodon_prod), ("cuda_terrain", st_cuda),
            ("concurrency", st_concurrency), ("determinism", st_determinism), ("e2e", st_e2e)]


def fm(x, nd=1, unit=""):
    return "-" if x is None else ("%.*f%s" % (nd, x, unit))


def build_report(ctx):
    R = ctx.results
    L = ["# Speed test report", "", "Generated %s, total wall %.0f s." % (time.strftime("%Y-%m-%d %H:%M:%S"), time.time() - ctx.t0), ""]
    h = R.get("hardware", {}).get("host", {})
    L += ["## Hardware", "", "- CPU: %s, %s logical cores" % (h.get("cpu_model"), h.get("logical_cores")), "- RAM: %s GB (free now %s %%), free disk %s GB" % (h.get("ram_gb"), fm(h.get("free_ram_pct_now")), fm(h.get("free_disk_gb"))),
          "- GPU: %s" % (", ".join("%s (%s MB)" % (g["name"], g["mem_mb"]) for g in h.get("gpus", [])) or "none"), "- OS / Python: %s / %s (psutil: %s)" % (h.get("platform"), h.get("python"), h.get("psutil")), ""]
    L += ["## Stage summary", "", "| stage | status | wall (s) | peak RSS (MB) | note |", "|---|---|---|---|---|"]
    for n in STAGES:
        r = R.get(n)
        if not r:
            L.append("| %s | not run | | | |" % n)
            continue
        run = r.get("run") or {}
        wall = run.get("wall_s", r.get("wall_s", r.get("wall_mean_s")))
        rss = run.get("peak_rss_mb", r.get("peak_rss_mb"))
        note = r.get("reason") or r.get("note") or (r.get("exception") or "")
        L.append("| %s | %s | %s | %s | %s |" % (n, r.get("status"), fm(wall), fm(rss, 0), str(note)[:110]))
    ex, ms, mp = R.get("extract", {}), R.get("mastodon_small", {}), R.get("mastodon_prod", {})
    L += ["", "## Headline numbers", ""]
    ic, ifn = R.get("infinigen_coarse", {}), R.get("infinigen_fine", {})
    L.append("- Infinigen coarse: %s s; populate+fine_terrain: %s s; extraction (9 patches): %s s" % (fm(ic.get("run", {}).get("wall_s")), fm(ifn.get("run", {}).get("wall_s")), fm(ex.get("run", {}).get("wall_s"))))
    L.append("- MASTODON small: mean %s s (min %s s), peak RSS %s MB; production-size: %s s, peak RSS %s MB (old machine: 76 s, 8.6 GB)" % (fm(ms.get("wall_mean_s")), fm(ms.get("wall_min_s")), fm(ms.get("peak_rss_mb"), 0), fm(mp.get("wall_s")), fm(mp.get("peak_rss_mb"), 0)))
    L.append("- Heightmap used for MASTODON: %s" % (mp.get("heightmap_source") or ms.get("heightmap_source") or "-"))
    e2 = R.get("e2e", {})
    L.append("- **End-to-end evaluations per hour: %s** (%s ok of %s; mean %s s, median %s s, stdev %s s)" % (fm(e2.get("evals_per_hour"), 2), e2.get("n_ok"), e2.get("repeats_requested"), fm(e2.get("wall_mean_s")), fm(e2.get("wall_median_s")), fm(e2.get("wall_stdev_s"))))
    if e2.get("definition"):
        L.append("  - " + e2["definition"])
    cu = R.get("cuda_terrain", {})
    L += ["", "## CPU vs CUDA terrain", ""]
    if cu.get("status") in ("ok", "partial", "failed") and "cpu_coarse_s" in cu:
        L.append("| | CPU (s) | CUDA (s) |\n|---|---|---|\n| coarse | %s | %s |\n| populate+fine_terrain | %s | %s |" % (fm(cu["cpu_coarse_s"]), fm(cu["cuda_coarse_s"]), fm(cu["cpu_fine_s"]), fm(cu["cuda_fine_s"])))
        L.append("\nTotal speedup CPU/CUDA: %s" % fm(cu.get("speedup_total"), 2, "x"))
    else:
        L.append("%s: %s" % (cu.get("status", "not run"), cu.get("reason", "")))
    cn = R.get("concurrency", {})
    L += ["", "## Concurrency scaling (%s workload, %s cores)" % (cn.get("workload"), cn.get("cores")), ""]
    if cn.get("levels"):
        L += ["| jobs | threads/job | wall (s) | jobs/hour | speedup | per-job wall (s) | peak total RSS (MB) | min free RAM % | note |", "|---|---|---|---|---|---|---|---|---|"]
        for l in cn["levels"]:
            if l.get("skipped"):
                L.append("| %s | | | | | | | | skipped: %s |" % (l["n"], l["reason"]))
            else:
                L.append("| %s | %s | %s | %s | %s | %s | %s | %s | %s |" % (l["n"], l["threads_per_job"], fm(l["wall_s"]), fm(l["jobs_per_hour"], 0), fm(l.get("speedup_vs_first_level"), 2, "x"), fm(l["per_job_wall_mean_s"]),
                                                                          fm(l["peak_total_rss_mb"], 0), fm(l["min_free_pct"]), "ABORTED (low RAM)" if l["aborted_low_ram"] else ("%d/%d ok" % (l["jobs_ok"], l["n"]))))
        L.append("\nStopped: %s. Best level: %s." % (cn.get("stopped") or "levels exhausted", cn.get("best_level")))
    dt = R.get("determinism", {})
    L += ["", "## Determinism", ""]
    for k in ("infinigen", "mastodon", "reference"):
        if k in dt:
            L.append("- %s: `%s`" % (k, json.dumps(dt[k], default=str)[:400]))
    if not any(k in dt for k in ("infinigen", "mastodon")):
        L.append("not run / %s" % dt.get("status"))
    L += ["", "## Failures (command and last stderr lines)", ""]
    nf = 0

    def walk(node, where):
        nonlocal nf
        if isinstance(node, dict):
            if node.get("ok") is False and node.get("cmd"):
                nf += 1
                L.extend(["### %s / %s" % (where, node.get("name")), "", "rc=%s timeout=%s" % (node.get("rc"), node.get("timeout")), "", "```", "$ " + node["cmd"], *(node.get("stderr_tail") or node.get("stdout_tail") or ["<no output>"]), "```", ""])
            elif node.get("exception"):
                nf += 1
                L.extend(["### %s raised" % where, "", "```", node["exception"], *node.get("traceback", []), "```", ""])
            for k, x in node.items():
                walk(x, where)
        elif isinstance(node, list):
            for x in node:
                walk(x, where)
    for n in STAGES:
        if n in R:
            walk(R[n], n)
    if not nf:
        L.append("none")
    L += ["", "## Guessed configuration values", ""] + ["- `%s`: %s" % (k, v) for k, v in ctx.guesses.items()]
    return "\n".join(L) + "\n"


def redact_text(s, reps):
    for a, b in reps:
        if a:
            s = s.replace(a, b)
    s = re.sub(r"/home/[^/\s\"']+", "/home/<user>", s)
    s = re.sub(r"(?i)[a-z]:\\\\?users\\\\?[^\\/\s\"']+", r"C:\\Users\\<user>", s)
    return s


def redaction_pairs(ctx):
    pairs = []
    for a, b in ((ctx.out, "<OUT>"), (HERE, "<SPEEDTEST>"), (ctx.vars.get("infinigen_dir"), "<INFINIGEN>"), (os.path.expanduser(str(ctx.vars.get("mastodon_root", ""))), "<MASTODON>"),
                 (os.path.expanduser(str(ctx.vars.get("python", ""))), "<PYTHON>"), (os.path.expanduser("~"), "<HOME>"), (getpass.getuser(), "<user>"), (socket.gethostname(), "<host>"), (tempfile.gettempdir(), "<TMP>")):
        if a and len(str(a)) > 2:
            pairs.append((str(a), b))
            pairs.append((str(a).replace("\\", "/"), b))
    return sorted(pairs, key=lambda p: -len(p[0]))


def finalize(ctx, redact):
    rep = build_report(ctx)
    js = json.dumps(dict(generated=time.strftime("%Y-%m-%dT%H:%M:%S"), stages=ctx.results, guesses=ctx.guesses), indent=1, default=str)
    if redact:
        pairs = redaction_pairs(ctx)
        rep, js = redact_text(rep, pairs), redact_text(js, pairs)
    with open(os.path.join(ctx.out, "speedtest_report.md"), "w") as f:
        f.write(rep)
    with open(os.path.join(ctx.out, "speedtest_report.json"), "w") as f:
        f.write(js)
    ctx.log("report written: %s" % os.path.join(ctx.out, "speedtest_report.md"))


def run_all(ctx):
    only = set(ctx.args.only.split(",")) if ctx.args.only else None
    skip = set(ctx.args.skip.split(",")) if ctx.args.skip else set()
    ctx.log("speedtest start, output %s" % ctx.out)
    for name, fn in make_stage_table(ctx):
        run_stage(ctx, name, fn, only, skip, ctx.args.resume)
    finalize(ctx, ctx.args.redact)


# ============================================================================= selftest
class FakeSys(plat.SysInfo):
    """Scripted RAM pressure for the selftest: 70 % free minus 7 % per running job (8 jobs -> 14 % < 15 % floor)."""
    def total_ram(self):
        return 16 * 2 ** 30

    def free_pct(self, active=0):
        return 70.0 - 7.0 * active


def selftest_config(base_cfg_path, tmp):
    cfg, guesses = load_config(base_cfg_path)
    py, stubs = sys.executable, os.path.join(HERE, "stubs")
    p = cfg["paths"]
    p.update(python=py, ctrl_driver=os.path.join(stubs, "stub_infinigen.py"), extract_script=os.path.join(stubs, "stub_extract.py"), mastodon_exe=py, mastodon_exe_args=[os.path.join(stubs, "stub_mastodon.py")],
             mastodon_root=tmp, infinigen_dir=tmp, conda_prefix=tmp, output_dir=os.path.join(tmp, "out"), nvidia_smi=[py, os.path.join(stubs, "stub_nvidia.py")])
    cfg["infinigen"].update(install_gin=True, gin_install_dir=os.path.join(tmp, "gin_target"), cwd=tmp, timeout_s=60)
    cfg["mastodon"].update(small_repeats=2, timeout_s=60)
    cfg["mastodon"]["profiles"]["prod"]["map"] = {"xmin": 0.0, "xmax": 14.0, "ymin": -4.0, "ymax": 4.0}
    cfg["mastodon"]["profiles"]["small"]["map"] = {"xmin": 6.5, "xmax": 13.5, "ymin": -3.5, "ymax": 3.5}
    cfg["e2e"].update(repeats=2, mastodon_runs=3)
    cfg["limits"].update(sample_interval_s=0.05, min_free_disk_gb=0.01)
    cfg["env"] = {"mastodon": {"MASTODON_FOLDER": "{mastodon_root}"}, "infinigen": {"CTRL_LOG": "{outdir}/logs/ctrl_log.jsonl"}}
    return cfg, guesses


def selftest(args):
    tmp = tempfile.mkdtemp(prefix="speedtest_selftest_")
    fails = []
    check = lambda cond, msg: (print(("PASS  " if cond else "FAIL  ") + msg), None if cond else fails.append(msg))
    try:
        def mk(extra_env=None, out="out", resume=False, redact=True, fake=True):
            cfg, g = selftest_config(os.path.join(HERE, "speedtest_config.json"), tmp)
            cfg["paths"]["output_dir"] = os.path.join(tmp, out)
            a = argparse.Namespace(out=None, resume=resume, redact=redact, only=None, skip=None, reference=None, repeats=None)
            for k, v in (extra_env or {}).items():
                os.environ[k] = v
            return Ctx(cfg, g, a, FakeSys() if fake else None)
        print("== selftest 1: full flow against stubs (fake RAM pressure at 8 concurrent jobs)")
        c1 = mk()
        run_all(c1)
        R = c1.results
        check(all(R[s]["status"] == "ok" for s in ("hardware", "setup_gin", "infinigen_coarse", "infinigen_fine", "extract", "mastodon_small", "mastodon_prod", "cuda_terrain", "determinism", "e2e")), "all stages ok: " + str({k: v.get("status") for k, v in R.items()}))
        check(R["concurrency"]["status"] == "ok" and [l["n"] for l in R["concurrency"]["levels"]][-1] == 8 and R["concurrency"]["levels"][-1].get("aborted_low_ram"), "concurrency: levels 1,2,4 ran, level 8 aborted at the 15 % RAM floor")
        check(R["cuda_terrain"].get("speedup_total", 0) > 1, "CUDA stage ran against GPU stub, speedup %s" % fm(R["cuda_terrain"].get("speedup_total"), 2))
        check(R["determinism"]["mastodon"]["identical"] and R["determinism"]["infinigen"]["identical"], "determinism: stubs identical")
        check(R["e2e"]["n_ok"] == 2 and R["e2e"]["evals_per_hour"] > 0, "e2e %s evals/hour" % fm(R["e2e"]["evals_per_hour"], 0))
        check(R["mastodon_prod"]["peak_rss_mb"] > R["mastodon_small"]["peak_rss_mb"], "RAM sampling: prod peak %.0f MB > small %.0f MB" % (R["mastodon_prod"]["peak_rss_mb"], R["mastodon_small"]["peak_rss_mb"]))
        check(not any(f.lower() == "stavedata.h5" for _r, _d, fs in os.walk(c1.out) for f in fs), "no StaveData.h5 left behind")
        check(R["mastodon_prod"]["run"].get("peak_rss_mb") and R["mastodon_prod"]["validation_warnings"] >= 0 and not R["mastodon_prod"]["validation_errors"], "generated configs pass the validator (no ERROR)")
        rep = open(os.path.join(c1.out, "speedtest_report.md")).read() + open(os.path.join(c1.out, "speedtest_report.json")).read()
        user, host = getpass.getuser(), socket.gethostname()
        check(tmp not in rep and (len(user) < 3 or user not in rep) and (len(host) < 3 or host not in rep), "--redact removed temp paths, user name and host name")
        print("== selftest 2: resume skips finished stages")
        c2 = mk(resume=True)
        run_all(c2)
        check(all(c2.results[s]["status"] in ("ok", "skipped") for s in STAGES) and c2.results["e2e"]["wall_mean_s"] == R["e2e"]["wall_mean_s"], "resume reused the stage files")
        print("== selftest 3: failures are logged and the run continues; non-determinism is detected")
        c3 = mk(dict(STUB_FAIL="infinigen_fine", STUB_NONDET="1"), out="out_fail", redact=False)
        run_all(c3)
        R3 = c3.results
        check(R3["infinigen_fine"]["status"] == "failed" and R3["extract"]["status"] == "skipped", "failed fine stage -> extract skipped")
        check(R3["mastodon_small"]["status"] == "ok" and "synthetic" in R3["mastodon_small"]["heightmap_source"], "MASTODON stages continued with the synthetic heightmap")
        check(len(R3["infinigen_fine"]["run"]["stderr_tail"]) == 20, "failure record keeps the last 20 stderr lines")
        check(R3["determinism"]["mastodon"]["identical"] is False, "non-deterministic MASTODON stub detected (differing byte fraction %.2e)" % R3["determinism"]["mastodon"]["differing_byte_fraction"])
        check("stub failure injected" in open(os.path.join(c3.out, "speedtest_report.md")).read(), "report lists the failing command and stderr")
        os.environ.pop("STUB_FAIL", None)
        os.environ.pop("STUB_NONDET", None)
    finally:
        os.environ.pop("STUB_FAIL", None)
        os.environ.pop("STUB_NONDET", None)
        shutil.rmtree(tmp, ignore_errors=True)
    print("\nSELFTEST %s (%d failures)" % ("PASSED" if not fails else "FAILED", len(fails)))
    return 1 if fails else 0


# ============================================================================= main
def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default=os.path.join(HERE, "speedtest_config.json"))
    ap.add_argument("--out", help="output folder (overrides paths.output_dir)")
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--resume", action="store_true", help="skip stages whose JSON says ok/skipped")
    ap.add_argument("--redact", action="store_true", help="strip paths, user and host names from speedtest_report.*")
    ap.add_argument("--reference", help="reference: a speedtest_report.json (compare hashes) or a heightmap txt (compare numerically)")
    ap.add_argument("--repeats", type=int, help="end-to-end repeats (default from config: 5)")
    ap.add_argument("--only", help="comma-separated stage names: " + ",".join(STAGES))
    ap.add_argument("--skip", help="comma-separated stage names to skip")
    ap.add_argument("--set", action="append", default=[], help="override a config value: --set concurrency.levels=[1,2] ")
    ap.add_argument("--list-guesses", action="store_true")
    ap.add_argument("--list-stages", action="store_true")
    args = ap.parse_args(argv)
    if args.selftest:
        return selftest(args)
    if args.list_stages:
        print("\n".join(STAGES))
        return 0
    cfg, guesses = load_config(args.config, args.set)
    if args.list_guesses:
        for k, v in guesses.items():
            print("GUESS %s: %s" % (k, v))
        return 0
    ctx = Ctx(cfg, guesses, args)
    if args.check:
        return 1 if print_check(check_items(ctx)) else 0
    run_all(ctx)
    return 0


if __name__ == "__main__":
    sys.exit(main())
