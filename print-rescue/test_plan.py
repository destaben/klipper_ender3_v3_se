"""End-to-end incident regressions through plan.run (fixtures only, no network)."""

import contextlib
import io
import json
import os
import re
import tempfile
import unittest
from collections import namedtuple

import plan
import rescue_fixtures as fx
import refusals as rf
import resume_file
import stop_point as sp
from unittest import mock

Result = namedtuple("Result", "code out err preview")


def run_full(*extra, profile=None, profile_text=None, log=None, log_text=None,
             gcode=None, preexisting_preview=None, gmap_source=None, evidence=True):
    """Run plan.run with fixture defaults. Returns (exit code, stdout, stderr,
    preview text or None). {tmp} in extra arguments is the scratch directory.
    gmap_source replaces the "source" name in a copy of the excerpt map."""
    with tempfile.TemporaryDirectory() as d:
        gmap = fx.GMAP
        if gmap_source is not None:
            with open(fx.GMAP, encoding="utf-8") as fh:
                meta = json.load(fh)
            meta["source"] = gmap_source
            gmap = os.path.join(d, "map.json")
            with open(gmap, "w", encoding="utf-8") as fh:
                json.dump(meta, fh)
        prof_path = fx.PROFILE
        if profile is not None or profile_text is not None:
            prof_path = os.path.join(d, "profile.json")
            with open(prof_path, "w", encoding="utf-8") as fh:
                if profile_text is not None:
                    fh.write(profile_text)
                else:
                    json.dump(profile, fh)
        log_path = fx.KLIPPY
        if log is not None or log_text is not None:
            log_path = os.path.join(d, "klippy.log")
            text = log_text if log_text is not None else log
            if isinstance(text, bytes):
                with open(log_path, "wb") as fh:
                    fh.write(text)
            else:
                with open(log_path, "w", encoding="utf-8") as fh:
                    fh.write(text)
        pv = os.path.join(d, "preview.gcode")
        if preexisting_preview is not None:
            with open(pv, "w", encoding="utf-8") as fh:
                fh.write(preexisting_preview)
        if gcode is None:
            head = ["--gcode", fx.GCODE, "--gcode-map", gmap]
        else:
            head = ["--gcode", gcode]
        argv = head + ["--log", log_path, "--profile", prof_path,
                       "--now", "2026-11-07T09:30:00Z"]
        # Supply the evidence inputs by default: without them the run prints
        # NOT CHECKED and exits 3, which most tests are not about.
        defaults = []
        if evidence:
            defaults = [("--queue", fx.INC3_QUEUE), ("--printer-cfg", fx.REPO_PRINTER_CFG)]
            if gcode is None:
                hist = fx.HISTORY
                if gmap_source is not None:
                    with open(fx.HISTORY, encoding="utf-8") as fh:
                        h = json.load(fh)
                    h["job"]["filename"] = gmap_source
                    hist = os.path.join(d, "history.json")
                    with open(hist, "w", encoding="utf-8") as fh:
                        json.dump(h, fh)
                defaults.append(("--history", hist))
        for flag, path in defaults:
            if flag not in extra:
                argv += [flag, path]
        argv += [a.replace("{tmp}", d) for a in extra]
        out, err = io.StringIO(), io.StringIO()
        ns = plan.build_parser().parse_args(argv)
        with contextlib.redirect_stderr(err):
            code = plan.run(ns, out=out)
        preview = None
        if os.path.exists(pv):
            with open(pv, encoding="utf-8") as fh:
                preview = fh.read()
        return Result(code, out.getvalue(), err.getvalue(), preview)


def run(*extra, profile=None, log=None):
    r = run_full(*extra, profile=profile, log=log)
    return r.code, r.out, r.preview


def codes(out):
    return re.findall(r"^RESCUE: (?:REFUSED|WARNING) \[([A-Z0-9_]+)\]", out, re.M)


def klippy_log():
    with open(fx.KLIPPY, encoding="utf-8") as fh:
        return fh.read()


def resumable_profile():
    """Measured everywhere, with a 5 s overlap budget: the incident resumes."""
    p = fx.measured_profile()
    p["stop_estimation"]["max_overlap_s"] = 5.0
    return p


def preview_lines(text):
    return text.split("\n")


def at(lines, prefix):
    """Index of the first line starting with prefix (lines carry ; comments)."""
    return next(i for i, ln in enumerate(lines) if ln.startswith(prefix))


class TemplateProfile(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.r = run_full("--history", fx.HISTORY, "--kernel-log", fx.KERNEL,
                         "--preview", "{tmp}/preview.gcode", evidence=False)
        cls.code, cls.out, cls.preview = cls.r.code, cls.r.out, cls.r.preview

    def test_fails_closed_with_r13_and_r02(self):
        self.assertEqual(self.code, 2)
        self.assertIn("R13", codes(self.out))
        self.assertIn("R02", codes(self.out))
        self.assertIn("RESULT: REFUSED", self.out)

    def test_incident_overlap_refused(self):
        self.assertIn("R12", codes(self.out))

    def test_missing_evidence_inputs_are_reported(self):
        self.assertIn("NOT CHECKED: R15 job queue", self.out)
        self.assertIn("NOT CHECKED: R14 printer.cfg", self.out)
        self.assertNotIn("NOT CHECKED: R07", self.out)

    def test_pin_at_boot_warning(self):
        self.assertIn("PIN_AT_BOOT", codes(self.out))

    def test_no_spurious_findings(self):
        self.assertEqual(sorted(set(codes(self.out))),
                         ["PIN_AT_BOOT", "R02", "R12", "R13"])

    def test_report(self):
        self.assertIn("anchors: sd+end", self.out)
        self.assertIn("P_end - P_good 5.23 s", self.out)
        self.assertIn("earliest stop: line 201951", self.out)
        self.assertIn("latest stop:   line 202295", self.out)
        self.assertIn("part box X 29.8-188.4 Y 29.8-188.4, top 20.60", self.out)
        self.assertIn("last dispatched line: 202396", self.out)
        self.assertIn("USB serial read stalled (-32) at Nov 07 10:09:45", self.out)
        m = re.search(r"resume line (\d+) at X([\d.]+) Y([\d.]+)", self.out)
        self.assertIsNotNone(m)
        self.assertLessEqual(int(m.group(1)), 202314)
        self.assertGreaterEqual(int(m.group(1)), fx.CUT_LAYER_START)
        self.assertIn("resume candidate", self.out)
        self.assertRegex(self.out, r"overlap to the latest possible stop: 4\.83 s, [\d.]+ mm")

    def test_no_preview_when_refused(self):
        self.assertIsNone(self.preview)
        self.assertIn("preview not written", self.out)


class MeasuredProfile(unittest.TestCase):
    def test_resumable(self):
        code, out, _ = run(profile=resumable_profile())
        self.assertEqual(code, 0, out)
        self.assertEqual(codes(out), ["PIN_AT_BOOT"])
        self.assertIn("rear left: ok", out)

    def test_incident_over_budget_warns_with_extra_material(self):
        # The incident needs about 4.8 s of overlap; the 3 s budget only warns
        # (R12) and prints the extra material. A gap would be worse.
        r = run_full("--preview", "{tmp}/preview.gcode", profile=fx.measured_profile())
        self.assertEqual(r.code, 0, r.out)
        self.assertIn("R12", codes(r.out))
        self.assertIn("mm3", r.out)
        self.assertIsNotNone(r.preview)

    def test_missing_evidence_exits_3_not_0(self):
        r = run_full(profile=fx.measured_profile(), evidence=False)
        self.assertEqual(r.code, 3, r.out)
        self.assertIn("NOT CHECKED", r.out)
        self.assertIn("evidence not supplied", r.out)

    def test_oversized_text_input_is_a_usage_error(self):
        old = plan.MAX_TEXT_CHARS
        plan.MAX_TEXT_CHARS = 1000
        try:
            r = run_full(profile=fx.measured_profile())
        finally:
            plan.MAX_TEXT_CHARS = old
        self.assertEqual(r.code, 1)
        self.assertNotIn("Traceback", r.err)

    def test_operator_budget_removes_the_warning(self):
        r = run_full("--max-overlap-s", "5", profile=fx.measured_profile())
        self.assertEqual(r.code, 0, r.out)
        self.assertNotIn("R12", codes(r.out))

    def test_absurd_overlap_is_refused_r21(self):
        p = fx.measured_profile()
        p["stop_estimation"]["absurd_overlap_s"] = 4.0
        r = run_full(profile=p)
        self.assertEqual(r.code, 2)
        self.assertIn("R21", codes(r.out))
        self.assertIsNone(r.preview)

    def test_budget_above_the_absurd_cap_is_a_usage_error(self):
        r = run_full("--max-overlap-s", "31", profile=fx.measured_profile())
        self.assertEqual(r.code, 1)
        self.assertIn("limit for any budget", r.err)

    def test_park_spot_inside_the_cutter_zone_warns_r03(self):
        p = resumable_profile()
        p["park"]["xy"]["value"] = [215, 100]
        r = run_full(profile=p)
        self.assertIn("R03", codes(r.out))
        self.assertIn("park spot", r.out)

    def test_bad_overlap_budget_is_a_usage_error(self):
        for bad in ("0", "-1", "nan", "inf"):
            with self.subTest(bad=bad):
                r = run_full("--max-overlap-s", bad, profile=fx.measured_profile())
                self.assertEqual(r.code, 1)

    def test_manual_line_with_gap_is_refused(self):
        code, out, _ = run("--line", str(fx.MANUAL_RESUME_LINE), profile=fx.measured_profile())
        self.assertEqual(code, 2)
        self.assertIn("R22", codes(out))

    def test_r01_tall_part(self):
        p = fx.measured_profile()
        p["envelope"]["z_max"]["value"] = 30
        code, out, _ = run(profile=p)
        self.assertEqual(code, 2)
        self.assertIn("R01", codes(out))

    def test_r15_queue(self):
        with tempfile.TemporaryDirectory() as d:
            q = os.path.join(d, "q.json")
            with open(q, "w") as fh:
                json.dump({"result": {"queue_state": "ready",
                                      "queued_jobs": [{"filename": "x.gcode"}]}}, fh)
            code, out, _ = run("--queue", q, profile=fx.measured_profile())
        self.assertIn("R15", codes(out))
        self.assertEqual(code, 2)

    def test_queue_file_without_queued_jobs_is_not_an_empty_queue(self):
        # A history JSON (or any other file) passed to --queue must not read as
        # "no jobs waiting": R15 would fail open.
        with tempfile.TemporaryDirectory() as d:
            q = os.path.join(d, "q.json")
            for name, body in (("no key", {"result": {"queue_state": "ready"}}),
                               ("history", {"result": {"job": {"filename": "x.gcode"}}})):
                with self.subTest(body=name):
                    with open(q, "w") as fh:
                        json.dump(body, fh)
                    r = run_full("--queue", q, profile=fx.measured_profile())
                    self.assertEqual(r.code, 1)
                    self.assertIn("queued_jobs", r.err)

    def test_log_without_file_text_leaves_the_content_check_not_checked(self):
        # With no 'Virtual sdcard (N)' / 'Upcoming (N)' text the text half of the
        # R07 content check has nothing to compare; that must not pass silently.
        log = "\n".join(ln for ln in klippy_log().split("\n")
                        if "Virtual sdcard (" not in ln and "Upcoming (" not in ln)
        r = run_full(log=log, profile=fx.measured_profile())
        self.assertIn("NOT CHECKED: R07 file text", r.out)
        self.assertEqual(r.code, 3)

    def test_r19_text_does_not_overclaim(self):
        self.assertIn("same-length edit", plan.R19_REUPLOAD_NOTE)
        self.assertNotIn("contents match the print", plan.R19_REUPLOAD_NOTE)

    def test_pin_unknown_note_says_what_to_do(self):
        # PIN_AT_BOOT is a warning (exit 0 possible), so the text must act.
        self.assertIn("FIRMWARE_RESTART", plan.PIN_UNKNOWN_NOTE)
        self.assertIn("lift Z", plan.PIN_UNKNOWN_NOTE)

    def test_r07_history_mismatch(self):
        with tempfile.TemporaryDirectory() as d:
            h = os.path.join(d, "h.json")
            with open(fx.HISTORY) as fh:
                job = json.load(fh)
            job["job"]["metadata"]["size"] += 1
            with open(h, "w") as fh:
                json.dump(job, fh)
            code, out, _ = run("--history", h, profile=fx.measured_profile())
        self.assertIn("R07", codes(out))

    def test_r09_non_comm_shutdown(self):
        log = klippy_log().replace(
            "Transition to shutdown state: Lost communication with MCU 'mcu'",
            "Transition to shutdown state: MCU 'mcu' shutdown: Timer too close")
        code, out, _ = run(log=log, profile=fx.measured_profile())
        self.assertEqual(code, 2)
        self.assertIn("R09", codes(out))

    def test_r14_fingerprint(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = os.path.join(d, "printer.cfg")
            with open(cfg, "w") as fh:
                fh.write("[bltouch]\nx_offset: -20\ny_offset: -14.5\n")
            code, out, _ = run("--printer-cfg", cfg, profile=fx.measured_profile())
        self.assertEqual(code, 2)
        self.assertIn("R14", codes(out))

    def test_r11_long_time(self):
        code, out, _ = run(profile=fx.measured_profile())
        self.assertNotIn("R11", codes(out))
        _, out, _ = run("--now", "2026-11-07T15:00:00Z", profile=fx.measured_profile())
        self.assertIn("R11", codes(out))

    def test_r10_without_freeze_is_refused(self):
        # Drop the frozen Stats lines: the last sample before the shutdown
        # still shows receive_seq advancing, so no window can be computed.
        lines = klippy_log().split("\n")
        keep = [ln for ln in lines if not ("Stats 15503" in ln and
                                          ln.split("Stats ")[1][:8] in
                                          ("155034.2", "155035.2", "155036.2", "155037.2"))]
        r = run_full(log="\n".join(keep), profile=fx.measured_profile())
        self.assertIn("R10", codes(r.out))
        self.assertIn("R18", codes(r.out))
        self.assertEqual(r.code, 2)

    def test_r13_only_exits_3_and_still_writes_the_preview(self):
        # N14: resumable on paper, profile not verified -> exit 3.
        p = resumable_profile()
        p["envelope"]["z_max"]["source"] = "config"
        r = run_full("--preview", "{tmp}/preview.gcode", profile=p)
        self.assertEqual(r.code, 3, r.out)
        self.assertEqual(sorted(set(codes(r.out))), ["PIN_AT_BOOT", "R13"])
        self.assertIn("RESULT: resumable on paper; motion blocked", r.out)
        self.assertIsNotNone(r.preview)
        self.assertIn("NOT VERIFIED", r.preview.split("\n")[0])

    def test_r13_and_r02_is_refused_not_exit_3(self):
        p = resumable_profile()
        p["envelope"]["z_max"]["source"] = "config"
        p["probe_spots"] = []
        r = run_full(profile=p)
        self.assertEqual(r.code, 2)
        self.assertIn("R02", codes(r.out))


class Preview(unittest.TestCase):
    """The preview of a resumable plan (measured profile, 5 s budget)."""

    @classmethod
    def setUpClass(cls):
        cls.r = run_full("--preview", "{tmp}/preview.gcode", profile=resumable_profile())
        assert cls.r.preview is not None, cls.r.out
        cls.lines = preview_lines(cls.r.preview)

    def test_labelled_not_verified(self):
        self.assertTrue(self.lines[0].startswith("; print-rescue resume PREVIEW"))
        self.assertIn("NOT VERIFIED", self.lines[0])
        self.assertIn("do not print", self.lines[0])

    def _gate(self):
        return next(i for i, ln in enumerate(self.lines) if ln.startswith(resume_file.GATE_MARK))

    def test_no_absolute_z_before_the_gate(self):
        # N5: Z is unknown until it is re-referenced: only relative up moves.
        self.assertEqual(resume_file.check_generated(self.lines[:self._gate()]), [])
        lower = next(i for i, ln in enumerate(self.lines) if i > self._gate() and
                     ln.startswith("G1 Z"))
        self.assertIn("absolute, after the gate", self.lines[lower])

    def test_relative_lift_is_bracketed(self):
        i = at(self.lines, "G91")
        self.assertTrue(self.lines[i + 1].startswith("G1 Z3 F300"))
        self.assertEqual(self.lines[i + 2], "G90")

    def test_retraction_and_heat_order(self):
        # N6: retract before the approach; no heat before the off-part park.
        retract = next(i for i, ln in enumerate(self.lines) if ln.startswith("G1 E-"))
        park = at(self.lines, "G1 X15 Y205 F6000")
        bed = at(self.lines, "M190 S60")
        nozzle = at(self.lines, "M109 S220")
        self.assertLess(retract, park)
        self.assertLess(park, bed)
        self.assertLess(park, nozzle)

    def test_bed_mesh_profile_loaded(self):
        # N8: the docs promise the mesh restore; the preview emits it.
        self.assertIn("BED_MESH_PROFILE LOAD=default", self.lines)

    def test_preamble_passes_the_allow_list(self):
        # N11: the generated part (before the original file) is allow-listed.
        end = at(self.lines, "; ---- original file from line 201925 ----")
        self.assertEqual(resume_file.check_generated(self.lines[:end]), [])

    def test_preview_not_overwritten_without_force(self):
        r = run_full("--preview", "{tmp}/preview.gcode", profile=resumable_profile(),
                     preexisting_preview="OLD")
        self.assertEqual(r.code, 1)
        self.assertIn("--force-preview", r.err)
        self.assertEqual(r.preview, "OLD")

    def test_force_preview_overwrites(self):
        r = run_full("--preview", "{tmp}/preview.gcode", "--force-preview",
                     profile=resumable_profile(), preexisting_preview="OLD")
        self.assertEqual(r.code, 0, r.out)
        self.assertTrue(r.preview.startswith("; print-rescue resume PREVIEW"))


class Refusals(unittest.TestCase):
    """S2 and N3: no evidence, no resume line, never exit 0."""

    @staticmethod
    def _log_without(*fragments):
        return "\n".join(ln for ln in klippy_log().split("\n")
                         if not any(f in ln for f in fragments))

    def test_missing_exit_position_refused(self):
        r = run_full(log=self._log_without("Exiting SD card print"),
                     profile=fx.measured_profile())
        self.assertNotEqual(r.code, 0)
        self.assertEqual(r.code, 2)
        self.assertIn("R18", codes(r.out))
        self.assertIn("RESULT: REFUSED", r.out)
        self.assertIsNone(r.preview)

    def test_missing_exit_position_still_runs_profile_checks(self):
        # The shipped template profile: R02 and R13 must still be reported.
        r = run_full(log=self._log_without("Exiting SD card print"))
        # The profile checks still run before the refusal (no early return).
        # The pin check needs a stop window, so it cannot run here.
        self.assertIn("R02", codes(r.out))
        self.assertIn("R13", codes(r.out))

    def test_no_stats_lines_refused(self):
        r = run_full(log=self._log_without("Stats "), profile=fx.measured_profile())
        self.assertEqual(r.code, 2)
        self.assertIn("R18", codes(r.out))
        self.assertIn("R10", codes(r.out))

    def test_manual_line_without_a_window_is_refused(self):
        # N3: a manual line is never accepted unchecked.
        r = run_full("--line", "201955", log=self._log_without("Stats "),
                     profile=resumable_profile())
        self.assertEqual(r.code, 2)
        self.assertIn("R18", codes(r.out))
        self.assertIn("manual", r.out)
        self.assertNotRegex(r.out, r"resume line \d+ at X")
        self.assertIsNone(r.preview)

    def test_manual_line_without_exit_position_is_refused(self):
        r = run_full("--line", "201955", log=self._log_without("Exiting SD card print"),
                     profile=resumable_profile())
        self.assertEqual(r.code, 2)
        self.assertNotRegex(r.out, r"resume line \d+ at X")

    def test_exit_position_before_any_motion_is_refused(self):
        # The dispatched line is not in the plan: no stop window, no traceback.
        log = klippy_log().replace("Exiting SD card print (position 5404182)",
                                   "Exiting SD card print (position 1)")
        r = run_full(log=log, profile=fx.measured_profile())
        self.assertNotIn("Traceback", r.err)
        self.assertEqual(r.code, 2)
        self.assertIn("R18", codes(r.out))

    def test_file_without_motion_is_refused(self):
        with tempfile.TemporaryDirectory() as d:
            g = os.path.join(d, "empty.gcode")
            with open(g, "w") as fh:
                fh.write(";LAYER_CHANGE\n;Z:0.2\nG90\n")
            r = run_full(gcode=g, profile=fx.measured_profile())
        self.assertNotIn("Traceback", r.err)
        self.assertEqual(r.code, 2)
        self.assertIn("R18", codes(r.out))

    def test_finish_refuses_without_a_line(self):
        out = io.StringIO()
        self.assertEqual(plan._finish([], lambda t="": print(t, file=out), None), 2)
        self.assertIn("R18", out.getvalue())

    def test_finish_refuses_motion_only_finding_without_a_line(self):
        out = io.StringIO()
        code = plan._finish([rf.Finding("R13", "x")], lambda t="": print(t, file=out), None)
        self.assertEqual(code, 2)

    def test_finish_is_zero_only_with_a_chosen_line(self):
        out = io.StringIO()
        ch = sp.Choice(201955, 1.0, (1.0, 2.0), 1.0, 1.0, False, False)
        self.assertEqual(plan._finish([], lambda t="": print(t, file=out), ch), 0)


class Inputs(unittest.TestCase):
    """N2 and S1: malformed input is a clean message and exit 1, never a traceback."""

    def assertClean(self, r, code=1):
        self.assertEqual(r.code, code, r.out + r.err)
        self.assertNotIn("Traceback", r.err)
        self.assertNotIn("Traceback", r.out)

    def test_negative_margin_exits_1(self):
        p = fx.measured_profile()
        p["stop_estimation"]["margin_s"] = -0.5
        r = run_full(profile=p)
        self.assertClean(r)
        self.assertIn("margin_s", r.err)

    def test_nan_margin_exits_1(self):
        p = fx.measured_profile()
        p["stop_estimation"]["margin_s"] = float("nan")
        r = run_full(profile=p)
        self.assertClean(r)
        self.assertIn("margin_s", r.err)

    def test_string_margin_exits_1(self):
        p = fx.measured_profile()
        p["stop_estimation"]["margin_s"] = "0.5"
        self.assertClean(run_full(profile=p))

    def test_missing_probe_area_exits_1(self):
        p = fx.measured_profile()
        del p["envelope"]["probe_area"]
        r = run_full(profile=p)
        self.assertClean(r)
        self.assertIn("probe_area", r.err)

    def test_malformed_profile_json_exits_1(self):
        r = run_full(profile_text="{")
        self.assertClean(r)
        self.assertIn("profile", r.err)

    def test_profile_not_an_object_exits_1(self):
        self.assertClean(run_full(profile_text="[]"))

    def test_malformed_history_exits_1(self):
        with tempfile.TemporaryDirectory() as d:
            h = os.path.join(d, "h.json")
            with open(h, "w") as fh:
                fh.write("[1, 2]")
            r = run_full("--history", h, profile=fx.measured_profile())
        self.assertClean(r)
        self.assertIn("history", r.err)

    def test_malformed_queue_exits_1(self):
        with tempfile.TemporaryDirectory() as d:
            q = os.path.join(d, "q.json")
            with open(q, "w") as fh:
                fh.write("5")
            self.assertClean(run_full("--queue", q, profile=fx.measured_profile()))

    def test_missing_optional_inputs_exit_1(self):
        for flag in ("--kernel-log", "--printer-cfg", "--queue"):
            with self.subTest(flag=flag):
                self.assertClean(run_full(flag, "/nonexistent/print-rescue-input",
                                          profile=fx.measured_profile()))

    def test_invalid_now_exits_1(self):
        self.assertClean(run_full("--now", "banana", profile=fx.measured_profile()))

    def test_directory_as_gcode_is_r07(self):
        with tempfile.TemporaryDirectory() as d:
            r = run_full(gcode=d, profile=fx.measured_profile())
        self.assertNotIn("Traceback", r.err)
        self.assertEqual(r.code, 2)
        self.assertIn("R07", codes(r.out))

    def test_binary_gcode_no_traceback(self):
        with tempfile.TemporaryDirectory() as d:
            g = os.path.join(d, "bin.gcode")
            with open(g, "wb") as fh:
                fh.write(bytes(range(256)) * 40 + b"\nG1 X1e300 Y1\n")
            r = run_full(gcode=g, profile=fx.measured_profile())
        self.assertNotIn("Traceback", r.err)
        self.assertIn(r.code, (0, 1, 2, 3))

    def test_huge_arc_exits_1(self):
        with tempfile.TemporaryDirectory() as d:
            g = os.path.join(d, "arc.gcode")
            with open(g, "w") as fh:
                fh.write("M83\nG1 X1 Y1 F600\nG2 X2 Y0 I1e300 J0 E1\n")
            r = run_full(gcode=g, profile=fx.measured_profile())
        self.assertClean(r)
        self.assertIn("arc", r.err)

    def test_non_utf8_and_non_finite_log_no_traceback(self):
        log = klippy_log().replace("sd_pos=5398158", "sd_pos=inf")
        r = run_full(log_text=log.encode("utf-8") + b"\n\xff\xfe\n",
                     profile=fx.measured_profile())
        self.assertNotIn("Traceback", r.err)
        self.assertIn(r.code, (0, 1, 2))


def command_words(text):
    """The first word of every line that carries code (not a comment)."""
    words = []
    for ln in text.split("\n"):
        tokens = ln.split(";", 1)[0].split()
        if tokens:
            words.append(tokens[0])
    return words


class SourceInjection(unittest.TestCase):
    """S1: a source name cannot put a command on a line of its own, and a
    hostile name is a hard refusal (exit 2), never a preview."""

    NORMAL = "ps2-slim-wall-mount_PLA_3h15m.gcode"
    HOSTILE = ["ps2-slim-wall-mount_PLA_3h15m.gcode\nG28 ; injected",
               "ps2-slim-wall-mount_PLA_3h15m.gcode\nG1 Z-50 ; injected",
               "ps2-slim-wall-mount_PLA_3h15m.gcode\rG28 ; injected",
               "ps2\nG28.gcode"]

    def test_hostile_source_is_refused_with_exit_2(self):
        for name in self.HOSTILE:
            with self.subTest(name=repr(name)):
                r = run_full("--preview", "{tmp}/preview.gcode", gmap_source=name,
                             profile=resumable_profile())
                self.assertEqual(r.code, 2, r.out + r.err)
                self.assertIn("R17", codes(r.out))
                self.assertIn("RESULT: REFUSED", r.out)
                self.assertIsNone(r.preview)
                self.assertNotIn("Traceback", r.err)

    def test_normal_name_still_resumes_without_a_g28(self):
        r = run_full("--preview", "{tmp}/preview.gcode", gmap_source=self.NORMAL,
                     profile=resumable_profile())
        self.assertEqual(r.code, 0, r.out + r.err)
        self.assertIsNotNone(r.preview)
        self.assertNotIn("G28", command_words(r.preview))


def _copy_bytes(src, dst):
    with open(src, "rb") as fh:
        data = fh.read()
    with open(dst, "wb") as fh:
        fh.write(data)


def _read_bytes(path):
    with open(path, "rb") as fh:
        return fh.read()


CORE = ["--gcode", "--gcode-map", "--log", "--profile"]
EVIDENCE = CORE + ["--history", "--queue", "--printer-cfg"]
OPTIONAL = ["--history", "--queue", "--printer-cfg", "--kernel-log"]
FILE_NAMES = {"--gcode": "print.gcode.gz", "--gcode-map": "map.json",
              "--log": "klippy.log", "--profile": "profile.json",
              "--history": "history.json", "--queue": "queue.json",
              "--printer-cfg": "printer.cfg", "--kernel-log": "kernel.log"}
SOURCES = {"--gcode": fx.GCODE, "--gcode-map": fx.GMAP, "--log": fx.KLIPPY,
           "--history": fx.HISTORY, "--kernel-log": fx.KERNEL}


def copied_inputs(d, flags):
    """Copies of the named inputs, so a wrong write can never touch a fixture.
    The profile copy is the resumable one, so the plan exits 0 on its own."""
    paths = {}
    for flag in flags:
        dst = os.path.join(d, FILE_NAMES[flag])
        if flag == "--profile":
            with open(dst, "w", encoding="utf-8") as fh:
                json.dump(resumable_profile(), fh)
        elif flag == "--queue":
            with open(dst, "w", encoding="utf-8") as fh:
                json.dump({"result": {"queue_state": "ready", "queued_jobs": []}}, fh)
        elif flag == "--printer-cfg":
            _copy_bytes(fx.REPO_PRINTER_CFG, dst)
        else:
            _copy_bytes(SOURCES[flag], dst)
        paths[flag] = dst
    return paths


def argv_for(paths, *extra):
    argv = []
    for flag, path in paths.items():
        argv += [flag, path]
    return argv + ["--now", "2026-11-07T09:30:00Z"] + list(extra)


def run_argv(argv):
    """plan.run on an explicit argument list: (exit code, stdout, stderr)."""
    out, err = io.StringIO(), io.StringIO()
    ns = plan.build_parser().parse_args(argv)
    with contextlib.redirect_stderr(err):
        code = plan.run(ns, out=out)
    return code, out.getvalue(), err.getvalue()


class PreviewTarget(unittest.TestCase):
    """S2: --preview never overwrites an input, a symlink, or a hard link to one,
    and a replaced preview is swapped in by rename."""

    def test_no_input_can_be_the_preview_target(self):
        with tempfile.TemporaryDirectory() as d:
            paths = copied_inputs(d, CORE + OPTIONAL)
            for flag, path in paths.items():
                with self.subTest(flag=flag):
                    before = _read_bytes(path)
                    code, out, err = run_argv(argv_for(paths, "--preview", path,
                                                       "--force-preview"))
                    self.assertEqual(code, 1, out + err)
                    self.assertNotIn("Traceback", err)
                    self.assertEqual(_read_bytes(path), before)

    def test_hard_link_to_an_input_is_refused(self):
        with tempfile.TemporaryDirectory() as d:
            paths = copied_inputs(d, CORE)
            alias = os.path.join(d, "alias.log")
            os.link(paths["--log"], alias)
            before = _read_bytes(paths["--log"])
            code, out, err = run_argv(argv_for(paths, "--preview", alias, "--force-preview"))
            self.assertEqual(code, 1, out + err)
            self.assertEqual(_read_bytes(paths["--log"]), before)

    def test_symlink_is_refused_with_and_without_force(self):
        with tempfile.TemporaryDirectory() as d:
            paths = copied_inputs(d, CORE)
            target = os.path.join(d, "target.txt")
            with open(target, "w", encoding="utf-8") as fh:
                fh.write("OLD")
            link = os.path.join(d, "resume.gcode")
            os.symlink(target, link)
            for extra in ([], ["--force-preview"]):
                with self.subTest(extra=extra):
                    code, out, err = run_argv(argv_for(paths, "--preview", link, *extra))
                    self.assertEqual(code, 1, out + err)
                    self.assertIn("symbolic link", err)
                    self.assertTrue(os.path.islink(link))
                    self.assertEqual(_read_bytes(target), b"OLD")

    def test_dangling_symlink_is_refused_with_force(self):
        with tempfile.TemporaryDirectory() as d:
            paths = copied_inputs(d, CORE)
            missing = os.path.join(d, "missing.txt")
            link = os.path.join(d, "resume.gcode")
            os.symlink(missing, link)
            code, out, err = run_argv(argv_for(paths, "--preview", link, "--force-preview"))
            self.assertEqual(code, 1, out + err)
            self.assertFalse(os.path.exists(missing))

    def test_force_replaces_a_regular_file_by_rename(self):
        with tempfile.TemporaryDirectory() as d:
            paths = copied_inputs(d, EVIDENCE)
            pv = os.path.join(d, "preview.gcode")
            with open(pv, "w", encoding="utf-8") as fh:
                fh.write("OLD")
            old_inode = os.stat(pv).st_ino
            code, out, err = run_argv(argv_for(paths, "--preview", pv, "--force-preview"))
            self.assertEqual(code, 0, out + err)
            with open(pv, encoding="utf-8") as fh:
                self.assertTrue(fh.read().startswith("; print-rescue resume PREVIEW"))
            self.assertNotEqual(os.stat(pv).st_ino, old_inode)
            self.assertEqual(sorted(os.listdir(d)),
                             sorted([os.path.basename(p) for p in paths.values()]
                                    + ["preview.gcode"]))

    def test_failed_replace_keeps_the_old_preview_and_leaves_no_temp_file(self):
        with tempfile.TemporaryDirectory() as d:
            paths = copied_inputs(d, CORE)
            pv = os.path.join(d, "preview.gcode")
            with open(pv, "w", encoding="utf-8") as fh:
                fh.write("OLD")
            with mock.patch.object(plan.os, "replace",
                                   side_effect=OSError(28, "No space left on device")):
                code, out, err = run_argv(argv_for(paths, "--preview", pv, "--force-preview"))
            self.assertIn("preview not written", out)
            self.assertNotIn("Traceback", err)
            with open(pv, encoding="utf-8") as fh:
                self.assertEqual(fh.read(), "OLD")
            self.assertEqual(sorted(os.listdir(d)),
                             sorted([os.path.basename(p) for p in paths.values()]
                                    + ["preview.gcode"]))


class TailWarnings(unittest.TestCase):
    def tail(self, text):
        import gcode_scan as gs
        r = gs.scan([(i + 1, 0, ln) for i, ln in enumerate(text.split("\n"))])
        return gs.tail_findings(r, 1, None)

    def test_relative_xyz_in_the_tail_is_reported(self):
        got = plan.tail_warnings(self.tail("G1 X1 Y1 E1\nG91\nG1 Z1\nG90"))
        self.assertEqual([c for c, _ in got], ["R23"])
        self.assertIn("line 2", got[0][1])

    def test_absolute_extrusion_in_the_tail_is_still_reported(self):
        got = plan.tail_warnings(self.tail("G1 X1 Y1 E1\nM82"))
        self.assertEqual([c for c, _ in got], ["R05"])

    def test_clean_tail_has_no_warning(self):
        self.assertEqual(plan.tail_warnings(self.tail("G1 X1 Y1 E1\nG1 X2 E1")), [])


class HistoryJob(unittest.TestCase):
    def job(self, status, name="a.gcode"):
        return {"filename": name, "status": status, "metadata": {"size": 1}}

    def test_no_shutdown_like_job_is_not_a_match(self):
        data = {"result": {"jobs": [self.job("completed"), self.job("in_progress")]}}
        self.assertIsNone(plan._history_job(data))

    def test_shutdown_like_statuses_match(self):
        for st in ("klippy_shutdown", "klippy_disconnect", "error", "cancelled",
                   "interrupted"):
            with self.subTest(status=st):
                data = {"result": {"jobs": [self.job("completed", "old.gcode"),
                                            self.job(st)]}}
                self.assertEqual(plan._history_job(data)["filename"], "a.gcode")


class NoTraceback(unittest.TestCase):
    def test_unexpected_attribute_or_memory_error_is_a_message_not_a_traceback(self):
        from unittest import mock
        for exc in (AttributeError("x"), MemoryError()):
            with self.subTest(exc=type(exc).__name__):
                with mock.patch.object(plan, "_run", side_effect=exc):
                    err = io.StringIO()
                    with contextlib.redirect_stderr(err):
                        code = plan.run(None, out=io.StringIO())
                self.assertEqual(code, plan.EXIT_USAGE)
                self.assertIn("error:", err.getvalue())


class Cli(unittest.TestCase):
    def test_usage_error_exit_1(self):
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(plan.main(["--gcode", "x"]), 1)

    def test_force_preview_needs_preview(self):
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(plan.main(["--gcode", fx.GCODE, "--gcode-map", fx.GMAP,
                                        "--log", fx.KLIPPY, "--profile", fx.PROFILE,
                                        "--force-preview"]), 1)

    def test_missing_gcode_is_r07(self):
        out = io.StringIO()
        ns = plan.build_parser().parse_args(
            ["--gcode", "/nonexistent/x.gcode", "--log", fx.KLIPPY,
             "--profile", fx.PROFILE])
        self.assertEqual(plan.run(ns, out=out), 2)
        self.assertIn("R07", codes(out.getvalue()))


class ParseTimestamp(unittest.TestCase):
    def test_offset_is_converted_to_utc(self):
        a = plan._parse_ts("2026-11-07T10:30:00+01:00")
        b = plan._parse_ts("2026-11-07T09:30:00Z")
        self.assertEqual(a, b)


if __name__ == "__main__":
    unittest.main()
