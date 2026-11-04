#!/usr/bin/env python3
# Run state-machine macros that do not move or heat anything, through the Klippy
# API, after the printer reports ready. Every script must return without error.
#
# Usage: smoke.py <api-socket>
# Exit status 0 when every macro runs cleanly, 1 otherwise.
#
# check_ready.py must sit next to this file: call() comes from it.

import sys

from check_ready import call

# Each entry runs in order. The shutdown pair checks that the delayed power-off
# can be scheduled and cancelled; the countdown itself is far longer than a run.
# The run ends within a few seconds of ready, well inside the 30 s window before
# _PRINTER_SHUTDOWN_RESUME fires, so the resume body never runs in the sim.
SCRIPTS = [
    "CUT_COUNT_STATUS",
    "PRINTER_SHUTDOWN",
    "CANCEL_PRINTER_SHUTDOWN",
]

# Pending save-and-power-off flag (see config/macros/PRINTER_SHUTDOWN.cfg). It is
# set by hand here, never through SAVE_CONFIG, which would restart Klippy.
SHUTDOWN_FLAG = "shutdown_after_save"


def shutdown_flag(path):
    objects = call(path, "objects/query", {"objects": {"save_variables": None}})
    return objects["status"]["save_variables"]["variables"].get(SHUTDOWN_FLAG, 0)


def check_shutdown_flag(path):
    # Disarm the resume timer first so a slow start cannot clear the flag between
    # the write and the read below.
    call(path, "gcode/script",
         {"script": "UPDATE_DELAYED_GCODE ID=_PRINTER_SHUTDOWN_RESUME DURATION=0"})
    call(path, "gcode/script",
         {"script": "SAVE_VARIABLE VARIABLE=%s VALUE=1" % SHUTDOWN_FLAG})
    if shutdown_flag(path) != 1:
        raise ValueError("SAVE_VARIABLE did not set %s" % SHUTDOWN_FLAG)
    call(path, "gcode/script", {"script": "CANCEL_PRINTER_SHUTDOWN"})
    if shutdown_flag(path) != 0:
        raise ValueError("CANCEL_PRINTER_SHUTDOWN did not clear %s" % SHUTDOWN_FLAG)


def main():
    path = sys.argv[1]
    # save_variables backs the MMU state; a missing section means the
    # [save_variables] file path was not created or read.
    objects = call(path, "objects/query", {"objects": {"save_variables": None}})
    if "save_variables" not in objects.get("status", {}):
        print("save_variables object is not loaded", flush=True)
        return 1
    for script in SCRIPTS:
        try:
            call(path, "gcode/script", {"script": script})
        except (ValueError, OSError) as e:
            print("macro %s failed: %s" % (script, e), flush=True)
            return 1
        print("macro %s ok" % script, flush=True)
    try:
        check_shutdown_flag(path)
    except (ValueError, OSError) as e:
        print("shutdown flag check failed: %s" % e, flush=True)
        return 1
    print("shutdown flag check ok", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
