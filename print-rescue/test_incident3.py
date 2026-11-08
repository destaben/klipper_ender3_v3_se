"""Incident 3 (2026-11-07 18:59, vase, 53.0 mm tall at the stop): the real
numbers of the live rescue, on a scrubbed excerpt of the G-code and the log."""

import contextlib
import io
import json
import os
import re
import tempfile
import unittest
from unittest import mock

import plan
import rescue_fixtures as fx


def profile3(**kw):
    """Every safety leaf measured (test only), the owner's head estimate, the
    real bed and mesh areas, and NO stored probe spots."""
    p = fx.measured_profile(owner_footprint=True, live_z_offset=2.96, **kw)
    p["probe_spots"] = []
    p["envelope"]["bed"]["value"] = {"x_min": 0, "x_max": 220, "y_min": 0, "y_max": 220}
    p["envelope"]["probe_area"]["value"] = {"x_min": 30, "x_max": 190,
                                            "y_min": 30, "y_max": 190}
    return p


def run(*extra, profile=None, history=None, cfg=fx.INC3_PRINTER_CFG, gmap=None):
    with tempfile.TemporaryDirectory() as d:
        prof_path = os.path.join(d, "profile.json")
        with open(prof_path, "w") as fh:
            json.dump(profile or profile3(), fh)
        argv = ["--gcode", fx.INC3_GCODE, "--gcode-map", gmap or fx.INC3_GMAP,
                "--log", fx.INC3_KLIPPY, "--profile", prof_path,
                "--history", history or fx.INC3_HISTORY, "--queue", fx.INC3_QUEUE,
                "--kernel-log", fx.INC3_KERNEL, "--now", "2026-11-07T20:00:00Z"]
        if cfg:
            argv += ["--printer-cfg", cfg]
        argv += [a.replace("{tmp}", d) for a in extra]
        out, err = io.StringIO(), io.StringIO()
        ns = plan.build_parser().parse_args(argv)
        with contextlib.redirect_stderr(err):
            code = plan.run(ns, out=out)
        preview = None
        pv = os.path.join(d, "preview.gcode")
        if os.path.exists(pv):
            with open(pv, encoding="utf-8") as fh:
                preview = fh.read()
        return code, out.getvalue(), err.getvalue(), preview


def codes(out):
    return re.findall(r"^RESCUE: (?:REFUSED|WARNING) \[([A-Z0-9_]+)\]", out, re.M)


class ContentCheck(unittest.TestCase):
    """A1: the exit position must be the end of a line of the file."""

    def test_real_numbers(self):
        code, out, err, _ = run()
        self.assertEqual(err, "")
        self.assertIn("last dispatched line: %d (exit position %d)"
                      % (fx.INC3_LAST_LINE, fx.INC3_EXIT_POSITION), out)
        self.assertIn("content check: exit position 8772080 is the end of line 391419",
                      out)
        self.assertNotIn("R07", codes(out))
        self.assertNotIn("R19", codes(out))

    def test_reupload_with_a_new_mtime_is_only_a_warning(self):
        # The excerpt has no mtime, so give the planner one that differs by an
        # hour: the contents still match (byte-offset check), so R19 warns.
        real = plan._gcode_identity

        def fake(args):
            size, _, name = real(args)
            return size, 1794064929.97 + 3600, name

        with mock.patch.object(plan, "_gcode_identity", fake):
            code, out, _, _ = run()
        self.assertNotIn("R07", codes(out))
        self.assertIn("R19", codes(out))
        self.assertIn("content check: exit position 8772080 is the end of line", out)

    def test_logged_text_that_differs_from_the_file_is_refused(self):
        # A same-length edit keeps every byte offset; the text the log printed
        # at "Upcoming (N)" is what catches it.
        with open(fx.INC3_KLIPPY, encoding="utf-8") as fh:
            text = fh.read()
        self.assertIn("Upcoming (8772053): 'G1 X79.084 Y92.754", text)
        text = text.replace("Upcoming (8772053): 'G1 X79.084 Y92.754",
                            "Upcoming (8772053): 'G1 X79.084 Y92.999")
        with tempfile.TemporaryDirectory() as d:
            lp = os.path.join(d, "klippy.log")
            with open(lp, "w", encoding="utf-8") as fh:
                fh.write(text)
            real = fx.INC3_KLIPPY
            fx.INC3_KLIPPY = lp
            try:
                code, out, _, _ = run()
            finally:
                fx.INC3_KLIPPY = real
        self.assertEqual(code, 2)
        self.assertIn("R07", codes(out))
        self.assertIn("differs from the file", out)

    def test_a_file_that_differs_is_refused(self):
        # Shift every byte offset by one: the exit position is then inside a line.
        with open(fx.INC3_GMAP, encoding="utf-8") as fh:
            m = json.load(fh)
        for r in m["runs"]:
            r[2] += 1
        with tempfile.TemporaryDirectory() as d:
            mp = os.path.join(d, "m.json")
            with open(mp, "w") as fh:
                json.dump(m, fh)
            code, out, _, _ = run(gmap=mp)
        self.assertEqual(code, 2)
        self.assertIn("R07", codes(out))
        self.assertIn("not a line boundary", out)


class GcodeMapGuard(unittest.TestCase):
    def test_gcode_map_is_refused_outside_the_tests(self):
        old = os.environ.pop("PRINT_RESCUE_ALLOW_GCODE_MAP")
        try:
            code, out, err, _ = run()
        finally:
            os.environ["PRINT_RESCUE_ALLOW_GCODE_MAP"] = old
        self.assertEqual(code, 1)
        self.assertIn("test fixtures only", err)


class ExitBoundary(unittest.TestCase):
    def test_line_boundary_and_end_of_file(self):
        import gcode_scan as gs
        res = gs.scan(iter([(1, 0, "G1 X1 Y1 E1"), (2, 12, "G1 X2 Y2 E1")]), None)
        ok, why = plan._exit_boundary(res, 12, 24)
        self.assertTrue(ok, why)
        ok, why = plan._exit_boundary(res, 24, 24)
        self.assertTrue(ok, why)
        ok, why = plan._exit_boundary(res, 13, 24)
        self.assertFalse(ok)


class LiveZOffset(unittest.TestCase):
    def test_live_z_offset_replaces_the_stale_profile_copy(self):
        p = profile3()
        p["config_fingerprint"]["bltouch"]["z_offset"]["value"] = 3.035
        code, out, _, _ = run(profile=p)
        self.assertIn("live config: bltouch.z_offset: live printer.cfg 2.96 replaces "
                      "the profile copy 3.035", out)
        self.assertNotIn("R14", codes(out))


class Verdict(unittest.TestCase):
    def test_overlap_is_a_warning_and_the_rear_spot_is_valid(self):
        code, out, _, preview = run("--probe-spot", "134,215",
                                    "--preview", "{tmp}/preview.gcode")
        self.assertEqual(code, 0, out)
        self.assertIn("R12", codes(out))
        self.assertIn("R20", codes(out))
        self.assertNotIn("R02", codes(out))
        self.assertIn("resume line 390866", out)
        self.assertIn("overlap to the latest possible stop: 3.55 s", out)
        self.assertIsNotNone(preview)

    def test_front_spot_is_rejected_and_search_suggests_the_rear(self):
        code, out, _, _ = run("--probe-spot", "134,30", "--suggest-spots")
        self.assertEqual(code, 2)
        self.assertIn("R02", codes(out))
        self.assertIn("proposed (134,30): REJECTED", out)
        m = re.search(r"best first.*\n  nozzle X(\S+) Y(\S+),", out)
        self.assertIsNotNone(m, out)
        self.assertEqual(float(m.group(2)), 215.0)

    def test_owner_estimate_keeps_motion_blocked(self):
        p = fx.profile()
        code, out, _, _ = run("--probe-spot", "134,215", profile=p)
        self.assertEqual(code, 3, out)  # resumable on paper, motion blocked
        self.assertIn("R13", codes(out))
        self.assertIn("head_footprint.tiers[head and fan envelope (owner estimate)] "
                      "(source user)", out)


if __name__ == "__main__":
    unittest.main()
