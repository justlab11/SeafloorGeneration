"""Stdlib port (no numpy/libconf) of tmp/L_main_test/validate_config.py for the speed test. Rules R1-R9 are unchanged; the heightmap argument is
(top, finite, x0, dx) = (max relief above bottom.depth, all values finite?, file x0, file dx) instead of a numpy grid.
Original docstring: Reference pre-flight validator for generated MASTODON v0.22.6 imaging configs (a script, not a repo edit).

usage:  python validate_config.py CONFIG.cfg [--heightmap hm.txt]
        or import:  problems = validate(cfg_dict, heightmap=(grid, x0, y0, dx, dy) | None)

Every rule cites the probe that shows the failure behaviour (see MASTODON_TUNABLE_PARAMETERS.md section 8 and results/p*.json).
Severity: ERROR = MASTODON exits 4 (no image) or the image is empty/garbage with exit 0; WARN = runs but result probably not what you want;
INFO = informational. Returns a list of (severity, rule_id, message).
heightmap: grid[iy, ix] with y ASCENDING (hm_io.read_heightmap convention), value = floor height above bottom.depth (+ = up).
"""
import math
import sys


C = 1500.0
KNOWN_TOP = {"simulation", "output", "log", "propagation", "environment", "sonars", "platforms", "postprocess", "targets", "calc", "numerical",
             "occlusion", "visualization", "noise", "config", "system", "tl", "ts", "mastodon_options"}


def _get(cfg, dotted, default=None):
    node = cfg
    for p in dotted.split("."):
        # keys are case-insensitive in MASTODON (probe v_case_keys)
        if not hasattr(node, "keys"):
            return default
        hit = [k for k in node.keys() if k.lower() == p.lower()]
        if not hit:
            return default
        node = node[hit[0]]
    return node


def _num(v, units=None):
    """Convert numbers or unit strings ('2.1 cm', '5 ms', '200 kHz') to SI floats. Unit grammar from probes p06:
    space between number and unit REQUIRED; accepted: m, meter(s), cm, mm, km, ft, in, s, ms, us?(NO), kHz/Hz/MHz/GHz (case-insensitive), deg."""
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        parts = v.split()
        if len(parts) != 2:
            raise ValueError(f"unit string {v!r} must be '<number> <unit>' with a space")
        f = {"m": 1, "meter": 1, "meters": 1, "cm": .01, "centimeter": .01, "centimeters": .01, "mm": .001, "millimeter": .001, "millimeters": .001,
             "km": 1000, "kilometer": 1000, "kilometers": 1000, "ft": .3048, "feet": .3048, "foot": .3048, "in": .0254, "inch": .0254, "inches": .0254,
             "s": 1, "second": 1, "seconds": 1, "ms": .001, "millisecond": .001, "milliseconds": .001, "hz": 1, "khz": 1e3, "mhz": 1e6, "ghz": 1e9,
             "kilohertz": 1e3, "megahertz": 1e6, "gigahertz": 1e9}
        u = parts[1].lower()
        if u not in f:
            raise ValueError(f"unknown unit {parts[1]!r} in {v!r} (probe p06: 'msec','us','centimetres','2.1cm' are fatal)")
        return float(parts[0]) * f[u]
    raise ValueError(f"cannot interpret {v!r}")


def validate(cfg, heightmap=None, expect_side="starboard"):
    P = []
    add = lambda sev, rid, msg: P.append((sev, rid, msg))
    g = lambda k, d=None: _get(cfg, k, d)

    # ---------------- R1 structure / typos (unknown keys are SILENTLY ignored: p05)
    for k in cfg.keys():
        if k.lower() not in KNOWN_TOP:
            add("WARN", "R1.unknown_section", f"top-level section {k!r} is not read by MASTODON (silently ignored, exit 0)")
    known_leaf = {"propagation": {"model", "angle_min", "angle_max", "angle_inc", "range_min", "range_max", "range_inc", "bounce_surf_max", "bounce_btm_max",
                                  "max_target_eigenrays", "write_eigenrays", "eigenrays", "depth_inc", "use_attenuation", "bellhop"},
                  "platforms": {"model", "motion", "bearing", "dping", "num_pings", "depth", "altitude", "radius", "length", "speed", "sas_overlap", "ping_rate_type",
                                "start_pos", "pos", "path", "path_file", "ping_times", "ping_times_file", "total_travel", "roll", "pitch", "yaw", "ins_rate", "vehicle"},
                  "calc": {"num_threads", "apply_signal", "bottom_reverb", "noise", "surface_reverb", "target_scatter", "use_gpu"},
                  "postprocess": {"beamform", "matchedfilter", "tr_correction", "agc", "agcparams", "beamformer"}}
    for sec, leaves in known_leaf.items():
        node = g(sec)
        if node is not None and hasattr(node, "keys"):
            for k in node.keys():
                if k.lower() not in leaves:
                    add("WARN", "R1.unknown_key", f"{sec}.{k} is not a known key (typos are silently ignored: probe v_typo_*)")
    if str(g("simulation.type", "")).lower() not in ("imaging", "imaging monostatic"):
        add("ERROR", "R1.simtype", "simulation.type must be Imaging for this validator (typo => 'Could not determine simulation type', exit 4)")

    # ---------------- R2 required keys (exit 4 when missing: p05)
    for k in ("output.file", "environment.bottom.depth", "platforms.motion", "sonars.projector.model", "sonars.projector.frequency", "sonars.projector.bandwidth",
              "sonars.projector.pulse_length", "sonars.receiver.model", "sonars.receiver.num_elem_hor"):
        if g(k) is None:
            add("ERROR", "R2.required", f"missing {k} (MASTODON exits 4)")
    if g("platforms.depth") is None and g("platforms.altitude") is None:
        add("ERROR", "R2.required", "platforms.depth or platforms.altitude must be set (exit 4)")
    if g("platforms.depth") is not None and g("platforms.altitude") is not None:
        add("WARN", "R2.depth_and_altitude", "both platforms.depth and platforms.altitude set (probe pf_platform_alt_and_depth: check which wins)")
    if g("platforms.num_pings") is None and g("platforms.total_travel") is None:
        add("ERROR", "R2.required", "need platforms.num_pings or total_travel")
    npg = g("platforms.num_pings")
    if npg is not None and (not isinstance(npg, int) or npg <= 0):
        add("ERROR", "R2.num_pings", f"platforms.num_pings={npg!r}: 0 logs an Error but exits 0 with a DEFAULT 10-ping image; floats are truncated; strings are fatal")
    if not any(g(k) is not None for k in ("sonars.receiver.sample_frequency", "sonars.receiver.sample_freq", "sonars.receiver.fs")):
        add("ERROR", "R2.required", "receiver sample rate missing (sample_frequency; aliases sample_freq / fs work but warn 'deprecated')")
    for who in ("projector", "receiver"):
        if g(f"sonars.{who}.elem_width") is None and g(f"sonars.{who}.hor_beamwidth") is None:
            add("ERROR", "R2.required", f"sonars.{who}.elem_width or hor_beamwidth required")
        if g(f"sonars.{who}.elem_height") is None and g(f"sonars.{who}.ver_beamwidth") is None:
            add("ERROR", "R2.required", f"sonars.{who}.elem_height or ver_beamwidth required")
        has_side = g(f"sonars.{who}.side") is not None
        has_b = g(f"sonars.{who}.bearing") is not None
        if has_side and has_b:
            add("ERROR", "R2.side_and_bearing", f"{who}: side and bearing are mutually exclusive (exit 4)")
        if not has_side and (not has_b or g(f"sonars.{who}.depression") is None):
            add("ERROR", "R2.required", f"{who}: need side (+depression) or bearing+depression")
    if g("sonars.projector.source_level") is None:
        add("WARN", "R2.source_level_default", "source_level missing: MASTODON silently uses a default ~80 dB lower than 120 dB (probe w_missing_source_level: image 1e-4 x)")
    if g("propagation.range_max") is None:
        add("WARN", "R2.range_max_default", "propagation.range_max missing: silently defaults to 100 m (probe w_missing_range_max: 32000 samples)")
    if g("propagation.bounce_surf_max") is None:
        add("INFO", "R2.bounces", "bounce limits missing: warning 'using default of no bounces' (bounce limits had NO effect on bottom imaging in probes q_bounce_*)")

    # ---------------- R3 unit strings
    for k in ("sonars.projector.elem_width", "sonars.receiver.elem_width", "sonars.projector.pulse_length", "sonars.projector.frequency", "propagation.range_max",
              "environment.bottom.bathymetry.dx", "platforms.depth"):
        v = g(k)
        if isinstance(v, str):
            try:
                _num(v)
            except ValueError as e:
                add("ERROR", "R3.units", f"{k}: {e}")
    # ---------------- numbers
    try:
        f0 = _num(g("sonars.projector.frequency", 0)); bw = _num(g("sonars.projector.bandwidth", 0)); tau = _num(g("sonars.projector.pulse_length", 0))
        fs = _num(g("sonars.receiver.sample_frequency", g("sonars.receiver.sample_freq", g("sonars.receiver.fs", 0))))
        rmax = _num(g("propagation.range_max", 100.0)); depth = _num(g("environment.bottom.depth", 0))
        pdepth = g("platforms.depth"); alt = g("platforms.altitude")
        H = (depth - _num(pdepth)) if pdepth is not None else (_num(alt) if alt is not None else None)
        nel = int(g("sonars.receiver.num_elem_hor", 1)); w = _num(g("sonars.receiver.elem_width", 0) or 0)
        sp = _num(g("sonars.receiver.elem_spacing_hor", w) or w); dping = g("platforms.dping")
        bb = bool(g("sonars.receiver.baseband", False)); dec = int(g("sonars.receiver.decimate", 1) or 1)
    except Exception as e:  # noqa
        add("ERROR", "R3.parse", f"could not parse numeric keys: {e}")
        return P
    # R4 sampling (probe pf_fs_*)
    fs_eff = fs / max(dec, 1)
    need = bw * 1.0 if bb else 2 * (f0 + bw / 2)
    if fs <= 0:
        add("ERROR", "R4.fs", "sample rate must be > 0")
    elif fs_eff < bw:
        add("ERROR", "R4.fs_lt_bandwidth", f"effective complex rate {fs_eff:.0f} Hz < bandwidth {bw:.0f} Hz: range speckle/pixel aliasing (probe s_fs_120k_dec1: correlation 3.4 px)")
    elif not bb and f0 > fs:
        add("WARN", "R4.carrier_above_fs", f"passband fs {fs:.0f} Hz below the carrier {f0:.0f} Hz: runs silently (probe k_520_63_fs240_pass) but the spectrum is aliased; use baseband=true with fs>=4*B or fs>=2*(f0+B/2)")
    elif not bb and fs < need:
        add("INFO", "R4.passband_undersampled", f"passband fs {fs:.0f} < 2*(f0+B/2)={need:.0f}: template does this (analytic signals); see probe k_520_63_fs240_pass (runs, but output is not a calibrated passband sampling)")
    if dec > 1 and not bb:
        add("WARN", "R4.decimate_passband", "decimate only works well with baseband=true (docs); probe pf_decimate_with_passband")
    # R5 geometry
    if H is not None:
        if H <= 0:
            add("ERROR", "R5.platform_below_bottom", f"platform at/below flat bottom (altitude {H:.2f} m): empty/garbage image, exit 0 (probe pf_platdepth_*)")
        if tau > 0 and rmax <= C * tau / 2 * 1.05:
            add("ERROR", "R5.range_max_lt_pulse", f"range_max {rmax} m <= c*tau/2 = {C*tau/2:.2f} m: 'Matched filter - Signal length currently must be less than data size', NO image but exit 0 (probe pf_rangemax_1.0/2.9/3.1; 4.0 m worked for 3.75 m)")
        if pdepth is not None and _num(pdepth) <= 0:
            add("ERROR", "R5.platform_depth_le0", "platforms.depth <= 0: 'Cannot set vehicle depth <= 0' (exit 4, probe pf_platdepth_0/-1)")
        if rmax <= H:
            add("ERROR", "R5.range_max_lt_altitude", f"range_max {rmax} <= altitude {H}: no bottom return, empty image with exit 0 (probe pf_rangemax_*)")
        else:
            reach = math.sqrt(rmax ** 2 - H ** 2)
            add("INFO", "R5.ground_reach", f"max ground reach at range_max={rmax}: {reach:.2f} m (flat floor)")
    ang_min = float(g("propagation.angle_min", -90)); ang_max = float(g("propagation.angle_max", 90))
    if ang_max <= 0:
        add("ERROR", "R5.angle_sign", "angle_max <= 0: positive angles point DOWN (probe h_ang_*); an all-negative fan gives an all-zero image with exit 0")
    if ang_min > ang_max:
        add("WARN", "R5.angle_swapped", "angle_min > angle_max: swapped with only a warning, changes the image")
    side = str(g("sonars.receiver.side", "")).lower()
    b = g("sonars.receiver.bearing")
    looks_x = (side == expect_side) or (b is not None and float(b) == -90.0 and expect_side == "starboard") or (b is not None and float(b) == 90.0 and expect_side == "port")
    if not looks_x:
        add("WARN", "R5.look_direction", "receiver does not look toward +x (starboard / bearing -90 with vehicle heading +y); the map lies at x>=0 -> near-empty image if it looks the other way (probe f2/f4/f6/f8)")
    if g("sonars.projector.bearing") != g("sonars.receiver.bearing") or g("sonars.projector.side") != g("sonars.receiver.side"):
        add("WARN", "R5.proj_rx_mismatch", "projector and receiver point different ways -> collapsed image (probe f8)")
    # R6 SAS geometry
    if nel > 1 and dping is not None:
        half = nel * sp / 2
        if _num(dping) > half * 1.0001:
            add("WARN", "R6.dping_gt_half_array", f"dping {_num(dping)} m > half array {half:.3f} m: undersampled along-track (grating lobes) (probe pf_dping_*)")
    lam = C / f0 if f0 > 0 else None
    if lam and sp > lam / 2 and nel > 1:
        add("INFO", "R6.element_spacing", f"element spacing {sp*100:.2f} cm > lambda/2 = {lam/2*100:.2f} cm (MASTODON only prints a warning; template does this on purpose)")
    # R7 bathymetry / map
    dx = g("environment.bottom.bathymetry.dx")
    xmin, xmax, ymin, ymax = (g(f"environment.bottom.bathymetry.{k}") for k in ("xmin", "xmax", "ymin", "ymax"))
    if None in (xmin, xmax, ymin, ymax):
        add("WARN", "R7.extent_missing", "bathymetry xmin/xmax/ymin/ymax missing: extent taken from file header? (probe pf_cfg_extent_removed)")
    elif xmin >= xmax or ymin >= ymax:
        add("WARN", "R7.extent_swapped", "xmin>=xmax or ymin>=ymax gets swapped with a warning")
    elif H is not None and npg is not None and dping is not None and isinstance(npg, int):
        track = (npg - 1) * _num(dping)
        if rmax > H:
            reach = math.sqrt(rmax ** 2 - H ** 2)
            if xmax < reach:
                add("WARN", "R7.map_short_in_x", f"map xmax {xmax} < ground reach {reach:.1f} m: returns beyond the map are zero or garbage (probe pf_map_x_short_8m)")
        if ymax - ymin < track + 2 * 0.0:
            add("WARN", "R7.map_short_in_y", f"map y extent {ymax-ymin:.2f} m < track {track:.2f} m (+ beam footprint): pings see no floor (probe pf_map_y_short_track_outside)")
        if ymin > -track / 2 or ymax < track / 2:
            add("WARN", "R7.track_not_centred", "track runs y in +-track/2 around 0 for linear motion (probe: 'Pinged at (0.000, +-0.614)' for 10 pings x 0.1365): map must cover it")
    if heightmap is not None and H is not None:
        top, finite, x0, hdx = heightmap
        if not finite:
            add("ERROR", "R7.nan", "heightmap contains NaN/inf")
        clearance = H - top
        if clearance <= 0:
            add("ERROR", "R7.platform_inside_relief", f"max relief {top:.2f} m >= altitude {H:.2f} m: sonar is inside the seabed; empty/garbage image, exit 0 (probe pf_inside_boulder)")
        elif clearance < 0.3 * H:
            add("WARN", "R7.low_clearance", f"clearance over highest relief only {clearance:.2f} m")
        if dx is not None:
            dxs = _num(dx)
            if abs(dxs - hdx) > 1e-9:
                add("WARN", "R7.dx_mismatch", f"config dx {dxs} != heightmap dx {hdx}: map is bilinearly resampled (docs); probe pf_cfg_dx_*")
        if xmin is not None and (abs(xmin - x0) > 1e-6):
            add("WARN", "R7.xmin_vs_file", f"config xmin {xmin} != file x0 {x0}: see probe pf_cfg_extent_shifted_x+1 for which wins")
    # R8 SSP (probe pf_ssp_*)
    ssp_dim = g("environment.ssp.dimension", g("environment.ssp.dim", 0))
    depths = g("environment.ssp.depths"); sp_ = g("environment.ssp.ssp")
    if depths is None or sp_ is None:
        add("ERROR", "R8.ssp_missing", "environment.ssp.depths and .ssp required")
    else:
        dl = list(depths) if hasattr(depths, "__iter__") else [depths]; sl = list(sp_) if hasattr(sp_, "__iter__") else [sp_]
        if int(ssp_dim or 0) == 0 and (len(dl) > 1 or len(sl) > 1):
            add("ERROR", "R8.ssp_dim0_arrays", "ssp.dimension=0 needs scalar depths/ssp (arrays => 'Depth and sound speed must be specified', exit 4)")
        if int(ssp_dim or 0) == 1:
            if len(dl) != len(sl) or len(dl) < 2:
                add("ERROR", "R8.ssp_len", "ssp.dimension=1 needs >=2 equal-length depths/ssp")
            elif dl[0] != 0:
                add("WARN", "R8.ssp_start", "first SSP depth must be 0 (MASTODON duplicates the first entry at 0 with a warning)")
            if any(b2 <= a for a, b2 in zip(dl, dl[1:])):
                add("ERROR", "R8.ssp_monotonic", "SSP depths must be strictly increasing")
            if dl and abs(dl[-1] - depth) > 1e-9:
                add("WARN", "R8.ssp_end", f"last SSP depth {dl[-1]} != bottom.depth {depth} (docs require equality; probe pf_ssp_*)")
    # R9 silent-empty switches
    if g("calc.bottom_reverb") is False:
        add("ERROR", "R9.bottom_reverb_off", "calc.bottom_reverb=false => zero image, exit 0 (probe t_calc_bottom_reverb_False)")
    if str(g("environment.bottom.model", "")).lower() == "dead":
        add("ERROR", "R9.dead_bottom", "bottom.model Dead => zero image, exit 0 (probe s_bottom_Dead)")
    if str(g("environment.bottom.model", "")).lower() in ("bounce",):
        add("ERROR", "R9.bounce_bottom", "BOUNCE bottom needs the BOUNCE executable; exit 4 here")
    if str(g("environment.bottom.model", "")).lower() == "ssa":
        add("WARN", "R9.ssa", "SSA forward scattering 'does not work properly - using Lambertian' (probe s_bottom_SSA)")
    if str(g("environment.bottom.bathymetry.model", "")).lower() == "perlin":
        add("ERROR", "R9.perlin", "Perlin bathymetry is listed as valid but 'does not exist' in v0.22.6 (probe i_perlin, exit 4)")
    bt = str(g("postprocess.beamformer.type", "")).lower()
    if bt in ("omega-k", "omegak"):
        add("ERROR", "R9.beamformer_name", "beamformer.type 'omega-k' (docs) is rejected; use 'wk' (probe h_bf_wk_explicit)")
    if bt == "phase shift" and g("postprocess.beamformer.num_angles") is None:
        add("ERROR", "R9.phase_shift", "phase shift beamformer needs min_angle/max_angle/num_angles, else a 1-row image (probe h_bf_phaseshift)")
    if g("calc.noise") is True and g("noise.seed") is None:
        add("INFO", "R9.noise_seed", "noise.seed missing: default seed is used (noise is deterministic)")
    if g("environment.bottom.reverb_mode") == "fast":
        add("WARN", "R9.reverb_fast", "reverb_mode=fast changes scatterer sampling completely (corr 0.43 vs accurate) and samples outside the map (probe r_reverb_fast_*)")
    return P
