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
from dataclasses import dataclass, field
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
    warnings: List[str] = field(default_factory=list)
    # Distance (mm) the probe point lies outside the bed-mesh area; 0 inside.
    mesh_extrapolation_mm: float = 0.0
    # Smallest distance (mm) between the head footprint at probe height and
    # the part box; None when no tier reaches below the part top.
    margin_mm: Optional[float] = None


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


def rect_distance(a: Rect, b: Rect) -> float:
    """Gap between two rectangles (0 when they touch or overlap)."""
    dx = max(a[0] - b[1], b[0] - a[1], 0.0)
    dy = max(a[2] - b[3], b[2] - a[3], 0.0)
    return math.hypot(dx, dy)


def _area_rect(leaf) -> Optional[Rect]:
    area = val(leaf) if leaf is not None else None
    if not area:
        return None
    return (area["x_min"], area["x_max"], area["y_min"], area["y_max"])


def check_probe_spot(spot: dict, part: Part, profile: dict) -> Verdict:
    """Is it safe to probe with the NOZZLE at spot {x, y}?

    Hard reasons: nozzle outside the axis envelope or beyond x_max_safe, in a
    no-go zone, the probe point off the bed, too close to the part, or any
    head tier at probe height overlapping the part. Being outside the bed
    MESH area is only a warning (the mesh is extrapolated there; the probe
    itself works anywhere on the bed), see Verdict.mesh_extrapolation_mm."""
    reasons, warnings = [], []
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
    bed = _area_rect(profile["envelope"].get("bed"))
    if bed is None:
        # N2: without a bed rectangle the check cannot pass.
        reasons.append("the profile has no bed area, so the probe point cannot be checked")
    elif not point_in_rect(px, py, bed):
        reasons.append("probe point (%.1f,%.1f) is off the bed" % (px, py))
    mesh = _area_rect(profile["envelope"].get("probe_area"))
    extrapolation = 0.0
    if mesh is not None and not point_in_rect(px, py, mesh):
        extrapolation = point_rect_distance(px, py, mesh)
        warnings.append("probe point (%.1f,%.1f) is %.1f mm outside the bed mesh area, "
                        "so the mesh is extrapolated there" % (px, py, extrapolation))
    d = point_rect_distance(px, py, part.rect())
    if d < cl["probe_spot_margin"]:
        reasons.append("probe point (%.1f,%.1f) %.1f mm from the part < %.1f"
                       % (px, py, d, cl["probe_spot_margin"]))
    # Head footprint with the nozzle at probe trigger height (about z_offset).
    inflated = part.rect(cl["footprint_margin"])
    margin = None
    for tier in profile["head_footprint"]["tiers"]:
        t = val(tier)
        if zo + t["z_from"] >= part.top:
            continue
        r = (x + t["x_min"], x + t["x_max"], y + t["y_min"], y + t["y_max"])
        gap = rect_distance(r, part.rect())
        margin = gap if margin is None else min(margin, gap)
        if rects_overlap(r, inflated):
            reasons.append("head tier %r (z %.1f-%.1f above the nozzle) overlaps "
                           "the part + %.0f mm at probe height"
                           % (tier.get("name"), t["z_from"], t["z_to"],
                              cl["footprint_margin"]))
    return Verdict(not reasons, reasons, warnings, extrapolation, margin)


MARGIN_CAP_MM = 15.0


def suggest_probe_spots(part: Part, profile: dict, step: float = 5.0,
                        limit: int = 5) -> List[Tuple[dict, Verdict]]:
    """SEARCH mode: nozzle positions on a grid (multiples of `step` inside the
    axis envelope) that pass check_probe_spot for this part, best first.

    Rank: footprint margin to the part (capped at MARGIN_CAP_MM, more is not
    better once it is comfortable), then the smaller mesh extrapolation, then
    the closer to the part's X centre. Suggestions are advice only: the
    operator still confirms a spot, and R13 still blocks motion while any
    safety tier is not measured."""
    env = {k: val(v) for k, v in profile["envelope"].items()}
    out = []
    x = math.ceil(env["x_min"] / step) * step
    while x <= env["x_max_safe"] + 1e-9:
        y = math.ceil(env["y_min"] / step) * step
        while y <= env["y_max"] + 1e-9:
            v = check_probe_spot({"x": x, "y": y}, part, profile)
            if v.ok:
                out.append(({"x": x, "y": y}, v))
            y += step
        x += step
    cx = (part.x_min + part.x_max) / 2.0

    def key(item):
        spot, v = item
        m = MARGIN_CAP_MM if v.margin_mm is None else min(v.margin_mm, MARGIN_CAP_MM)
        return (-m, v.mesh_extrapolation_mm, abs(spot["x"] - cx), spot["y"])

    out.sort(key=key)
    return out[:limit]


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
