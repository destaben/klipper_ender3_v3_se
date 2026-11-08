"""Streaming G-code scanner: machine state, extrusion bounding box and tail checks.

The scanner reads (line_number, byte_offset, text) records one at a time, so
a full 8 MB print file is never held in memory. Line numbers are 1-based and
always refer to the ORIGINAL file, which lets a test fixture that holds only
an excerpt of a file (see fixtures/SOURCES.md) report the same numbers as the
real file.

What it models, as Klipper's gcode_move/gcode_arcs would see it:
  G90/G91, M82/M83, G92, G0/G1, G2/G3 (G17 plane, split into segments of
  gcode_arcs resolution like Klipper), F, G4, SET_VELOCITY_LIMIT, M204,
  M220/M221, M104/M109/M140/M190, M106/M107, SET_PRESSURE_ADVANCE,
  EXCLUDE_OBJECT_DEFINE/START/END/EXCLUDE_OBJECT, ;LAYER_CHANGE and ;Z:,
  T<n> tool changes.

Pure logic except the two record loaders at the bottom. Standard library only.
"""

import bisect
import copy
import gzip
import json
import math
import re
from array import array
from dataclasses import dataclass, field
from typing import Dict, Iterable, Iterator, List, Optional, Tuple

Record = Tuple[int, int, str]

# END_PRINT is allowed at the end of the file only (see END_MACROS).
# A DENY-LIST (anything not listed passes). Commands that must never run again in a resumed tail: they home, probe or
# start a fresh print (which homes) with a part on the bed.
# Ends of a job: only legitimate as the end of the file (refused when
# extrusion follows them).
# _SP_PRINT_END is the MMU end-of-print macro the slicer end code calls.
END_MACROS = ("END_PRINT", "_SP_PRINT_END", "M84", "M18", "TURN_OFF_HEATERS")
FORBIDDEN_TAIL = {
    "G28": "homing (any form)",
    "G29": "bed probing",
    "G30": "single probe",
    "G32": "gantry levelling probe",
    "HOME": "homing macro",
    "START_PRINT": "print start macro (homes all axes)",
    "START_PRINT_EXTRA": "print start macro (homes all axes)",
    "_SP_PRINT_START": "MMU print start (loads a lane)",
    "BED_MESH_CALIBRATE": "bed mesh probing",
    "PROBE": "probing",
    "PROBE_ACCURACY": "probing",
    "PROBE_CALIBRATE": "probing",
    "Z_TILT_ADJUST": "gantry levelling",
    "QUAD_GANTRY_LEVEL": "gantry levelling",
    "SCREWS_TILT_CALCULATE": "probing",
    "SP_HOME": "MMU homing macro",
    "_SP_CONDITIONAL_HOME": "MMU conditional homing macro",
    "_SP_PRINT_START_B": "MMU print start (homes all axes)",
    "SP_FORCE_MOVE_LANE": "lane move without homing",
    "CANCEL_PRINT": "ends the print (runs the end-of-print macro)",
    "PAUSE": "pauses the print",
    "M600": "filament change pause",
    "M0": "stop",
    "M1": "stop",
    "PRINTER_SHUTDOWN": "powers the printer off",
    "G10": "firmware retraction (its length is not modelled)",
    "G11": "firmware unretraction (its length is not modelled)",
    "ARC_NO_IJ": "arc without I/J (Klipper rejects it; not modelled)",
    "CR_IN_LINE": "carriage return inside a line (hidden command)",
    "CTRL_IN_LINE": "control character inside a line (hidden command)",
    "PROBE_ZOFFSET": "probes (calls PRTOUCH_PROBE_ZOFFSET)",
    "PRTOUCH_PROBE_ZOFFSET": "probes",
    "PID_TUNE_BED": "heater tuning (homes)",
    "PID_TUNE_EXTRUDER": "heater tuning (homes)",
    "_SP_TIP_FORM": "MMU cut (moves to the cutter at X=225)",
    "SP_TEST_MANUAL_TIP_FORMING": "MMU test macro (force-moves)",
    "_SP_LOAD_EXTRUDER_C": "MMU extruder load (force-moves)",
    "_SP_UNLOAD_EXTRUDER": "MMU extruder unload (force-moves)",
    "_SP_SYNC_MOVE": "MMU sync move (force-moves)",
    "SP_LOAD_LANE": "MMU lane load", "_SP_LOAD_LANE": "MMU lane load",
    "SP_UNLOAD_LANE": "MMU lane unload", "_SP_UNLOAD_LANE": "MMU lane unload",
    "SP_LOAD_EXTRUDER": "MMU extruder load", "SP_UNLOAD_EXTRUDER": "MMU extruder unload",
    "SP_LOAD_HOTEND": "MMU hotend load", "SP_UNLOAD_HOTEND": "MMU hotend unload",
    "SP_LOAD_HUB": "MMU hub load", "SP_UNLOAD_HUB": "MMU hub unload",
    "MANUAL_STEPPER": "moves an auxiliary stepper",
    "STEPPER_BUZZ": "moves a stepper",
    "SP_CHANGE": "MMU lane change",
    "_SP_CHANGE": "MMU lane change",
    "_SP_PAUSE": "MMU pause",
    "M112": "emergency stop",
    "M999": "reset after shutdown",
    "M410": "quick stop",
    "FIRMWARE_RESTART": "restarts the firmware",
    "RESTART": "restarts the host",
    "SAVE_CONFIG": "restarts the host",
    "FORCE_MOVE": "move without homing",
    "SET_KINEMATIC_POSITION": "declares a position without a measurement",
    "BLTOUCH_DEBUG": "deploys or stows the probe pin",
    "TESTZ": "moves Z for manual probing",
    # Klipper built-ins that probe, home or sweep the head (not config macros).
    "BED_TILT_CALIBRATE": "probes (bed tilt)",
    "Z_ENDSTOP_CALIBRATE": "manual probing of the Z endstop",
    "TEST_RESONANCES": "sweeps the head (resonance test)",
    "SHAPER_CALIBRATE": "sweeps the head (resonance test)",
    "AXIS_TWIST_COMPENSATION_CALIBRATE": "probes (axis twist)",
    "DELTA_CALIBRATE": "probes (delta calibration)",
    "DELTA_ANALYZE": "probes (delta analysis)",
    "BED_SCREWS_ADJUST": "probes and moves to the bed screws",
    # Not a command name: SET_GCODE_OFFSET with MOVE=1 is reported under this key.
    "SET_GCODE_OFFSET_MOVE": "SET_GCODE_OFFSET with MOVE=1 moves the toolhead",
}

# Macros that reach a homing, probing, force-move or stepper command through
# other macros (transitive callers, derived from config/ by
# test_every_repo_macro_that_reaches_a_moving_command_is_on_the_deny_list: a
# macro added to config/ that reaches one fails that test until it is listed).
FORBIDDEN_TAIL.update({name: "macro that reaches homing, probing or a stepper move"
                       for name in (
    "CHANGE0", "CHANGE1", "CHANGE2", "CHANGE3", "RESUME", "SP_FILAMENT_CHANGE",
    "SP_INIT", "SP_TEST_LOAD_UNLOAD_EXTRUDER", "SP_TEST_MMU", "SP_TEST_SYNC",
    "_SP_FILAMENT_CHANGE", "_SP_LANE_SELECT", "_SP_LOAD_EXTRUDER",
    "_SP_LOAD_EXTRUDER_B", "_SP_LOAD_HOTEND", "_SP_MOTOR", "_SP_OFF",
    "_SP_PURGE", "_SP_RESUME", "_SP_RESUME_FINISH", "_SP_RUNOUT",
    "_SP_RUNOUT_CHECK", "_SP_RUNOUT_CHECK_B", "_SP_RUNOUT_END",
    "_SP_RUNOUT_INITIAL_PURGE", "_SP_SENSOR_OUT", "_SP_TEST", "_SP_TOOL_CHANGE",
    "_SP_UNLOAD_EXTRUDER_B", "_SP_UNLOAD_EXTRUDER_C", "_SP_UNLOAD_HOTEND",
    "MANUAL_PROBE", "TESTZ")})

# Entries of FORBIDDEN_TAIL that make a macro dangerous only when the macro is
# meant to run inside a print: firmware retraction (a timelapse macro retracts
# with G10 and is fine in a tail) and the pseudo-names the scanner raises itself.
_NOT_SEEDS = frozenset(("G10", "G11", "ARC_NO_IJ", "CR_IN_LINE", "CTRL_IN_LINE",
                        "SET_GCODE_OFFSET_MOVE"))
_WORD = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_ACTION_CALL = re.compile(r"^\{\s*action_")


def _builds_a_command(line: str) -> bool:
    """A macro line that creates or runs a G-code command at run time: it calls
    run_script*, or it is a bare Jinja expression (the text becomes a gcode line)
    that concatenates strings with ~. What command results cannot be known, so
    the macro counts as dangerous. action_*() calls only print or notify."""
    s = line.strip()
    if "run_script" in s.lower():
        return True
    return (s.startswith("{") and not s.startswith(("{%", "{#")) and "~" in s
            and not _ACTION_CALL.match(s))


_FIRST_WORD = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)")
# Built-in commands that home, probe, sweep the head or restart: these count when
# they appear ANYWHERE in a macro body (a Jinja string or expression included).
# Every other deny-listed name counts only as the first word of a gcode line, so
# a variable or message that merely mentions PAUSE does not condemn a macro.
_HARD_WORDS = frozenset((
    "G28", "G29", "G30", "G32", "HOME", "PROBE", "PROBE_ACCURACY", "PROBE_CALIBRATE",
    "BED_MESH_CALIBRATE", "Z_TILT_ADJUST", "QUAD_GANTRY_LEVEL", "SCREWS_TILT_CALCULATE",
    "BED_SCREWS_ADJUST", "BED_TILT_CALIBRATE", "Z_ENDSTOP_CALIBRATE", "TEST_RESONANCES",
    "SHAPER_CALIBRATE", "AXIS_TWIST_COMPENSATION_CALIBRATE", "DELTA_CALIBRATE",
    "DELTA_ANALYZE", "MANUAL_PROBE", "TESTZ", "FORCE_MOVE", "SET_KINEMATIC_POSITION",
    "MANUAL_STEPPER", "STEPPER_BUZZ", "FIRMWARE_RESTART", "RESTART", "SAVE_CONFIG",
    "M112", "M999", "M410", "BLTOUCH_DEBUG"))


def dangerous_macros(sections: Dict[str, Dict[str, str]]) -> Dict[str, str]:
    """Macros of a parsed printer.cfg (plus includes) that reach a forbidden
    command, directly or through other macros: {NAME: reason}. The first word of
    a line counts when it is a deny-listed command or a dangerous macro; the
    hard words (homing, probing, restart...) count anywhere in the body, Jinja
    expressions and every {% if %} branch included (over-approximation, so it
    fails closed). Comments (; and #) are dropped first.
    END_MACROS and tool changes are not seeds: they are accepted or refused by
    their own checks."""
    body: Dict[str, List[str]] = {}
    for name, opts in sections.items():
        if name.lower().startswith("gcode_macro "):
            body[name.split(None, 1)[1].strip().upper()] = [
                re.split(r"[;#]", ln, maxsplit=1)[0]     # drop comments
                for ln in opts.get("gcode", "").split("\n")]
    seeds = {n for n in FORBIDDEN_TAIL if n not in END_MACROS and n not in _NOT_SEEDS}
    # First word of each line = a call to any deny-listed command or dangerous
    # macro. Hard words (_HARD_WORDS) also count anywhere in the body, so a
    # command run through a Jinja expression (run_script_from_command("G28")) or
    # a string is caught too.
    calls = {}
    for n, lines in body.items():
        first = {m.group(1).upper() for ln in lines for m in [_FIRST_WORD.match(ln)] if m}
        hard = {w.upper() for ln in lines for w in _WORD.findall(ln)} & _HARD_WORDS
        calls[n] = first | hard
        if any(_builds_a_command(ln) for ln in lines):
            calls[n].add("(runs a command built at run time)")
    danger: Dict[str, str] = {}
    grew = True
    while grew:
        grew = False
        for n, called in calls.items():
            if (n in danger or n in FORBIDDEN_TAIL or n in END_MACROS
                    or re.fullmatch(r"T\d+", n)):
                continue
            hit = next((c for c in sorted(called)
                        if c in seeds or c in danger
                        or c == "(runs a command built at run time)"), None)
            if hit:
                danger[n] = ("your macro %s" % hit.strip("()")) if hit.startswith("(") \
                    else "your macro reaches %s" % hit
                grew = True
    return danger


# Extrusion moves that change Z by more than this count as vase-style.
VASE_MIN_MOVES = 10
EPS = 1e-9
# Printer coordinates and filament lengths beyond this are not real moves
# (a corrupt file, not a print); arcs longer than MAX_ARC_SEGMENTS are not
# modelled. Both refuse with ValueError, which plan.py reports as exit 1.
MAX_COORDINATE = 1e6
MAX_ARC_SEGMENTS = 100000
MAX_EXCERPT_CHARS = 256 * 1024 * 1024
MAX_LINE_BYTES = 4 * 1024 * 1024


@dataclass
class Limits:
    """Toolhead limits as configured in printer.cfg ([printer], [extruder])."""
    max_velocity: float = 200.0
    max_accel: float = 4000.0
    square_corner_velocity: float = 5.0
    minimum_cruise_ratio: float = 0.5
    max_z_velocity: float = 5.0
    max_z_accel: float = 100.0
    max_extrude_only_velocity: float = 45.0
    max_extrude_only_accel: float = 3000.0
    instant_corner_velocity: float = 1.0
    arc_resolution: float = 1.0


@dataclass
class Move:
    """One straight toolhead move as Klipper's toolhead receives it."""
    line: int
    start: Tuple[float, float, float, float]
    end: Tuple[float, float, float, float]
    speed: float            # requested mm/s (F/60 times the M220 factor)
    max_velocity: float
    accel: float
    accel_to_decel: float
    square_corner_velocity: float
    extruding: bool         # E > 0 with XY motion


@dataclass
class Dwell:
    """A pause in motion (G4) or a full lookahead flush (M400, M109, M190)."""
    line: int
    seconds: float
    reason: str


@dataclass
class Bbox:
    x_min: float = math.inf
    x_max: float = -math.inf
    y_min: float = math.inf
    y_max: float = -math.inf
    z_max: float = -math.inf

    def add(self, x, y, z):
        self.x_min = min(self.x_min, x)
        self.x_max = max(self.x_max, x)
        self.y_min = min(self.y_min, y)
        self.y_max = max(self.y_max, y)
        self.z_max = max(self.z_max, z)

    @property
    def empty(self):
        return self.x_min > self.x_max

    def as_dict(self):
        return {"x_min": self.x_min, "x_max": self.x_max, "y_min": self.y_min,
                "y_max": self.y_max, "z_max": self.z_max}


@dataclass
class State:
    """Machine state after the last processed line."""
    line: int = 0
    # pos[3] is the E coordinate scaled by M221 (what Klipper keeps); divide by
    # extrude_factor / 100 to get the number the source's G-code would write.
    pos: List[float] = field(default_factory=lambda: [0.0, 0.0, 0.0, 0.0])
    absolute: bool = True
    e_absolute: bool = True
    feed: float = 1500.0          # mm/min (Klipper's default speed is 25 mm/s)
    max_velocity: float = 200.0
    accel: float = 4000.0
    minimum_cruise_ratio: float = 0.5
    square_corner_velocity: float = 5.0
    speed_factor: float = 100.0   # M220 percent
    extrude_factor: float = 100.0  # M221 percent
    extruder_temp: Optional[float] = None
    bed_temp: Optional[float] = None
    fan: Optional[int] = None     # 0-255
    pressure_advance: Optional[float] = None
    pa_smooth_time: Optional[float] = None
    accel_to_decel_set: bool = False
    mcr_set: bool = False
    objects: Dict[str, str] = field(default_factory=dict)   # name -> define line
    current_object: Optional[str] = None
    excluded: List[str] = field(default_factory=list)
    layer: int = 0
    layer_z: Optional[float] = None
    layer_start_line: Optional[int] = None
    tool: Optional[int] = None
    plane: str = "G17"
    retracted: float = 0.0        # filament the slicer has retracted right now
    e_total: float = 0.0          # net filament pushed since the start (survives G92 E0)

    @property
    def accel_to_decel(self):
        return self.accel * (1.0 - self.minimum_cruise_ratio)


@dataclass
class Event:
    line: int
    kind: str      # forbidden | tool | m82 | g91 | g92_xyz | plane | layer_z
    detail: str
    value: Optional[float] = None


@dataclass
class ScanResult:
    state: State
    total: Bbox
    per_object: Dict[Optional[str], Bbox]
    moves: List[object]           # Move | Dwell, the last `keep_layers` layers
    events: List[Event]
    line_offsets: Tuple[array, array]   # (byte offsets, line numbers)
    layer_starts: List[Tuple[int, int, Optional[float]]]  # (line, layer, z)
    vase_lines: List[int]          # extrusion moves that change Z
    tools_used: List[int]
    last_line: int
    bbox_until: int


def split_code(text: str) -> str:
    return text.split(";", 1)[0].strip()


_CLASSIC = re.compile(r"^([GMT])(\d+(?:\.\d+)?)(.*)$", re.IGNORECASE)
# Words are letter + number only. A KEY=VALUE word such as "G1 X=10 E=5" is not
# parsed on purpose: Klipper keeps "=10" as the value and float("=10") raises,
# so such a line aborts the print with "Unable to parse move" instead of moving
# (checked against klippy/gcode.py and klippy/extras/gcode_move.py; docs
# "Design notes"). The scanner models what a move does, and this one does nothing.
_PARAM = re.compile(r"([A-Za-z])\s*([-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?)?")


# N<digits> is a line number; a bare N followed by a space is stripped too, so
# "N M112" cannot hide M112 (NEOPIXEL-style names are not touched).
_LINE_NUMBER = re.compile(r"^\s*[Nn](?:\d+\s*|\s+)")
_SPACED_CLASSIC = re.compile(r"^\s*([GMTgmt])\s+(\d)")


def parse(text: str):
    """Return (command, params) for one line, or (None, {}) for no command.

    Classic commands (G1 X10 E.5) give single-letter float params; extended
    commands (SET_VELOCITY_LIMIT ACCEL=2500) give KEY=VALUE string params.
    """
    code = split_code(text)
    if not code:
        return None, {}
    # Normalise like Klipper so a command cannot hide: drop an N<line>
    # prefix and the spaces between a G/M/T letter and its number.
    code = _LINE_NUMBER.sub("", code, count=1)
    code = _SPACED_CLASSIC.sub(r"\1\2", code, count=1)
    if not code:
        return None, {}
    m = _CLASSIC.match(code)
    if m and "=" not in code.split()[0]:
        num = m.group(2)
        cmd = m.group(1).upper() + (num if "." in num else str(int(num)))
        params = {}
        for key, val in _PARAM.findall(m.group(3)):
            key = key.upper()
            if val in (None, ""):
                params[key] = None
            else:
                params[key] = float(val)
        return cmd, params
    parts = code.split()
    cmd = parts[0].upper()
    params = {}
    for p in parts[1:]:
        if "=" in p:
            k, v = p.split("=", 1)
            params[k.upper()] = v
    return cmd, params


def _fparam(params, key, default=None):
    """A finite float parameter, or default (NaN and infinity are not values)."""
    v = params.get(key)
    if v is None:
        return default
    try:
        f = float(v)
    except (TypeError, ValueError):
        return default
    return f if math.isfinite(f) else default


class Scanner:
    """Feed records with feed(); read results with result()."""

    def __init__(self, limits: Limits = None, bbox_until: Optional[int] = None,
                 keep_layers: int = 2, collect_moves_until: Optional[int] = None,
                 until_byte: Optional[int] = None,
                 extra_forbidden: Optional[Dict[str, str]] = None):
        """until_byte: lines starting at or after this byte offset (Klipper's
        exit position) count neither for the bounding box nor for the moves.
        extra_forbidden: more command names refused in the tail (for example
        the user's own macros from dangerous_macros)."""
        self.until_byte = until_byte
        self.extra_forbidden = dict(extra_forbidden or {})
        self.limits = limits or Limits()
        lim = self.limits
        self.st = State(max_velocity=lim.max_velocity, accel=lim.max_accel,
                        minimum_cruise_ratio=lim.minimum_cruise_ratio,
                        square_corner_velocity=lim.square_corner_velocity)
        self.bbox_until = bbox_until
        self.collect_until = collect_moves_until
        self.keep_layers = keep_layers
        self.total = Bbox()
        self.per_object: Dict[Optional[str], Bbox] = {}
        self.layer_moves: List[List[object]] = [[]]
        self.end_seen = False
        self.events: List[Event] = []
        self.offsets = array("q")
        self.lines = array("q")
        self.layer_starts: List[Tuple[int, int, Optional[float]]] = []
        self.vase_lines: List[int] = []
        self.tools_used: List[int] = []
        # True once the scan has passed until_byte: what the file does after the
        # stop (tools, object definitions) is not part of the printed state.
        self.past_stop = False
        self.last_line = 0

    # -- helpers -----------------------------------------------------------
    def _bbox_active(self, line):
        return self.bbox_until is None or line <= self.bbox_until

    def _collect_active(self, line):
        return self.collect_until is None or line <= self.collect_until

    def _emit(self, item):
        self.layer_moves[-1].append(item)

    def _new_layer(self):
        self.layer_moves.append([])
        if self.keep_layers and len(self.layer_moves) > self.keep_layers:
            del self.layer_moves[0]

    def _speed(self):
        return self.st.feed / 60.0 * self.st.speed_factor / 100.0

    def _segment(self, line, start, end):
        st = self.st
        dxy = math.hypot(end[0] - start[0], end[1] - start[1])
        de = end[3] - start[3]
        st.e_total += de
        extruding = de > EPS and dxy > EPS
        if de < -EPS:
            st.retracted += -de
        elif de > EPS:
            st.retracted = 0.0 if extruding else max(0.0, st.retracted - de)
        if extruding and self._bbox_active(line):
            z = max(start[2], end[2])
            for box in (self.total,
                        self.per_object.setdefault(st.current_object, Bbox())):
                box.add(start[0], start[1], z)
                box.add(end[0], end[1], z)
        if extruding and abs(end[2] - start[2]) > EPS:
            self.vase_lines.append(line)
        if self._collect_active(line):
            self._emit(Move(line=line, start=tuple(start), end=tuple(end),
                            speed=self._speed(), max_velocity=st.max_velocity,
                            accel=st.accel, accel_to_decel=st.accel_to_decel,
                            square_corner_velocity=st.square_corner_velocity,
                            extruding=extruding))

    def _target(self, params, line):
        st = self.st
        tgt = list(st.pos)
        for i, ax in enumerate("XYZ"):
            v = _fparam(params, ax)
            if v is not None:
                tgt[i] = v if st.absolute else st.pos[i] + v
        e = _fparam(params, "E")
        if e is not None:
            e *= st.extrude_factor / 100.0
            tgt[3] = e if (st.e_absolute and st.absolute) else st.pos[3] + e
        if any(abs(v) > MAX_COORDINATE for v in tgt):
            raise ValueError("line %d: move target outside the modelled range "
                             "(|value| > %g)" % (line, MAX_COORDINATE))
        return tgt

    def _linear(self, line, params):
        f = _fparam(params, "F")
        if f is not None and f > 0:
            self.st.feed = f
        tgt = self._target(params, line)
        if any(abs(tgt[i] - self.st.pos[i]) > EPS for i in range(4)):
            self._segment(line, self.st.pos, tgt)
        self.st.pos = tgt

    def _arc(self, line, params, clockwise):
        """Split G2/G3 into segments exactly like klippy/extras/gcode_arcs.py."""
        st = self.st
        f = _fparam(params, "F")
        if f is not None and f > 0:
            st.feed = f
        if st.plane != "G17":
            self.events.append(Event(line, "plane", st.plane))
        cur = list(st.pos)
        tgt = self._target(params, line)
        i_off = _fparam(params, "I", 0.0)
        j_off = _fparam(params, "J", 0.0)
        if not (i_off or j_off):
            # Klipper rejects this (R-form arcs are not supported); treat as a
            # straight move for timing and flag it for the tail check.
            self.events.append(Event(line, "forbidden", "ARC_NO_IJ"))
            self._linear(line, params)
            return
        if max(abs(i_off), abs(j_off)) > MAX_COORDINATE:
            raise ValueError("line %d: arc centre offset too large to model" % line)
        cx, cy = cur[0] + i_off, cur[1] + j_off
        r0, r1 = -i_off, -j_off
        rt0, rt1 = tgt[0] - cx, tgt[1] - cy
        ang = math.atan2(r0 * rt1 - r1 * rt0, r0 * rt0 + r1 * rt1)
        if ang < 0.0:
            ang += 2.0 * math.pi
        if clockwise:
            ang -= 2.0 * math.pi
        if ang == 0.0 and cur[0] == tgt[0] and cur[1] == tgt[1]:
            ang = 2.0 * math.pi
        radius = math.hypot(r0, r1)
        linear = tgt[2] - cur[2]
        flat = radius * ang
        travel = math.hypot(flat, linear) if linear else abs(flat)
        if not math.isfinite(travel) or travel / self.limits.arc_resolution > MAX_ARC_SEGMENTS:
            raise ValueError("line %d: arc too long to model (more than %d segments)"
                             % (line, MAX_ARC_SEGMENTS))
        segments = max(1.0, math.floor(travel / self.limits.arc_resolution))
        n = int(segments)
        d_theta = ang / segments
        d_lin = linear / segments
        d_e = (tgt[3] - cur[3]) / segments
        prev = cur
        for k in range(1, n + 1):
            if k == n:
                p = list(tgt)
            else:
                c, s = math.cos(k * d_theta), math.sin(k * d_theta)
                p = [cx + r0 * c - r1 * s, cy + r0 * s + r1 * c,
                     cur[2] + k * d_lin, cur[3] + k * d_e]
            self._segment(line, prev, p)
            prev = p
        st.pos = list(tgt)

    # -- main entry --------------------------------------------------------
    def feed(self, line: int, offset: int, text: str):
        if self.until_byte is not None and offset >= self.until_byte:
            self.past_stop = True
            if self.bbox_until is None:
                self.bbox_until = line - 1
            if self.collect_until is None:
                self.collect_until = line - 1
            self.until_byte = None
        self.offsets.append(offset)
        self.lines.append(line)
        self.last_line = line
        st = self.st
        st.line = line
        stripped = text.strip()
        if any((ord(ch) < 0x20 and ch not in "\t\r") or ord(ch) == 0x7F
               for ch in stripped):
            # VT, FF, NUL, ESC...: same reasoning as a bare CR, and the preview
            # builder refuses them too, so the verdict does not depend on
            # whether --preview was given.
            self.events.append(Event(line, "forbidden", "CTRL_IN_LINE"))
        if "\r" in stripped:
            # A bare CR may start a second command that this scanner cannot see.
            # Comment lines are included: whether Klipper splits at a bare CR is
            # unverified (KI-14), so ";note\rG28 X" is refused too. A CRLF line
            # ending is stripped above and does not count.
            self.events.append(Event(line, "forbidden", "CR_IN_LINE"))
        if stripped.startswith(";"):
            self._comment(line, stripped)
            return
        cmd, params = parse(text)
        if cmd is None:
            return
        if cmd in ("G0", "G1"):
            self._linear(line, params)
        elif cmd in ("G2", "G3"):
            self._arc(line, params, clockwise=(cmd == "G2"))
        elif cmd == "G4":
            ms = _fparam(params, "P", 0.0)
            secs = _fparam(params, "S")
            seconds = secs if secs is not None else ms / 1000.0
            if self._collect_active(line):
                self._emit(Dwell(line, seconds, "G4"))
        elif cmd in ("M400",):
            if self._collect_active(line):
                self._emit(Dwell(line, 0.0, cmd))
        elif cmd == "G90":
            st.absolute = True
        elif cmd == "G91":
            st.absolute = False
            self.events.append(Event(line, "g91", "relative XYZ moves"))
        elif cmd == "M82":
            st.e_absolute = True
            self.events.append(Event(line, "m82", "absolute extrusion"))
        elif cmd == "M83":
            st.e_absolute = False
        elif cmd == "G92":
            for i, ax in enumerate("XYZE"):
                v = _fparam(params, ax)
                if v is not None:
                    # Klipper scales G92 E by M221 like any E value, so pos[3]
                    # stays in the same (scaled) units as the move targets.
                    st.pos[i] = v * st.extrude_factor / 100.0 if ax == "E" else v
                    if ax != "E":
                        self.events.append(Event(line, "g92_xyz", "G92 " + ax))
            if not params:
                st.pos = [0.0, 0.0, 0.0, 0.0]
                self.events.append(Event(line, "g92_xyz", "G92 (all axes)"))
        elif cmd in ("G17", "G18", "G19"):
            st.plane = cmd
        elif cmd == "M204":
            s = _fparam(params, "S")
            if s is None:
                p, t = _fparam(params, "P"), _fparam(params, "T")
                s = min(v for v in (p, t) if v is not None) if (p or t) else None
            if s and s > 0:
                st.accel = s
        elif cmd == "SET_VELOCITY_LIMIT":
            v = _fparam(params, "VELOCITY")
            if v and v > 0:
                st.max_velocity = v
            a = _fparam(params, "ACCEL")
            if a and a > 0:
                st.accel = a
            scv = _fparam(params, "SQUARE_CORNER_VELOCITY")
            if scv is not None and scv >= 0:
                st.square_corner_velocity = scv
            # Klipper rejects MINIMUM_CRUISE_RATIO outside 0 <= r < 1 and
            # ACCEL_TO_DECEL <= 0 (the line errors and changes nothing), so such a
            # value must not reach the timing model: 1.0 would leave no
            # deceleration headroom and break the look-ahead.
            mcr = _fparam(params, "MINIMUM_CRUISE_RATIO")
            a2d = _fparam(params, "ACCEL_TO_DECEL")
            if mcr is not None:
                if 0.0 <= mcr < 1.0:
                    st.minimum_cruise_ratio = mcr
                    st.mcr_set = True
            elif a2d is not None and a2d > 0:
                # Recent Klipper maps the deprecated ACCEL_TO_DECEL to a
                # minimum_cruise_ratio relative to the current accel.
                st.minimum_cruise_ratio = min(max(1.0 - min(1.0, a2d / st.accel), 0.0), 1.0)
                st.accel_to_decel_set = True
        elif cmd == "M220":
            s = _fparam(params, "S")
            if s and s > 0:
                st.speed_factor = s
        elif cmd == "M221":
            s = _fparam(params, "S")
            if s and s > 0:
                st.extrude_factor = s
        elif cmd in ("M104", "M109"):
            s = _fparam(params, "S")
            if s is not None:
                st.extruder_temp = s
            if cmd == "M109" and self._collect_active(line):
                self._emit(Dwell(line, 0.0, cmd))
        elif cmd in ("M140", "M190"):
            s = _fparam(params, "S")
            if s is not None:
                st.bed_temp = s
            if cmd == "M190" and self._collect_active(line):
                self._emit(Dwell(line, 0.0, cmd))
        elif cmd == "M106":
            s = _fparam(params, "S")
            st.fan = int(round(s)) if s is not None else 255
        elif cmd == "M107":
            st.fan = 0
        elif cmd == "SET_PRESSURE_ADVANCE":
            pa = _fparam(params, "ADVANCE")
            if pa is not None:
                st.pressure_advance = pa
            sm = _fparam(params, "SMOOTH_TIME")
            if sm is not None:
                st.pa_smooth_time = sm
        elif cmd == "EXCLUDE_OBJECT_DEFINE":
            name = params.get("NAME")
            if name and not self.past_stop:
                st.objects[name.upper()] = split_code(text)
        elif cmd == "EXCLUDE_OBJECT_START":
            st.current_object = params.get("NAME") or None
        elif cmd == "EXCLUDE_OBJECT_END":
            st.current_object = None
        elif cmd == "EXCLUDE_OBJECT":
            name = params.get("NAME") or ""
            if name and name.upper() not in [n.upper() for n in st.excluded]:
                st.excluded.append(name)
        elif re.match(r"^T\d+$", cmd):
            tool = int(cmd[1:])
            self.events.append(Event(line, "tool", cmd, float(tool)))
            if tool not in self.tools_used and not self.past_stop:
                self.tools_used.append(tool)
            st.tool = tool
        if cmd in FORBIDDEN_TAIL or cmd in self.extra_forbidden:
            self.events.append(Event(line, "forbidden", cmd))
        elif cmd == "SET_GCODE_OFFSET" and _fparam(params, "MOVE", 0.0) != 0.0:
            # A plain offset only shifts later moves; with MOVE=1 it moves now.
            self.events.append(Event(line, "forbidden", "SET_GCODE_OFFSET_MOVE"))
        if cmd in END_MACROS:
            self.end_seen = True
        elif self.end_seen and cmd in ("G0", "G1", "G2", "G3"):
            e = params.get("E")
            if e is not None and e > 0:
                # The end-of-print macro cools, parks and powers off: it is
                # only legitimate as the end of the file.
                self.events.append(Event(line, "forbidden", "END_PRINT"))  # an end macro ran
                self.end_seen = False

    def _comment(self, line, text):
        st = self.st
        if text.startswith(";LAYER_CHANGE"):
            st.layer += 1
            st.layer_start_line = line
            if self._collect_active(line):
                self._new_layer()
            self.layer_starts.append((line, st.layer, None))
        elif text.startswith(";Z:"):
            try:
                z = float(text[3:].strip())
            except ValueError:
                return
            if not math.isfinite(z):
                return    # ";Z:nan" / ";Z:inf" would poison the Z-monotonic check
            st.layer_z = z
            self.events.append(Event(line, "layer_z", text, z))
            if self.layer_starts and self.layer_starts[-1][1] == st.layer:
                ln, layer, _ = self.layer_starts[-1]
                self.layer_starts[-1] = (ln, layer, z)

    def result(self) -> ScanResult:
        moves = [m for layer in self.layer_moves for m in layer]
        return ScanResult(state=copy.deepcopy(self.st), total=self.total,
                          per_object=self.per_object, moves=moves,
                          events=self.events,
                          line_offsets=(self.offsets, self.lines),
                          layer_starts=self.layer_starts,
                          vase_lines=self.vase_lines,
                          tools_used=list(self.tools_used),
                          last_line=self.last_line,
                          bbox_until=self.bbox_until or self.last_line)


def scan(records: Iterable[Record], limits: Limits = None,
         until_line: Optional[int] = None, bbox_until: Optional[int] = None,
         collect_moves_until: Optional[int] = None,
         keep_layers: int = 2, until_byte: Optional[int] = None,
         extra_forbidden: Optional[Dict[str, str]] = None) -> ScanResult:
    """Scan records. Stops after until_line (inclusive) if given."""
    sc = Scanner(limits, bbox_until=bbox_until, keep_layers=keep_layers,
                 collect_moves_until=collect_moves_until, until_byte=until_byte,
                 extra_forbidden=extra_forbidden)
    for line, offset, text in records:
        if until_line is not None and line > until_line:
            break
        sc.feed(line, offset, text)
    return sc.result()


def state_at(records: Iterable[Record], line: int, limits: Limits = None) -> State:
    """State in effect just BEFORE `line` runs (after line - 1)."""
    return scan(records, limits, until_line=line - 1, keep_layers=1,
                collect_moves_until=0).state


def line_at_byte(result: ScanResult, offset: int) -> Optional[int]:
    """Line containing byte `offset` (the line that starts at or before it)."""
    offsets, lines = result.line_offsets
    i = bisect.bisect_right(offsets, offset) - 1
    if i < 0:
        return None
    return lines[i]


def last_processed_line(result: ScanResult, exit_position: int) -> Optional[int]:
    """Klipper's 'Exiting SD card print (position N)': N is the byte after the
    last dispatched line, so that line contains byte N - 1."""
    return line_at_byte(result, exit_position - 1)


def tail_findings(result: ScanResult, from_line: int,
                  current_tool: Optional[int]) -> Dict[str, list]:
    """Problems in the part of the file that a resume would run again."""
    out = {"forbidden": [], "tool_change": [], "m82": [], "vase": [],
           "non_monotonic_z": [], "g91": [], "g92_xyz": []}
    max_z = None
    for ev in result.events:
        if ev.line < from_line:
            if ev.kind == "layer_z":
                max_z = ev.value if max_z is None else max(max_z, ev.value)
            continue
        if ev.kind == "forbidden":
            out["forbidden"].append((ev.line, ev.detail))
        elif ev.kind == "tool" and (current_tool is None or
                                    int(ev.value) != current_tool):
            out["tool_change"].append((ev.line, ev.detail))
        elif ev.kind == "m82":
            out["m82"].append((ev.line, ev.detail))
        elif ev.kind == "g91":
            out["g91"].append((ev.line, ev.detail))
        elif ev.kind == "g92_xyz":
            out["g92_xyz"].append((ev.line, ev.detail))
        elif ev.kind == "layer_z":
            if max_z is not None and ev.value < max_z - 1e-6:
                out["non_monotonic_z"].append((ev.line, ev.detail))
            max_z = ev.value if max_z is None else max(max_z, ev.value)
    vase = [ln for ln in result.vase_lines if ln >= from_line]
    if len(vase) >= VASE_MIN_MOVES:
        out["vase"].append((vase[0], "%d extrusion moves change Z" % len(vase)))
    return out


# -- loaders (the only I/O in this module) ---------------------------------

def iter_file(path: str, start_line: int = 1) -> Iterator[Record]:
    """Records of a plain G-code file (byte offsets exact, any line ending)."""
    offset = 0
    with open(path, "rb") as fh:
        n = 0
        while True:
            # A single line is read in bounded pieces: a file with one enormous
            # line must not be pulled into memory whole.
            raw = fh.readline(MAX_LINE_BYTES + 1)
            if not raw:
                break
            if len(raw) > MAX_LINE_BYTES and not raw.endswith(b"\n"):
                raise ValueError("line %d is longer than %d bytes" % (n + 1, MAX_LINE_BYTES))
            n += 1
            if n >= start_line:
                yield n, offset, raw.decode("utf-8", "replace").rstrip("\r\n")
            offset += len(raw)


def _tail_from_byte(text: str, skip: int) -> str:
    """`text` from byte `skip` of its UTF-8 encoding (a cut inside a
    multi-byte character gives a replacement character, which then fails
    the comparison closed)."""
    if skip <= 0:
        return text
    return text.encode("utf-8", "replace")[skip:].decode("utf-8", "replace")


def _with_next_offset(records: Iterable[Record]):
    """(record, offset of the next record or None), one record of lookahead."""
    prev = None
    for rec in records:
        if prev is not None:
            yield prev, rec[1]
        prev = rec
    if prev is not None:
        yield prev, None


def file_text_at(records: Iterable[Record], wanted: List[Tuple[int, int]]) -> Dict[int, str]:
    """For each (byte offset, length) in `wanted`, the file's text from that
    offset (up to `length` characters), rebuilt from the records. Lines are
    joined with newlines (a carriage return is dropped), which is what the log
    shows. An offset that no record covers gives the empty string."""
    out = {pos: "" for pos, _ in wanted}
    todo = dict(wanted)
    horizon = max((p + n for p, n in wanted), default=0)
    pending = {}
    for (_, offset, text), next_offset in _with_next_offset(records):
        if offset > horizon:
            break
        # The next record's offset is exact even when this line holds invalid
        # UTF-8 (each such byte re-encodes to three bytes); an excerpt can have
        # a gap after the line, so never extend past the line's own length.
        end = offset + len(text.encode("utf-8", "replace")) + 1
        if next_offset is not None:
            end = min(end, next_offset)
        for pos in list(todo):
            if pos < end and pos + todo[pos] > offset:
                piece = pending.get(pos)
                if piece is None:
                    # pos and offset are byte offsets: slice the encoded line.
                    pending[pos] = _tail_from_byte(text, pos - offset) + "\n"
                else:
                    pending[pos] = piece + text + "\n"
                if len(pending[pos]) >= todo[pos]:
                    out[pos] = pending[pos][:todo[pos]]
                    del todo[pos]
    for pos, piece in pending.items():
        if pos in todo:
            out[pos] = piece[:todo[pos]]
    return out


def iter_excerpt(gz_path: str, map_path: str) -> Iterator[Record]:
    """Records of a fixture excerpt.

    The map file holds runs [original_first_line, line_count, byte_offset of
    that line in the original file]; lines inside a run are consecutive in the
    original, so their offsets follow from their lengths (plus one newline).
    """
    with open(map_path, "r", encoding="utf-8") as fh:
        runs = json.load(fh)["runs"]
    with gzip.open(gz_path, "rt", encoding="utf-8", newline="\n") as fh:
        text = fh.read(MAX_EXCERPT_CHARS + 1)
    if len(text) > MAX_EXCERPT_CHARS:
        raise ValueError("excerpt is larger than %d characters when unpacked" % MAX_EXCERPT_CHARS)
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    idx = 0
    for first, count, offset in runs:
        for k in range(count):
            if idx >= len(lines):
                raise ValueError("excerpt map covers more lines than the file has (%d)"
                                 % len(lines))
            text = lines[idx]
            idx += 1
            yield first + k, offset, text
            offset += len(text.encode("utf-8")) + 1
    if idx != len(lines):
        raise ValueError("excerpt map covers %d lines, file has %d"
                         % (idx, len(lines)))
