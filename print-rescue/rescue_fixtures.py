"""Shared fixture paths and a cached scan of the 2026-11-07 incident excerpt."""

import copy
import json
import os
from functools import lru_cache

import gcode_scan as gs
import planner
import machine_profile as prof
import stop_point as sp

# The fixture excerpts need --gcode-map, which is otherwise refused.
os.environ["PRINT_RESCUE_ALLOW_GCODE_MAP"] = "1"

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


# The placeholder head boxes of the first profile version. The incident-1
# tests were written against them (a rear spot that clears a part spanning
# Y 29.8-188.4 cannot exist with the owner's larger head estimate).
OLD_TIERS = [
    {"name": "nozzle tip", "unit": "mm", "source": "default", "safety_critical": True,
     "value": {"z_from": 0, "z_to": 2, "x_min": -3, "x_max": 3, "y_min": -3, "y_max": 3}},
    {"name": "heater block and fan duct", "unit": "mm", "source": "default",
     "safety_critical": True,
     "value": {"z_from": 2, "z_to": 15, "x_min": -25, "x_max": 25, "y_min": -20, "y_max": 20}},
    {"name": "probe body", "unit": "mm", "source": "default", "safety_critical": True,
     "value": {"z_from": 4, "z_to": 30, "x_min": -30, "x_max": -16, "y_min": -21, "y_max": -8}},
    {"name": "probe pin deployed", "unit": "mm", "source": "default", "safety_critical": True,
     "value": {"z_from": -5, "z_to": 4, "x_min": -25, "x_max": -21, "y_min": -16.5, "y_max": -12.5}},
]


def measured_profile(owner_footprint=False, live_z_offset=3.035, **changes):
    """The real profile with every safety leaf marked measured, plus a wide
    probe area and one measured rear probe spot. For tests only. The head
    footprint is the old placeholder set unless owner_footprint is True."""
    p = profile()
    if not owner_footprint:
        p["head_footprint"]["tiers"] = copy.deepcopy(OLD_TIERS)
    for _, leaf in prof._walk(p):
        if leaf.get("safety_critical"):
            leaf["source"] = "measured"
            leaf["measured_on"] = "2026-11-07"
            leaf["method"] = "test fixture"
    # The live z_offset of the test printer.cfg (3.035 for incident 1, 2.96 for
    # incident 3): a measured value that differs from the live one stops
    # counting as measured (apply_live_config).
    p["config_fingerprint"]["bltouch"]["z_offset"]["value"] = live_z_offset
    p["envelope"]["probe_area"]["value"] = {"x_min": 0, "x_max": 230,
                                            "y_min": 0, "y_max": 230}
    p["envelope"]["bed"]["value"] = {"x_min": 0, "x_max": 230, "y_min": 0, "y_max": 230}
    p["probe_spots"] = [{"name": "rear left", "unit": "mm", "source": "measured",
                         "safety_critical": True, "measured_on": "2026-11-07",
                         "method": "test fixture", "value": {"x": 60, "y": 225}}]
    for k, v in changes.items():
        p[k] = v
    return json.loads(json.dumps(p))


# Incident 3 (2026-11-07 18:59, vase, 53.0 mm tall at the stop). The excerpt
# files are scrubbed like the incident-1 ones, see fixtures/SOURCES.md.
INC3_GCODE = os.path.join(FIX, "inc3-gcode-excerpt.gcode.gz")
INC3_GMAP = os.path.join(FIX, "inc3-gcode-excerpt.map.json")
INC3_KLIPPY = os.path.join(FIX, "inc3-klippy-excerpt.log")
INC3_KERNEL = os.path.join(FIX, "inc3-kernel-usb.log")
INC3_HISTORY = os.path.join(FIX, "inc3-history-job.json")
INC3_QUEUE = os.path.join(FIX, "inc3-queue.json")
INC3_PRINTER_CFG = os.path.join(FIX, "inc3-printer-cfg.cfg")
INC3_EXIT_POSITION = 8772080
INC3_LAST_LINE = 391419
INC3_RESUME_LINE = 390881
