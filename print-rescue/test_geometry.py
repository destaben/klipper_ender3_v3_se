import unittest

import geometry as g
import rescue_fixtures as fx

# The incident part: extrusion box through the last dispatched line.
PART = g.Part(29.797, 188.425, 29.797, 188.41, 20.6)


class Basics(unittest.TestCase):
    def test_rects(self):
        self.assertTrue(g.rects_overlap((0, 10, 0, 10), (5, 15, 5, 15)))
        self.assertFalse(g.rects_overlap((0, 10, 0, 10), (10, 15, 0, 10)))
        self.assertEqual(g.point_rect_distance(13, 14, (0, 10, 0, 10)), 5.0)
        self.assertEqual(g.point_rect_distance(5, 5, (0, 10, 0, 10)), 0.0)

    def test_gantry_zone_ignores_y(self):
        zone = {"frame": "gantry", "x": [205, None], "y": None, "z": None, "margin": 0}
        self.assertTrue(g.point_in_zone(210, -50, None, zone))
        self.assertFalse(g.point_in_zone(204.9, 0, None, zone))
        self.assertTrue(g.point_in_zone(203, 0, None, zone, extra_margin=2))

    def test_bed_zone_checks_y(self):
        zone = {"frame": "bed", "x": [0, 10], "y": [0, 10], "margin": 1}
        self.assertTrue(g.point_in_zone(5, 10.5, None, zone))
        self.assertFalse(g.point_in_zone(5, 12, None, zone))

    def test_cutter_caller_allowed(self):
        p = fx.profile()
        self.assertEqual(g.zones_hit(p, 225, 110), ["cutter lever"])
        self.assertEqual(g.zones_hit(p, 225, 110, caller="_SP_TIP_FORM"), [])

    def test_pass1_height_includes_deployed_pin(self):
        # top 20.6 + travel_clearance 5 + 5 mm of deployed pin below the tip
        self.assertAlmostEqual(g.pass1_height(PART, fx.profile()), 30.6)

    def test_too_tall(self):
        p = fx.profile()
        self.assertIsNone(g.too_tall(PART, p))
        tall = g.Part(50, 60, 50, 60, 240.0)
        self.assertIn("top 240.0", g.too_tall(tall, p))


class ProbeSpots(unittest.TestCase):
    def setUp(self):
        self.p = fx.measured_profile()

    def test_cutter_side_spot_rejected(self):
        v = g.check_probe_spot({"x": 224, "y": 110}, PART, self.p)
        self.assertFalse(v.ok)
        text = " ".join(v.reasons)
        self.assertIn("x_max_safe", text)
        self.assertIn("cutter lever", text)

    def test_footprint_rejects_spot_beside_the_part(self):
        # Nozzle at X=195, just right of the part edge (188.4); the probe
        # point (172, 20.5) is off the part, the nozzle tier clears it, but the
        # heater block tier hangs over the part at probe height.
        v = g.check_probe_spot({"x": 195, "y": 35}, PART, self.p)
        self.assertFalse(v.ok)
        self.assertTrue(all(r.startswith("head tier") for r in v.reasons), v.reasons)
        self.assertIn("head tier", v.reasons[0])

    def test_rear_spot_accepted(self):
        spot = self.p["probe_spots"][0]["value"]
        v = g.check_probe_spot(spot, PART, self.p)
        self.assertTrue(v.ok, v.reasons)

    def test_probe_point_on_the_part_rejected(self):
        v = g.check_probe_spot({"x": 120, "y": 120}, PART, self.p)
        self.assertTrue(any("from the part" in r for r in v.reasons))

    def test_missing_bed_area_refuses_instead_of_skipping(self):
        # N2: without a bed rectangle the spot cannot be checked, so it is refused.
        del self.p["envelope"]["bed"]
        v = g.check_probe_spot({"x": 60, "y": 225}, PART, self.p)
        self.assertFalse(v.ok)
        self.assertTrue(any("bed area" in r for r in v.reasons), v.reasons)

    def test_probe_point_off_the_bed_is_a_hard_reason(self):
        self.p["envelope"]["bed"]["value"] = {"x_min": 0, "x_max": 100, "y_min": 0, "y_max": 100}
        v = g.check_probe_spot({"x": 60, "y": 225}, PART, self.p)
        self.assertTrue(any("off the bed" in r for r in v.reasons), v.reasons)

    def test_outside_the_mesh_is_only_a_warning_with_the_distance(self):
        # Real profile: the mesh ends at 190. A nozzle at Y 225 probes at
        # Y 210.5, 20.5 mm beyond it; that is allowed but reported.
        p = fx.measured_profile()
        p["envelope"]["probe_area"]["value"] = {"x_min": 30, "x_max": 190,
                                                "y_min": 30, "y_max": 190}
        small = g.Part(100, 120, 100, 120, 10.0)
        v = g.check_probe_spot({"x": 60, "y": 225}, small, p)
        self.assertTrue(v.ok, v.reasons)
        self.assertAlmostEqual(v.mesh_extrapolation_mm, 20.5, places=1)
        self.assertTrue(any("mesh" in w for w in v.warnings))


class IncidentThreeGeometry(unittest.TestCase):
    """The 2026-11-07 vase (X/Y 71.1-148.9, top 53.0) with the owner's head
    estimate (left 70, right 100, front 50, back 65). The live run used a
    REAR spot (nozzle X134 Y215) that is outside the bed mesh; any front
    spot puts the crossbar over the part."""

    PART3 = g.Part(71.1, 148.9, 71.1, 148.9, 53.0)

    def setUp(self):
        self.p = fx.measured_profile(owner_footprint=True)

    def test_rear_spot_of_the_live_run_is_accepted_with_a_mesh_warning(self):
        v = g.check_probe_spot({"x": 134, "y": 215}, self.PART3, self.p)
        self.assertTrue(v.ok, v.reasons)
        self.p["envelope"]["probe_area"]["value"] = {"x_min": 30, "x_max": 190,
                                                     "y_min": 30, "y_max": 190}
        v = g.check_probe_spot({"x": 134, "y": 215}, self.PART3, self.p)
        self.assertTrue(v.ok, v.reasons)
        self.assertAlmostEqual(v.mesh_extrapolation_mm, 10.5, places=1)
        self.assertAlmostEqual(v.margin_mm, 16.1, places=1)

    def test_every_front_spot_is_rejected(self):
        for y in (-14, -9, 0, 10, 30, 60):
            for x in (0, 60, 134, 200):
                with self.subTest(x=x, y=y):
                    v = g.check_probe_spot({"x": x, "y": y}, self.PART3, self.p)
                    self.assertFalse(v.ok)

    def test_search_returns_only_rear_spots_and_ranks_the_live_one_first(self):
        found = g.suggest_probe_spots(self.PART3, self.p, limit=500)
        self.assertTrue(found)
        self.assertTrue(all(spot["y"] >= 202 for spot, _ in found), found[:3])
        best, v = found[0]
        self.assertEqual(best["y"], 215)
        self.assertTrue(v.ok)
        self.assertTrue(any(abs(s["x"] - 134) <= 5 and s["y"] == 215 for s, _ in found))

    def test_search_finds_nothing_when_the_part_fills_the_bed(self):
        huge = g.Part(0, 220, 0, 220, 50.0)
        self.assertEqual(g.suggest_probe_spots(huge, self.p), [])


class PinAtBoot(unittest.TestCase):
    def test_incident_stop_puts_the_pin_over_the_part(self):
        # Nozzle positions along the incident stop window (lines 202002-202238)
        v = g.check_pin_at_boot([(141.5, 101.8), (80.2, 37.8), (133.9, 91.3)],
                                PART, fx.profile())
        self.assertFalse(v.ok)
        self.assertIn("over the part", v.reasons[0])

    def test_head_parked_off_the_part(self):
        v = g.check_pin_at_boot([(225, 225)], PART, fx.profile())
        self.assertTrue(v.ok)


if __name__ == "__main__":
    unittest.main()
