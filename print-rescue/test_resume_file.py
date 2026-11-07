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
        self.assertEqual(out[4:8], ["G90", "G21", "M83", "SET_IDLE_TIMEOUT TIMEOUT=600"])
        # Heat only after the off-part park (N6); Z relative until the gate (N5).
        order = ["EXCLUDE_OBJECT_DEFINE", "SET_PRESSURE_ADVANCE", "SET_VELOCITY_LIMIT",
                 "M220", "M221", "M106", "SET_GCODE_OFFSET", "G92 E0", "G1 E-2.5",
                 "G91", "G1 Z3", "G90", "G1 X15 Y205", "BED_MESH_PROFILE",
                 "M140", "M190", "M104", "M109", "G1 X170.721 Y131.153",
                 rfile.GATE_MARK, "G1 Z20.6", "EXCLUDE_OBJECT_START", "G1 E3.3",
                 "G92 E0", "G1 F6000", "; ---- original file"]
        idx = []
        pos = 0
        for prefix in order:
            k = next(i for i in range(pos, len(out)) if out[i].startswith(prefix))
            idx.append(k)
            pos = k + 1
        self.assertEqual(idx, sorted(idx))

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
                     "ps2\nG28.gcode", "café.gcode", "tab\tname.gcode"]:
            with self.subTest(name=repr(name)):
                with self.assertRaises(rfile.ResumeRefused):
                    self.build(name)

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

class GateStop(unittest.TestCase):
    def test_gate_stop_line_is_accepted_by_the_allow_list(self):
        self.assertEqual(rfile.check_generated(["G90", rfile.GATE_MARK, rfile.GATE_STOP]), [])

    def test_gate_stop_is_not_a_valid_command(self):
        # The line must fail in Klipper; the allow-list must not know it.
        self.assertNotIn(rfile.GATE_STOP.split()[0], rfile.ALLOWED)
        self.assertTrue(rfile.check_generated(["G90", rfile.GATE_STOP + " X1"]))



if __name__ == "__main__":
    unittest.main()
