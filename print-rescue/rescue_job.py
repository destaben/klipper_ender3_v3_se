"""print-rescue: build the data for ONE rescue run from the files of a stopped print.

Inputs: the G-code that was printing, the Klippy log of the stop, and (optionally) the watcher's poll CSV for the file rate.
Outputs: job.json (stop state, resume line, park point, part envelope, temperatures) and the resume G-code file.
Nothing here talks to the printer. Standard library only; no personal data.

What the Klippy log gives EXACTLY at a "Lost communication" shutdown (found live on 2026-11-08):
  "gcode state: ... last_position=[x, y, z, e]"       the host's position when it stopped
  "Virtual sdcard (N): '...'" / "Upcoming (M): '...'"  the file text around the stop
  "Exiting SD card print (position P)"                 the byte offset just after the last dispatched line
The MCU keeps running its queue after the link dies, so the physical stop is at or before that point by at most Klipper's
lookahead (about 2 s of print time). Converting bytes to seconds is NOT exact (the file rate varied 516-5317 B/s on one
vase), so the overlap is a bounded safety margin chosen in bytes at the measured rate, not an estimate of the stop.
A power cut writes no such lines: the log just ends; use --exit-pos from the last Moonraker poll (see the runbook).
"""

import argparse
import csv
import json
import math
import re
import statistics
import sys
from typing import Dict, List, Optional, Tuple

MARKER = "; --- ORIGINAL G-CODE FROM LINE "
EXIT_RE = re.compile(r"Exiting SD card print \(position (\d+)\)")
LASTPOS_RE = re.compile(r"gcode state:.*?last_position=\[([-\d.]+), ([-\d.]+), ([-\d.]+), ([-\d.]+)\]")
NUM = r"-?\d*\.?\d+"


class JobError(ValueError):
    pass


def _read(path: str, mode: str = "r", **kw):
    with open(path, mode, **kw) as f:
        return f.read()


def parse_stop_state(log_text: str) -> Dict[str, object]:
    """Last exit position and last host position found in a Klippy log (docker stdout or klippy.log)."""
    exits = EXIT_RE.findall(log_text)
    poss = LASTPOS_RE.findall(log_text)
    out: Dict[str, object] = {"exit_pos": int(exits[-1]) if exits else None, "last_position": None}
    if poss:
        out["last_position"] = [float(v) for v in poss[-1][:3]]
    return out


def _xyz(line: str, cur: Tuple[float, float, float]) -> Tuple[Dict[str, float], Tuple[float, float, float]]:
    d = {k: float(v) for k, v in re.findall(r"([XYZEF])(" + NUM + ")", line)}
    return d, (d.get("X", cur[0]), d.get("Y", cur[1]), d.get("Z", cur[2]))


def part_envelope(lines: List[str], upto: Optional[int] = None) -> Dict[str, object]:
    """Bounding boxes of every extrusion (G1 with X/Y and E>0), per layer Z and overall. Absolute coordinates assumed (G90).

    This is the real footprint of the part: it replaces any hand-drawn box."""
    cur = (0.0, 0.0, 0.0)
    layers: Dict[float, List[float]] = {}
    n = len(lines) if upto is None else upto
    for ln in lines[:n]:
        if not ln.startswith(("G1 ", "G0 ")):
            continue
        d, cur = _xyz(ln, cur)
        if d.get("E", 0) > 0 and ("X" in d or "Y" in d):
            b = layers.setdefault(round(cur[2], 3), [1e9, -1e9, 1e9, -1e9])
            b[0], b[1] = min(b[0], cur[0]), max(b[1], cur[0])
            b[2], b[3] = min(b[2], cur[1]), max(b[3], cur[1])
    if not layers:
        raise JobError("no extrusion found in the G-code")
    allb = list(layers.values())
    return {
        "x0": min(b[0] for b in allb), "x1": max(b[1] for b in allb),
        "y0": min(b[2] for b in allb), "y1": max(b[3] for b in allb),
        "top": max(layers), "layers": layers,
    }


def file_rate(poll_csv: str, window_s: int = 100) -> float:
    """Bytes of file per second over the last window_s seconds of printing in the watcher CSV (time,state,klippy,progress,file_pos,...)."""
    with open(poll_csv) as fh:
        rows = [r for r in csv.DictReader(fh) if r["state"] == "printing"]
    def secs(s: str) -> int:
        h, m, x = (int(v) for v in s.split(":"))
        return h * 3600 + m * 60 + x
    if len(rows) < 3:
        raise JobError("not enough poll rows")
    end = secs(rows[-1]["time"])
    win = [r for r in rows if end - secs(r["time"]) <= window_s]
    dt = secs(win[-1]["time"]) - secs(win[0]["time"])
    if dt <= 0:
        raise JobError("poll window has no duration")
    return (int(win[-1]["file_pos"]) - int(win[0]["file_pos"])) / dt


def line_of_offset(data: bytes, offset: int) -> int:
    """1-based index of the line that ENDS at byte offset (offset must be a line boundary); raises otherwise."""
    if offset < 1 or offset > len(data) or data[offset - 1:offset] != b"\n":
        raise JobError("exit position %d is not a line boundary in this file: it is not the file that was printing" % offset)
    return data.count(b"\n", 0, offset)


def to_original_numbers(lines: List[str]) -> List[int]:
    """Map each line of a file to the line number in the ORIGINAL G-code. For a plain G-code that is the identity; for a
    resume file (marker line + inserted SET_PRINT_STATS_INFO CURRENT_LAYER lines) it follows the marker."""
    mk = next((i for i, l in enumerate(lines) if l.startswith(MARKER)), None)
    if mk is None:
        return list(range(1, len(lines) + 1))
    start = int(lines[mk][len(MARKER):].split()[0])
    nums = [0] * len(lines)
    n = start - 1
    for i in range(mk + 1, len(lines)):
        if not lines[i].startswith("SET_PRINT_STATS_INFO CURRENT_LAYER"):
            n += 1
        nums[i] = n
    return nums


def pick_resume_line(lines: List[str], exit_idx: int, overlap_bytes: int) -> int:
    """Index (0-based, in `lines`) of the resume line: walk back overlap_bytes from the end of line exit_idx, then to the
    nearest earlier plain extrusion move 'G1 X.. Y.. E<positive>', so the nozzle restarts mid-bead on printed material."""
    offs, o = [], 0
    for l in lines:
        offs.append(o)
        o += len(l.encode("utf-8", "surrogateescape")) + 1
    target = offs[exit_idx] + len(lines[exit_idx].encode("utf-8", "surrogateescape")) + 1 - overlap_bytes
    i = max(j for j in range(len(lines)) if offs[j] <= target)
    # POSITIVE E only: a wipe is 'G1 X.. Y.. E-.1' and must never be chosen
    while i > 0 and not re.match(r"^G1 X" + NUM + r" Y" + NUM + r" E\d*\.?\d+\s*$", lines[i]):
        i -= 1
    if i == 0:
        raise JobError("no plain extrusion move before the stop")
    return i


def park_point(lines: List[str], i: int) -> Tuple[float, float]:
    """XY where the nozzle must be before line i runs: the end point of the previous G1 with X and Y."""
    j = i - 1
    while j >= 0 and not re.match(r"^G1 X" + NUM + r" Y" + NUM, lines[j]):
        j -= 1
    if j < 0:
        raise JobError("no earlier XY move")
    m = re.match(r"^G1 X(" + NUM + r") Y(" + NUM + ")", lines[j])
    return float(m.group(1)), float(m.group(2))


def layer_state(lines: List[str], i: int) -> Dict[str, object]:
    changes = [k for k in range(i) if lines[k] == ";LAYER_CHANGE"]
    zs = [lines[k] for k in range(i) if lines[k].startswith(";Z:")]
    total = next((int(m.group(1)) for l in lines[:2000] if (m := re.match(r"^; total layer number: (\d+)", l))), None)
    return {"layer_index": len(changes), "layer_z": float(zs[-1][3:]) if zs else None, "total_layers": total}


def header_end(lines: List[str]) -> int:
    h = 0
    while h < len(lines) and (lines[h].startswith(";") or not lines[h].strip()):
        h += 1
    return h


def temps_before(lines: List[str], i: int) -> Tuple[int, int]:
    bed = hot = None
    for l in lines[:i]:
        m = re.match(r"^M1[04]0 S(\d+)|^M104 S(\d+)|^M140 S(\d+)", l)
        if l.startswith(("M104 S", "M109 S")) and re.search(r"S(\d+)", l) and int(re.search(r"S(\d+)", l).group(1)) > 0:
            hot = int(re.search(r"S(\d+)", l).group(1))
        if l.startswith(("M140 S", "M190 S")) and re.search(r"S(\d+)", l) and int(re.search(r"S(\d+)", l).group(1)) > 0:
            bed = int(re.search(r"S(\d+)", l).group(1))
    if bed is None or hot is None:
        raise JobError("no bed/nozzle temperature found before the resume line")
    return bed, hot


def build_resume(lines: List[str], i: int, bed: int, hot: int) -> List[str]:
    """Resume file = slicer header + state-only preamble + original lines from i. NO motion, heating, mesh or probe in the
    preamble (temperatures are the file's own, not START_PRINT's first-layer values)."""
    h = header_end(lines)
    pre = lines[h:i]

    def last(pat: str) -> Optional[str]:
        for t in reversed(pre):
            if re.match(pat, t):
                return t
        return None

    f = None
    for t in reversed(pre):
        if t.startswith("G1") and re.search(r"\bF(\d+)", t):
            f = re.search(r"\bF(\d+)", t).group(1)
            break
    ex = last(r"EXCLUDE_OBJECT_(START|END)")
    st = ["CANCEL_PRINTER_SHUTDOWN", "M117", "G90", "G21", "M83"]
    st += [t for t in pre if t.startswith("EXCLUDE_OBJECT_DEFINE")]
    st += [t for t in (last(r"SET_PRESSURE_ADVANCE"), last(r"SET_VELOCITY_LIMIT"), last(r"M900"), last(r"M204"), last(r"M10[67]\b")) if t]
    st += ["M140 S%d" % bed, "M104 S%d" % hot, "G92 E0"]
    if ex and ex.startswith("EXCLUDE_OBJECT_START"):
        st.append(ex)
    ls = layer_state(lines, i)
    st += ["G17"] + (["G1 F" + f] if f else [])
    if ls["total_layers"]:
        st.append("SET_PRINT_STATS_INFO TOTAL_LAYER=%d CURRENT_LAYER=%d" % (ls["total_layers"], ls["layer_index"]))
    out = lines[:h] + ["; --- RESUME PREAMBLE (state only: no motion, heating, mesh or probe) ---"] + st
    out.append(MARKER + "%d ---" % (i + 1))
    n = ls["layer_index"]
    for t in lines[i:]:
        out.append(t)
        if t == ";LAYER_CHANGE":
            n += 1
            out.append("SET_PRINT_STATS_INFO CURRENT_LAYER=%d" % n)
    return out


def build_job(gcode_path: str, log_path: Optional[str], exit_file: Optional[str], exit_pos: Optional[int],
              overlap_s: float, rate: float, resume_name: str) -> Tuple[dict, List[str]]:
    lines = _read(gcode_path, errors="surrogateescape").split("\n")
    state = parse_stop_state(_read(log_path, errors="ignore")) if log_path else {"exit_pos": None, "last_position": None}
    ep = exit_pos if exit_pos is not None else state["exit_pos"]
    if ep is None:
        raise JobError("no exit position: pass --exit-pos (power cut) or a log with 'Exiting SD card print'")
    src = exit_file or gcode_path
    data = _read(src, "rb")
    src_lines = data.decode("utf-8", "surrogateescape").split("\n")
    idx_src = line_of_offset(data, ep) - 1
    nums = to_original_numbers(src_lines)
    exit_orig = nums[idx_src]
    exit_idx = exit_orig - 1
    overlap_bytes = int(round(overlap_s * rate))
    i = pick_resume_line(lines, exit_idx, overlap_bytes)
    bed, hot = temps_before(lines, i)
    ls = layer_state(lines, i)
    park = park_point(lines, i)
    env = part_envelope(lines, i)
    job = {
        "schema": "print-rescue-job", "version": 1,
        "gcode": gcode_path.rsplit("/", 1)[-1], "resume_file": resume_name,
        "stop": {"exit_pos": ep, "exit_line_original": exit_orig, "exit_line_text": lines[exit_idx], "host_last_position": state["last_position"]},
        "overlap": {"seconds": overlap_s, "rate_bytes_per_s": round(rate, 1), "bytes": overlap_bytes,
                    "note": "bounded safety margin, not an estimate of the stop"},
        "resume": {"line_original": i + 1, "text": lines[i], "park_xy": list(park), "layer_z": ls["layer_z"],
                   "layer_index": ls["layer_index"], "total_layers": ls["total_layers"]},
        "part": {"x0": env["x0"], "x1": env["x1"], "y0": env["y0"], "y1": env["y1"], "top": ls["layer_z"],
                 "source": "every extrusion of the G-code up to the resume line"},
        "temps": {"bed": bed, "nozzle": hot, "source": "the file's own M140/M104 before the resume line"},
    }
    return job, build_resume(lines, i, bed, hot)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Build a rescue job (job.json + resume G-code) from a stopped print. Never talks to the printer.")
    ap.add_argument("--gcode", required=True, help="the ORIGINAL G-code")
    ap.add_argument("--log", help="Klippy log with 'Exiting SD card print'")
    ap.add_argument("--exit-file", help="the file that was printing, if it is a resume file (default: --gcode)")
    ap.add_argument("--exit-pos", type=int, help="byte offset of the end of the last dispatched line (power cut: last Moonraker poll)")
    ap.add_argument("--polls", help="watcher poll CSV, used for the file rate")
    ap.add_argument("--rate-bps", type=float, help="file rate in bytes/s (overrides --polls)")
    ap.add_argument("--overlap-s", type=float, default=3.0, help="overlap budget in seconds at the measured rate (default 3.0 = stop_estimation.max_overlap_s)")
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--resume-name", required=True)
    a = ap.parse_args(argv)
    try:
        rate = a.rate_bps if a.rate_bps else file_rate(a.polls) if a.polls else None
        if not rate:
            raise JobError("need --rate-bps or --polls")
        job, resume = build_job(a.gcode, a.log, a.exit_file, a.exit_pos, a.overlap_s, rate, a.resume_name)
    except (JobError, OSError) as e:
        print("REFUSED:", e, file=sys.stderr)
        return 2
    import os
    os.makedirs(a.out_dir, exist_ok=True)
    with open(os.path.join(a.out_dir, "job.json"), "w") as f:
        json.dump(job, f, indent=2)
    with open(os.path.join(a.out_dir, a.resume_name), "w", errors="surrogateescape") as f:
        f.write("\n".join(resume))
    print(json.dumps({k: job[k] for k in ("stop", "overlap", "resume", "part", "temps")}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
