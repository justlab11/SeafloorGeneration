"""Unit tests for the OS-specific parsers (recorded sample output) and pure helpers. Run: python -m unittest discover -s tests -v"""
import os, sys, unittest, tempfile, json
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
sys.dont_write_bytecode = True
import st_platform as plat, st_mastodon as mm, st_validate, speedtest as st

MEMINFO = """MemTotal:       65792196 kB
MemFree:         1203456 kB
MemAvailable:   40123456 kB
Buffers:          123456 kB
Cached:         30000000 kB
SwapTotal:       2097148 kB
HugePages_Total:       0
"""
MEMINFO_OLD = "MemTotal: 1000 kB\nMemFree: 100 kB\nBuffers: 50 kB\nCached: 150 kB\n"
CPUINFO = "processor\t: 0\nmodel name\t: AMD EPYC 7763 64-Core Processor\ncpu MHz\t: 2450\n\nprocessor\t: 1\nmodel name\t: AMD EPYC 7763 64-Core Processor\n"
STATUS = "Name:\tMASTODON\nVmPeak:\t 9000000 kB\nVmHWM:\t 8800000 kB\nVmRSS:\t 8600000 kB\nThreads:\t5\n"
STAT = "4242 (MAST (worker) x) S 1234 4242 4242 0 -1 4194560 1000 0 0 0 5 1 0 0 20 0 5 0 100 1000000 500 18446744073709551615"
NVIDIA = "NVIDIA A100-SXM4-40GB, 40960, 535.104.05\nNVIDIA A100-SXM4-40GB, 40960, 535.104.05\n"
LDD = """\tlinux-vdso.so.1 (0x00007ffc)
\tlibX11.so.6 => not found
\tlibGLX.so.0 => /lib/x86_64-linux-gnu/libGLX.so.0 (0x00007f)
\tlibc.so.6 => /lib/x86_64-linux-gnu/libc.so.6 (0x00007f)
\t/lib64/ld-linux-x86-64.so.2 (0x00007f)
"""
BIN = os.path.join(os.path.dirname(os.path.dirname(HERE)), "MASTODON-PR-0.22.6-Linux", "bin", "MASTODON")


class Parsers(unittest.TestCase):
    def test_meminfo(self):
        self.assertEqual(plat.parse_meminfo(MEMINFO)["MemTotal"], 65792196 * 1024)
        self.assertAlmostEqual(plat.meminfo_free_pct(MEMINFO), 100 * 40123456 / 65792196)
        self.assertAlmostEqual(plat.meminfo_free_pct(MEMINFO_OLD), 30.0)   # no MemAvailable: free+buffers+cached
        self.assertIsNone(plat.meminfo_free_pct("garbage"))

    def test_cpuinfo(self):
        self.assertEqual(plat.parse_cpuinfo(CPUINFO), ("AMD EPYC 7763 64-Core Processor", 2))

    def test_status_and_stat(self):
        self.assertEqual(plat.parse_status_kb(STATUS), 8600000 * 1024)
        self.assertEqual(plat.parse_status_kb(STATUS, "VmHWM"), 8800000 * 1024)
        self.assertIsNone(plat.parse_status_kb("Name: x\n"))
        self.assertEqual(plat.parse_stat_ppid(STAT), (4242, 1234))     # name contains spaces and parentheses

    def test_nvidia(self):
        g = plat.parse_nvidia_smi_csv(NVIDIA)
        self.assertEqual((len(g), g[0]["mem_mb"], g[0]["driver"]), (2, 40960.0, "535.104.05"))
        self.assertEqual(plat.parse_nvidia_smi_csv("No devices were found\n"), [])

    def test_ldd(self):
        found, missing = plat.parse_ldd(LDD)
        self.assertEqual(missing, ["libX11.so.6"])
        self.assertIn("libGLX.so.0", found)

    @unittest.skipUnless(os.path.exists(BIN), "Linux MASTODON binary not present")
    def test_elf_needed_real_binary(self):
        need = plat.parse_elf_needed(open(BIN, "rb").read())
        self.assertIn("libX11.so.6", need)
        self.assertIn("libgomp.so.1", need)

    def test_elf_rejects_non_elf(self):
        self.assertIsNone(plat.parse_elf_needed(b"MZ\x90\x00" + b"\0" * 100))

    def test_tail_lines(self):
        with tempfile.NamedTemporaryFile("w", delete=False) as f:
            f.write("\n".join("l%d" % i for i in range(100)))
        self.assertEqual(plat.tail_lines(f.name, 20)[-1], "l99")
        self.assertEqual(len(plat.tail_lines(f.name, 20)), 20)
        os.remove(f.name)


class Helpers(unittest.TestCase):
    def test_fill_argv(self):
        v = dict(python="py", seed=3, gin_stems=["a.gin", "b.gin"], outdir="/o")
        self.assertEqual(st.fill_argv(["{python}", "-g", "{*gin_stems}", "--s", "{seed}", "{outdir}/x"], v), ["py", "-g", "a.gin", "b.gin", "--s", "3", "/o/x"])
        with self.assertRaises(KeyError):
            st.fill_argv(["{nope}"], v)

    def test_expand_env_refs(self):
        self.assertEqual(st.expand_env_refs("/a:$LD_LIBRARY_PATH", {}), "/a")
        self.assertEqual(st.expand_env_refs("/a:$LD_LIBRARY_PATH", {"LD_LIBRARY_PATH": "/b"}), "/a:/b")

    def test_guesses(self):
        c, g = st.strip_guesses({"a": 1, "a__GUESS": "why", "b": {"c": 2, "c__GUESS": "x"}, "_comment": "y"})
        self.assertEqual(c, {"a": 1, "b": {"c": 2}})
        self.assertEqual(set(g), {"a", "b.c"})

    def test_config_file_has_guess_reasons(self):
        c, g = st.load_config(os.path.join(HERE, "speedtest_config.json"))
        self.assertGreater(len(g), 8)
        self.assertTrue(all(isinstance(v, str) and len(v) > 10 for v in g.values()))

    def test_dumps_roundtrip_shape(self):
        t = mm.dumps({"a": {"b": 1, "c": 2.0, "d": "x\"y", "e": [1, 2], "f": True, "g": None}})
        self.assertIn("c = 2.0;", t)
        self.assertIn('d = "x\\"y";', t)
        self.assertIn("e = (1, 2);", t)
        self.assertNotIn("g =", t)

    def test_validator_catches_known_failures(self):
        prof = dict(pings=111, range_max=30.0, range_inc=0.05, angle_inc=2.0, altitude=3.0)
        ok = mm.build_profile_cfg(prof, "h.txt", (0, 32, -6.5, 6.5), 0.025, 4)
        self.assertEqual([p for p in st_validate.validate(ok, (0.5, True, 0.0, 0.025)) if p[0] == "ERROR"], [])
        bad = mm.build_profile_cfg(prof, "h.txt", (0, 32, -6.5, 6.5), 0.025, 4, {"calc.bottom_reverb": False, "platforms.num_pings": 0})
        ids = {p[1] for p in st_validate.validate(bad, (0.5, True, 0.0, 0.025)) if p[0] == "ERROR"}
        self.assertEqual(ids, {"R9.bottom_reverb_off", "R2.num_pings"})
        inside = st_validate.validate(ok, (3.5, True, 0.0, 0.025))
        self.assertIn("R7.platform_inside_relief", {p[1] for p in inside})

    def test_map_building_and_io(self):
        patch = mm.synthetic_patch(1.0, 0.025, 2)
        m, top = mm.make_map(patch, (0, 4, -2, 2), cx=2.0)
        self.assertEqual((m["cols"], m["rows"]), (160, 160))
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "m.txt")
            mm.write_hm(p, m)
            r = mm.read_hm(p)
            self.assertEqual((r["cols"], r["rows"]), (160, 160))
            self.assertLess(abs(mm.hm_stats(r)[0] - mm.hm_stats(m)[0]), 1e-5)
            with self.assertRaises(ValueError):
                mm.make_map(patch, (0, 0.5, -0.2, 0.2), cx=0.2)

    def test_compare_files(self):
        with tempfile.TemporaryDirectory() as d:
            a, b = os.path.join(d, "a"), os.path.join(d, "b")
            open(a, "wb").write(b"abcd" * 100)
            open(b, "wb").write(b"abcd" * 100)
            self.assertTrue(st.compare_files(a, b)["identical"])
            open(b, "wb").write(b"abcd" * 99 + b"abcx")
            r = st.compare_files(a, b)
            self.assertFalse(r["identical"])
            self.assertAlmostEqual(r["differing_byte_fraction"], 1 / 400)


if __name__ == "__main__":
    unittest.main()
