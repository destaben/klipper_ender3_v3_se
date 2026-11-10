import copy
import os
import unittest

import machine_profile as mp
import rescue_run as rr

PROFILE = os.path.join(os.path.dirname(__file__), "profiles", "creality-ender3-v3-se.json")

JOB = {
    "resume_file": "resume_test.gcode",
    "resume": {"park_xy": [108.953, 81.017], "layer_z": 81.6},
    "part": {"x0": 71.07, "x1": 148.93, "y0": 71.07, "y1": 148.93, "top": 81.6},
    "temps": {"bed": 60, "nozzle": 220},
}
REAR = (134.0, 215.0)


class FakeMoonraker:
    """Just enough of Moonraker/Klipper to run the sequence: tracks X/Y/Z, relative mode, temperatures and the command log."""

    def __init__(self, probe_script=None, z_offset=2.96, queue=None, files=None, bed=60.0, print_state="standby"):
        self.cmds, self.calls, self.xy_moves = [], [], []
        self.x = self.y = self.z = 0.0
        self.rel = False
        self.probe_script = list(probe_script or ["ok"])
        self.z_offset, self.queue, self.bed = z_offset, queue or [], bed
        self.files = files if files is not None else ["resume_test.gcode"]
        self.targets = {"extruder": 0, "bed": 0}
        self.started = None
        self.print_state = print_state

    def _script(self, cmd):
        self.cmds.append(cmd)
        if cmd == "G91":
            self.rel = True
        elif cmd == "G90":
            self.rel = False
        elif cmd.startswith("SET_KINEMATIC_POSITION"):
            self.z = float(cmd.split("Z=")[1].split()[0])
        elif cmd == "G28 X":
            self.x = -6.0
        elif cmd == "G28 Y":
            self.y = -14.0
        elif cmd.startswith("G1 "):
            d = {}
            for tok in cmd.split()[1:]:
                if tok[0] in "XYZ":
                    d[tok[0]] = float(tok[1:])
            if "X" in d or "Y" in d:
                self.xy_moves.append((self.z, d.get("X", self.x), d.get("Y", self.y)))
            self.x = self.x + d["X"] if self.rel and "X" in d else d.get("X", self.x)
            self.y = self.y + d["Y"] if self.rel and "Y" in d else d.get("Y", self.y)
            self.z = self.z + d["Z"] if self.rel and "Z" in d else d.get("Z", self.z)
        elif cmd.startswith("M104 S"):
            self.targets["extruder"] = int(cmd.split("S")[1])
        elif cmd.startswith("PROBE"):
            r = self.probe_script.pop(0)
            if r == "ok":
                self.z = self.z_offset - 5.0
            elif r == "notrigger":
                self.z = -3.0
                return {"error": {"message": "No trigger on probe after full movement"}}
            elif r == "alarm":
                return {"error": {"message": "Probe triggered prior to movement"}}
        return {"result": "ok"}

    def __call__(self, method, path, data=None, timeout=60):
        self.calls.append((method, path))
        if path == "/printer/gcode/script":
            return self._script(data["script"])
        if path == "/printer/info":
            return {"result": {"state": "ready"}}
        if path.startswith("/printer/objects/query"):
            return {"result": {"status": {
                "toolhead": {"position": [self.x, self.y, self.z, 0]},
                "print_stats": {"state": self.print_state},
                "configfile": {"settings": {"bltouch": {"z_offset": self.z_offset}}},
                "heater_bed": {"temperature": self.bed},
                "extruder": {"temperature": 220.0}}}}
        if path == "/server/job_queue/status":
            return {"result": {"queued_jobs": self.queue}}
        if path.startswith("/server/files/list"):
            return {"result": [{"path": f} for f in self.files]}
        if path == "/printer/print/start":
            self.started = data["filename"]
            return {"result": "ok"}
        return {"result": "ok"}


def make(fake, allow=True, **kw):
    out = []
    profile = mp.load_profile(PROFILE)
    r = rr.Runner(fake, profile, copy.deepcopy(JOB), REAR, allow_unverified=allow, sleep=lambda s: None, log=lambda *a: out.append(" ".join(map(str, a))), **kw)
    return r, out


class TestGuards(unittest.TestCase):
    def setUp(self):
        self.r, _ = make(FakeMoonraker())

    def test_low_travel_from_the_rear_to_the_park_point_is_blocked(self):
        for z in (50.0, 85.0):
            with self.assertRaises(rr.RescueError):
                self.r.check_xy_move(REAR, (108.953, 81.017), z)

    def test_the_same_travel_is_allowed_at_travel_height(self):
        self.r.check_xy_move(REAR, (108.953, 81.017), 100.0)

    def test_probe_spot_over_the_part_or_with_the_crossbar_over_it_is_refused(self):
        with self.assertRaises(rr.RescueError):
            self.r.check_probe_spot(110.0, 146.0)
        with self.assertRaises(rr.RescueError):
            self.r.check_probe_spot(110.0, 30.0)   # the head would sweep over the part
        self.r.check_probe_spot(*REAR)

    def test_margin_grows_the_head_box_only_when_unverified(self):
        a, _ = make(FakeMoonraker())
        self.assertTrue(a.unverified)
        x0, x1, y0, y1 = a.head_rect(100.0, 100.0)
        self.assertEqual((x0, x1, y0, y1), (100 - 70 - 10, 100 + 100 + 10, 100 - 50 - 10, 100 + 65 + 10))


class TestRun(unittest.TestCase):
    def test_refuses_without_the_unverified_permission_and_sends_no_motion(self):
        f = FakeMoonraker()
        r, out = make(f, allow=False)
        self.assertEqual(r.run(), 1)
        self.assertTrue(any("R13" in l for l in out))
        self.assertFalse([c for c in f.cmds if c.startswith(("G28", "G1 X", "PROBE"))])

    def test_a_refused_preflight_sends_no_command_at_all(self):
        # Regression: the abort cleanup ran after every refusal, so a rejected run still sent M104 S0 and a
        # relative Z lift. With a print active that would switch off the nozzle and raise Z on a live job.
        cases = [
            ("a print is active", dict(print_state="printing"), True),
            ("a paused print", dict(print_state="paused"), True),
            ("R13 unverified without the permission", {}, False),
            ("cold bed", dict(bed=30.0), True),
            ("z_offset mismatch", dict(z_offset=3.035), True),
            ("missing resume file", dict(files=[]), True),
            ("queued job", dict(queue=[{"filename": "x"}]), True),
        ]
        for name, kw, allow in cases:
            f = FakeMoonraker(**kw)
            r, out = make(f, allow=allow)
            self.assertEqual(r.run(), 1, name)
            self.assertEqual(f.cmds, [], "%s: no command may reach the printer, got %s" % (name, f.cmds))

    def test_a_failure_after_motion_started_still_cools_and_lifts(self):
        f = FakeMoonraker()
        r, out = make(f)
        r.probe_z = lambda: (_ for _ in ()).throw(rr.RescueError("probe failed"))
        self.assertEqual(r.run(), 1)
        self.assertIn("M104 S0", f.cmds)
        self.assertIn("G1 Z10 F300", f.cmds)

    def test_full_sequence_order_and_height_of_every_xy_move(self):
        f = FakeMoonraker(probe_script=["notrigger", "ok"])
        r, out = make(f)
        self.assertEqual(r.run(), 0, out)
        c = f.cmds
        self.assertLess(c.index("SET_KINEMATIC_POSITION Z=81.6 SET_HOMED=z"), c.index("G28 X"))
        self.assertLess(c.index("G28 X"), c.index("G28 Y"))
        self.assertLess(c.index("G28 Y"), c.index("PROBE PROBE_SPEED=5"))
        self.assertIn("SET_KINEMATIC_POSITION Z=2.96 SET_HOMED=z", c)
        self.assertEqual(f.started, "resume_test.gcode")
        self.assertIn("M140 S60", c)
        self.assertIn("M104 S220", c)
        # the file starts last, after the prime of 2.5 mm on the printed bead, with the nozzle at the layer Z
        self.assertEqual(c[-1], "G1 E2.5 F300")
        self.assertAlmostEqual(f.z, 81.6, places=2)
        # every XY move happened at Z >= part top + clearance, or with the head box clear of the part
        for z, x, y in f.xy_moves:
            self.assertTrue(z >= 86.6 or not r.hits_part(x, y), (z, x, y))

    def test_a_failed_power_off_stops_the_run_and_cools(self):
        f = FakeMoonraker(probe_script=["alarm", "ok"])
        original = f.__call__

        def failing(method, path, data=None, timeout=60):
            if path.startswith("/machine/device_power/off"):
                return {"error": {"message": "plug unreachable"}}
            return original(method, path, data, timeout)
        r, out = make(failing)
        f.calls = []
        self.assertEqual(r.run(), 1)
        self.assertTrue(any("power off failed" in l for l in out))
        self.assertNotIn("/machine/device_power/on?printer", [p for _, p in f.calls])

    def test_latched_probe_power_cycles_then_succeeds(self):
        f = FakeMoonraker(probe_script=["alarm", "ok"])
        r, out = make(f)
        self.assertEqual(r.run(), 0, out)
        paths = [p for _, p in f.calls]
        self.assertLess(paths.index("/machine/device_power/off?printer"), paths.index("/machine/device_power/on?printer"))
        self.assertEqual(f.cmds.count("G28 X"), 2)
        self.assertEqual(f.started, "resume_test.gcode")

    def test_cold_bed_is_refused(self):
        f = FakeMoonraker(bed=30.0)
        r, out = make(f)
        self.assertEqual(r.run(), 1)
        self.assertFalse([c for c in f.cmds if c.startswith("G28")])

    def test_z_offset_mismatch_is_refused(self):
        f = FakeMoonraker(z_offset=3.035)
        r, out = make(f)
        self.assertEqual(r.run(), 1)
        self.assertTrue(any("R14" in l for l in out))

    def test_missing_resume_file_or_queued_job_is_refused(self):
        for kw in ({"files": []}, {"queue": [{"filename": "x"}]}):
            f = FakeMoonraker(**kw)
            r, _ = make(f)
            self.assertEqual(r.run(), 1)
            self.assertFalse([c for c in f.cmds if c.startswith("G28")])

    def test_hot_dwell_over_the_limit_cools_and_lifts_instead_of_starting(self):
        f = FakeMoonraker()
        r, out = make(f)
        r.max_dwell = -1.0
        self.assertEqual(r.run(), 1)
        self.assertIsNone(f.started)
        self.assertIn("M104 S0", f.cmds)

    def test_dry_run_changes_nothing(self):
        f = FakeMoonraker()
        r, _ = make(f, dry_run=True)
        self.assertEqual(r.run(), 0)
        self.assertEqual(f.cmds, [])
        self.assertIsNone(f.started)


if __name__ == "__main__":
    unittest.main()
