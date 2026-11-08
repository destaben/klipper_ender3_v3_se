import math
import os
import unittest

import gcode_scan as gs
import rescue_fixtures as fx


def recs(text, first=1):
    """Records for an inline G-code snippet (offsets as in a real file)."""
    out, off = [], 0
    for i, line in enumerate(text.strip("\n").split("\n")):
        out.append((first + i, off, line))
        off += len(line) + 1
    return out


class Parse(unittest.TestCase):
    def test_classic(self):
        self.assertEqual(gs.parse("G1 X10 Y-2.5 E.4 F3000 ; c"),
                         ("G1", {"X": 10.0, "Y": -2.5, "E": 0.4, "F": 3000.0}))

    def test_leading_zero_and_no_spaces(self):
        self.assertEqual(gs.parse("G01X5Y6"), ("G1", {"X": 5.0, "Y": 6.0}))

    def test_extended(self):
        cmd, p = gs.parse("SET_PRESSURE_ADVANCE ADVANCE=0.08; Override")
        self.assertEqual((cmd, p), ("SET_PRESSURE_ADVANCE", {"ADVANCE": "0.08"}))

    def test_comment_only(self):
        self.assertEqual(gs.parse(";LAYER_CHANGE"), (None, {}))


class State(unittest.TestCase):
    def test_modes_feed_and_g92(self):
        r = gs.scan(recs("""
G90
M83
G1 X10 Y10 F6000
G91
G1 X5 E1
G90
G92 E0
M82
G1 E2
"""))
        st = r.state
        self.assertEqual(st.pos[:2], [15.0, 10.0])
        self.assertTrue(st.e_absolute)
        self.assertEqual(st.pos[3], 2.0)
        self.assertEqual(st.feed, 6000.0)
        kinds = [e.kind for e in r.events]
        self.assertIn("g91", kinds)
        self.assertIn("m82", kinds)

    def test_m221_scales_absolute_e_and_g92_e_alike(self):
        r = gs.scan(recs("""
M82
M221 S90
G92 E0
G1 X1 E10
G92 E20
G1 X2 E21
"""))
        # Klipper keeps E scaled by M221: G92 E20 is 18, then E21 adds 0.9.
        self.assertAlmostEqual(r.state.pos[3], 18.9)
        self.assertAlmostEqual(r.state.pos[3] * 100.0 / r.state.extrude_factor, 21.0)

    def test_m221_keeps_the_logical_e_coordinate(self):
        r = gs.scan(recs("""
M82
G92 E0
G1 X1 E10
M221 S90
"""))
        self.assertAlmostEqual(r.state.pos[3], 9.0)
        self.assertAlmostEqual(r.state.pos[3] * 100.0 / r.state.extrude_factor, 10.0)

    def test_limits_temps_fan_pa_objects(self):
        r = gs.scan(recs("""
SET_VELOCITY_LIMIT ACCEL=2500 ACCEL_TO_DECEL=1750
M204 S3000
M220 S90
M221 S95
M104 S220
M140 S60
M106 S128
SET_PRESSURE_ADVANCE ADVANCE=0.05 SMOOTH_TIME=0.03
EXCLUDE_OBJECT_DEFINE NAME=part_a CENTER=1,1
EXCLUDE_OBJECT_START NAME=part_a
EXCLUDE_OBJECT NAME=part_b
T0
"""))
        st = r.state
        self.assertEqual(st.accel, 3000.0)
        self.assertAlmostEqual(st.minimum_cruise_ratio, 0.3)
        self.assertAlmostEqual(st.accel_to_decel, 2100.0)
        self.assertEqual((st.speed_factor, st.extrude_factor), (90.0, 95.0))
        self.assertEqual((st.extruder_temp, st.bed_temp, st.fan), (220.0, 60.0, 128))
        self.assertEqual((st.pressure_advance, st.pa_smooth_time), (0.05, 0.03))
        self.assertEqual(st.current_object, "part_a")
        self.assertIn("PART_A", st.objects)
        self.assertEqual(st.excluded, ["part_b"])
        self.assertEqual(st.tool, 0)

    def test_m107_and_end_object(self):
        st = gs.scan(recs("M106 S255\nM107\nEXCLUDE_OBJECT_START NAME=a\n"
                          "EXCLUDE_OBJECT_END NAME=a")).state
        self.assertEqual(st.fan, 0)
        self.assertIsNone(st.current_object)

    def test_state_at_is_before_the_line(self):
        r = recs("G1 X1 F100\nG1 X2\nG1 X3")
        self.assertEqual(gs.state_at(r, 3).pos[0], 2.0)

    def test_retraction_tracking(self):
        st = gs.scan(recs("M83\nG1 X1 E1\nG1 E-.28\nG1 X2 E-.12")).state
        self.assertAlmostEqual(st.retracted, 0.4)
        st = gs.scan(recs("M83\nG1 X1 E1\nG1 E-.4\nG1 E.4")).state
        self.assertAlmostEqual(st.retracted, 0.0)

    def test_layers(self):
        r = gs.scan(recs(";LAYER_CHANGE\n;Z:0.2\nG1 Z.2\n;LAYER_CHANGE\n;Z:0.4"))
        self.assertEqual((r.state.layer, r.state.layer_z, r.state.layer_start_line),
                         (2, 0.4, 4))
        self.assertEqual(r.layer_starts, [(1, 1, 0.2), (4, 2, 0.4)])


class Arcs(unittest.TestCase):
    def test_quarter_arc_segments_like_klipper(self):
        # radius 10, quarter circle: 15.7 mm -> floor(15.7 / 1.0) = 15 segments
        r = gs.scan(recs("G90\nG1 X10 Y0 F600\nG3 X0 Y10 I-10 J0 E1"),
                    collect_moves_until=99)
        segs = [m for m in r.moves if m.line == 3]
        self.assertEqual(len(segs), 15)
        self.assertEqual(segs[-1].end[:2], (0.0, 10.0))
        for m in segs:
            self.assertAlmostEqual(math.hypot(m.end[0], m.end[1]), 10.0, places=6)
        self.assertAlmostEqual(sum(m.end[3] - m.start[3] for m in segs), 1.0)

    def test_full_circle_spiral_lift(self):
        # OrcaSlicer's spiral lift: G3 Z.. I J with no X/Y is a full turn.
        r = gs.scan(recs("G90\nG1 X10 Y10 Z1 F600\nG3 Z2 I-1 J0 P1 F12000"),
                    collect_moves_until=99)
        segs = [m for m in r.moves if m.line == 3]
        self.assertEqual(len(segs), 6)          # 2*pi*1 = 6.28 mm -> 6
        self.assertEqual(segs[-1].end[:3], (10.0, 10.0, 2.0))
        self.assertFalse(any(m.extruding for m in segs))

    def test_clockwise(self):
        r = gs.scan(recs("G1 X10 Y0\nG2 X0 Y-10 I-10 J0"), collect_moves_until=99)
        mid = [m for m in r.moves if m.line == 2][7].end
        self.assertGreater(mid[0], 0)
        self.assertLess(mid[1], 0)


class Bbox(unittest.TestCase):
    def test_extrusion_only_and_per_object(self):
        r = gs.scan(recs("""
M83
G1 X0 Y0 Z.2 F600
G1 X50 Y5 E1
G1 X200 Y200
EXCLUDE_OBJECT_START NAME=a
G1 X60 Y40 E1
EXCLUDE_OBJECT_END NAME=a
"""))
        self.assertEqual((r.total.x_min, r.total.x_max, r.total.y_max), (0.0, 200.0, 200.0))
        self.assertEqual(r.per_object[None].as_dict()["x_max"], 50.0)
        self.assertEqual(r.per_object["a"].as_dict()["x_min"], 60.0)

    def test_bbox_until(self):
        r = gs.scan(recs("M83\nG1 X1 Y1 E1\nG1 X100 Y100 E1"), bbox_until=2)
        self.assertEqual(r.total.x_max, 1.0)


class Bytes(unittest.TestCase):
    def test_line_at_byte_and_exit_position(self):
        r = gs.scan(recs("G1 X1\nG1 X22\nG1 X333"))
        self.assertEqual(gs.line_at_byte(r, 0), 1)
        self.assertEqual(gs.line_at_byte(r, 6), 2)       # "G1 X1\n" is 6 bytes
        self.assertEqual(gs.last_processed_line(r, 13), 2)

    def test_until_byte_stops_bbox_and_moves(self):
        # "M83\n" + "G1 X1 Y1 E1\n" = 16 bytes: line 3 starts at byte 16
        r = gs.scan(recs("M83\nG1 X1 Y1 E1\nG1 X100 Y100 E1"), until_byte=16)
        self.assertEqual(r.total.x_max, 1.0)
        self.assertEqual({m.line for m in r.moves}, {2})

    def test_tools_and_objects_after_the_stop_are_not_counted(self):
        # "T0\n" + "G1 X1 Y1 E1\n" = 3 + 12 bytes: line 3 starts at byte 15.
        text = ("T0\nG1 X1 Y1 E1\nT1\n"
                "EXCLUDE_OBJECT_DEFINE NAME=late CENTER=1,1\nG1 X2 E1")
        r = gs.scan(recs(text), until_byte=15)
        self.assertEqual(r.tools_used, [0])
        self.assertNotIn("LATE", r.state.objects)
        whole = gs.scan(recs(text))
        self.assertEqual(whole.tools_used, [0, 1])

    def test_file_text_at_uses_byte_offsets_with_multibyte_text(self):
        # "; é\n" is 5 bytes (é is 2): line 2 starts at byte 5.
        records = [(1, 0, "; \u00e9"), (2, 5, "G1 X1 Y1 E1")]
        got = gs.file_text_at(records, [(5, 6)])
        self.assertEqual(got[5], "G1 X1 ")
        got = gs.file_text_at(records, [(7, 4)])      # two bytes into line 2
        self.assertEqual(got[7], " X1 ")

    def test_key_equals_value_words_are_not_moves(self):
        # Klipper aborts such a line ("Unable to parse move"); it never moves.
        r = gs.scan(recs("G1 X1 Y1 E1\nG1 X=10 Y=20 E=5"))
        self.assertEqual({m.line for m in r.moves}, {1})
        self.assertEqual(r.total.x_max, 1.0)

    def test_probe_pin_and_offset_move_commands_are_forbidden_in_the_tail(self):
        for form in ("BLTOUCH_DEBUG COMMAND=pin_down", "SET_GCODE_OFFSET Z=5 MOVE=1",
                     "SET_GCODE_OFFSET Z_ADJUST=0.1 MOVE=1"):
            with self.subTest(form=form):
                f = gs.tail_findings(gs.scan(recs("G1 X1 Y1 E1\n%s\nG1 X2 E1" % form)), 1, None)
                self.assertEqual(len(f["forbidden"]), 1, form)

    def test_gcode_offset_without_move_is_not_forbidden(self):
        for form in ("SET_GCODE_OFFSET Z=0.05", "SET_GCODE_OFFSET Z=0.05 MOVE=0"):
            with self.subTest(form=form):
                f = gs.tail_findings(gs.scan(recs("G1 X1 Y1 E1\n%s\nG1 X2 E1" % form)), 1, None)
                self.assertEqual(f["forbidden"], [], form)

    def test_non_finite_layer_z_is_ignored(self):
        # nan before the resume line must not hide a real Z drop after it
        text = "G1 X1 Y1 E1\n;Z:nan\n;Z:5\nG1 X2 E1\n;Z:3\nG1 X3 E1"
        f = gs.tail_findings(gs.scan(recs(text)), 4, None)
        self.assertEqual([ln for ln, _ in f["non_monotonic_z"]], [5])
        # inf before the resume line must not flag every later layer
        text = "G1 X1 Y1 E1\n;Z:inf\n;Z:5\nG1 X2 E1\n;Z:6\nG1 X3 E1"
        f = gs.tail_findings(gs.scan(recs(text)), 4, None)
        self.assertEqual(f["non_monotonic_z"], [])

    def test_testz_and_bed_screws_adjust_are_forbidden_in_the_tail(self):
        for form in ("TESTZ Z=-1", "BED_SCREWS_ADJUST"):
            with self.subTest(form=form):
                f = gs.tail_findings(gs.scan(recs("G1 X1 Y1 E1\n%s\nG1 X2 E1" % form)), 1, None)
                self.assertEqual(len(f["forbidden"]), 1, form)


class Tail(unittest.TestCase):
    def test_forbidden_tool_m82_vase_nonmonotonic(self):
        r = gs.scan(recs("""
T0
;Z:5
G1 X1 Y1 E1
G28
BED_MESH_CALIBRATE
T1
M82
;Z:4
G1 X1 Y2 Z4.1 E1
G1 X1 Y3 Z4.2 E1
"""))
        f = gs.tail_findings(r, 3, current_tool=0)
        self.assertEqual([c for _, c in f["forbidden"]], ["G28", "BED_MESH_CALIBRATE"])
        self.assertEqual(f["tool_change"], [(6, "T1")])
        self.assertEqual(f["m82"][0][0], 7)
        self.assertEqual(f["non_monotonic_z"][0][0], 8)
        self.assertEqual(f["vase"], [])            # 2 moves < VASE_MIN_MOVES
        f_after = gs.tail_findings(r, 9, current_tool=0)
        self.assertEqual(f_after["forbidden"], [])

    def test_vase(self):
        body = "\n".join("G1 X%d Y1 Z%.2f E.1" % (i, 1 + i * 0.01) for i in range(12))
        r = gs.scan(recs("M83\n" + body))
        self.assertEqual(len(gs.tail_findings(r, 1, 0)["vase"]), 1)


class Denylist(unittest.TestCase):
    """N11: homing, probing, start macros and forced positions are all refused."""

    FORMS = ["START_PRINT_EXTRA", "START_PRINT", "HOME", "G30", "G32", "G28", "G28 X",
             "G28 X Y", "G28 Z", "G28 Z0", "g28x", "G28X", "SET_KINEMATIC_POSITION Z=20",
             "SET_KINEMATIC_POSITION X=10 Y=10", "FORCE_MOVE STEPPER=stepper_z DISTANCE=1",
             "_SP_PRINT_START", "BED_MESH_CALIBRATE", "QUAD_GANTRY_LEVEL"]

    def test_every_form_is_flagged_in_the_tail(self):
        for form in self.FORMS:
            with self.subTest(form=form):
                r = gs.scan(recs("G1 X1 Y1 E1\n%s\nG1 X2 E1" % form))
                f = gs.tail_findings(r, 1, None)
                self.assertEqual(len(f["forbidden"]), 1, form)
                self.assertEqual(f["forbidden"][0][0], 2)

    def test_denylist_covers_the_phase_one_commands(self):
        for cmd in ("G28", "G29", "G30", "G32", "HOME", "START_PRINT", "START_PRINT_EXTRA",
                    "_SP_PRINT_START", "BED_MESH_CALIBRATE", "PROBE", "Z_TILT_ADJUST",
                    "QUAD_GANTRY_LEVEL", "SET_KINEMATIC_POSITION", "FORCE_MOVE"):
            with self.subTest(cmd=cmd):
                self.assertIn(cmd, gs.FORBIDDEN_TAIL)

    def test_command_cannot_hide_behind_spaces_or_a_line_number(self):
        for form in ("G 28 X0", "N5 G28", "g  28", "N12 G 29"):
            with self.subTest(form=form):
                r = gs.scan(recs("G1 X1 Y1 E1\n%s\nG1 X2 E1" % form))
                f = gs.tail_findings(r, 1, None)
                self.assertEqual(len(f["forbidden"]), 1, form)

    def test_g0_extrusion_after_end_print_is_refused(self):
        r = gs.scan(recs("G1 X1 Y1 E1\nEND_PRINT\nG0 X20 Y20 E0.5"))
        f = gs.tail_findings(r, 1, None)
        self.assertEqual([c for _, c in f["forbidden"]], ["END_PRINT"])

    def test_tool_change_in_tail_with_unknown_current_tool_is_reported(self):
        r = gs.scan(recs("G1 X1 Y1 E1\nT1\nG1 X2 E1"))
        self.assertTrue(gs.tail_findings(r, 1, None)["tool_change"])

    def test_carriage_return_inside_a_line_is_refused(self):
        r = gs.scan(recs("G1 X1 Y1 E1\nG1 X10 F3000\rM112\nG1 X2 E1"))
        f = gs.tail_findings(r, 1, None)
        self.assertIn("CR_IN_LINE", [c for _, c in f["forbidden"]])

    def test_carriage_return_inside_a_comment_line_is_refused(self):
        # Whether Klipper splits a line at a bare CR is unverified (KI-14), so a
        # comment that hides a command behind one is refused as well.
        r = gs.scan(recs("G1 X1 Y1 E1\n;note\rG28 X\nG1 X2 E1"))
        f = gs.tail_findings(r, 1, None)
        self.assertIn("CR_IN_LINE", [c for _, c in f["forbidden"]])

    def test_a_crlf_line_ending_is_not_a_carriage_return_inside_a_line(self):
        r = gs.scan(recs("G1 X1 Y1 E1\r\n;note\r\nG1 X2 E1\r"))
        f = gs.tail_findings(r, 1, None)
        self.assertNotIn("CR_IN_LINE", [c for _, c in f["forbidden"]])

    def test_a_bare_n_prefix_cannot_hide_a_command(self):
        for form in ("N M112", "N G28", "n  FIRMWARE_RESTART", "N12 M112"):
            with self.subTest(form=form):
                f = gs.tail_findings(gs.scan(recs("G1 X1 Y1 E1\n%s\nG1 X2 E1" % form)), 1, None)
                self.assertEqual(len(f["forbidden"]), 1, form)

    def test_names_that_start_with_n_are_not_mangled(self):
        cmd, _ = gs.parse("NEOPIXEL_SET RED=1")
        self.assertEqual(cmd, "NEOPIXEL_SET")

    def test_every_repo_macro_that_homes_or_probes_is_on_the_deny_list(self):
        """Derived from config/: a macro whose own body homes, probes or
        force-moves must be in FORBIDDEN_TAIL (transitive callers are listed by
        hand; KI-08 explains why an allow-list is the P2 answer)."""
        import glob, re
        root = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "config")
        bad = re.compile(r"\b(G28|G29|G30|G32|PROBE(?:_\w+)?|BED_MESH_CALIBRATE|FORCE_MOVE|"
                         r"SET_KINEMATIC_POSITION|Z_TILT_ADJUST)\b", re.I)
        found, cur = set(), None
        for path in glob.glob(os.path.join(root, "**", "*.cfg"), recursive=True):
            with open(path, encoding="utf-8", errors="replace") as fh:
                for ln in fh:
                    m = re.match(r"\[gcode_macro\s+([^\]]+)\]", ln)
                    if m:
                        cur = m.group(1).strip().upper()
                    elif ln.startswith("["):
                        cur = None
                    elif cur and bad.search(re.sub(r"[;#].*", "", ln)):
                        found.add(cur)
        self.assertTrue(found, "no macros found under config/")
        missing = sorted(n for n in found if n not in gs.FORBIDDEN_TAIL)
        self.assertEqual(missing, [])

    def test_every_repo_macro_that_reaches_a_moving_command_is_on_the_deny_list(self):
        """Transitive version of the test above, derived from config/: a macro
        that calls (at the start of a line, through any depth of macros) a
        homing, probing, force-moving or stepper command is on the list. Tool
        changes T<n> are refused by the tool-change check instead, and the end
        macros (END_MACROS) are accepted only as the end of the file."""
        import glob, re
        root = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "config")
        bad = re.compile(r"^\s*(G28|G29|G30|G32|PROBE\w*|BED_MESH_CALIBRATE|FORCE_MOVE|"
                         r"SET_KINEMATIC_POSITION|Z_TILT_ADJUST|MANUAL_STEPPER|MANUAL_PROBE|"
                         r"STEPPER_BUZZ|BLTOUCH_DEBUG|TESTZ|BED_SCREWS_ADJUST|"
                         r"SCREWS_TILT_CALCULATE|QUAD_GANTRY_LEVEL|"
                         r"BED_TILT_CALIBRATE|Z_ENDSTOP_CALIBRATE|TEST_RESONANCES|"
                         r"SHAPER_CALIBRATE|AXIS_TWIST_COMPENSATION_CALIBRATE|"
                         r"DELTA_CALIBRATE|DELTA_ANALYZE)\b", re.I)
        body, cur = {}, None
        for path in glob.glob(os.path.join(root, "**", "*.cfg"), recursive=True):
            with open(path, encoding="utf-8", errors="replace") as fh:
                for ln in fh:
                    m = re.match(r"\[gcode_macro\s+([^\]]+)\]", ln)
                    if m:
                        cur = m.group(1).strip().upper()
                        body.setdefault(cur, [])
                    elif ln.startswith("["):
                        cur = None
                    elif cur and not ln.lstrip().startswith(("#", ";")):
                        body[cur].append(re.sub(r"[;#].*", "", ln))
        calls = lambda lines: {m.group(1).upper() for ln in lines
                               for m in [re.match(r"^\s*([A-Za-z_]\w*)\b", ln)] if m}
        danger = {n for n, b in body.items() if any(bad.match(ln) for ln in b)}
        grew = True
        while grew:
            grew = False
            for n, b in body.items():
                if n not in danger and calls(b) & danger:
                    danger.add(n)
                    grew = True
        self.assertTrue(danger)
        missing = sorted(n for n in danger
                         if n not in gs.FORBIDDEN_TAIL and n not in gs.END_MACROS
                         and not re.fullmatch(r"T\d+", n))
        self.assertEqual(missing, [])

    def test_other_control_characters_inside_a_line_are_refused(self):
        for ch in ("\x0b", "\x0c", "\x00", "\x1b"):
            with self.subTest(ch=repr(ch)):
                r = gs.scan(recs("G1 X1 Y1 E1\nG1 X10%sG28\nG1 X2 E1" % ch))
                f = gs.tail_findings(r, 1, None)
                self.assertTrue(f["forbidden"])

    def test_tab_inside_a_line_is_fine(self):
        r = gs.scan(recs("G1 X1 Y1 E1\nG1\tX10\nG1 X2 E1"))
        self.assertEqual(gs.tail_findings(r, 1, None)["forbidden"], [])

    def test_zero_padded_command_numbers_cannot_hide_a_command(self):
        # Klipper looks the command up as the literal text, so "M0112" is an
        # unknown command there; the scanner normalises the number and refuses
        # it anyway (stricter, never looser).
        for form in ("M0112", "G028", "G0028 X0", "m00112"):
            with self.subTest(form=form):
                f = gs.tail_findings(gs.scan(recs("G1 X1 Y1 E1\n%s\nG1 X2 E1" % form)), 1, None)
                self.assertEqual(len(f["forbidden"]), 1, form)

    def test_klipper_native_probing_and_moving_commands_are_forbidden(self):
        # Built-in commands (not config macros) that probe, home or sweep the head.
        for cmd in ("BED_TILT_CALIBRATE", "Z_ENDSTOP_CALIBRATE", "TEST_RESONANCES",
                    "SHAPER_CALIBRATE", "AXIS_TWIST_COMPENSATION_CALIBRATE",
                    "DELTA_CALIBRATE", "DELTA_ANALYZE", "MANUAL_PROBE", "TESTZ",
                    "BED_SCREWS_ADJUST", "SCREWS_TILT_CALCULATE", "PROBE_ACCURACY"):
            with self.subTest(cmd=cmd):
                f = gs.tail_findings(gs.scan(recs("G1 X1 Y1 E1\n%s\nG1 X2 E1" % cmd)), 1, None)
                self.assertEqual(len(f["forbidden"]), 1, cmd)

    def test_velocity_limits_that_klipper_rejects_are_ignored(self):
        # Klipper: MINIMUM_CRUISE_RATIO must be >= 0 and < 1, ACCEL_TO_DECEL > 0;
        # the line errors there, so it must not reach the timing model.
        base = gs.scan(recs("G1 X1 Y1 E1")).state
        for form in ("SET_VELOCITY_LIMIT MINIMUM_CRUISE_RATIO=1.0",
                     "SET_VELOCITY_LIMIT MINIMUM_CRUISE_RATIO=1.5",
                     "SET_VELOCITY_LIMIT MINIMUM_CRUISE_RATIO=-0.1",
                     "SET_VELOCITY_LIMIT ACCEL_TO_DECEL=0"):
            with self.subTest(form=form):
                st = gs.scan(recs("G1 X1 Y1 E1\n%s" % form)).state
                self.assertEqual(st.minimum_cruise_ratio, base.minimum_cruise_ratio)
                self.assertFalse(st.mcr_set or st.accel_to_decel_set)
        st = gs.scan(recs("SET_VELOCITY_LIMIT MINIMUM_CRUISE_RATIO=0.25")).state
        self.assertEqual(st.minimum_cruise_ratio, 0.25)

    SECTIONS = {
        "gcode_macro MY_HOME_ALL": {"gcode": "M117 homing\nG28\nG1 X1"},
        "gcode_macro outer": {"gcode": "{% if True %}\n  MY_HOME_ALL\n{% endif %}"},
        "gcode_macro OUTER2": {"gcode": "OUTER"},
        "gcode_macro SAFE": {"gcode": "M117 x\nG1 X1 Y1\nSET_LED LED=a"},
        "gcode_macro COMMENTED": {"gcode": "# G28\n; MY_HOME_ALL\nM117 y"},
        "gcode_macro ENDS": {"gcode": "END_PRINT\nM84"},
        "gcode_macro RETRACTING": {"gcode": "G10\nG1 X1\nG11"},
        "gcode_macro T0": {"gcode": "MY_HOME_ALL"},
        "stepper_x": {"position_max": "230"},
    }

    def test_user_macros_that_reach_a_forbidden_command_are_found(self):
        got = gs.dangerous_macros(self.SECTIONS)
        # firmware retraction (G10/G11) alone is not dangerous, and a T<n> tool
        # change is refused by the tool-change check instead
        self.assertEqual(sorted(got), ["MY_HOME_ALL", "OUTER", "OUTER2"])

    def test_scanner_refuses_the_extra_forbidden_names(self):
        extra = gs.dangerous_macros(self.SECTIONS)
        r = gs.scan(recs("G1 X1 Y1 E1\nMY_HOME_ALL\nG1 X2 E1\nSAFE"), extra_forbidden=extra)
        f = gs.tail_findings(r, 1, None)
        self.assertEqual([c for _, c in f["forbidden"]], ["MY_HOME_ALL"])
        plain = gs.tail_findings(gs.scan(recs("G1 X1 Y1 E1\nMY_HOME_ALL")), 1, None)
        self.assertEqual(plain["forbidden"], [])      # not on the built-in list

    def test_excerpt_map_longer_than_the_excerpt_is_a_value_error(self):
        import gzip, json, tempfile
        with tempfile.TemporaryDirectory() as d:
            gz = os.path.join(d, "x.gcode.gz")
            with gzip.open(gz, "wt", encoding="utf-8") as fh:
                fh.write("G1 X1\nG1 X2\n")
            for runs in ([[1, 5, 0]], [[1, 1, 0], [10, 3, 100]], [[1, 1, 0]]):
                with self.subTest(runs=runs):
                    mp = os.path.join(d, "x.map.json")
                    with open(mp, "w") as fh:
                        json.dump({"runs": runs}, fh)
                    with self.assertRaises(ValueError):
                        list(gs.iter_excerpt(gz, mp))

    def test_a_jinja_expression_that_runs_a_command_is_flagged(self):
        sections = {
            "gcode_macro VIA_JINJA": {"gcode": '{printer.gcode.run_script_from_command("G28")}'},
            "gcode_macro VIA_STRING": {"gcode": "{% set cmd = 'BED_TILT_CALIBRATE' %}\nM117 x"},
            "gcode_macro BUILT": {"gcode": '{ "G" ~ "28" }'},
            "gcode_macro CHAINED": {"gcode": "{printer.gcode.run_script_from_command(cmd)}"},
            "gcode_macro RESPOND": {"gcode": '{ action_respond_info("hi") }\nM117 x'},
            "gcode_macro PLAIN": {"gcode": "{% set a = 1 %}\nM117 hello\nG1 X1"},
            "gcode_macro NOTAWORD": {"gcode": "M117 PAUSED_HOMED_G280"},
            "gcode_macro MENTIONS": {"gcode": "{% if printer.idle_timeout.state == 'PAUSE' %}\nM117 x\n{% endif %}"},
        }
        got = gs.dangerous_macros(sections)
        self.assertEqual(sorted(got), ["BUILT", "CHAINED", "VIA_JINJA", "VIA_STRING"])

    def test_text_offsets_survive_invalid_utf8_before_them(self):
        import tempfile
        raw = b"G1 X1 Y1 E1\n; caf\xe9 \xe9\xe9\nG1 X2 Y2 E1\nG1 X3 Y3 E1\n"
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "x.gcode")
            with open(path, "wb") as fh:
                fh.write(raw)
            pos = raw.index(b"G1 X2")
            got = gs.file_text_at(gs.iter_file(path), [(pos, 5)])
        self.assertEqual(got[pos], "G1 X2")

    def test_a_single_huge_line_is_refused(self):
        import tempfile
        old = gs.MAX_LINE_BYTES
        gs.MAX_LINE_BYTES = 50
        try:
            with tempfile.TemporaryDirectory() as d:
                path = os.path.join(d, "x.gcode")
                with open(path, "w") as fh:
                    fh.write("G1 X1\n; " + "x" * 200 + "\nG1 X2\n")
                with self.assertRaises(ValueError):
                    list(gs.iter_file(path))
        finally:
            gs.MAX_LINE_BYTES = old

    def test_stepper_commands_are_forbidden(self):
        for cmd in ("MANUAL_STEPPER", "STEPPER_BUZZ"):
            self.assertIn(cmd, gs.FORBIDDEN_TAIL)

    def test_job_end_commands_are_fine_at_the_end_and_refused_before_more_extrusion(self):
        for end in ("M84", "M18", "TURN_OFF_HEATERS"):
            with self.subTest(end=end):
                ok = gs.tail_findings(gs.scan(recs("G1 X1 Y1 E1\nG1 X2 E1\n%s" % end)), 1, None)
                self.assertEqual(ok["forbidden"], [])
                bad = gs.tail_findings(
                    gs.scan(recs("G1 X1 Y1 E1\n%s\nG1 X2 E1\nG1 X3 E1" % end)), 1, None)
                self.assertEqual([c for _, c in bad["forbidden"]], ["END_PRINT"])

    def test_firmware_retraction_and_arcs_without_ij_are_refused_in_the_tail(self):
        for body in ("G10", "G11", "G2 X5 Y5 R3 E1"):
            with self.subTest(body=body):
                r = gs.scan(recs("G1 X1 Y1 E1\n%s\nG1 X2 E1" % body))
                f = gs.tail_findings(r, 1, None)
                self.assertEqual(len(f["forbidden"]), 1, f)

    def test_excerpt_size_is_capped(self):
        import tempfile, gzip, os, json
        with tempfile.TemporaryDirectory() as d:
            gz, mp = os.path.join(d, "x.gz"), os.path.join(d, "x.json")
            with gzip.open(gz, "wt") as fh:
                fh.write("G1 X1 Y1 E1\n" * 100)
            with open(mp, "w") as fh:
                json.dump({"runs": [[1, 100, 0]]}, fh)
            old = gs.MAX_EXCERPT_CHARS
            gs.MAX_EXCERPT_CHARS = 100
            try:
                with self.assertRaises(ValueError):
                    list(gs.iter_excerpt(gz, mp))
            finally:
                gs.MAX_EXCERPT_CHARS = old

    def test_stop_and_restart_commands_are_forbidden(self):
        for cmd in ("M112", "M999", "M410", "FIRMWARE_RESTART", "RESTART", "SAVE_CONFIG"):
            self.assertIn(cmd, gs.FORBIDDEN_TAIL)

    def test_ending_and_pausing_commands_are_forbidden(self):
        for cmd in ("CANCEL_PRINT", "PAUSE", "M600", "M0", "PRINTER_SHUTDOWN",
                    "SP_CHANGE", "_SP_CHANGE", "_SP_PAUSE"):
            self.assertIn(cmd, gs.FORBIDDEN_TAIL)

    def test_end_print_is_allowed_only_at_the_end_of_the_file(self):
        at_end = gs.tail_findings(gs.scan(recs("G1 X1 Y1 E1\nG1 X2 E1\nEND_PRINT")), 1, None)
        self.assertEqual(at_end["forbidden"], [])
        mid = gs.tail_findings(
            gs.scan(recs("G1 X1 Y1 E1\nEND_PRINT\nG1 X2 E1\nG1 X3 E1")), 1, None)
        self.assertEqual([c for _, c in mid["forbidden"]], ["END_PRINT"])

    def test_mmu_homing_macros_are_forbidden(self):
        for cmd in ("SP_HOME", "_SP_CONDITIONAL_HOME", "_SP_PRINT_START_B",
                    "SP_FORCE_MOVE_LANE"):
            self.assertIn(cmd, gs.FORBIDDEN_TAIL)


class Robustness(unittest.TestCase):
    """N2: malformed G-code gives a clear error, never a hang or a crash."""

    def test_non_finite_parameters_are_ignored(self):
        r = gs.scan(recs("SET_VELOCITY_LIMIT ACCEL=nan\nM220 S=inf\nM204 S=nan"))
        self.assertEqual(r.state.accel, 4000.0)
        self.assertEqual(r.state.speed_factor, 100.0)

    def test_negative_speed_and_acceleration_are_ignored(self):
        r = gs.scan(recs("SET_VELOCITY_LIMIT ACCEL=-5 VELOCITY=-1\nM220 S-50\nM204 S-900"))
        self.assertEqual(r.state.accel, 4000.0)
        self.assertEqual(r.state.max_velocity, 200.0)
        self.assertEqual(r.state.speed_factor, 100.0)

    def test_huge_arc_is_refused(self):
        with self.assertRaises(ValueError):
            gs.scan(recs("G90\nG1 X1 Y1 F600\nG2 X2 Y0 I1e300 J0"))

    def test_huge_coordinate_is_refused(self):
        with self.assertRaises(ValueError):
            gs.scan(recs("G90\nG1 X1e9 Y0 F600"))


class Incident(unittest.TestCase):
    """The 2026-11-07 excerpt (fixtures/SOURCES.md)."""

    @classmethod
    def setUpClass(cls):
        cls.ev, cls.res, cls.timed = fx.incident()

    def test_part_box_and_height(self):
        # The total box includes the first-layer skirt; it matches a scan of
        # the full original file up to the same line.
        b = self.res.total
        self.assertEqual((round(b.x_min, 1), round(b.x_max, 1)), (29.8, 188.4))
        self.assertEqual((round(b.y_min, 1), round(b.y_max, 1)), (29.8, 188.4))
        self.assertAlmostEqual(b.z_max, 20.6)
        obj = self.res.per_object["ps2-slim-wall-mount.stl_id_0_copy_0"]
        self.assertAlmostEqual(obj.z_max, 20.6)

    def test_layer_and_state(self):
        st = gs.state_at(fx.records(), fx.LAST_LINE + 1, fx.limits())
        self.assertEqual((st.layer, st.layer_z, st.layer_start_line),
                         (158, 20.6, fx.CUT_LAYER_START))
        self.assertEqual((st.accel, st.fan, st.extruder_temp, st.bed_temp,
                          st.pressure_advance, st.tool),
                         (2500.0, 255, 220.0, 60.0, 0.08, 0))
        self.assertFalse(st.e_absolute)

    def test_byte_mapping(self):
        full = gs.scan(fx.records())
        self.assertEqual(gs.line_at_byte(full, fx.EXIT_POSITION), 202397)
        self.assertEqual(gs.last_processed_line(full, fx.EXIT_POSITION), fx.LAST_LINE)
        self.assertEqual(gs.line_at_byte(full, 5398158), fx.SD_LINE_GOOD)
        self.assertEqual(gs.line_at_byte(full, 5404152), fx.LAST_LINE)

    def test_last_line_matches_log_last_position(self):
        last = [m for m in self.res.moves if m.line == fx.LAST_LINE][-1]
        self.assertEqual(list(last.end[:2]), self.ev.gcode_state["last_position"][:2])

    def test_tail_is_clean(self):
        f = gs.tail_findings(gs.scan(fx.records()), fx.CUT_LAYER_START, 0)
        self.assertEqual(f["forbidden"] + f["tool_change"] + f["m82"], [])


if __name__ == "__main__":
    unittest.main()
