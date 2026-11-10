"""print-rescue: run ONE rescue against a live printer through Moonraker (prototype of phase 2; no AI needed).

    python3 print-rescue/rescue_run.py --host <moonraker-host> --profile print-rescue/profiles/creality-ender3-v3-se.json \\
        --job <dir>/job.json --probe-spot 134,215 [--dry-run] [--allow-unverified]

Run it with a finger on the emergency stop. It performs, in one pass: preflight, Z reference (declare a lower bound, lift, home X/Y, bounded
PROBE loop at a bare-bed spot), heat + purge at that spot, mesh, travel to the park point, descent, prime, print start. Every XY move is
checked against the head footprint and the part BEFORE it is sent. A failed check stops the run; if the nozzle is hot over the part it is
switched off and lifted. --dry-run sends nothing that changes the printer (reads only). Standard library only.

Head footprint, probe offsets, z_offset, axis limits and hot-dwell limit come from the machine profile (single source of truth). While
safety-critical profile leaves are not measured (refusal R13) it refuses to move unless --allow-unverified is given; that flag is logged
and adds --margin mm on every side of the head box.
"""

import argparse
import json
import math
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Callable, Dict, List, Optional, Tuple

import machine_profile as mp


class RescueError(RuntimeError):
    pass


def http_api(host: str) -> Callable:
    def api(method: str, path: str, data: Optional[dict] = None, timeout: int = 60) -> dict:
        body = urllib.parse.urlencode(data).encode() if data is not None else None
        req = urllib.request.Request("http://%s%s" % (host, path), data=body, method=method)
        try:
            return json.load(urllib.request.urlopen(req, timeout=timeout))
        except urllib.error.HTTPError as e:
            try:
                err = json.loads(e.read().decode() or "{}").get("error", {})
            except ValueError:
                err = {}
            return {"error": {"message": err.get("message", str(e))}}
    return api


class Runner:
    def __init__(self, api: Callable, profile: dict, job: dict, probe_spot: Tuple[float, float], *, margin: float = 10.0,
                 v_clear: float = 5.0, allow_unverified: bool = False, dry_run: bool = False,
                 sleep: Callable = time.sleep, log: Callable = print, z_lower_bound: Optional[float] = None,
                 purge_mm: float = 10.0, lift_z: float = 120.0, travel_z: float = 100.0):
        self.api, self.job, self.dry = api, job, dry_run
        self.sleep, self.say = sleep, log
        self.margin, self.v_clear = margin, v_clear
        self.allow_unverified = allow_unverified
        self.probe_spot = probe_spot
        self.profile = profile
        self.purge_mm, self.lift_z, self.travel_z = purge_mm, lift_z, travel_z
        tier = next(t for t in profile["head_footprint"]["tiers"] if "head" in t["name"])
        v = mp.val(tier)
        self.head = {"left": -v["x_min"], "right": v["x_max"], "front": -v["y_min"], "back": v["y_max"], "low": v.get("z_from", 0.0)}
        self.part = dict(job["part"])
        self.probe_off = mp.probe_offsets(profile)
        self.z_off = self.probe_off[2]
        env = profile["envelope"]
        self.z_max = float(mp.val(env["z_max"]))
        self.y_max, self.x_max = float(mp.val(env["y_max"])), float(mp.val(env["x_max"]))
        self.max_dwell = float(profile["thermal"]["max_hot_dwell_over_part_s"])
        self.lb = z_lower_bound if z_lower_bound is not None else float(job["resume"]["layer_z"])
        self.hot_since: Optional[float] = None
        self.unverified = bool(mp.motion_ready(profile))

    # -- plumbing ----------------------------------------------------------
    def log(self, *a):
        self.say(time.strftime("%H:%M:%S"), *a)

    def gcode(self, cmd: str, timeout: int = 200):
        self.log("  >", cmd)
        if self.dry:
            return {"result": "dry"}
        r = self.api("POST", "/printer/gcode/script", {"script": cmd}, timeout)
        if "error" in r:
            raise RescueError(r["error"].get("message", str(r["error"])))
        return r

    def query(self, objs: List[str]) -> dict:
        return self.api("GET", "/printer/objects/query?" + "&".join(objs))["result"]["status"]

    def pos(self) -> List[float]:
        return self.query(["toolhead"])["toolhead"]["position"][:3]

    # -- geometry guard ----------------------------------------------------
    def head_rect(self, x: float, y: float):
        m = self.margin if self.unverified else 0.0
        return (x - self.head["left"] - m, x + self.head["right"] + m, y - self.head["front"] - m, y + self.head["back"] + m)

    def hits_part(self, x: float, y: float) -> bool:
        a0, a1, b0, b1 = self.head_rect(x, y)
        p = self.part
        return not (a1 < p["x0"] or a0 > p["x1"] or b1 < p["y0"] or b0 > p["y1"])

    def check_xy_move(self, p0, p1, z: float):
        if z + self.head["low"] >= self.part["top"] + self.v_clear:
            return
        n = max(2, int(math.dist(p0, p1) / 2))
        for i in range(n + 1):
            x = p0[0] + (p1[0] - p0[0]) * i / n
            y = p0[1] + (p1[1] - p0[1]) * i / n
            if self.hits_part(x, y):
                raise RescueError("GUARD: head footprint would overlap the part at X%.1f Y%.1f Z%.1f (needs Z >= %.1f)"
                                  % (x, y, z, self.part["top"] + self.v_clear - self.head["low"]))

    def check_probe_spot(self, x: float, y: float):
        px, py = x + self.probe_off[0], y + self.probe_off[1]
        if not (0 <= px <= self.x_max and 0 <= py <= self.y_max):
            raise RescueError("GUARD: probe point X%.1f Y%.1f is outside the bed" % (px, py))
        m = self.margin if self.unverified else 5.0
        p = self.part
        if p["x0"] - m <= px <= p["x1"] + m and p["y0"] - m <= py <= p["y1"] + m:
            raise RescueError("GUARD: probe point over the part")
        if self.hits_part(x, y):
            raise RescueError("GUARD: head footprint overlaps the part at the probe spot")

    # -- motion helpers ----------------------------------------------------
    def move_xy(self, x: float, y: float, z_now: float, f: int = 6000):
        p = self.pos()
        self.check_xy_move((p[0], p[1]), (x, y), z_now)
        self.gcode("G1 X%s Y%s F%d" % (x, y, f))

    def move_z(self, z: float, f: int, tol: float = 0.12):
        self.gcode("G1 Z%s F%d" % (z, f))
        if not self.dry:
            got = self.pos()[2]
            if abs(got - z) > tol:
                raise RescueError("Z mismatch: asked %s, at %s" % (z, got))

    def lift_to(self, z_target: float, cur: float, step: float = 10.0, f: int = 600):
        self.gcode("G91")
        try:
            while cur < z_target - 1e-6:
                d = min(step, z_target - cur)
                if cur + d > self.z_max - 5:
                    raise RescueError("GUARD: lift would exceed the Z axis maximum")
                self.gcode("G1 Z%.2f F%d" % (d, f))
                cur += d
        finally:
            self.gcode("G90")

    def wait_temp(self, target: float, tol: float = 1.5, tmax: float = 240):
        t0 = time.time()
        while time.time() - t0 < tmax:
            if self.dry:
                return
            if self.query(["extruder"])["extruder"]["temperature"] >= target - tol:
                return
            self.sleep(4)
        raise RescueError("nozzle did not reach temperature")

    # -- steps -------------------------------------------------------------
    def preflight(self):
        self.log("PREFLIGHT")
        pend = mp.motion_ready(self.profile)
        self.unverified = bool(pend)
        if pend and not self.allow_unverified:
            raise RescueError("R13: %d safety-critical profile fields are not measured (first: %s). Measure them, or pass "
                              "--allow-unverified to accept the owner-stated values with a %s mm margin" % (len(pend), pend[0], self.margin))
        if pend:
            self.log("  WARNING: running on %d unverified profile fields (owner-authorised), margin %s mm on the head box" % (len(pend), self.margin))
        info = self.api("GET", "/printer/info")["result"]
        if info["state"] != "ready":
            raise RescueError("Klippy is %s" % info["state"])
        s = self.query(["print_stats", "configfile", "heater_bed"])
        if s["print_stats"]["state"] in ("printing", "paused"):
            raise RescueError("a print is active")
        live = float(s["configfile"]["settings"]["bltouch"]["z_offset"])
        if abs(live - self.z_off) > 0.001:
            raise RescueError("R14: live z_offset %s differs from the profile %s" % (live, self.z_off))
        bed = self.job["temps"]["bed"]
        if s["heater_bed"]["temperature"] < bed - 5:
            raise RescueError("bed is %.1f C: heat it first (it keeps the part stuck)" % s["heater_bed"]["temperature"])
        if self.api("GET", "/server/job_queue/status")["result"]["queued_jobs"]:
            raise RescueError("job queue is not empty")
        self.check_probe_spot(*self.probe_spot)
        p = self.part
        px, py = self.job["resume"]["park_xy"]
        if not (p["x0"] < px < p["x1"] and p["y0"] < py < p["y1"]):
            raise RescueError("park point is outside the part envelope")
        names = [f["path"] for f in self.api("GET", "/server/files/list?root=gcodes")["result"]]
        if self.job["resume_file"] not in names:
            raise RescueError("resume file %s is not on the printer (upload it with print=false)" % self.job["resume_file"])
        self.log("  ok: z_offset %s, bed %.1f C, head L%s R%s F%s B%s" % (live, s["heater_bed"]["temperature"], self.head["left"],
                                                                         self.head["right"], self.head["front"], self.head["back"]))

    def power_cycle_and_rebuild(self):
        self.log("  CR Touch latched: power cycle >= 25 s, then rebuild the reference")
        if not self.dry:
            self.api("POST", "/machine/device_power/off?printer", {})
            self.sleep(25)
            self.api("POST", "/machine/device_power/on?printer", {})
            for _ in range(60):
                self.sleep(4)
                try:
                    st = self.api("GET", "/printer/info")["result"]["state"]
                except Exception:
                    continue
                if st == "ready":
                    break
                if st in ("shutdown", "error"):
                    self.api("POST", "/printer/firmware_restart", {})
        self.gcode("SET_IDLE_TIMEOUT TIMEOUT=14400")
        self.gcode("M140 S%d" % self.job["temps"]["bed"])
        self.gcode("SET_KINEMATIC_POSITION Z=%s SET_HOMED=z" % self.lift_z)
        self.gcode("G28 X")
        self.gcode("G28 Y")
        self.move_xy(*self.probe_spot, self.lift_z)

    def probe_z(self):
        for _ in range(80):
            try:
                self.gcode("PROBE PROBE_SPEED=5", timeout=300)
                self.gcode("SET_KINEMATIC_POSITION Z=%s SET_HOMED=z" % self.z_off)
                return
            except RescueError as e:
                m = str(e)
                if "prior to movement" in m or "Failed to verify" in m:
                    raise
                if "no trigger" in m.lower():
                    self.log("  no trigger in this stroke: the pin cannot be below z_offset, declare it as the lower bound and repeat")
                    self.gcode("SET_KINEMATIC_POSITION Z=%s SET_HOMED=z" % self.z_off)
                    continue
                raise
        raise RescueError("probe loop did not reach the bed")

    def abort_hot(self, msg: str):
        self.log("ABORT:", msg)
        try:
            if not self.dry:
                self.gcode("M104 S0")
                self.gcode("G91")
                self.gcode("G1 Z10 F300")
                self.gcode("G90")
        except Exception as e:  # keep the original error visible
            self.log("  (abort cleanup failed: %s)" % e)

    def run(self) -> int:
        job, bed, hot = self.job, self.job["temps"]["bed"], self.job["temps"]["nozzle"]
        try:
            self.preflight()
            self.gcode("SET_IDLE_TIMEOUT TIMEOUT=14400")
            self.gcode("M140 S%d" % bed)
            self.log("Z REFERENCE (declared %s is a LOWER bound: the head may have been lifted by hand)" % self.lb)
            self.gcode("SET_KINEMATIC_POSITION Z=%s SET_HOMED=z" % self.lb)
            self.lift_to(self.lift_z, cur=self.lb)
            self.gcode("G28 X")
            self.gcode("G28 Y")
            self.move_xy(*self.probe_spot, self.lift_z)
            try:
                self.probe_z()
            except RescueError as e:
                if "prior to movement" in str(e) or "Failed to verify" in str(e):
                    self.power_cycle_and_rebuild()
                    self.probe_z()
                else:
                    raise
            self.log("HEAT + PURGE (probe spot, bare bed)")
            self.gcode("M104 S%d" % hot)
            self.lift_to(10.0, cur=self.z_off)
            self.wait_temp(hot)
            self.sleep(12)
            self.gcode("M83")
            self.gcode("G1 E%s F150" % self.purge_mm)
            self.gcode("G1 E-2.5 F1800")
            self.lift_to(self.travel_z, cur=10.0)
            self.gcode("BED_MESH_PROFILE LOAD=%s" % mp.bed_mesh_profile(self.profile))
            self.log("TRAVEL TO PARK + DESCENT (hot nozzle over the part: dwell clock starts)")
            self.hot_since = time.time()
            px, py = job["resume"]["park_xy"]
            self.move_xy(px, py, self.travel_z)
            top = job["resume"]["layer_z"]
            self.move_z(top + 8.4, 600)
            self.move_z(top + 3.0, 300)
            self.move_z(top, 60)
            self.gcode("G1 E2.5 F300")  # nominal filament: the first resumed line is a plain extrusion
            dwell = time.time() - self.hot_since
            if not self.dry and dwell > self.max_dwell:
                raise RescueError("hot dwell %.0f s over the part exceeds the limit %.0f s" % (dwell, self.max_dwell))
            r = self.api("POST", "/printer/print/start", {"filename": job["resume_file"]}, 30) if not self.dry else {}
            if "error" in r:
                raise RescueError(r["error"].get("message"))
            self.log("STARTED. nozzle was hot over the part %.0f s before the start" % dwell)
            return 0
        except Exception as e:
            self.abort_hot(str(e))
            return 1


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Run one rescue through Moonraker (finger on the emergency stop).")
    ap.add_argument("--host", required=True, help="Moonraker host[:port]")
    ap.add_argument("--profile", required=True)
    ap.add_argument("--job", required=True, help="job.json from rescue_job.py")
    ap.add_argument("--probe-spot", required=True, help="nozzle X,Y of a bare-bed probe spot clear of the part, e.g. 134,215")
    ap.add_argument("--margin", type=float, default=10.0, help="mm added on every side of the head box when profile fields are unverified")
    ap.add_argument("--allow-unverified", action="store_true", help="move although safety-critical profile fields are not measured (owner decision, logged)")
    ap.add_argument("--z-lower-bound", type=float, help="declared Z lower bound (default: the layer Z of the job)")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)
    try:
        profile = mp.load_profile(a.profile)
        job = json.load(open(a.job))
        x, y = (float(v) for v in a.probe_spot.split(","))
    except (OSError, ValueError, mp.ProfileError) as e:
        print("REFUSED:", e, file=sys.stderr)
        return 2
    r = Runner(http_api(a.host), profile, job, (x, y), margin=a.margin, allow_unverified=a.allow_unverified,
               dry_run=a.dry_run, z_lower_bound=a.z_lower_bound)
    return r.run()


if __name__ == "__main__":
    sys.exit(main())
