import copy
import os
import unittest
from unittest import skipUnless

import machine_profile as prof
import rescue_fixtures as fx

CFG = """
[stepper_x]
position_min: -6
position_max: 230   # comment
[stepper_y]
position_min: -14
position_max: 230
[stepper_z]
rotation_distance: 8
position_min: -3
position_max: 250
[printer]
max_velocity: 200
max_accel: 4000
square_corner_velocity: 5.0
[bltouch]
x_offset: -23.0
y_offset: -14.5
[safe_z_home]
home_xy_position: 134,123
z_hop: 10
[bed_mesh]
mesh_min: 30,30
#mesh_max: 207,215.5
mesh_max: 190,190

#*# <---------------------- SAVE_CONFIG ---------------------->
#*# DO NOT EDIT THIS BLOCK OR BELOW. The contents are auto-generated.
#*#
#*# [bltouch]
#*# z_offset = 3.035
#*#
#*# [bed_mesh default]
#*# points =
#*# 	0.072500, 0.065000
"""


class Validate(unittest.TestCase):
    def test_template_is_valid(self):
        self.assertEqual(prof.validate(fx.profile()), [])

    def test_schema_version_and_source(self):
        p = fx.profile()
        p["schema"] = "x"
        p["version"] = 2
        p["envelope"]["x_max_safe"]["source"] = "guess"
        errs = " ".join(prof.validate(p))
        self.assertIn("schema", errs)
        self.assertIn("version", errs)
        self.assertIn("guess", errs)

    def test_missing_section(self):
        p = fx.profile()
        del p["no_go_zones"]
        self.assertIn("missing section 'no_go_zones'", prof.validate(p))

    def test_bad_zone_frame(self):
        p = fx.profile()
        p["no_go_zones"][0]["value"]["frame"] = "toolhead"
        self.assertTrue(any("frame" in e for e in prof.validate(p)))

    def test_load_rejects_invalid(self):
        import json
        import tempfile
        p = fx.profile()
        p["version"] = 99
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "p.json")
            with open(path, "w") as fh:
                json.dump(p, fh)
            with self.assertRaises(prof.ProfileError):
                prof.load_profile(path)


class MotionReady(unittest.TestCase):
    def test_template_fails_closed(self):
        missing = prof.motion_ready(fx.profile())
        self.assertIn("envelope.z_top_margin (source default)", missing)
        self.assertEqual(sum("head_footprint.tiers" in m for m in missing), 4)

    def test_measured_profile_is_ready(self):
        self.assertEqual(prof.motion_ready(fx.measured_profile()), [])

    def test_plain_values_carry_no_provenance(self):
        p = fx.profile()
        self.assertEqual(prof.val(p["clearances"]["min_lift"]), 10)
        self.assertEqual(prof.val(p["envelope"]["x_max_safe"]), 205)


class Fingerprint(unittest.TestCase):
    def test_parse_with_save_config(self):
        cfg = prof.parse_printer_cfg(CFG)
        self.assertEqual(cfg["bltouch"]["z_offset"], "3.035")
        self.assertEqual(cfg["stepper_x"]["position_max"], "230")
        self.assertEqual(cfg["bed_mesh"]["mesh_max"], "190,190")

    def test_matching_config(self):
        self.assertEqual(prof.fingerprint_mismatches(fx.profile(), prof.parse_printer_cfg(CFG)), [])

    def test_x_offset_change_is_r14(self):
        cfg = prof.parse_printer_cfg(CFG.replace("x_offset: -23.0", "x_offset: -25.0"))
        mm = prof.fingerprint_mismatches(fx.profile(), cfg)
        self.assertEqual(len(mm), 1)
        self.assertIn("bltouch.x_offset", mm[0])

    def test_missing_option(self):
        cfg = prof.parse_printer_cfg(CFG.replace("z_hop: 10\n", ""))
        self.assertTrue(any("z_hop: missing" in m for m in prof.fingerprint_mismatches(fx.profile(), cfg)))

    @skipUnless(os.path.exists(fx.REPO_PRINTER_CFG), "repo config not present")
    def test_repo_printer_cfg_matches_profile(self):
        # Changing config/printer.cfg (offsets, limits, safe_z_home, z_offset
        # after PROBE_CALIBRATE) must come with a profile update.
        cfg = prof.parse_printer_cfg(prof.read_text(fx.REPO_PRINTER_CFG))
        self.assertEqual(prof.fingerprint_mismatches(fx.profile(), cfg), [])

    def test_probe_offsets(self):
        self.assertEqual(prof.probe_offsets(fx.profile()), (-23.0, -14.5, 3.035))

    def test_profile_not_mutated(self):
        p = fx.profile()
        before = copy.deepcopy(p)
        prof.fingerprint_mismatches(p, prof.parse_printer_cfg(CFG))
        self.assertEqual(p, before)


class Provenance(unittest.TestCase):
    """N1: only explicitly measured and confirmed values count as verified."""

    def test_config_and_user_values_are_not_verified(self):
        p = fx.measured_profile()
        p["envelope"]["x_max_safe"]["source"] = "user"
        p["config_fingerprint"]["stepper_x"]["position_max"]["source"] = "config"
        missing = prof.motion_ready(p)
        self.assertTrue(any(m.startswith("envelope.x_max_safe (source user)") for m in missing))
        self.assertTrue(any("position_max (source config)" in m for m in missing))

    def test_measured_needs_a_date_and_a_method(self):
        p = fx.measured_profile()
        del p["envelope"]["z_max"]["measured_on"]
        p["envelope"]["x_max_safe"]["method"] = ""
        missing = prof.motion_ready(p)
        self.assertTrue(any("envelope.z_max" in m for m in missing))
        self.assertTrue(any("envelope.x_max_safe" in m for m in missing))

    def test_is_verified_is_fail_closed(self):
        leaf = {"value": 1, "source": "measured", "safety_critical": True,
                "measured_on": "2026-11-07", "method": "caliper"}
        self.assertTrue(prof.is_verified(leaf))
        for change in ({"source": "config"}, {"source": "user"}, {"measured_on": "today"},
                       {"method": None}):
            with self.subTest(change=change):
                self.assertFalse(prof.is_verified({**leaf, **change}))

    def test_template_has_no_verified_safety_leaf_from_config_or_user(self):
        missing = prof.motion_ready(fx.profile())
        self.assertTrue(any("(source config)" in m for m in missing))
        self.assertTrue(any("(source user)" in m for m in missing))


class Validation(unittest.TestCase):
    """N2: types, ranges, finite numbers and required keys are checked."""

    def _errs(self, mutate):
        p = fx.profile()
        mutate(p)
        return prof.validate(p)

    CASES = {
        "version as string": lambda p: p.__setitem__("version", "1"),
        "version as bool": lambda p: p.__setitem__("version", True),
        "margin_s negative": lambda p: p["stop_estimation"].__setitem__("margin_s", -0.5),
        "margin_s nan": lambda p: p["stop_estimation"].__setitem__("margin_s", float("nan")),
        "margin_s infinite": lambda p: p["stop_estimation"].__setitem__("margin_s", float("inf")),
        "margin_s string": lambda p: p["stop_estimation"].__setitem__("margin_s", "0.5"),
        "max_overlap_s missing": lambda p: p["stop_estimation"].pop("max_overlap_s"),
        "min_resume_layer float": lambda p: p["stop_estimation"].__setitem__("min_resume_layer", 1.5),
        "probe_area missing": lambda p: p["envelope"].pop("probe_area"),
        "probe_area incomplete": lambda p: p["envelope"]["probe_area"].__setitem__(
            "value", {"x_min": 0}),
        "x_max_safe above x_max": lambda p: p["envelope"]["x_max_safe"].__setitem__("value", 300),
        "envelope value a string": lambda p: p["envelope"]["x_min"].__setitem__("value", "x"),
        "tier z_from above z_to": lambda p: p["head_footprint"]["tiers"][0]["value"].__setitem__(
            "z_from", 5),
        "tier x_min above x_max": lambda p: p["head_footprint"]["tiers"][0]["value"].__setitem__(
            "x_min", 9),
        "no-go margin negative": lambda p: p["no_go_zones"][0]["value"].__setitem__(
            "margin", -1),
        "no-go allowed_callers a string": lambda p: p["no_go_zones"][0]["value"].__setitem__(
            "allowed_callers", "_SP_TIP_FORM"),
        "motion max_velocity zero": lambda p: p["motion"].__setitem__("max_velocity", 0),
        "motion max_velocity a string": lambda p: p["motion"].__setitem__("max_velocity", "200"),
        "motion key missing": lambda p: p["motion"].pop("max_accel"),
        "thermal retract missing": lambda p: p["thermal"].pop("retract_before_travel"),
        "clearance negative": lambda p: p["clearances"].__setitem__("probe_spot_margin", -5),
        "bed mesh name with a newline": lambda p: p.__setitem__(
            "bed_mesh_profile", "default\nG28"),
        "bed mesh name missing": lambda p: p.pop("bed_mesh_profile"),
        "park missing": lambda p: p.pop("park"),
        "park with three numbers": lambda p: p["park"]["xy"].__setitem__("value", [1, 2, 3]),
        "probe spot not an object": lambda p: p.__setitem__("probe_spots", [5]),
        "nan in a leaf": lambda p: p["envelope"]["z_max"].__setitem__("value", float("nan")),
        "printer is a list": lambda p: p.__setitem__("printer", []),
        "clearances is a list": lambda p: p.__setitem__("clearances", []),
        "stop_estimation is null": lambda p: p.__setitem__("stop_estimation", None),
    }

    def test_each_malformed_profile_is_refused(self):
        for name, mutate in self.CASES.items():
            with self.subTest(name):
                self.assertTrue(self._errs(mutate), name)

    def test_validate_never_raises_on_junk(self):
        junk = (None, 1, "x", [], [1], {}, True, float("nan"))
        for section in list(fx.profile().keys()):
            for value in junk:
                with self.subTest(section=section, value=repr(value)):
                    p = fx.profile()
                    p[section] = value
                    self.assertIsInstance(prof.validate(p), list)
        for path, _ in prof._walk(fx.profile()):
            for value in junk:
                with self.subTest(path=path, value=repr(value)):
                    p = fx.profile()
                    dict(prof._walk(p))[path]["value"] = value
                    self.assertIsInstance(prof.validate(p), list)

    def test_load_wraps_wrong_shapes_in_profile_error(self):
        import json
        import tempfile
        p = fx.profile()
        p["envelope"] = 5
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "p.json")
            with open(path, "w") as fh:
                json.dump(p, fh)
            with self.assertRaises(prof.ProfileError):
                prof.load_profile(path)


if __name__ == "__main__":
    unittest.main()
