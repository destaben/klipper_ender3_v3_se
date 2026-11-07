"""Build the resume G-code text (P1: preview only; upload and start are P2).

The generated part, in order:
  1. the state the scanner found at the resume line (no heat, no homing);
  2. a retraction to retract_before_travel before anything moves (N6);
  3. a RELATIVE lift (G91) by APPROACH_LIFT: Z is not known until it has been
     re-referenced, so no absolute Z move is generated before the gate (N5);
  4. a travel at that height to the off-part park spot (profile park.xy);
  5. only there: bed mesh, bed and nozzle heating (N6, N8);
  6. a travel over the part to the resume XY at the lifted height;
  7. GATE_MARK: stop here until procedure step 9 (two measured probe spots);
  8. after the gate: lower to the resume Z, prime, and continue with the
     original file from the chosen line (the sliced job, copied as it is).

UI metadata (A9): the original file's leading comment block (slicer header and
thumbnails) is copied FIRST, with its time and filament estimates rewritten to
the remaining values, the footer comes with the tail, and SET_PRINT_STATS_INFO
sets the layer counter at the start and after every ;LAYER_CHANGE. How
Moonraker and Mainsail derive layer, slicer and estimate from a file is TO
VERIFY on the printer (docs/print-rescue.md).

It never homes, probes or calls START_PRINT: the toolhead must already be
re-referenced and parked above the part by the operator (P4 runs that
interactively). check_generated() is an ALLOW-LIST for every generated line
(N11): a command or parameter that is not listed is refused.

Pure logic, standard library only.
"""

import math
import re
from typing import Iterable, List, Optional

from gcode_scan import State

APPROACH_LIFT = 3.0     # mm above the resume Z for the XY approach
Z_FEED = 300            # mm/min (Klipper clamps to max_z_velocity anyway)
TRAVEL_FEED = 6000      # mm/min, deliberately slower than the slicer travel
PRIME_FEED = 300        # mm/min
GATE_MARK = "; GATE: Z is not re-referenced yet"
# Not a valid command on purpose: Klipper is EXPECTED to report "Unknown
# command" and stop the SD print here (TO VERIFY on the printer, KI-14), so
# the preview should not run past the gate by accident.
# The operator removes this line only after procedure step 9.
GATE_STOP = "PRINT_RESCUE_GATE_REMOVE_THIS_LINE_AFTER_STEP_9"

# Generated commands and the parameters each may carry. Classic G/M words are
# one letter plus a number; extended commands are KEY=VALUE.
ALLOWED = {
    "G1": ("X", "Y", "Z", "E", "F"),
    "G21": (), "G90": (), "G91": (), "G92": ("E",),
    "M83": (), "M107": (), "M106": ("S",), "M220": ("S",), "M221": ("S",),
    "M104": ("S",), "M109": ("S",), "M140": ("S",), "M190": ("S",),
    "SET_IDLE_TIMEOUT": ("TIMEOUT",),
    "SET_PRESSURE_ADVANCE": ("ADVANCE", "SMOOTH_TIME"),
    "SET_VELOCITY_LIMIT": ("ACCEL", "ACCEL_TO_DECEL", "MINIMUM_CRUISE_RATIO",
                           "VELOCITY", "SQUARE_CORNER_VELOCITY"),
    "SET_GCODE_OFFSET": ("Z",),
    "BED_MESH_PROFILE": ("LOAD",),
    "EXCLUDE_OBJECT_DEFINE": ("NAME", "CENTER", "POLYGON", "RADIUS"),
    "EXCLUDE_OBJECT": ("NAME",),
    "EXCLUDE_OBJECT_START": ("NAME",),
    # A resume skips START_PRINT, so what START_PRINT does for the whole job
    # must be restated here: CANCEL_PRINTER_SHUTDOWN (an armed auto power-off
    # must not cut the power mid-print) and G17 (the slicer's arc plane).
    "CANCEL_PRINTER_SHUTDOWN": (),
    "G17": (),
    # M117 without text clears the stale display message that
    # CANCEL_PRINTER_SHUTDOWN leaves in the job panel.
    "M117": (),
    # Layer counter for the UI (the resume file has no layer history).
    "SET_PRINT_STATS_INFO": ("TOTAL_LAYER", "CURRENT_LAYER"),
}
_NUMBER = re.compile(r"^[-+]?(\d+\.?\d*|\.\d+)$")
_VALUE = re.compile(r"^[A-Za-z0-9_.,+\-\[\]]*$")


class ResumeRefused(Exception):
    pass


def _num(v: float) -> str:
    s = ("%.4f" % v).rstrip("0").rstrip(".")
    return "0" if s in ("-0", "") else s


_TIME_LINE = re.compile(r"^(;\s*TIME\s*:\s*)(\d+(?:\.\d+)?)\s*$")
_FILAMENT_M = re.compile(r"^(;\s*Filament used\s*:\s*)(\d+(?:\.\d+)?)\s*m\s*$")
_HMS = re.compile(r"(?:(\d+)d)?\s*(?:(\d+)h)?\s*(?:(\d+)m)?\s*(?:(\d+)s)?$")
_TOTAL_LAYERS = re.compile(r"^;\s*total layer number\s*:\s*(\d+)\s*$")


def total_layers_from(header: Iterable[str]) -> Optional[int]:
    for ln in header:
        m = _TOTAL_LAYERS.match(ln)
        if m:
            return int(m.group(1))
    return None


def header_from_records(records: Iterable) -> List[str]:
    """The leading comment block of the original file: every line up to the
    first line that holds a command (slicer header, thumbnails)."""
    out = []
    for _, _, text in records:
        if text.strip() and not text.lstrip().startswith(";"):
            break
        out.append(text)
    return out


def remaining_fraction(state: State, header: Iterable[str]) -> Optional[float]:
    """Share of the job's filament still to be printed at the resume line, or
    None when it cannot be known (no ';Filament used:' header, absolute
    extrusion, or an E position that does not fit the total)."""
    total_mm = None
    for ln in header:
        m = _FILAMENT_M.match(ln)
        if m:
            total_mm = float(m.group(2)) * 1000.0
            break
    if not total_mm or state.e_absolute:
        return None
    used = state.e_total
    if not (0.0 <= used < total_mm):
        return None
    return (total_mm - used) / total_mm


def _fmt_hms(seconds: float) -> str:
    seconds = int(round(max(seconds, 0.0)))
    h, rem = divmod(seconds, 3600)
    m, sec = divmod(rem, 60)
    return "%dh %dm %ds" % (h, m, sec) if h else "%dm %ds" % (m, sec)


def rewrite_estimate(line: str, fraction: Optional[float]) -> str:
    """Scale a slicer estimate comment (;TIME:, ;Filament used:, estimated
    printing time) to the remaining fraction of the job. Other lines, and any
    line when fraction is None, are returned unchanged. The scaling is
    proportional to filament, so it is an estimate, and the file says so."""
    if fraction is None or not line.startswith(";"):
        return line
    m = _TIME_LINE.match(line)
    if m:
        return "%s%.2f" % (m.group(1), float(m.group(2)) * fraction)
    m = _FILAMENT_M.match(line)
    if m:
        return "%s%.2fm" % (m.group(1), float(m.group(2)) * fraction)
    # Plain string operations: no regex that could backtrack on a crafted line.
    if len(line) <= 300 and "estimated printing time" in line and "=" in line:
        head, _, rest = line.partition("=")
        if head.lstrip(";").strip().startswith("estimated printing time"):
            hms = _HMS.match(rest.strip())
            if hms and any(hms.groups()):
                d, h, mi, sec = (int(g or 0) for g in hms.groups())
                total = ((d * 24 + h) * 60 + mi) * 60 + sec
                return "%s= %s" % (head, _fmt_hms(total * fraction))
    return line


def _code_tokens(line: str) -> List[str]:
    return line.split(";", 1)[0].split()


def _printable(text: str) -> bool:
    """Printable ASCII only: no control character (CR, LF, TAB, ...) and no
    non-ASCII character, so no interpolated value can start a new line."""
    return all(0x20 <= ord(ch) <= 0x7E for ch in text)


def _violation(line: str) -> Optional[str]:
    """Why a generated line is not allowed, or None."""
    tokens = _code_tokens(line)
    if not tokens:
        return None
    cmd = tokens[0].upper()
    keys = ALLOWED.get(cmd)
    if keys is None:
        return "command %s is not allowed in generated output" % cmd
    classic = cmd[0] in "GM" and cmd[1:].isdigit()
    for tok in tokens[1:]:
        if classic:
            key, value = tok[:1].upper(), tok[1:]
            ok = key in keys and _NUMBER.match(value) is not None
        else:
            key, _, value = tok.partition("=")
            ok = "=" in tok and key.upper() in keys and _VALUE.match(value) is not None
        if not ok:
            return "%s: parameter %r is not allowed" % (cmd, tok)
    return None


def check_generated(lines: Iterable[str]) -> List[str]:
    """Problems in the generated part (not the copied original file).

    The lines are joined and split again, so the allow-list sees what the
    printer sees: an element with an embedded newline is checked line by line,
    and a control or non-ASCII character anywhere is a problem.
    Allow-list for every command and parameter. Before the gate, the only Z
    motion allowed is a relative lift (G91 in force, Z > 0), because Z is not
    known until it has been re-referenced. Returns [] when the part is clean."""
    problems = []
    relative = False          # G90 is the default
    gated = False
    text = "\n".join(lines)
    for n, line in enumerate(text.split("\n"), 1):
        if not _printable(line):
            problems.append("line %d: control or non-ASCII character in generated "
                            "output: %r" % (n, line))
            continue
        if line.startswith(GATE_MARK):
            gated = True
            continue
        if line == GATE_STOP:
            continue
        tokens = _code_tokens(line)
        if not tokens:
            continue
        bad = _violation(line)
        if bad:
            problems.append("line %d: %s" % (n, bad))
            continue
        cmd = tokens[0].upper()
        if cmd == "G90":
            relative = False
        elif cmd == "G91":
            relative = True
        if cmd == "G1" and not gated:
            for tok in tokens[1:]:
                if tok[:1].upper() == "Z":
                    z = float(tok[1:])
                    if not relative or z <= 0:
                        problems.append("line %d: Z move before the gate must be a "
                                        "relative lift up (G91, Z > 0)" % n)
    return problems


def build(state: State, source_name: str, resume_line: int,
          tail: Iterable[str], *, overlap_s: float, overlap_mm: float,
          park_xy, bed_mesh_profile: str,
          retract_before_travel: float = 2.5, prime_extra: float = 0.8,
          excluded: Optional[List[str]] = None,
          gcode_offset_z: Optional[float] = None,
          speed_factor: Optional[float] = None,
          extrude_factor: Optional[float] = None,
          default_scv: Optional[float] = None,
          default_velocity: Optional[float] = None,
          refusal_codes: Iterable[str] = (),
          open_codes: Iterable[str] = (),
          header_lines: Iterable[str] = (),
          total_layers: Optional[int] = None,
          remaining_fraction: Optional[float] = None) -> str:
    """Return the resume file text. Raises ResumeRefused if R17 is present or
    the generated part is not allowed."""
    if "R17" in set(refusal_codes):
        raise ResumeRefused("R17: the tail contains a forbidden command")
    if not _printable(source_name):
        raise ResumeRefused("source file name is not printable ASCII: %r" % source_name)
    x, y, z = state.pos[0], state.pos[1], state.pos[2]
    px, py = park_xy
    label = "NOT VERIFIED, do not print without review"
    codes = list(open_codes)
    if codes:
        label += "; open at planning time: %s" % ", ".join(codes)
    header = [ln.rstrip("\r\n") for ln in header_lines]
    for ln in header:
        if ln.strip() and not ln.startswith(";"):
            raise ResumeRefused("the copied header holds a command, not only comments")
        if not _printable(ln):
            raise ResumeRefused("the copied header holds a control or non-ASCII character")
    if total_layers is None:
        total_layers = total_layers_from(header)
    if remaining_fraction is not None and not (0.0 < remaining_fraction <= 1.0):
        remaining_fraction = None
    header = [rewrite_estimate(ln, remaining_fraction) for ln in header]
    gen = [
        "; print-rescue resume PREVIEW (phase 1, %s)" % label,
        "; source file: %s" % source_name,
        "; resumes at original line %d, nozzle X%s Y%s Z%s"
        % (resume_line, _num(x), _num(y), _num(z)),
        "; planned overlap: %.2f s, %.1f mm of path (overlap, never a gap)"
        % (overlap_s, overlap_mm),
        "; estimates in the header are scaled to the remaining filament (%s)"
        % ("%.1f%%" % (remaining_fraction * 100.0) if remaining_fraction else "not scaled"),
        # A resume skips START_PRINT: restate what it did for the whole job.
        "CANCEL_PRINTER_SHUTDOWN",
        "M117",
        "G90",
        "G21",
        "M83",
        # 600 s is the configured [idle_timeout] default, restated so the file
        # does not depend on an earlier SET_IDLE_TIMEOUT. The rescue itself
        # (parking, heating, the paper test) needs a longer timeout, which is
        # a runner step outside this file because the idle gcode turns the
        # heaters off and clears homing.
        "SET_IDLE_TIMEOUT TIMEOUT=600",
    ]
    if total_layers:
        gen.append("SET_PRINT_STATS_INFO TOTAL_LAYER=%d CURRENT_LAYER=%d"
                   % (total_layers, max(state.layer, 0)))
    for define in state.objects.values():
        gen.append(define)
    for name in list(state.excluded) + list(excluded or []):
        gen.append("EXCLUDE_OBJECT NAME=%s" % name)
    if state.pressure_advance is not None:
        pa = "SET_PRESSURE_ADVANCE ADVANCE=%s" % _num(state.pressure_advance)
        if state.pa_smooth_time is not None:
            pa += " SMOOTH_TIME=%s" % _num(state.pa_smooth_time)
        gen.append(pa)
    svl = "SET_VELOCITY_LIMIT ACCEL=%s" % _num(state.accel)
    if state.accel_to_decel_set:
        svl += " ACCEL_TO_DECEL=%s" % _num(state.accel_to_decel)
    elif state.mcr_set:
        svl += " MINIMUM_CRUISE_RATIO=%s" % _num(state.minimum_cruise_ratio)
    if default_velocity is not None and abs(state.max_velocity - default_velocity) > 1e-9:
        svl += " VELOCITY=%s" % _num(state.max_velocity)
    if default_scv is not None and abs(state.square_corner_velocity - default_scv) > 1e-9:
        svl += " SQUARE_CORNER_VELOCITY=%s" % _num(state.square_corner_velocity)
    gen.append(svl)
    gen.append("M220 S%s" % _num(speed_factor if speed_factor is not None
                                 else state.speed_factor))
    gen.append("M221 S%s" % _num(extrude_factor if extrude_factor is not None
                                 else state.extrude_factor))
    if state.fan is not None:
        gen.append("M106 S%d" % state.fan if state.fan > 0 else "M107")
    if gcode_offset_z is not None and not math.isfinite(gcode_offset_z):
        raise ResumeRefused("gcode offset Z from the log is not a finite number")
    if gcode_offset_z is not None:
        gen.append("SET_GCODE_OFFSET Z=%s ; baby-step from the log" % _num(gcode_offset_z))
    # Filament: the slicer retracted state.retracted already; top up to the
    # full retract_before_travel before any move, so nothing oozes en route.
    retract_pre = max(retract_before_travel - state.retracted, 0.0)
    gen.append("G92 E0")
    if retract_pre > 0:
        gen.append("G1 E-%s F%d ; retract before the approach (%s mm in total)"
                   % (_num(retract_pre), PRIME_FEED, _num(retract_before_travel)))
    gen += [
        "G91",
        "G1 Z%s F%d ; relative lift only: Z is not known until re-referenced"
        % (_num(APPROACH_LIFT), Z_FEED),
        "G90",
        "G1 X%s Y%s F%d ; off-part park spot (profile park.xy): nothing is heated before this"
        % (_num(px), _num(py), TRAVEL_FEED),
        "BED_MESH_PROFILE LOAD=%s" % bed_mesh_profile,
    ]
    if state.bed_temp is not None:
        gen.append("M140 S%s" % _num(state.bed_temp))
        gen.append("M190 S%s" % _num(state.bed_temp))
    if state.extruder_temp is not None:
        gen.append("M104 S%s" % _num(state.extruder_temp))
        gen.append("M109 S%s" % _num(state.extruder_temp))
    gen.append("G1 X%s Y%s F%d ; over the part at the lifted height, retracted"
               % (_num(x), _num(y), TRAVEL_FEED))
    gen.append("%s. Stop here until procedure step 9 (two measured probe spots) is done"
               % GATE_MARK)
    gen.append(GATE_STOP)
    gen.append("G1 Z%s F%d ; lower to the resume height (absolute, after the gate)"
               % (_num(z), Z_FEED))
    if state.current_object:
        gen.append("EXCLUDE_OBJECT_START NAME=%s" % state.current_object)
    # The slicer's own retraction (state.retracted) is undone by the original
    # lines, so only the rest of what is retracted at the gate is primed here.
    # More than retract_before_travel retracted already: nothing is retracted
    # on top of it (retract_pre above is 0), so the retracted total is the max.
    retracted_total = max(retract_before_travel, state.retracted)
    prime = retracted_total + prime_extra - state.retracted
    gen += [
        "G1 E%s F%d ; prime: %s retracted before travel + %s extra%s"
        % (_num(max(prime, 0.0)), PRIME_FEED, _num(retracted_total),
           _num(prime_extra),
           (" - %s already retracted by the slicer" % _num(state.retracted))
           if state.retracted else ""),
        "G92 E0",
        "G1 F%s" % _num(state.feed),
    ]
    problems = check_generated(gen)
    if problems:
        raise ResumeRefused("generated output not allowed: " + problems[0])
    gen.append("G17")
    gen.append("; ---- original file from line %d ----" % resume_line)
    layer = max(state.layer, 0)
    for raw in tail:
        line = raw.rstrip("\r\n")
        gen.append(rewrite_estimate(line, remaining_fraction))
        if line.startswith(";LAYER_CHANGE") and total_layers:
            layer += 1
            gen.append("SET_PRINT_STATS_INFO CURRENT_LAYER=%d" % layer)
    # The NOT VERIFIED label stays the very first line; the copied slicer
    # header follows it (Moonraker is expected to scan the head of the file,
    # TO VERIFY on the printer).
    return "\n".join(gen[:1] + header + gen[1:]) + "\n"
