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
SCRIPTS = [
    "CUT_COUNT_STATUS",
    "PRINTER_SHUTDOWN",
    "CANCEL_PRINTER_SHUTDOWN",
]


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
    return 0


if __name__ == "__main__":
    sys.exit(main())
