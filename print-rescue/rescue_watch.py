"""print-rescue: watch a (resumed) print through Moonraker and capture evidence at the first failure. No AI needed.

    python3 print-rescue/rescue_watch.py --host <moonraker-host> --out <dir> [--ssh <alias>] [--kernel-stream <file>]

Polls every 10 s and writes <dir>/poll.csv (time,state,klippy,progress,file_pos,z,hotend,bed). Exit codes: 0 print complete,
2 Klippy not ready / print paused or errored / file position frozen, 3 cancelled, 4 Moonraker unreachable 4 times,
5 the host rebooted (system_uptime went down: a mains power cut looks like this). On any non-zero exit it writes FAILURE.txt and, with
--ssh <alias> (a plain Host entry of ~/.ssh/config, key auth), the last 15 min of the kernel log and the Klippy container log.
The file position legitimately stays still while START_PRINT heats: --freeze-s defaults to 600 s. A new `urb stopped` line in
--kernel-stream is counted in events.txt without stopping. Standard library only.
"""

import argparse
import json
import pathlib
import subprocess
import sys
import time
import urllib.request

QUERY = "print_stats&virtual_sdcard&toolhead&extruder&heater_bed"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--ssh", help="ssh alias for evidence capture (optional)")
    ap.add_argument("--kernel-stream", help="file that `ssh <alias> journalctl -k -f` appends to (optional)")
    ap.add_argument("--freeze-s", type=int, default=600)
    ap.add_argument("--interval", type=int, default=10)
    a = ap.parse_args(argv)
    out = pathlib.Path(a.out)
    out.mkdir(parents=True, exist_ok=True)

    def get(path, t=6):
        return json.load(urllib.request.urlopen("http://%s%s" % (a.host, path), timeout=t))["result"]

    def ssh(cmd):
        if not a.ssh:
            return ""
        return subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=6", "--", a.ssh, cmd],
                              capture_output=True, text=True, timeout=60).stdout

    def n32():
        try:
            return sum(1 for l in open(a.kernel_stream, errors="ignore") if "urb stopped" in l)
        except (OSError, TypeError):
            return 0

    def capture(why):
        (out / "FAILURE.txt").write_text("%s: %s\n" % (time.ctime(), why))
        if a.ssh:
            (out / "kernel-at-failure.log").write_text(ssh("journalctl -k --no-pager --since '-15min'; vcgencmd get_throttled; uptime"))
            (out / "klippy-tail-at-failure.log").write_text(ssh("docker logs --tail 3000 klipper 2>&1 | grep -v analog_in_state"))
        print("FAILURE:", why, flush=True)

    log = (out / "poll.csv").open("a")
    log.write("time,state,klippy,progress,file_pos,z,hotend,bed\n")
    last_pos, last_move, last_up, misses, last32 = None, time.time(), None, 0, n32()
    while True:
        try:
            up = get("/machine/proc_stats").get("system_uptime")
            if last_up is not None and up is not None and up < last_up - 5:
                capture("host rebooted (uptime %.0f -> %.0f): mains power cut or crash" % (last_up, up))
                return 5
            last_up = up
            c = n32()
            if c != last32:
                print("NOTE new urb -32 events:", c - last32, flush=True)
                (out / "events.txt").open("a").write("%s -32 count %d\n" % (time.strftime("%H:%M:%S"), c))
                last32 = c
            info = get("/printer/info")
            s = get("/printer/objects/query?" + QUERY)["status"]
            misses = 0
            ps, vs, th = s["print_stats"], s["virtual_sdcard"], s["toolhead"]
            row = [time.strftime("%H:%M:%S"), ps["state"], info["state"], "%.1f" % (vs["progress"] * 100), vs["file_position"],
                   round(th["position"][2], 2), round(s["extruder"]["temperature"], 1), round(s["heater_bed"]["temperature"], 1)]
            log.write(",".join(map(str, row)) + "\n")
            log.flush()
            print(*row, flush=True)
            if info["state"] != "ready":
                capture("Klippy state %s" % info["state"])
                return 2
            if ps["state"] in ("error", "paused"):
                capture("print_stats %s" % ps["state"])
                return 2
            if ps["state"] == "complete":
                print("DONE", flush=True)
                return 0
            if ps["state"] == "cancelled":
                capture("cancelled")
                return 3
            if ps["state"] == "printing":
                if vs["file_position"] != last_pos:
                    last_pos, last_move = vs["file_position"], time.time()
                elif time.time() - last_move > a.freeze_s:
                    capture("file position frozen %d s" % a.freeze_s)
                    return 2
        except Exception as e:
            misses += 1
            print("poll error", repr(e)[:80], flush=True)
            if misses >= 4:
                capture("Moonraker unreachable x%d" % misses)
                return 4
        time.sleep(a.interval)


if __name__ == "__main__":
    sys.exit(main())
