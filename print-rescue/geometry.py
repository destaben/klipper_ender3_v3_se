"""Geometry checks: part box, no-go zones, probe spots, probe pin at boot.

All coordinates are nozzle coordinates in the printer's frame (what G1 X Y
moves to), in mm. A part is described by its extrusion bounding box and top
height (gcode_scan.Bbox). Rectangles are (x_min, x_max, y_min, y_max).

Frames of a no-go zone (profile no_go_zones[].value.frame):
  gantry, frame  fixed to the X gantry / printer frame. On a bed slinger the
                 bed moves in Y under them, so only X (and Z) are checked.
  bed            fixed to the bed plate: X and Y (and Z) are checked.
Missing ranges mean unbounded.

Gantry tilt (one side of the X gantry lower after a Z motor lost steps) is a
runtime check of a later phase (P4, two-point probe); nothing here models it.

Pure logic, standard library only.
"""

import math
from dataclasses import dataclass
from typing import Iterable, List, Optional, Sequence, Tuple

from machine_profile import probe_offsets, val

Rect = Tuple[float, float, float, float]


@dataclass
class Part:
    x_min: float
    x_max: float
    y_min: float
    y_max: float
    top: float

    @classmethod
    def from_bbox(cls, bbox, top: Optional[float] = None):
        return cls(bbox.x_min, bbox.x_max, bbox.y_min, bbox.y_max,
                   bbox.z_max if top is None else top)

    def rect(self, inflate: float = 0.0) -> Rect:
        return (self.x_min - inflate, self.x_max + inflate,
                self.y_min - inflate, self.y_max + inflate)


@dataclass
class Verdict:
    ok: bool
    reasons: List[str]


def rects_overlap(a: Rect, b: Rect) -> bool:
    return a[0] < b[1] and b[0] < a[1] and a[2] < b[3] and b[2] < a[3]


def point_in_rect(x: float, y: float, r: Rect) -> bool:
    return r[0] <= x <= r[1] and r[2] <= y <= r[3]


def point_rect_distance(x: float, y: float, r: Rect) -> float:
    dx = max(r[0] - x, 0.0, x - r[1])
    dy = max(r[2] - y, 0.0, y - r[3])
    return math.hypot(dx, dy)


def _in_range(v: Optional[float], rng, margin: float) -> bool:
    if v is None or rng is None:
        return True
    lo, hi = rng
    lo = -math.inf if lo is None else lo - margin
    hi = math.inf if hi is None else hi + margin
    return lo <= v <= hi


def point_in_zone(x: float, y: Optional[float], z: Optional[float], zone: dict,
                  extra_margin: float = 0.0) -> bool:
    """zone is the leaf value {frame, x, y, z, margin, allowed_callers}."""
    m = (zone.get("margin") or 0.0) + extra_margin
    if not _in_range(x, zone.get("x"), m):
        return False
    if zone.get("frame") == "bed" and not _in_range(y, zone.get("y"), m):
        return False
    return _in_range(z, zone.get("z"), m)


def zones_hit(profile: dict, x: float, y: Optional[float] = None,
              z: Optional[float] = None, caller: Optional[str] = None,
              extra_margin: float = 0.0) -> List[str]:
    """Names of no-go zones containing the point, unless caller is allowed."""
    out = []
    for leaf in profile.get("no_go_zones", []):
        zone = val(leaf)
        if caller and caller in (zone.get("allowed_callers") or []):
            continue
        if point_in_zone(x, y, z, zone, extra_margin):
            out.append(leaf.get("name", "zone"))
    return out


def pass1_height(part: Part, profile: dict) -> float:
    """Safe travel height over the part: top + travel_clearance, plus whatever
    hangs below the nozzle tip (a footprint tier with negative z_from)."""
    clear = val(profile["clearances"]["travel_clearance"])
    lowest = min([0.0] + [val(t)["z_from"] for t in profile["head_footprint"]["tiers"]])
    return part.top + clear - lowest


def too_tall(part: Part, profile: dict) -> Optional[str]:
    """R01: the head cannot lift min_lift above the part under the top limit."""
    env = profile["envelope"]
    z_max, margin = val(env["z_max"]), val(env["z_top_margin"])
    lift = val(profile["clearances"]["min_lift"])
    if part.top + lift > z_max - margin:
        return ("top %.1f + lift %.0f > z_max %.0f - margin %.0f"
                % (part.top, lift, z_max, margin))
    return None


def check_probe_spot(spot: dict, part: Part, profile: dict) -> Verdict:
    """Is it safe to probe with the NOZZLE at spot {x, y}?"""
    reasons = []
    x, y = float(spot["x"]), float(spot["y"])
    env = {k: val(v) for k, v in profile["envelope"].items()}
    cl = {k: val(v) for k, v in profile["clearances"].items()}
    xo, yo, zo = probe_offsets(profile)
    if not (env["x_min"] <= x <= env["x_max"] and env["y_min"] <= y <= env["y_max"]):
        reasons.append("nozzle (%.1f,%.1f) outside the axis envelope" % (x, y))
    if x > env["x_max_safe"]:
        reasons.append("nozzle X %.1f > x_max_safe %.1f" % (x, env["x_max_safe"]))
    hit = zones_hit(profile, x, y)
    if hit:
        reasons.append("nozzle in no-go zone: %s" % ", ".join(hit))
    px, py = x + xo, y + yo
    area = env.get("probe_area")
    if not area:
        # N2: without a probe area the check cannot pass, so the spot is refused.
        reasons.append("the profile has no probe area, so the probe point cannot be checked")
    elif not point_in_rect(px, py, (area["x_min"], area["x_max"],
                                    area["y_min"], area["y_max"])):
        reasons.append("probe point (%.1f,%.1f) outside the probe area" % (px, py))
    d = point_rect_distance(px, py, part.rect())
    if d < cl["probe_spot_margin"]:
        reasons.append("probe point (%.1f,%.1f) %.1f mm from the part < %.1f"
                       % (px, py, d, cl["probe_spot_margin"]))
    # Head footprint with the nozzle at probe trigger height (about z_offset).
    inflated = part.rect(cl["footprint_margin"])
    for tier in profile["head_footprint"]["tiers"]:
        t = val(tier)
        if zo + t["z_from"] >= part.top:
            continue
        r = (x + t["x_min"], x + t["x_max"], y + t["y_min"], y + t["y_max"])
        if rects_overlap(r, inflated):
            reasons.append("head tier %r (z %.1f-%.1f above the nozzle) overlaps "
                           "the part + %.0f mm at probe height"
                           % (tier.get("name"), t["z_from"], t["z_to"],
                              cl["footprint_margin"]))
    return Verdict(not reasons, reasons)


def check_pin_at_boot(nozzle_xy: Sequence[Tuple[float, float]], part: Part,
                      profile: dict) -> Verdict:
    """The CR Touch self-test at Klippy start deploys the pin at nozzle +
    offset. If any estimated nozzle position puts the pin over the part, a
    FIRMWARE_RESTART can drive the pin into the print."""
    xo, yo, _ = probe_offsets(profile)
    reasons = []
    pins = [(x + xo, y + yo) for x, y in nozzle_xy]
    if not pins:
        return Verdict(True, [])
    r = part.rect(val(profile["clearances"]["probe_spot_margin"]))
    over = [p for p in pins if point_in_rect(p[0], p[1], r)]
    if over:
        xs = [p[0] for p in pins]
        ys = [p[1] for p in pins]
        reasons.append("probe pin at boot (%.0f-%.0f, %.0f-%.0f) is over the part "
                       "(%.1f-%.1f, %.1f-%.1f, top %.2f)"
                       % (min(xs), max(xs), min(ys), max(ys), part.x_min,
                          part.x_max, part.y_min, part.y_max, part.top))
    return Verdict(not over, reasons)


def path_near_zones(profile: dict, points: Iterable[Tuple[float, float]],
                    margin: float) -> List[str]:
    """No-go zones within `margin` of any of the points (R03)."""
    out = []
    for x, y in points:
        for name in zones_hit(profile, x, y, extra_margin=margin):
            if name not in out:
                out.append(name)
    return out
