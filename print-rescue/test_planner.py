import math
import unittest

import planner
import rescue_fixtures as fx
from gcode_scan import Dwell, Limits, Move


def mv(line, start, end, speed=100.0, accel=1000.0, mcr=0.0, scv=5.0, vmax=200.0):
    s = tuple(start) + (0.0,) * (4 - len(start))
    e = tuple(end) + (0.0,) * (4 - len(end))
    return Move(line=line, start=s, end=e, speed=speed, max_velocity=vmax,
                accel=accel, accel_to_decel=accel * (1 - mcr),
                square_corner_velocity=scv, extruding=e[3] > s[3])


class Trapezoid(unittest.TestCase):
    def test_rest_to_rest_trapezoid(self):
        # 100 mm at 100 mm/s, 1000 mm/s^2: t = d/v + v/a = 1.1 s
        t = planner.plan([mv(1, (0, 0), (100, 0))])
        self.assertAlmostEqual(t[0].duration, 1.1, places=9)
        self.assertAlmostEqual(t[0].cruise_v, 100.0)

    def test_triangle(self):
        # 1 mm cannot reach 100 mm/s: t = 2 * sqrt(d / a)
        t = planner.plan([mv(1, (0, 0), (1, 0))])
        self.assertAlmostEqual(t[0].duration, 2 * math.sqrt(1 / 1000.0), places=9)

    def test_collinear_moves_keep_full_speed(self):
        t = planner.plan([mv(1, (0, 0), (50, 0)), mv(2, (50, 0), (100, 0))])
        self.assertAlmostEqual(t[-1].t_end, 1.1, places=9)
        self.assertAlmostEqual(t[0].end_v, 100.0)

    def test_square_corner_velocity(self):
        # Klipper's junction deviation is defined so a 90 degree corner is
        # taken at exactly square_corner_velocity.
        t = planner.plan([mv(1, (0, 0), (100, 0)), mv(2, (100, 0), (100, 100))])
        self.assertAlmostEqual(t[0].end_v, 5.0, places=9)
        self.assertAlmostEqual(t[1].start_v, 5.0, places=9)

    def test_mixed_speed_chain_matches_the_forks_two_pass_flush(self):
        # 10/1/10/1 mm collinear at 200/100/100/10 mm/s, accel 1000, ratio 0.5:
        # 0.4043545 s with the fork's uncapped peak and forward propagation
        # (a capped peak gives 0.4055 s).
        moves = [mv(1, (0, 0), (10, 0), speed=200, mcr=0.5),
                 mv(2, (10, 0), (11, 0), speed=100, mcr=0.5),
                 mv(3, (11, 0), (21, 0), speed=100, mcr=0.5),
                 mv(4, (21, 0), (22, 0), speed=10, mcr=0.5)]
        t = planner.plan(moves)
        self.assertAlmostEqual(t[-1].t_end, 0.4043545, places=6)

    def test_minimum_cruise_ratio(self):
        # 10 mm, v 100, a 1000. Without the ratio: triangle peak 100 -> 0.2 s.
        # With 0.5, accel_to_decel 500 caps the peak at sqrt(5000).
        t0 = planner.plan([mv(1, (0, 0), (10, 0), mcr=0.0)])[0]
        self.assertAlmostEqual(t0.duration, 0.2, places=9)
        t5 = planner.plan([mv(1, (0, 0), (10, 0), mcr=0.5)])[0]
        v = math.sqrt(5000.0)
        expect = 2 * 2.5 / (v / 2) + 5.0 / v
        self.assertAlmostEqual(t5.cruise_v, v, places=9)
        self.assertAlmostEqual(t5.duration, expect, places=9)

    def test_extrude_only_limits(self):
        # E 2.5 at 50 mm/s -> max_extrude_only_velocity 45, accel 3000
        t = planner.plan([mv(1, (0, 0, 0, 0), (0, 0, 0, 2.5), speed=50.0)], Limits())[0]
        self.assertAlmostEqual(t.duration, 2.5 / 45 + 45 / 3000.0, places=9)
        self.assertTrue(t.unretract)

    def test_z_limits(self):
        # Z 10 at 10 mm/s -> max_z_velocity 5, max_z_accel 100
        t = planner.plan([mv(1, (0, 0, 0), (0, 0, 10), speed=10.0)], Limits())[0]
        self.assertAlmostEqual(t.duration, 10 / 5.0 + 5 / 100.0, places=9)

    def test_velocity_limit(self):
        t = planner.plan([mv(1, (0, 0), (100, 0), speed=300.0, vmax=100.0)])[0]
        self.assertAlmostEqual(t.cruise_v, 100.0)

    def test_dwell_stops_and_adds_time(self):
        items = [mv(1, (0, 0), (50, 0)), Dwell(2, 0.5, "G4"), mv(3, (50, 0), (100, 0))]
        t = planner.plan(items)
        self.assertAlmostEqual(t[0].end_v, 0.0)
        self.assertAlmostEqual(t[-1].t_end, 2 * (0.5 + 0.1) + 0.5, places=9)

    def test_travel_flags(self):
        t = planner.plan([mv(1, (0, 0, 0, 0), (10, 0, 0, 0)),
                          mv(2, (10, 0, 0, 0), (20, 0, 0, 1))])
        self.assertTrue(t[0].travel)
        self.assertFalse(t[1].travel)
        self.assertTrue(t[1].extruding)


class AgainstKlipperLog(unittest.TestCase):
    """The planner must track Klipper's own print_time in the incident log.

    For each Stats sample, sd_pos names the line being dispatched. Between two
    samples, Klipper's print_time advance and the planned time between those
    lines must agree (only Klipper's forced lookahead flushes and its handling
    of single long moves make it slightly slower)."""

    def test_print_time_advance_matches_plan(self):
        ev, res, timed = fx.incident()
        import gcode_scan as gs
        import stop_point as sp
        pts = []
        for s in ev.samples:
            if s.index > ev.good.index or s.sd_pos is None:
                continue
            ln = gs.line_at_byte(res, s.sd_pos)
            t = sp.line_start_t(timed, ln) if ln and ln >= timed[0].line else None
            if t is not None and ln >= fx.CUT_LAYER_START + 20:
                pts.append((s.print_time, t))
        self.assertGreaterEqual(len(pts), 15)
        dp = pts[-1][0] - pts[0][0]
        dt = pts[-1][1] - pts[0][1]
        self.assertGreater(dp, 15.0)
        # Klipper ran 0-5 % slower than the plan over the cut layer.
        self.assertGreaterEqual(dp / dt, 1.0)
        self.assertLess(dp / dt, 1.05)


if __name__ == "__main__":
    unittest.main()
