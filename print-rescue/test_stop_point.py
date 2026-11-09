import unittest

import gcode_scan as gs
import planner
import rescue_fixtures as fx
import stop_point as sp
from gcode_scan import Move

STATS = ("2026-11-07T09:09:45.731773044Z INFO:root:Stats 155033.2: gcodein=0 "
         "mcu: mcu_awake=0.024 bytes_write=28097404 bytes_read=4945189 "
         "send_seq=493669 receive_seq=493669 retransmit_seq=422729 freq=71998773 "
         "PICO_MMU: send_seq=9044 receive_seq=9044 heater_bed: target=60 "
         "sd_pos=5398158 PRINTER_MCU: temp=43.5 print_time=8761.661 "
         "buffer_time=1.037 print_stall=0 extruder: target=220")


def synthetic_log(rows, reason="Lost communication with MCU 'mcu'", after=8.0):
    """rows: (eventtime, print_time, buffer_time, send_seq, receive_seq, bytes_read, sd)"""
    out = []
    for et, pt, bt, ss, rs, br, sd in rows:
        out.append("INFO:root:Stats %.1f: gcodein=0 mcu: bytes_read=%d send_seq=%d "
                   "receive_seq=%d sd_pos=%d print_time=%.3f buffer_time=%.3f"
                   % (et, br, ss, rs, sd, pt, bt))
    out.append("ERROR:root:Transition to shutdown state: %s" % reason)
    out.append("INFO:root:Stats 9.0: gcodein=0 mcu: bytes_read=1 send_seq=1 "
               "receive_seq=1 print_time=%.3f buffer_time=0.5" % after)
    out.append("INFO:root:Exiting SD card print (position 100)")
    return out


class ParseStats(unittest.TestCase):
    def test_sections_and_globals(self):
        s = sp.parse_stats(STATS)
        self.assertEqual(s.eventtime, 155033.2)
        self.assertEqual(s.timestamp, "2026-11-07T09:09:45.731773044Z")
        self.assertEqual((s.print_time, s.buffer_time, s.sd_pos), (8761.661, 1.037, 5398158))
        # PICO_MMU's counters must not leak into the main MCU's
        self.assertEqual(s.mcu["send_seq"], 493669)
        self.assertEqual(s.mcu["receive_seq"], 493669)
        self.assertEqual(s.mcu["bytes_read"], 4945189)

    def test_plain_klippy_log_without_timestamp(self):
        s = sp.parse_stats("Stats 12.5: gcodein=0 mcu: send_seq=1 receive_seq=1 print_time=3.0")
        self.assertIsNone(s.timestamp)
        self.assertEqual(s.print_time, 3.0)

    def test_not_stats(self):
        self.assertIsNone(sp.parse_stats("INFO:root:Starting Klippy..."))

    def test_non_finite_and_negative_fields_are_ignored(self):
        # A malformed log must not crash the analysis (int(inf), int(nan)).
        s = sp.parse_stats("INFO:root:Stats 12.5: gcodein=0 mcu: send_seq=1 "
                           "receive_seq=1 print_time=nan buffer_time=inf sd_pos=inf")
        self.assertIsNone(s.print_time)
        self.assertIsNone(s.buffer_time)
        self.assertIsNone(s.sd_pos)
        s = sp.parse_stats("INFO:root:Stats 12.5: gcodein=0 mcu: receive_seq=1 sd_pos=-7")
        self.assertIsNone(s.sd_pos)


class IncidentEvidence(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ev, cls.res, cls.timed = fx.incident()
        cls.w = sp.compute_window(cls.ev, cls.timed, fx.LAST_LINE, fx.SD_LINE_GOOD)

    def test_freeze_found(self):
        ev = self.ev
        self.assertTrue(ev.comm_loss)
        self.assertTrue(ev.freeze)
        self.assertEqual(ev.good.mcu["receive_seq"], 493669)
        self.assertEqual(ev.good.timestamp, "2026-11-07T09:09:45.731773044Z")
        self.assertEqual(ev.next_after_good.mcu["send_seq"], 493672)
        self.assertEqual((ev.good.print_time, ev.good.buffer_time), (8761.661, 1.037))
        self.assertEqual(ev.p_end, 8766.891)
        self.assertEqual(ev.p_end_source, "first Stats after shutdown")
        self.assertAlmostEqual(ev.p_end - ev.good.print_time, 5.23, places=2)
        self.assertAlmostEqual(ev.interval, 1.0)
        self.assertEqual(ev.reactor_busy_before, 0)

    def test_shutdown_markers(self):
        ev = self.ev
        self.assertEqual(ev.exit_position, fx.EXIT_POSITION)
        self.assertEqual(ev.sdcard_position, 5404152)
        self.assertEqual(ev.gcode_state["last_position"][:3], [184.976, 143.031, 20.625])
        self.assertEqual(ev.gcode_state["homing_position"][2], 0.025)
        self.assertFalse(ev.gcode_state["absolute_extrude"])

    def test_window_is_the_union_of_both_anchors(self):
        # Conservative combination (council policy after the S1 finding):
        # earliest = min of the two earliest stops, latest = max of the two latest.
        w, d = self.w, self.w.detail
        self.assertEqual(w.anchor, "sd+end")
        self.assertAlmostEqual(w.earliest_t,
                               min(d["sd_anchor_earliest_t"], d["end_anchor_earliest_t"]))
        self.assertAlmostEqual(w.latest_t,
                               max(d["sd_anchor_latest_t"], d["end_anchor_latest_t"]))
        # The end anchor (the plan's formula) is the one that sets both ends here.
        self.assertLess(d["end_anchor_earliest_t"], d["sd_anchor_earliest_t"])
        self.assertGreater(d["end_anchor_latest_t"], d["sd_anchor_latest_t"])
        self.assertEqual((w.earliest_line, w.latest_line), (201962, 202298))

    def test_end_anchor_numbers(self):
        # The plan's formula (end anchor): 4.23 s .. 6.27 s plus lookahead.
        d = self.w.detail
        self.assertAlmostEqual(self.w.file_end_t - d["end_anchor_latest_t"], 4.23, places=2)
        self.assertAlmostEqual(self.w.file_end_t - d["end_anchor_earliest_t"],
                               6.267 + d["lookahead_end_s"], places=3)
        self.assertGreater(d["lookahead_end_s"], 1.5)   # line 202396 is one long move

    def test_anchors_agree_on_endpoints(self):
        # Endpoint differences are 0.56 s and 0.62 s: below the 1 s R10 limit.
        self.assertIsNone(sp.anchors_disagree(self.w))

    def test_sd_anchor_keeps_its_model_free_bound(self):
        # The sd anchor alone: motion for lines dispatched after the link froze
        # was never sent, so its latest stop is not after the first frozen sample.
        d = self.w.detail
        self.assertLessEqual(sp.line_at_t(self.timed, d["sd_anchor_latest_t"]),
                             fx.SD_LINE_FIRST_FROZEN)
        self.assertLess(d["sd_anchor_earliest_t"], d["sd_anchor_latest_t"])

    def test_overlap_above_the_budget_warns_with_the_extra_material(self):
        # Window + margin 0.5 s + 3.0 s budget: the incident needs 4.95 s. That
        # is a warning (R12) with the extra material, not a refusal: the
        # alternative would be a gap in the part.
        c = sp.choose_resume(self.w, self.timed, fx.CUT_LAYER_START, 0.5, 1.0, 3.0)
        self.assertIsNone(c.refusal)
        self.assertIn("budget", c.warning)
        self.assertIn("mm3", c.warning)
        self.assertGreater(c.extra_filament_mm, 0)
        self.assertEqual(c.line, 201925)
        self.assertAlmostEqual(c.overlap_s, 4.95, places=2)

    def test_resume_choice_with_five_second_budget(self):
        c = sp.choose_resume(self.w, self.timed, fx.CUT_LAYER_START, 0.5, 1.0, 5.0)
        self.assertIsNone(c.refusal)
        self.assertEqual(c.line, 201925)
        self.assertGreaterEqual(c.line, fx.CUT_LAYER_START)
        self.assertLessEqual(c.t, self.w.earliest_t - 0.5 + 1e-9)       # never a gap
        self.assertLessEqual(c.overlap_s, 5.0)
        self.assertGreater(c.overlap_mm, 50.0)
        self.assertAlmostEqual(c.overlap_s, self.w.latest_t - c.t)

    def test_auto_choice_is_before_both_earliest_stops_minus_margin(self):
        # Regression for S1: the old code chose from the sd anchor only, which put
        # the resume line after the end anchor's earliest stop minus the margin.
        d = self.w.detail
        c = sp.choose_resume(self.w, self.timed, fx.CUT_LAYER_START, 0.5, 1.0, 5.0)
        self.assertLessEqual(c.t, d["end_anchor_earliest_t"] - 0.5 + 1e-9)
        self.assertLessEqual(c.t, d["sd_anchor_earliest_t"] - 0.5 + 1e-9)

    def test_auto_choice_respects_other_margins(self):
        for margin in (0.0, 0.5, 2.0):
            c = sp.choose_resume(self.w, self.timed, fx.CUT_LAYER_START, margin, 1.0, 9.0)
            self.assertIsNone(c.refusal)
            self.assertLessEqual(c.t, self.w.earliest_t - margin + 1e-9, margin)

    def test_negative_or_non_finite_margin_is_rejected(self):
        for bad in (-0.5, float("nan"), float("inf")):
            with self.assertRaises(ValueError):
                sp.choose_resume(self.w, self.timed, fx.CUT_LAYER_START, bad, 1.0, 5.0)

    def test_no_line_meets_the_margin_inside_the_cut_layer(self):
        # The target falls 0.2 s before the cut layer's first line: no line in
        # the layer starts early enough, so the auto path must refuse.
        layer_t = sp.line_start_t(self.timed, fx.CUT_LAYER_START)
        margin = self.w.earliest_t - layer_t + 0.2
        self.assertGreater(margin, 0)
        c = sp.choose_resume(self.w, self.timed, fx.CUT_LAYER_START, margin, 1.0, 9.0)
        self.assertIsNotNone(c.refusal)
        self.assertIn("margin", c.refusal)

    def test_manual_estimates_against_the_combined_window(self):
        # Line 202282 (the hand-built resume) starts 0.18 s INSIDE the latest
        # possible stop, not after it. Lines 202314 and 202335 do lie after it.
        t = sp.line_start_t(self.timed, fx.MANUAL_RESUME_LINE)
        self.assertLess(t, self.w.latest_t)
        self.assertAlmostEqual(self.w.latest_t - t, 0.176, delta=0.01)
        for line in (202314, 202335):
            self.assertGreater(sp.line_start_t(self.timed, line), self.w.latest_t)
        # It is still refused: it starts after the earliest possible stop (gap).
        c = sp.choose_resume(self.w, self.timed, fx.CUT_LAYER_START, 0.5, 1.0, 5.0,
                             manual_line=fx.MANUAL_RESUME_LINE)
        self.assertTrue(c.manual)
        self.assertIn("gap", c.refusal)

    def test_manual_line_inside_budget_is_accepted(self):
        c = sp.choose_resume(self.w, self.timed, fx.CUT_LAYER_START, 0.5, 1.0, 5.0,
                             manual_line=self.w.earliest_line - 5)
        self.assertIsNone(c.refusal)
        self.assertGreater(c.overlap_s, 0)

    def test_tighter_overlap_budget_still_only_warns(self):
        c = sp.choose_resume(self.w, self.timed, fx.CUT_LAYER_START, 0.5, 1.0, 2.0)
        self.assertIsNone(c.refusal)
        self.assertIn("budget", c.warning)

    def test_within_budget_has_no_warning(self):
        c = sp.choose_resume(self.w, self.timed, fx.CUT_LAYER_START, 0.5, 1.0, 5.0)
        self.assertIsNone(c.warning)

    def test_absurd_overlap_refuses_with_r21(self):
        c = sp.choose_resume(self.w, self.timed, fx.CUT_LAYER_START, 0.5, 1.0, 2.0,
                             absurd_overlap_s=4.0)
        self.assertEqual(c.refusal_code, "R21")
        self.assertIn("absurd", c.refusal)

    def test_manual_line_without_motion_says_which_line_is_used(self):
        have = {tm.line for tm in self.timed}
        gap = next(n for n in range(self.w.earliest_line - 40, self.w.earliest_line)
                   if n not in have)
        c = sp.choose_resume(self.w, self.timed, fx.CUT_LAYER_START, 0.5, 1.0, 5.0,
                             manual_line=gap)
        self.assertIn("no planned motion", c.note)
        self.assertGreater(c.line, gap)

    def test_gap_type_refusal_uses_r22(self):
        c = sp.choose_resume(self.w, self.timed, fx.CUT_LAYER_START, 0.5, 1.0, 5.0,
                             manual_line=self.w.earliest_line + 5)
        self.assertEqual(c.refusal_code, "R22")

    def test_end_anchor_fallback_without_sd(self):
        w = sp.compute_window(self.ev, self.timed, fx.LAST_LINE, None)
        self.assertEqual(w.anchor, "end")
        self.assertAlmostEqual(w.walkback_latest_s, 4.23, places=2)

    def test_planner_agreement(self):
        spread, pts = sp.planner_agreement(
            self.ev, self.timed, lambda b: gs.line_at_byte(self.res, b))
        self.assertLess(spread, 0.5)
        self.assertEqual(pts[-1][0], fx.SD_LINE_GOOD)


class Kernel(unittest.TestCase):
    def test_incident_usb_stalls(self):
        with open(fx.KERNEL, encoding="utf-8") as fh:
            stalls = sp.kernel_usb_stalls(fh.read().splitlines())
        # 10:09:45 local (UTC+1) is the 09:09:45 UTC freeze in the Stats lines
        self.assertEqual(stalls, [("Nov 07 07:51:48", -32), ("Nov 07 10:09:45", -32)])


class SnapBounds(unittest.TestCase):
    @staticmethod
    def timed():
        T = planner.Timed
        out = [T(1, 10.0, 0.0, 10.0)]
        # line 2: a travel arc cut into four segments, its source line starts at 10
        out += [T(2, 2.0, 10.0 + 2 * i, 12.0 + 2 * i, travel=True) for i in range(4)]
        out.append(T(3, 12.0, 18.0, 30.0, extruding=True))
        return out

    def window(self):
        return sp.Window("end", 20.0, 25.0, 30.0, 3, 3, 10.0, 5.0, 0.0)

    def test_snap_is_bounded_by_the_start_of_the_source_line(self):
        # The last arc segment starts at 16 s, inside the 3 s snap window below
        # the target (18 s), but the arc itself starts at 10 s: no snap.
        c = sp.choose_resume(self.window(), self.timed(), None, 2.0, 3.0, 20.0)
        self.assertEqual((c.line, c.snapped), (3, False))

    def test_snap_inside_the_bound_still_happens(self):
        c = sp.choose_resume(self.window(), self.timed(), None, 2.0, 9.0, 20.0)
        self.assertEqual((c.line, c.snapped), (2, True))
        self.assertAlmostEqual(c.t, 10.0)


class SyntheticEvidence(unittest.TestCase):
    ROWS = [(1.0, 10.0, 1.0, 10, 10, 100, 0), (2.0, 11.0, 1.0, 20, 20, 200, 0),
            (3.0, 12.0, 1.0, 23, 20, 200, 0), (4.0, 13.0, 1.0, 23, 20, 200, 0)]

    def test_non_comm_shutdown(self):
        ev = sp.analyze(synthetic_log(self.ROWS, reason="MCU 'mcu' shutdown: Timer too close"))
        self.assertFalse(ev.comm_loss)

    def test_second_shutdown_drops_the_first_dump(self):
        first = synthetic_log(self.ROWS)
        first[-1:-1] = ["INFO:root:Virtual sdcard (50): 'G1 X1\\n'",
                        "Upcoming (60): 'G1 X2\\n'",
                        "gcode state: absolute_coord=True"]
        second = synthetic_log(self.ROWS)
        second.insert(len(second) - 1, "INFO:root:Virtual sdcard (150): 'G1 X9\\n'")
        ev = sp.analyze(first + second)
        self.assertEqual(ev.sdcard_texts, [(150, "G1 X9\n")])
        self.assertIsNone(ev.sdcard_position)
        self.assertEqual(ev.gcode_state, {})
        self.assertEqual(ev.exit_position, 100)

    def test_stats_before_a_restart_do_not_supply_the_good_sample(self):
        old = synthetic_log(self.ROWS)[:-2]          # session 1: freeze, no shutdown
        rows2 = [(1.0, 10.0, 1.0, 10, 10, 100, 0), (2.0, 11.0, 1.0, 10, 10, 100, 0)]
        ev = sp.analyze(old + ["INFO:root:Starting Klippy..."] + synthetic_log(rows2))
        self.assertIsNone(ev.good)
        self.assertTrue(any("never advanced" in n for n in ev.notes))

    def test_a_later_normal_exit_does_not_replace_the_shutdown_exit(self):
        log = synthetic_log(self.ROWS) + ["INFO:root:Exiting SD card print (position 999)"]
        self.assertEqual(sp.analyze(log).exit_position, 100)

    def test_dump_after_a_restart_is_ignored(self):
        log = synthetic_log(self.ROWS)[:-1] + ["INFO:root:Starting Klippy...",
                                               "INFO:root:Exiting SD card print (position 999)"]
        self.assertIsNone(sp.analyze(log).exit_position)

    def test_no_freeze_visible(self):
        rows = [(1.0, 10.0, 1.0, 10, 10, 100, 0), (2.0, 11.0, 1.0, 20, 20, 200, 0)]
        ev = sp.analyze(synthetic_log(rows))
        self.assertIsNone(ev.good)
        self.assertTrue(any("no link freeze" in n for n in ev.notes))

    def test_freeze(self):
        ev = sp.analyze(synthetic_log(self.ROWS))
        self.assertEqual(ev.good.eventtime, 2.0)
        self.assertTrue(ev.freeze)
        self.assertEqual(ev.p_end, 8.0)

    def test_a_window_clamped_to_the_kept_moves_refuses_instead_of_skipping_print(self):
        # The scan keeps only the last two layers, so compute_window clamps the earliest stop to the
        # first kept move (t = 0 here) when the true one lay before it. With margin_s > 0 the resume
        # must refuse (R22) whether or not a layer marker is known, never land on a later line.
        items = [Move(line=i, start=(i * 100.0, 0, 0, 0), end=((i + 1) * 100.0, 0, 0, 1),
                      speed=100.0, max_velocity=100.0, accel=1e9, accel_to_decel=1e9,
                      square_corner_velocity=5.0, extruding=True) for i in range(1, 11)]
        timed = planner.plan(items)
        w = sp.Window("end", earliest_t=0.0, latest_t=6.0, file_end_t=10.0, earliest_line=1,
                      latest_line=7, walkback_earliest_s=10.0, walkback_latest_s=4.0,
                      lookahead_bound_s=0.0)
        for layer_start in (5, None):
            with self.subTest(layer_start=layer_start):
                c = sp.choose_resume(w, timed, layer_start, 0.5, 1.0, 20.0)
                self.assertIsNotNone(c.refusal)

    def test_layer_start_inside_the_margin_refuses(self):
        # 10 one-second moves; a layer starts at line 5 (t = 4 s).
        items = [Move(line=i, start=(i * 100.0, 0, 0, 0), end=((i + 1) * 100.0, 0, 0, 1),
                      speed=100.0, max_velocity=100.0, accel=1e9, accel_to_decel=1e9,
                      square_corner_velocity=5.0, extruding=True) for i in range(1, 11)]
        timed = planner.plan(items)
        w = sp.Window("sd", earliest_t=4.2, latest_t=6.0, file_end_t=10.0,
                      earliest_line=5, latest_line=7, walkback_earliest_s=5.8,
                      walkback_latest_s=4.0, lookahead_bound_s=0.0)
        # target 3.7 s is before the layer (4.0 s): the margin cannot be kept
        c = sp.choose_resume(w, timed, 5, 0.5, 1.0, 3.0)
        self.assertIsNotNone(c.refusal)
        self.assertIn("margin", c.refusal)
        self.assertEqual(c.line, 5)
        # earliest 4.6 s: target 4.1 s lies inside the layer, so it is accepted
        w.earliest_t = 4.6
        c = sp.choose_resume(w, timed, 5, 0.5, 1.0, 3.0)
        self.assertIsNone(c.refusal)
        self.assertEqual(c.line, 5)
        w.earliest_t = 3.5
        c = sp.choose_resume(w, timed, 5, 0.5, 1.0, 3.0)
        self.assertIn("before the cut layer", c.refusal)

    def test_anchor_gap_compares_window_endpoints(self):
        # Midpoints agree (15.0 vs 14.25 s) but the earliest endpoints differ by
        # 1.5 s: R10 must fire.
        w = sp.Window("sd+end", earliest_t=8.5, latest_t=20.0, file_end_t=30.0,
                      earliest_line=1, latest_line=2, walkback_earliest_s=21.5,
                      walkback_latest_s=10.0, lookahead_bound_s=0.0,
                      detail={"sd_anchor_earliest_t": 10.0, "sd_anchor_latest_t": 20.0,
                              "end_anchor_earliest_t": 8.5, "end_anchor_latest_t": 20.0})
        gap = sp.anchors_disagree(w)
        self.assertIsNotNone(gap)
        self.assertAlmostEqual(gap, 1.5)

    def test_anchor_gap_none_when_endpoints_agree(self):
        w = sp.Window("sd+end", earliest_t=10.0, latest_t=20.0, file_end_t=30.0,
                      earliest_line=1, latest_line=2, walkback_earliest_s=20.0,
                      walkback_latest_s=10.0, lookahead_bound_s=0.0,
                      detail={"sd_anchor_earliest_t": 10.0, "sd_anchor_latest_t": 20.0,
                              "end_anchor_earliest_t": 10.4, "end_anchor_latest_t": 19.6})
        self.assertIsNone(sp.anchors_disagree(w))


if __name__ == "__main__":
    unittest.main()
