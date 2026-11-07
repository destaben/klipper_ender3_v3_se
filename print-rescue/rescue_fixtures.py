"""Shared fixture paths and a cached scan of the 2026-11-07 incident excerpt."""

import copy
import json
import os
from functools import lru_cache

import gcode_scan as gs
import planner
import machine_profile as prof
import stop_point as sp

HERE = os.path.dirname(os.path.abspath(__file__))
FIX = os.path.join(HERE, "fixtures")
GCODE = os.path.join(FIX, "gcode-excerpt.gcode.gz")
GMAP = os.path.join(FIX, "gcode-excerpt.map.json")
KLIPPY = os.path.join(FIX, "klippy-excerpt.log")
KERNEL = os.path.join(FIX, "kernel-usb.log")
HISTORY = os.path.join(FIX, "history-job.json")
PROFILE = os.path.join(HERE, "profiles", "creality-ender3-v3-se.json")
REPO_PRINTER_CFG = os.path.join(HERE, "..", "config", "printer.cfg")

# Known facts of the incident (see docs/print-rescue-incident-2026-11-07.md).
EXIT_POSITION = 5404182
LAST_LINE = 202396
CUT_LAYER_START = 201325
SD_LINE_GOOD = 202133          # sd_pos 5398158 at the last good sample
SD_LINE_FIRST_FROZEN = 202238  # sd_pos 5400474 one second later
MANUAL_RESUME_LINE = 202282


def records():
    return gs.iter_excerpt(GCODE, GMAP)


def profile():
    return prof.load_profile(PROFILE)


def limits():
    import plan
    return plan.limits_from_profile(profile())


@lru_cache(maxsize=1)
def _incident():
    lim = limits()
    with open(KLIPPY, "r", encoding="utf-8") as fh:
        ev = sp.analyze(fh.read().splitlines())
    res = gs.scan(records(), lim, until_byte=ev.exit_position)
    timed = planner.plan(res.moves, lim)
    return ev, res, timed


def incident():
    """(evidence, scan result, timed moves); deep copies, safe to mutate."""
    return copy.deepcopy(_incident())


def measured_profile(**changes):
    """The real profile with every safety leaf marked measured, plus a wide
    probe area and one measured rear probe spot. For tests only."""
    p = profile()
    for _, leaf in prof._walk(p):
        if leaf.get("safety_critical"):
            leaf["source"] = "measured"
            leaf["measured_on"] = "2026-11-07"
            leaf["method"] = "test fixture"
    p["envelope"]["probe_area"]["value"] = {"x_min": 0, "x_max": 230,
                                            "y_min": 0, "y_max": 230}
    p["probe_spots"] = [{"name": "rear left", "unit": "mm", "source": "measured",
                         "safety_critical": True, "measured_on": "2026-11-07",
                         "method": "test fixture", "value": {"x": 60, "y": 225}}]
    for k, v in changes.items():
        p[k] = v
    return json.loads(json.dumps(p))
