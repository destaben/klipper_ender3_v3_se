"""Klipper-like trapezoid motion timing for a list of scanned moves.

A port of the planning arithmetic in klippy/toolhead.py (Move, LookAheadQueue
.flush) plus the per-axis limits of kinematics/cartesian.py (Z) and
kinematics/extruder.py (extrude-only moves, extruder junction speed):

- per-move cruise velocity: min(F/60 * M220 factor, max_velocity), lowered
  for Z moves (max_z_velocity * move_d/|dz|) and extrude-only moves
  (max_extrude_only_velocity);
- accel from SET_VELOCITY_LIMIT/M204, lowered the same way for Z/extrude-only;
- junction speed from square_corner_velocity through Klipper's junction
  deviation formula jd = scv^2 * (sqrt(2) - 1) / max_accel, with the
  centripetal limit that the circle may only use half of either move, and
  the extruder's instant_corner_velocity limit;
- minimum_cruise_ratio: the "smoothed" pass with max_accel_to_decel =
  accel * (1 - minimum_cruise_ratio), exactly as Klipper's flush();
- a backward pass (reachable start speeds) and the delayed-move forward
  propagation of the peak cruise speed, as in Klipper's flush().

Approximations (documented in docs/print-rescue.md):
- The whole stream is planned as if the lookahead queue had seen every move.
  Klipper flushes lazily and is forced to plan a stop when its buffer runs
  low; those forced stops are not modelled (they only make the real motion
  slower, by a few milliseconds per forced stop).
- Dwell items (G4, M400, M109, M190) end a planning segment with a full stop;
  waits for temperatures count as zero seconds.
- Input shaping, pressure advance and step timing do not change the planned
  move times in Klipper either, so they are not modelled.
- Moves are timed whole; which move is "running" at a given time is resolved
  to the line level, which is all stop_point.py needs.

Pure logic, standard library only.
"""

import math
from dataclasses import dataclass
from typing import List, Optional

from gcode_scan import Dwell, Limits, Move

INSTANT_CORNER_V = 1.0


@dataclass
class Timed:
    line: int
    duration: float
    t_start: float = 0.0
    t_end: float = 0.0
    start_v: float = 0.0
    cruise_v: float = 0.0
    end_v: float = 0.0
    min_move_t: float = 0.0
    kind: str = "move"           # move | dwell
    extruding: bool = False
    travel: bool = False         # XY motion without extrusion
    unretract: bool = False      # extrude-only move with E > 0
    retract: bool = False        # extrude-only move with E < 0
    e_mm: float = 0.0            # filament pushed by this move (E > 0 only)
    start_xy: tuple = (0.0, 0.0)
    end_xy: tuple = (0.0, 0.0)


class _PMove:
    """Mirror of klippy/toolhead.py Move (planning fields only)."""

    def __init__(self, mv: Move, lim: Limits):
        self.src = mv
        self.start_pos = mv.start
        self.end_pos = mv.end
        self.accel = mv.accel
        scv2 = mv.square_corner_velocity ** 2
        self.junction_deviation = scv2 * (math.sqrt(2.0) - 1.0) / mv.accel
        velocity = min(mv.speed, mv.max_velocity)
        self.is_kinematic_move = True
        axes_d = [mv.end[i] - mv.start[i] for i in range(4)]
        self.axes_d = axes_d
        move_d = math.sqrt(sum(d * d for d in axes_d[:3]))
        if move_d < 1e-9:
            axes_d[0] = axes_d[1] = axes_d[2] = 0.0
            move_d = abs(axes_d[3])
            inv = 1.0 / move_d if move_d else 0.0
            self.accel = 99999999.9
            velocity = mv.speed
            self.is_kinematic_move = False
        else:
            inv = 1.0 / move_d
        self.move_d = move_d
        self.axes_r = [d * inv for d in axes_d]
        self.min_move_t = move_d / velocity if velocity else 0.0
        self.max_start_v2 = 0.0
        self.max_cruise_v2 = velocity ** 2
        self.delta_v2 = 2.0 * move_d * self.accel
        self.max_smoothed_v2 = 0.0
        self.smooth_delta_v2 = 2.0 * move_d * mv.accel_to_decel
        self.next_junction_v2 = 999999999.9
        # kinematics.check_move (cartesian): slower Z axis
        if self.is_kinematic_move and axes_d[2]:
            z_ratio = move_d / abs(axes_d[2])
            self.limit_speed(lim.max_z_velocity * z_ratio,
                             lim.max_z_accel * z_ratio)
        # extruder.check_move: extrude-only and retract moves
        if axes_d[3]:
            axis_r = self.axes_r[3]
            if (not axes_d[0] and not axes_d[1]) or axis_r < 0.0:
                inv_r = 1.0 / abs(axis_r)
                self.limit_speed(lim.max_extrude_only_velocity * inv_r,
                                 lim.max_extrude_only_accel * inv_r)
        self.icv = lim.instant_corner_velocity

    def limit_speed(self, speed, accel):
        speed2 = speed ** 2
        if speed2 < self.max_cruise_v2:
            self.max_cruise_v2 = speed2
            self.min_move_t = self.move_d / speed
        self.accel = min(self.accel, accel)
        self.delta_v2 = 2.0 * self.move_d * self.accel
        self.smooth_delta_v2 = min(self.smooth_delta_v2, self.delta_v2)

    def calc_junction(self, prev):
        if not self.is_kinematic_move or not prev.is_kinematic_move:
            return
        diff_r = self.axes_r[3] - prev.axes_r[3]
        extruder_v2 = (self.icv / abs(diff_r)) ** 2 if diff_r else self.max_cruise_v2
        max_start_v2 = min(extruder_v2, self.max_cruise_v2, prev.max_cruise_v2,
                           prev.next_junction_v2,
                           prev.max_start_v2 + prev.delta_v2)
        a, p = self.axes_r, prev.axes_r
        cos_theta = -(a[0] * p[0] + a[1] * p[1] + a[2] * p[2])
        sin_d2 = math.sqrt(max(0.5 * (1.0 - cos_theta), 0.0))
        cos_d2 = math.sqrt(max(0.5 * (1.0 + cos_theta), 0.0))
        one_minus = 1.0 - sin_d2
        if one_minus > 0.0 and cos_d2 > 0.0:
            r_jd = sin_d2 / one_minus
            move_jd_v2 = r_jd * self.junction_deviation * self.accel
            pmove_jd_v2 = r_jd * prev.junction_deviation * prev.accel
            quarter_tan = 0.25 * sin_d2 / cos_d2
            max_start_v2 = min(max_start_v2, move_jd_v2, pmove_jd_v2,
                               self.delta_v2 * quarter_tan,
                               prev.delta_v2 * quarter_tan)
        self.max_start_v2 = max_start_v2
        self.max_smoothed_v2 = min(max_start_v2,
                                   prev.max_smoothed_v2 + prev.smooth_delta_v2)

    def set_junction(self, start_v2, cruise_v2, end_v2):
        half_inv_accel = 0.5 / self.accel
        accel_d = (cruise_v2 - start_v2) * half_inv_accel
        decel_d = (cruise_v2 - end_v2) * half_inv_accel
        cruise_d = self.move_d - accel_d - decel_d
        self.start_v = sv = math.sqrt(start_v2)
        self.cruise_v = cv = math.sqrt(cruise_v2)
        self.end_v = ev = math.sqrt(end_v2)
        self.accel_t = accel_d / ((sv + cv) * 0.5) if sv + cv else 0.0
        self.cruise_t = cruise_d / cv if cv else 0.0
        self.decel_t = decel_d / ((ev + cv) * 0.5) if ev + cv else 0.0


def _flush(queue: List[_PMove]):
    """Klipper LookAheadQueue.flush(lazy=False): stop after the last move."""
    delayed = []
    next_end_v2 = next_smoothed_v2 = peak_cruise_v2 = 0.0
    for i in range(len(queue) - 1, -1, -1):
        move = queue[i]
        reachable_start_v2 = next_end_v2 + move.delta_v2
        start_v2 = min(move.max_start_v2, reachable_start_v2)
        reachable_smoothed_v2 = next_smoothed_v2 + move.smooth_delta_v2
        smoothed_v2 = min(move.max_smoothed_v2, reachable_smoothed_v2)
        if smoothed_v2 < reachable_smoothed_v2:
            if smoothed_v2 + move.smooth_delta_v2 > next_smoothed_v2 or delayed:
                peak_cruise_v2 = min(move.max_cruise_v2,
                                     (smoothed_v2 + reachable_smoothed_v2) * 0.5)
                if delayed:
                    mc_v2 = peak_cruise_v2
                    for m, ms_v2, me_v2 in reversed(delayed):
                        mc_v2 = min(mc_v2, ms_v2)
                        m.set_junction(min(ms_v2, mc_v2), mc_v2, min(me_v2, mc_v2))
                    del delayed[:]
            cruise_v2 = min((start_v2 + reachable_start_v2) * 0.5,
                            move.max_cruise_v2, peak_cruise_v2)
            move.set_junction(min(start_v2, cruise_v2), cruise_v2,
                              min(next_end_v2, cruise_v2))
        else:
            delayed.append((move, start_v2, next_end_v2))
        next_end_v2 = start_v2
        next_smoothed_v2 = smoothed_v2


def _timed(pm: _PMove) -> Timed:
    mv = pm.src
    dxy = math.hypot(mv.end[0] - mv.start[0], mv.end[1] - mv.start[1])
    de = mv.end[3] - mv.start[3]
    return Timed(line=mv.line,
                 duration=pm.accel_t + pm.cruise_t + pm.decel_t,
                 start_v=pm.start_v, cruise_v=pm.cruise_v, end_v=pm.end_v,
                 min_move_t=pm.min_move_t, extruding=mv.extruding,
                 travel=dxy > 1e-9 and de <= 1e-9,
                 unretract=dxy <= 1e-9 and de > 1e-9,
                 retract=dxy <= 1e-9 and de < -1e-9,
                 e_mm=max(de, 0.0),
                 start_xy=(mv.start[0], mv.start[1]),
                 end_xy=(mv.end[0], mv.end[1]))


def plan(items: List[object], limits: Optional[Limits] = None) -> List[Timed]:
    """Return one Timed entry per Move/Dwell with cumulative start/end times."""
    lim = limits or Limits()
    out: List[Timed] = []
    queue: List[_PMove] = []

    def flush():
        if not queue:
            return
        _flush(queue)
        out.extend(_timed(pm) for pm in queue)
        del queue[:]

    for item in items:
        if isinstance(item, Dwell):
            flush()
            out.append(Timed(line=item.line, duration=max(item.seconds, 0.0),
                             kind="dwell"))
            continue
        pm = _PMove(item, lim)
        if not pm.move_d:
            continue
        if queue:
            pm.calc_junction(queue[-1])
        queue.append(pm)
    flush()
    t = 0.0
    for tm in out:
        tm.t_start = t
        t += tm.duration
        tm.t_end = t
    return out
