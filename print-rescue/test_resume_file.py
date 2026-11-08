import copy
import unittest

import gcode_scan as gs
import resume_file as rfile
import rescue_fixtures as fx

LINE = 201955
ORIGINAL_MARK = "; ---- original file from line %d ----" % LINE


def at(lines, prefix):
    """Index of the first line starting with prefix (generated lines carry
    trailing ; comments)."""
    return next(i for i, ln in enumerate(lines) if ln.startswith(prefix))

# Commands that must never appear in generated resume/preview output.
GENERATED_FORBIDDEN = [
    "G28", "G28 X", "G28 X Y", "G28 Z", "G28 Z0", "g28x", "G28X",
    "G30", "G32", "G29", "HOME", "START_PRINT", "START_PRINT_EXTRA",
    "_SP_PRINT_START", "SET_KINEMATIC_POSITION Z=20", "SET_KINEMATIC_POSITION",
    "FORCE_MOVE STEPPER=stepper_z DISTANCE=1", "BED_MESH_CALIBRATE", "PROBE",
    "QUAD_GANTRY_LEVEL", "Z_TILT_ADJUST", "M112", "FIRMWARE_RESTART",
    "G1 X1 Y1 G28", "EXCLUDE_OBJECT NAME=a HOME",
]


class Build(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.state = gs.state_at(fx.records(), LINE, fx.limits())
        cls.tail = [t for n, _, t in fx.records() if n >= LINE]

    def build(self, **kw):
        args = dict(overlap_s=2.98, overlap_mm=216.3, park_xy=(15, 205),
                    bed_mesh_profile="default")
        args.update(kw)
        return rfile.build(self.state, "part.gcode", LINE, self.tail, **args).split("\n")

    def build_with(self, **state_changes):
        st = copy.deepcopy(self.state)
        for k, v in state_changes.items():
            setattr(st, k, v)
        args = dict(overlap_s=2.98, overlap_mm=216.3, park_xy=(15, 205),
                    bed_mesh_profile="default")
        return rfile.build(st, "part.gcode", LINE, self.tail, **args).split("\n")

    def test_absolute_extrusion_at_the_resume_line_is_restored(self):
        # The preview runs its own prime in M83; the copied tail must then run
        # in the mode the source was in, with E continuing from the source value.
        st = copy.deepcopy(self.state)
        st.e_absolute = True
        st.pos[3] = 1234.5
        out = rfile.build(st, "part.gcode", LINE, self.tail, overlap_s=2.98,
                          overlap_mm=216.3, park_xy=(15, 205),
                          bed_mesh_profile="default").split("\n")
        mark = at(out, "; ---- original file")
        self.assertIn("M82", out[:mark])
        self.assertLess(out.index("M82"), at(out, "G92 E1234.5"))
        self.assertGreater(out.index("M82"), at(out, "G1 E"))

    def test_relative_xyz_at_the_resume_line_is_restored(self):
        out = self.build_with(absolute=False)
        mark = at(out, "; ---- original file")
        self.assertEqual(out[:mark].count("G91"), 2)   # the lift bracket + the restore
        self.assertGreater(len(out[:mark]) - 1 - out[:mark][::-1].index("G91"),
                           at(out, "G1 Z20.6"))

    def test_arc_plane_of_the_source_is_restored(self):
        for plane in ("G17", "G18", "G19"):
            with self.subTest(plane=plane):
                out = self.build_with(plane=plane)
                mark = at(out, "; ---- original file")
                self.assertEqual(out[mark - 1], plane)

    def test_control_characters_in_a_tail_line_are_refused(self):
        for ch in ("\x0b", "\x0c", "\x00", "\x1b", "\x7f", "\x85"[:1] and "\x1c"):
            with self.subTest(ch=repr(ch)):
                tail = list(self.tail) + ["G1 X1 ;a%sb" % ch]
                with self.assertRaises(rfile.ResumeRefused):
                    rfile.build(self.state, "part.gcode", LINE, tail, overlap_s=2.98,
                                overlap_mm=216.3, park_xy=(15, 205),
                                bed_mesh_profile="default")

    def test_tab_and_non_ascii_comments_in_a_tail_line_are_kept(self):
        tail = list(self.tail) + ["G1 X1\t; vase \u00e9"]
        out = rfile.build(self.state, "part.gcode", LINE, tail, overlap_s=2.98,
                          overlap_mm=216.3, park_xy=(15, 205), bed_mesh_profile="default")
        self.assertIn("G1 X1\t; vase \u00e9", out)

    def test_default_modes_add_nothing_after_the_gate(self):
        out = self.build()
        mark = at(out, "; ---- original file")
        self.assertNotIn("M82", out[:mark])
        self.assertEqual(out[:mark].count("G91"), 1)

    def test_non_finite_gcode_offset_is_refused(self):
        for bad in (float("nan"), float("inf")):
            with self.subTest(bad=bad):
                with self.assertRaises(rfile.ResumeRefused):
                    self.build(gcode_offset_z=bad)

    def test_header_order(self):
        out = self.build(gcode_offset_z=0.025, speed_factor=100.0, extrude_factor=100.0)
        self.assertTrue(out[0].startswith("; print-rescue resume PREVIEW"))
        self.assertIn("source file: part.gcode", out[1])
        self.assertIn("original line %d" % LINE, out[2])
        self.assertIn("2.98 s", out[3])
        # A resume skips START_PRINT, so its side effects are restated first.
        self.assertTrue(out[4].startswith("; estimates in the header"))
        self.assertEqual(out[5:10], ["CANCEL_PRINTER_SHUTDOWN", "M117", "G90", "G21", "M83"])
        self.assertEqual(out[10], "SET_IDLE_TIMEOUT TIMEOUT=600")
        # Heat only after the off-part park (N6); Z relative until the gate (N5).
        order = ["EXCLUDE_OBJECT_DEFINE", "SET_PRESSURE_ADVANCE", "SET_VELOCITY_LIMIT",
                 "M220", "M221", "M106", "SET_GCODE_OFFSET", "G92 E0", "G1 E-2.5",
                 "G91", "G1 Z3", "G90", "G1 X15 Y205", "BED_MESH_PROFILE",
                 "M140", "M190", "M104", "M109", "G1 X170.721 Y131.153",
                 rfile.GATE_MARK, "G1 Z20.6", "EXCLUDE_OBJECT_START", "G1 E3.3",
                 "G92 E0", "G1 F6000", "G17", "; ---- original file"]
        idx = []
        pos = 0
        for prefix in order:
            k = next(i for i in range(pos, len(out)) if out[i].startswith(prefix))
            idx.append(k)
            pos = k + 1
        self.assertEqual(idx, sorted(idx))

    def test_g17_and_shutdown_cancel_are_allowed_generated_commands(self):
        for cmd in ("CANCEL_PRINTER_SHUTDOWN", "G17", "M117",
                    "SET_PRINT_STATS_INFO TOTAL_LAYER=500 CURRENT_LAYER=265",
                    "SET_PRINT_STATS_INFO CURRENT_LAYER=266"):
            with self.subTest(cmd=cmd):
                self.assertEqual(rfile.check_generated([cmd]), [])
        self.assertTrue(rfile.check_generated(["SET_PRINT_STATS_INFO FOO=1"]))
        self.assertTrue(rfile.check_generated(["M117 hello"]))

    def test_restored_state(self):
        out = self.build()
        self.assertIn("SET_PRESSURE_ADVANCE ADVANCE=0.08", out)
        self.assertIn("SET_VELOCITY_LIMIT ACCEL=2500 ACCEL_TO_DECEL=1750", out)
        self.assertIn("M106 S255", out)
        self.assertIn("M109 S220", out)
        self.assertIn("M190 S60", out)
        self.assertNotIn("SET_GCODE_OFFSET", " ".join(out))

    def test_exclude_object_start_names_current_object(self):
        out = self.build()
        self.assertIn("EXCLUDE_OBJECT_START NAME=ps2-slim-wall-mount.stl_id_0_copy_0", out)

    def test_prime_default(self):
        out = self.build()
        preamble = out[:at(out, ORIGINAL_MARK)]
        prime = [ln for ln in preamble if ln.startswith("G1 E") and not ln.startswith("G1 E-")]
        self.assertEqual(len(prime), 1)
        self.assertTrue(prime[0].startswith("G1 E3.3 F300"))

    def test_prime_subtracts_slicer_retraction(self):
        st = gs.state_at(fx.records(), 201927, fx.limits())   # after a retract + wipe
        self.assertAlmostEqual(st.retracted, 0.4)
        text = rfile.build(st, "p", 201927, [], overlap_s=0, overlap_mm=0,
                           park_xy=(15, 205), bed_mesh_profile="default")
        self.assertIn("G1 E2.9 F300", text)
        # The retraction before the approach tops the filament up to 2.5 mm in total.
        self.assertIn("G1 E-2.1 F300", text)

    def test_retraction_precedes_approach_and_heat(self):
        # N6: a retraction before approaching or heating.
        out = self.build()
        retract = at(out, "G1 E-2.5 F300")
        self.assertLess(retract, at(out, "G91"))
        self.assertLess(retract, at(out, "M190 S60"))
        self.assertLess(retract, at(out, "M109 S220"))

    def test_heat_only_after_the_off_part_park(self):
        # N6: no heat command before the approach to the off-part park spot.
        out = self.build()
        park = at(out, "G1 X15 Y205 F6000")
        self.assertLess(park, at(out, "M140 S60"))
        self.assertLess(park, at(out, "M190 S60"))
        self.assertLess(park, at(out, "M104 S220"))
        self.assertLess(park, at(out, "M109 S220"))
        self.assertLess(at(out, "M109 S220"), at(out, "G1 X170.721 Y131.153 F6000"))

    def test_relative_lift_is_bracketed_by_g91_g90(self):
        # N5: while Z is unknown only relative up moves are generated.
        out = self.build()
        i = at(out, "G91")
        self.assertTrue(out[i + 1].startswith("G1 Z3 F300"))
        self.assertEqual(out[i + 2], "G90")

    def test_no_absolute_z_before_the_gate(self):
        out = self.build()
        gate = next(i for i, ln in enumerate(out) if ln.startswith(rfile.GATE_MARK))
        self.assertEqual(rfile.check_generated(out[:gate]), [])
        self.assertLess(gate, at(out, "G1 Z20.6 F300"))

    def test_absolute_z_before_the_gate_is_refused(self):
        self.assertTrue(rfile.check_generated(["G90", "G1 Z5 F300", rfile.GATE_MARK]))
        self.assertTrue(rfile.check_generated(["G1 Z5 F300"]))   # G90 is the default
        self.assertEqual(rfile.check_generated(["G91", "G1 Z3 F300", "G90",
                                                "G1 X1 Y1 F6000", rfile.GATE_MARK]), [])

    def test_bed_mesh_profile_is_loaded_with_the_profile_name(self):
        # N8: the docs say the mesh is restored; the preview loads the profile's mesh.
        self.assertIn("BED_MESH_PROFILE LOAD=default", self.build())
        self.assertIn("BED_MESH_PROFILE LOAD=mesh2", self.build(bed_mesh_profile="mesh2"))

    def test_generated_part_passes_the_allow_list(self):
        out = self.build(gcode_offset_z=0.025, speed_factor=100.0, extrude_factor=100.0)
        self.assertEqual(rfile.check_generated(out[:at(out, ORIGINAL_MARK)]), [])

    def test_forbidden_commands_refused_in_generated_output(self):
        # N11: generated lines are an allow-list; every forbidden form is refused.
        for cmd in GENERATED_FORBIDDEN:
            with self.subTest(cmd=cmd):
                self.assertTrue(rfile.check_generated([cmd]), cmd)

    def test_forbidden_define_line_in_the_header_is_refused(self):
        for cmd in GENERATED_FORBIDDEN:
            with self.subTest(cmd=cmd):
                st = copy.deepcopy(self.state)
                st.objects["INJECTED"] = cmd
                with self.assertRaises(rfile.ResumeRefused):
                    rfile.build(st, "p", LINE, self.tail, overlap_s=0, overlap_mm=0,
                                park_xy=(15, 205), bed_mesh_profile="default")

    def test_original_file_lines_after_the_resume_point_are_exempt(self):
        # The sliced job itself is copied as is; R17 in the plan refuses it instead.
        out = rfile.build(self.state, "p", LINE, ["G28 X", "BED_MESH_CALIBRATE"],
                          overlap_s=0, overlap_mm=0, park_xy=(15, 205),
                          bed_mesh_profile="default").split("\n")
        k = at(out, ORIGINAL_MARK)
        self.assertEqual(out[k + 1:k + 3], ["G28 X", "BED_MESH_CALIBRATE"])

    def test_open_findings_are_in_the_label(self):
        out = self.build(open_codes=["R13", "PIN_AT_BOOT"])
        self.assertIn("NOT VERIFIED", out[0])
        self.assertIn("R13, PIN_AT_BOOT", out[0])

    def test_tail_starts_at_the_chosen_line(self):
        out = self.build()
        k = next(i for i, ln in enumerate(out) if ln.startswith("; ---- original file"))
        self.assertEqual(out[k + 1], self.tail[0])
        self.assertEqual(out[k + 1], "G1 X170.157 Y130.573 E.02486")

    def test_r17_refuses(self):
        with self.assertRaises(rfile.ResumeRefused):
            self.build(refusal_codes=["R13", "R17"])

    def test_tail_with_g28_is_flagged(self):
        r = gs.scan([(1, 0, "G1 X1 Y1 E1"), (2, 12, "G28"), (3, 16, "G1 X2")])
        f = gs.tail_findings(r, 1, None)
        self.assertEqual(f["forbidden"], [(2, "G28")])


class Injection(unittest.TestCase):
    """S1: the allow-list sees the joined text, and a control or non-ASCII
    character in any generated element is refused."""

    @classmethod
    def setUpClass(cls):
        cls.state = gs.state_at(fx.records(), LINE, fx.limits())
        cls.tail = [t for n, _, t in fx.records() if n >= LINE]

    def build(self, source_name="part.gcode", **kw):
        args = dict(overlap_s=0, overlap_mm=0, park_xy=(15, 205),
                    bed_mesh_profile="default")
        args.update(kw)
        return rfile.build(self.state, source_name, LINE, self.tail, **args)

    def test_newline_inside_an_element_is_checked_as_its_own_line(self):
        for elem in ["ps2.gcode\nG28 ; injected", "ps2.gcode\nG1 Z-50", "ps2.gcode\nG28"]:
            with self.subTest(elem=elem):
                self.assertTrue(rfile.check_generated(["; source file: " + elem]))

    def test_control_characters_are_refused(self):
        for ch in ["\r", "\n", "\t", "\x00", "\x0b", "\x0c", "\x1b", "\x7f"]:
            with self.subTest(ch=repr(ch)):
                self.assertTrue(rfile.check_generated(["; source file: x" + ch + "G28"]))

    def test_hostile_source_name_refuses_the_build(self):
        for name in ["ps2.gcode\nG28 ; injected", "ps2.gcode\nG1 Z-50", "ps2.gcode\rG28",
                     "ps2\nG28.gcode", "tab\tname.gcode", "a\u2028G28.gcode",
                     "a\x85G28.gcode"]:
            with self.subTest(name=repr(name)):
                with self.assertRaises(rfile.ResumeRefused):
                    self.build(name)

    def test_accented_source_name_and_header_build(self):
        # A Spanish file name or slicer comment is not an injection risk: only
        # control characters and Unicode line separators are.
        text = self.build("Jarr\u00f3n+caf\u00e9.gcode",
                          header_lines=[";model: Caf\u00e9 vase", ";FLAVOR:Marlin"])
        self.assertIn("; source file: Jarr\u00f3n+caf\u00e9.gcode", text.split("\n"))
        self.assertIn(";model: Caf\u00e9 vase", text.split("\n"))

    def test_non_ascii_is_still_refused_outside_comment_lines(self):
        self.assertTrue(rfile.check_generated(["G1 X1 ; ok", "M117 caf\u00e9"]))
        self.assertEqual(rfile.check_generated(["; caf\u00e9"]), [])

    def test_normal_source_name_still_builds(self):
        text = self.build("ps2-slim-wall-mount_PLA_3h15m.gcode")
        self.assertIn("; source file: ps2-slim-wall-mount_PLA_3h15m.gcode", text.split("\n"))

    def test_hostile_bed_mesh_profile_refuses_the_build(self):
        with self.assertRaises(rfile.ResumeRefused):
            self.build(bed_mesh_profile="default\nG28")

    def test_hostile_object_define_refuses_the_build(self):
        st = copy.deepcopy(self.state)
        st.objects["X"] = "EXCLUDE_OBJECT_DEFINE NAME=a\rG28"
        with self.assertRaises(rfile.ResumeRefused):
            rfile.build(st, "p", LINE, self.tail, overlap_s=0, overlap_mm=0,
                        park_xy=(15, 205), bed_mesh_profile="default")

    def test_hostile_current_object_refuses_the_build(self):
        st = copy.deepcopy(self.state)
        st.current_object = "a\nG28"
        with self.assertRaises(rfile.ResumeRefused):
            rfile.build(st, "p", LINE, self.tail, overlap_s=0, overlap_mm=0,
                        park_xy=(15, 205), bed_mesh_profile="default")

HEADER = [
    ";FLAVOR:Marlin", ";TIME:14744.32", ";Filament used:17.89m", ";Layer height:0.2",
    "; HEADER_BLOCK_START", "; generated by OrcaSlicer 2.4.2 on 2026-11-07",
    "; total layer number: 500", "; HEADER_BLOCK_END", "",
    "; THUMBNAIL_BLOCK_START", "; thumbnail begin 32x32 671", "; iVBORw0KGgo=",
    "; thumbnail end", "; THUMBNAIL_BLOCK_END",
]


class UiMetadata(unittest.TestCase):
    """A9: the resume file keeps the slicer header and thumbnails, scales the
    estimates to what is left, and sets the layer counter."""

    @classmethod
    def setUpClass(cls):
        cls.state = gs.state_at(fx.records(), LINE, fx.limits())
        cls.tail = ["G1 X1 Y1 E1", ";LAYER_CHANGE", ";Z:21", "G1 X2 Y2 E1",
                    ";LAYER_CHANGE", ";Z:21.2", "G1 X3 E1",
                    "; estimated printing time (normal mode) = 4h 5m 44s", "; max_z_height: 100"]

    def build(self, **kw):
        args = dict(overlap_s=1.0, overlap_mm=10.0, park_xy=(15, 205),
                    bed_mesh_profile="default", header_lines=HEADER)
        args.update(kw)
        return rfile.build(self.state, "part.gcode", LINE, self.tail, **args).split("\n")

    def test_header_comes_first_and_is_unchanged_without_a_fraction(self):
        out = self.build()
        self.assertTrue(out[0].startswith("; print-rescue resume PREVIEW"))
        self.assertEqual(out[1:1 + len(HEADER)], HEADER)
        self.assertIn("; thumbnail begin 32x32 671", out)

    def test_estimates_are_scaled_to_the_remaining_fraction(self):
        out = self.build(remaining_fraction=0.5)
        self.assertIn(";TIME:7372.16", out)
        self.assertIn(";Filament used:8.95m", out)
        self.assertIn("; estimated printing time (normal mode) = 2h 2m 52s", out)
        self.assertIn("; total layer number: 500", out)

    def test_a_crafted_estimate_line_is_cheap(self):
        import time
        line = "; estimated printing time (normal mode) = " + " " * 5000 + "x"
        t0 = time.time()
        self.assertEqual(rfile.rewrite_estimate(line, 0.5), line)
        self.assertLess(time.time() - t0, 1.0)

    def test_bad_fraction_leaves_estimates_alone(self):
        for bad in (0.0, 1.5, -1.0):
            out = self.build(remaining_fraction=bad)
            self.assertIn(";TIME:14744.32", out)

    def test_layer_counter_at_the_start_and_after_each_layer_change(self):
        out = self.build()
        n = self.state.layer
        self.assertIn("SET_PRINT_STATS_INFO TOTAL_LAYER=500 CURRENT_LAYER=%d" % n, out)
        k = out.index(";LAYER_CHANGE")
        self.assertEqual(out[k + 1], "SET_PRINT_STATS_INFO CURRENT_LAYER=%d" % (n + 1))
        k2 = out.index(";LAYER_CHANGE", k + 1)
        self.assertEqual(out[k2 + 1], "SET_PRINT_STATS_INFO CURRENT_LAYER=%d" % (n + 2))

    def test_stale_message_is_cleared_after_the_shutdown_cancel(self):
        out = self.build()
        self.assertEqual(out.index("M117"), out.index("CANCEL_PRINTER_SHUTDOWN") + 1)

    def test_a_command_in_the_header_is_refused(self):
        with self.assertRaises(rfile.ResumeRefused):
            self.build(header_lines=[";FLAVOR:Marlin", "G28"])

    def test_non_ascii_header_is_refused(self):
        with self.assertRaises(rfile.ResumeRefused):
            self.build(header_lines=[";FLAVOR:Marlin\x01"])

    def test_no_layer_counter_without_a_total(self):
        out = rfile.build(self.state, "part.gcode", LINE, self.tail, overlap_s=1.0,
                          overlap_mm=1.0, park_xy=(15, 205), bed_mesh_profile="default"
                          ).split("\n")
        self.assertFalse(any(ln.startswith("SET_PRINT_STATS_INFO") for ln in out))


class PrimeAmount(unittest.TestCase):
    """The original lines undo state.retracted; the gate retracts at least
    retract_before_travel, so the prime is the rest plus the extra."""

    @classmethod
    def setUpClass(cls):
        cls.base = gs.state_at(fx.records(), LINE, fx.limits())

    def prime(self, retracted):
        import copy
        st = copy.deepcopy(self.base)
        st.retracted = retracted
        out = rfile.build(st, "part.gcode", LINE, ["G1 X1 Y1 E1"], overlap_s=1.0,
                          overlap_mm=1.0, park_xy=(15, 205),
                          bed_mesh_profile="default").split("\n")
        line = next(ln for ln in out if "; prime:" in ln)
        return float(line.split()[1][1:])

    def test_nothing_retracted_by_the_slicer(self):
        self.assertAlmostEqual(self.prime(0.0), 3.3)

    def test_partly_retracted(self):
        self.assertAlmostEqual(self.prime(1.0), 2.3)

    def test_slicer_retraction_larger_than_the_pre_retract_is_not_under_primed(self):
        self.assertAlmostEqual(self.prime(3.0), 0.8)
        self.assertAlmostEqual(self.prime(3.5), 0.8)


class RemainingFraction(unittest.TestCase):
    def test_net_filament_survives_g92_resets_and_gives_the_fraction(self):
        lines = ["M83", "G1 X1 Y1 E10", "G92 E0", "G1 X2 Y2 E10", "G1 E-2", "G1 X3 Y3 E10",
                 "G1 X4 Y4 E1"]
        recs = [(i + 1, 0, t) for i, t in enumerate(lines)]
        st = gs.state_at(iter(recs), 7)
        self.assertAlmostEqual(st.e_total, 28.0)
        frac = rfile.remaining_fraction(st, [";Filament used:0.05m"])
        self.assertAlmostEqual(frac, (50.0 - 28.0) / 50.0)

    def test_unknown_cases_give_none(self):
        st = gs.State()
        self.assertIsNone(rfile.remaining_fraction(st, [";FLAVOR:Marlin"]))
        st.e_total = 99999.0
        self.assertIsNone(rfile.remaining_fraction(st, [";Filament used:1.00m"]))
        st.e_total, st.e_absolute = 1.0, True
        self.assertIsNone(rfile.remaining_fraction(st, [";Filament used:1.00m"]))

    def test_header_stops_at_the_first_command(self):
        recs = [(1, 0, ";FLAVOR:Marlin"), (2, 0, ""), (3, 0, "; x"), (4, 0, "M73 P0"),
                (5, 0, ";late comment")]
        self.assertEqual(rfile.header_from_records(iter(recs)), [";FLAVOR:Marlin", "", "; x"])


class GateStop(unittest.TestCase):
    def test_gate_stop_line_is_accepted_by_the_allow_list(self):
        self.assertEqual(rfile.check_generated(["G90", rfile.GATE_MARK, rfile.GATE_STOP]), [])

    def test_gate_stop_is_not_a_valid_command(self):
        # The line must fail in Klipper; the allow-list must not know it.
        self.assertNotIn(rfile.GATE_STOP.split()[0], rfile.ALLOWED)
        self.assertTrue(rfile.check_generated(["G90", rfile.GATE_STOP + " X1"]))



if __name__ == "__main__":
    unittest.main()
