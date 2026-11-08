"""Versioned machine profile for print-rescue: load, validate, fingerprint.

A profile is JSON: {"schema": "print-rescue-profile", "version": 1, ...}.
Safety-critical leaves carry provenance (council ruling: provenance only on
safety fields):

    {"value": ..., "unit": "mm", "source": "measured", "safety_critical": true,
     "method": "...", "measured_on": "YYYY-MM-DD", "note": "..."}

source is one of measured | config | user | vendor | community | default.
Only a leaf that is measured AND confirmed (source "measured" with a
measured_on date and a method) is verified. Every other safety-critical
leaf, including values taken from printer.cfg (config) or stated by the
user (user), is unverified: motion_ready() lists it and it blocks motion
(refusal R13: plan mode still runs, motion does not). Non-safety leaves may
be plain values.

validate() never raises on malformed input; it returns the problems.

Pure logic except load_profile() / read_text(). Standard library only.
"""

import json
import math
import re
from typing import Dict, List, Optional, Tuple

SCHEMA = "print-rescue-profile"
VERSION = 1
SOURCES = ("measured", "config", "user", "vendor", "community", "default")
VERIFIED_SOURCE = "measured"
FRAMES = ("gantry", "frame", "bed")
DATE_RE = re.compile(r"^\d{4}-\d\d-\d\d$")
BED_MESH_RE = re.compile(r"^[A-Za-z0-9_]{1,32}$")

# config_fingerprint path -> (printer.cfg section, option, kind)
FINGERPRINT_KEYS = {
    ("bltouch", "x_offset"): ("bltouch", "x_offset", "float"),
    ("bltouch", "y_offset"): ("bltouch", "y_offset", "float"),
    ("bltouch", "z_offset"): ("bltouch", "z_offset", "float"),
    ("stepper_x", "position_min"): ("stepper_x", "position_min", "float"),
    ("stepper_x", "position_max"): ("stepper_x", "position_max", "float"),
    ("stepper_y", "position_min"): ("stepper_y", "position_min", "float"),
    ("stepper_y", "position_max"): ("stepper_y", "position_max", "float"),
    ("stepper_z", "position_min"): ("stepper_z", "position_min", "float"),
    ("stepper_z", "position_max"): ("stepper_z", "position_max", "float"),
    ("stepper_z", "rotation_distance"): ("stepper_z", "rotation_distance", "float"),
    ("safe_z_home", "home_xy_position"): ("safe_z_home", "home_xy_position", "list"),
    ("safe_z_home", "z_hop"): ("safe_z_home", "z_hop", "float"),
    ("bed_mesh", "mesh_min"): ("bed_mesh", "mesh_min", "list"),
    ("bed_mesh", "mesh_max"): ("bed_mesh", "mesh_max", "list"),
    ("printer", "max_velocity"): ("printer", "max_velocity", "float"),
    ("printer", "max_accel"): ("printer", "max_accel", "float"),
    ("printer", "max_z_velocity"): ("printer", "max_z_velocity", "float"),
    ("printer", "max_z_accel"): ("printer", "max_z_accel", "float"),
    ("printer", "square_corner_velocity"): ("printer", "square_corner_velocity", "float"),
}

# section name -> required type
SECTION_TYPES = {
    "printer": dict, "motion": dict, "config_fingerprint": dict, "envelope": dict,
    "probe": dict, "head_footprint": dict, "probe_spots": list, "no_go_zones": list,
    "clearances": dict, "thermal": dict, "stop_estimation": dict, "accessories": dict,
    "witness": dict, "park": dict,
}
REQUIRED_SECTIONS = tuple(SECTION_TYPES)
ENVELOPE_KEYS = ("x_min", "x_max", "y_min", "y_max", "x_max_safe", "z_max",
                 "z_top_margin", "probe_area", "bed")
MOTION_KEYS = ("max_velocity", "max_accel", "square_corner_velocity",
               "minimum_cruise_ratio", "max_z_velocity", "max_z_accel",
               "max_extrude_only_velocity", "max_extrude_only_accel",
               "instant_corner_velocity", "arc_resolution")
STOP_KEYS = ("margin_s", "snap_window_s", "max_overlap_s", "min_resume_layer")
THERMAL_KEYS = ("retract_before_travel", "prime_extra", "max_hot_dwell_over_part_s")
CLEARANCE_KEYS = ("travel_clearance", "min_lift", "probe_spot_margin", "footprint_margin")
TIER_KEYS = ("z_from", "z_to", "x_min", "x_max", "y_min", "y_max")
RECT_KEYS = ("x_min", "x_max", "y_min", "y_max")


class ProfileError(ValueError):
    pass


# -- value helpers -----------------------------------------------------------

def _is_num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def _is_int(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _finite_tree(v) -> bool:
    if isinstance(v, bool) or v is None or isinstance(v, str):
        return True
    if isinstance(v, (int, float)):
        return math.isfinite(v)
    if isinstance(v, list):
        return all(_finite_tree(x) for x in v)
    if isinstance(v, dict):
        return all(_finite_tree(x) for x in v.values())
    return False


def is_leaf(node) -> bool:
    return isinstance(node, dict) and "value" in node and "source" in node


def val(node):
    """Value of a leaf, or the node itself when it is a plain value."""
    return node["value"] if is_leaf(node) else node


def _walk(node, path=""):
    if is_leaf(node):
        yield path, node
        return
    if isinstance(node, dict):
        for k, v in node.items():
            yield from _walk(v, "%s.%s" % (path, k) if path else k)
    elif isinstance(node, list):
        for i, v in enumerate(node):
            name = v.get("name") if isinstance(v, dict) else None
            yield from _walk(v, "%s[%s]" % (path, name or i))


def is_verified(leaf) -> bool:
    """Measured and confirmed: source measured, a measured_on date and a method."""
    if not isinstance(leaf, dict) or leaf.get("source") != VERIFIED_SOURCE:
        return False
    date = leaf.get("measured_on")
    method = leaf.get("method")
    return (isinstance(date, str) and DATE_RE.match(date) is not None and
            isinstance(method, str) and method.strip() != "")


def unverified_reason(leaf) -> Optional[str]:
    """Why a safety-critical leaf does not count as verified, or None."""
    if leaf.get("safety_critical") is not True:
        return None
    if leaf.get("source") != VERIFIED_SOURCE:
        return "source %s" % leaf.get("source")
    if not is_verified(leaf):
        return "source measured, no measured_on/method"
    return None


# -- validation --------------------------------------------------------------

def _check_leaves(p: dict) -> List[str]:
    errs = []
    for path, leaf in _walk(p):
        if leaf.get("source") not in SOURCES:
            errs.append("%s: source %r not in %s" % (path, leaf.get("source"), SOURCES))
        sc = leaf.get("safety_critical")
        if sc is not True and sc is not False:
            errs.append("%s: safety_critical must be true or false" % path)
        if not _finite_tree(leaf.get("value")):
            errs.append("%s: value must be finite numbers" % path)
    return errs


def _check_structure(p: dict) -> List[str]:
    errs = []
    if p.get("schema") != SCHEMA:
        errs.append("schema must be %r" % SCHEMA)
    if not _is_int(p.get("version")) or p.get("version") != VERSION:
        errs.append("unsupported version %r (expected %d)" % (p.get("version"), VERSION))
    if not isinstance(p.get("bed_mesh_profile"), str) or \
            BED_MESH_RE.match(p.get("bed_mesh_profile")) is None:
        errs.append("bed_mesh_profile must be 1-32 letters, digits or underscores")
    errs += _check_leaves(p)
    section_errs = []
    for s, kind in SECTION_TYPES.items():
        if s not in p:
            section_errs.append("missing section %r" % s)
        elif not isinstance(p[s], kind):
            section_errs.append("section %r must be a %s" % (s, kind.__name__))
    if section_errs:
        return errs + section_errs
    if not isinstance(p["head_footprint"].get("tiers"), list):
        return errs + ["head_footprint.tiers must be a list"]
    if val(p["printer"].get("kinematics")) != "cartesian":
        errs.append("printer.kinematics must be cartesian")
    for k in MOTION_KEYS:
        if k not in p["motion"]:
            errs.append("motion.%s missing" % k)
        elif not _is_num(val(p["motion"][k])):
            errs.append("motion.%s must be a finite number" % k)
    env = p["envelope"]
    for k in ENVELOPE_KEYS:
        if k not in env:
            errs.append("envelope.%s missing" % k)
    for k in ENVELOPE_KEYS:
        if k in env and k not in ("probe_area", "bed") and not _is_num(val(env[k])):
            errs.append("envelope.%s must be a finite number" % k)
    for rect_key in ("probe_area", "bed"):
        if rect_key in env:
            area = val(env[rect_key])
            if not isinstance(area, dict) or not all(_is_num(area.get(k)) for k in RECT_KEYS):
                errs.append("envelope.%s needs numeric x_min, x_max, y_min, y_max" % rect_key)
    for k in STOP_KEYS:
        if k not in p["stop_estimation"]:
            errs.append("stop_estimation.%s missing" % k)
    se = p["stop_estimation"]
    for k in ("margin_s", "snap_window_s", "max_overlap_s"):
        if k in se and not _is_num(se[k]):
            errs.append("stop_estimation.%s must be a finite number" % k)
    if "min_resume_layer" in se and not _is_int(se["min_resume_layer"]):
        errs.append("stop_estimation.min_resume_layer must be an integer")
    for k in THERMAL_KEYS:
        if k not in p["thermal"]:
            errs.append("thermal.%s missing" % k)
        elif not _is_num(val(p["thermal"][k])):
            errs.append("thermal.%s must be a finite number" % k)
    if not isinstance(p["thermal"].get("standby_temp", None), dict):
        errs.append("thermal.standby_temp must be an object")
    for k in CLEARANCE_KEYS:
        if k not in p["clearances"]:
            errs.append("clearances.%s missing" % k)
        elif not _is_num(val(p["clearances"][k])):
            errs.append("clearances.%s must be a finite number" % k)
    park = p["park"].get("xy")
    if not is_leaf(park) or not isinstance(park["value"], list) or \
            len(park["value"]) != 2 or not all(_is_num(v) for v in park["value"]):
        errs.append("park.xy needs a leaf with two numbers [x, y]")
    mmu = val(p["accessories"].get("mmu", {}))
    if not isinstance(mmu, dict) or not isinstance(mmu.get("multi_tool_resume", False), bool):
        errs.append("accessories.mmu must be an object, multi_tool_resume a boolean")
    for t in p["head_footprint"].get("tiers", []):
        v = val(t)
        if not isinstance(v, dict) or not all(_is_num(v.get(k)) for k in TIER_KEYS):
            errs.append("head_footprint tier %r needs numeric %s"
                        % (t.get("name") if isinstance(t, dict) else None, ", ".join(TIER_KEYS)))
    for z in p["no_go_zones"]:
        v = val(z)
        name = z.get("name") if isinstance(z, dict) else None
        if not isinstance(v, dict) or v.get("frame") not in FRAMES:
            errs.append("no_go_zone %r: frame must be one of %s" % (name, FRAMES))
            continue
        for axis in ("x", "y", "z"):
            rng = v.get(axis)
            if rng is None:
                continue
            if not (isinstance(rng, list) and len(rng) == 2 and
                    all(b is None or _is_num(b) for b in rng)):
                errs.append("no_go_zone %r: %s must be null or [low, high]" % (name, axis))
        m = v.get("margin")
        if m is not None and not (_is_num(m) and m >= 0):
            errs.append("no_go_zone %r: margin must be a number >= 0" % name)
        callers = v.get("allowed_callers", [])
        if not isinstance(callers, list) or not all(isinstance(c, str) for c in callers):
            errs.append("no_go_zone %r: allowed_callers must be a list of names" % name)
    for s in p["probe_spots"]:
        v = val(s)
        name = s.get("name") if isinstance(s, dict) else None
        if not isinstance(v, dict) or not _is_num(v.get("x")) or not _is_num(v.get("y")):
            errs.append("probe_spot %r: needs numeric x and y" % name)
    return errs


def _check_ranges(p: dict) -> List[str]:
    errs = []
    env = p["envelope"]
    x_min, x_max_safe, x_max = (val(env[k]) for k in ("x_min", "x_max_safe", "x_max"))
    if not x_min < x_max_safe <= x_max:
        errs.append("envelope: need x_min < x_max_safe <= x_max")
    if not val(env["y_min"]) < val(env["y_max"]):
        errs.append("envelope: need y_min < y_max")
    if val(env["z_max"]) <= 0:
        errs.append("envelope.z_max must be > 0")
    if val(env["z_top_margin"]) < 0:
        errs.append("envelope.z_top_margin must be >= 0")
    for rect_key in ("probe_area", "bed"):
        area = val(env[rect_key])
        if not (area["x_min"] < area["x_max"] and area["y_min"] < area["y_max"]):
            errs.append("envelope.%s: need min < max on both axes" % rect_key)
    for k, v in p["motion"].items():
        if k not in MOTION_KEYS:
            continue
        v = val(v)
        if k == "minimum_cruise_ratio":
            if not 0 <= v <= 1:
                errs.append("motion.minimum_cruise_ratio must be within 0..1")
        elif _is_num(v) and v <= 0:
            errs.append("motion.%s must be > 0" % k)
    for k in CLEARANCE_KEYS:
        if val(p["clearances"][k]) < 0:
            errs.append("clearances.%s must be >= 0" % k)
    for k in THERMAL_KEYS:
        if val(p["thermal"][k]) < 0:
            errs.append("thermal.%s must be >= 0" % k)
    se = p["stop_estimation"]
    for k in ("margin_s", "snap_window_s", "max_overlap_s"):
        if se[k] < 0:
            errs.append("stop_estimation.%s must be >= 0 (got %r)" % (k, se[k]))
    absurd = se.get("absurd_overlap_s")
    if absurd is not None and (not _is_num(absurd) or absurd < se["max_overlap_s"]):
        errs.append("stop_estimation.absurd_overlap_s must be a number >= max_overlap_s")
    if se["min_resume_layer"] < 0:
        errs.append("stop_estimation.min_resume_layer must be >= 0")
    for t in p["head_footprint"]["tiers"]:
        v = val(t)
        if v["z_from"] > v["z_to"]:
            errs.append("head_footprint tier %r: z_from above z_to" % t.get("name"))
        if v["x_min"] > v["x_max"] or v["y_min"] > v["y_max"]:
            errs.append("head_footprint tier %r: min above max" % t.get("name"))
    return errs


def validate(p: dict) -> List[str]:
    """Return a list of problems; empty means the profile is usable."""
    if not isinstance(p, dict):
        return ["profile is not a JSON object"]
    errs = _check_structure(p)
    if errs:
        return errs
    return _check_ranges(p)


def load_profile(path: str) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            p = json.load(fh)
    except RecursionError:
        raise ProfileError("profile is nested too deeply")
    errs = validate(p)
    if errs:
        raise ProfileError("; ".join(errs))
    return p


def motion_ready(p: dict) -> List[str]:
    """Safety-critical leaves not verified on this machine (R13 when any).

    A missing safety list (no measured probe spots) is reported by R02, not
    here."""
    out = []
    for path, leaf in _walk(p):
        reason = unverified_reason(leaf)
        if reason:
            out.append("%s (%s)" % (path, reason))
    return out


def park_xy(p: dict) -> Tuple[float, float]:
    x, y = val(p["park"]["xy"])
    return float(x), float(y)


def bed_mesh_profile(p: dict) -> str:
    return p["bed_mesh_profile"]


# -- printer.cfg -------------------------------------------------------------

def parse_printer_cfg(text: str) -> Dict[str, Dict[str, str]]:
    """Sections/options of printer.cfg, with the SAVE_CONFIG block applied on
    top (as Klipper does). [include] lines are not followed. Read-only."""
    sections: Dict[str, Dict[str, str]] = {}
    cur = None
    autosave = False
    last_key = None
    for raw in text.splitlines():
        line = raw
        if line.startswith("#*#"):
            autosave = True
            line = line[3:].strip()
        elif autosave:
            continue
        else:
            line = re.split(r"\s[#;]", " " + line, maxsplit=1)[0][1:]
            if line.lstrip().startswith(("#", ";")):
                continue
        if not line.strip():
            continue
        m = re.match(r"^\[([^\]]+)\]\s*$", line.strip())
        if m:
            cur = m.group(1).strip()
            sections.setdefault(cur, {})
            last_key = None
            continue
        if cur is None:
            continue
        if line[:1].isspace() and last_key:
            sections[cur][last_key] += "\n" + line.strip()
            continue
        m = re.match(r"^([A-Za-z0-9_]+)\s*[:=]\s*(.*)$", line.strip())
        if m:
            last_key = m.group(1).lower()
            sections[cur][last_key] = m.group(2).strip()
    return sections


def _cfg_value(cfg, section, option, kind):
    raw = cfg.get(section, {}).get(option)
    if raw is None:
        return None
    try:
        if kind == "list":
            return [float(x) for x in raw.split(",")]
        return float(raw)
    except ValueError:
        return raw


def apply_live_config(p: dict, cfg: Dict[str, Dict[str, str]]) -> List[str]:
    """Replace every fingerprint leaf marked "live" by the value in the live
    printer.cfg (in memory only; the profile file is never written). Used for
    values that drift, such as the CR Touch z_offset after a PROBE_CALIBRATE.
    Returns one note per replaced value; a value missing from the cfg keeps
    the profile copy and is reported as an R14 mismatch by the caller."""
    notes = []
    for (sec, opt), (csec, copt, kind) in FINGERPRINT_KEYS.items():
        leaf = p.get("config_fingerprint", {}).get(sec, {}).get(opt)
        if not (isinstance(leaf, dict) and leaf.get("live") is True):
            continue
        have = _cfg_value(cfg, csec, copt, kind)
        if have is None:
            continue
        if leaf.get("value") != have:
            notes.append("%s.%s: live printer.cfg %s replaces the profile copy %s"
                         % (sec, opt, have, leaf.get("value")))
        leaf["value"] = have
    return notes


def fingerprint_mismatches(p: dict, cfg: Dict[str, Dict[str, str]],
                           tol: float = 1e-6) -> List[str]:
    """Differences between config_fingerprint and parsed printer.cfg (R14)."""
    out = []
    fp = p.get("config_fingerprint", {})
    for (sec, opt), (csec, copt, kind) in FINGERPRINT_KEYS.items():
        if sec not in fp or opt not in fp[sec]:
            out.append("%s.%s: not in the profile fingerprint (not checked)" % (sec, opt))
            continue
        if isinstance(fp[sec][opt], dict) and fp[sec][opt].get("live") is True:
            # Taken from the live printer.cfg (apply_live_config), so there is
            # nothing to compare; only a missing value is a problem.
            if _cfg_value(cfg, csec, copt, kind) is None:
                out.append("%s.%s: missing in printer.cfg (live value needed)" % (sec, opt))
            continue
        want = val(fp[sec][opt])
        have = _cfg_value(cfg, csec, copt, kind)
        if have is None:
            out.append("%s.%s: missing in printer.cfg (profile %s)" % (sec, opt, want))
            continue
        if isinstance(want, list) or isinstance(have, list):
            ok = (isinstance(want, list) and isinstance(have, list) and
                  len(want) == len(have) and
                  all(abs(float(a) - float(b)) <= tol for a, b in zip(want, have)))
        else:
            try:
                ok = abs(float(want) - float(have)) <= tol
            except (TypeError, ValueError):
                ok = want == have
        if not ok:
            out.append("%s.%s: profile %s, printer.cfg %s" % (sec, opt, want, have))
    return out


def probe_offsets(p: dict) -> Tuple[float, float, float]:
    fp = p["config_fingerprint"]["bltouch"]
    return val(fp["x_offset"]), val(fp["y_offset"]), val(fp["z_offset"])


def section_values(p: dict, name: str) -> Dict[str, object]:
    return {k: val(v) for k, v in p.get(name, {}).items()}


def read_text(path: str) -> Optional[str]:
    with open(path, "r", encoding="utf-8") as fh:
        return fh.read()
