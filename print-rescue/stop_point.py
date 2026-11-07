"""Where did the toolhead physically stop? Evidence from klippy.log + timing.

Klippy logs one "Stats" line per second. For the main MCU it carries the
serial counters (send_seq, receive_seq, bytes_read) and, globally, the
toolhead's print_time (end of the motion already handed to the step
generator), buffer_time (print_time minus the MCU's current time) and the
virtual SD card's sd_pos (byte offset of the G-code line being dispatched).

When the USB link stalls, receive_seq freezes while send_seq still advances
and bytes_read stops. The last sample where receive_seq advanced is the last
moment the MCU is known to have been listening:

  P_good, B_good, sd_good  print_time, buffer_time, sd_pos of that sample
  P_end                    print_time in the first Stats line after the
                           "Transition to shutdown state" (planning stopped)

The MCU executed motion at least up to print_time P_good - B_good (its own
clock at that sample) and at most up to P_good + one Stats interval (data
sent before the next sample may have arrived). That window is mapped to
G-code lines with planner.py in one of two ways (see docs/print-rescue.md,
"Stop analysis"):

Anchor "sd" (preferred, used when the last good sample has sd_pos): at that
sample the dispatched line S was known. Moves before S that sat in Klipper's
lookahead queue were not yet in print_time (up to `lookahead_bound` seconds),
and print_time may already include S itself. So in planned file time:
    earliest = t_start(S) - lookahead_bound(S) - B_good
    latest   = t_start(S) + duration(S) + interval

Anchor "end" (the plan's formula; fallback and cross-check): walk back from
the end of the last dispatched line (Klipper's "Exiting SD card print
(position N)"), by
    [P_end - min(P_end, P_good + interval),  P_end - (P_good - B_good)]
seconds, plus the lookahead bound at the end on the early side, because
moves still in the lookahead queue at shutdown were never counted in P_end.

With both anchors the window is their conservative union: the earliest
possible stop is the earlier of the two earliest stops, the latest is the later
of the two latest (the end anchor alone when there is no sd_pos).

Policy (council): resume at a line starting no later than the EARLIEST possible
stop minus margin_s (a gap is never acceptable, an overlap is); snap back to a
travel or unretract within snap_window_s if that keeps the overlap within
budget; never cross the start of the cut layer, and refuse (R12) when no line
of the layer meets the margin; refuse (R12) when the overlap from the resume
point to the latest possible stop exceeds max_overlap_s.

Pure logic, standard library only.
"""

import bisect
import math
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional

LOOKAHEAD_FLUSH_TIME = 0.250   # klippy/toolhead.py LOOKAHEAD_FLUSH_TIME
ANCHOR_AGREEMENT_S = 1.0       # anchors further apart than this -> R10

_TS = re.compile(r"^(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?Z?)\s")
_STATS = re.compile(r"Stats (\d+(?:\.\d+)?):")
_SHUTDOWN = re.compile(r"Transition to shutdown state: (.*)$")
_EXIT = re.compile(r"Exiting SD card print \(position (\d+)\)")
_SDCARD = re.compile(r"Virtual sdcard \((\d+)\)")
_UPCOMING = re.compile(r"Upcoming \((\d+)\)")
_GSTATE = re.compile(r"gcode state: (.*)$")
_BUSY = re.compile(r"Reactor \d+(?:\.\d+)? busy for")
COMM_LOSS = "Lost communication with MCU"


@dataclass
class Sample:
    index: int                 # line index in the log
    timestamp: Optional[str]
    eventtime: float
    print_time: Optional[float]
    buffer_time: Optional[float]
    sd_pos: Optional[int]
    mcu: Dict[str, float]


@dataclass
class Evidence:
    samples: List[Sample] = field(default_factory=list)
    shutdown_reason: Optional[str] = None
    shutdown_index: Optional[int] = None
    shutdown_timestamp: Optional[str] = None
    exit_position: Optional[int] = None
    sdcard_position: Optional[int] = None    # "Upcoming (N)": file position
    gcode_state: Dict[str, object] = field(default_factory=dict)
    reactor_busy_before: int = 0
    good: Optional[Sample] = None
    next_after_good: Optional[Sample] = None
    p_end: Optional[float] = None
    p_end_source: Optional[str] = None
    freeze: bool = False                     # receive_seq frozen, send_seq ahead
    notes: List[str] = field(default_factory=list)

    @property
    def comm_loss(self):
        return bool(self.shutdown_reason) and COMM_LOSS in self.shutdown_reason

    @property
    def interval(self):
        if self.good and self.next_after_good:
            return max(self.next_after_good.eventtime - self.good.eventtime, 0.0)
        return 1.0


def parse_stats(line: str, index: int = 0) -> Optional[Sample]:
    m = _STATS.search(line)
    if not m:
        return None
    ts = _TS.match(line)
    body = line[m.end():].split()
    section = None
    mcu: Dict[str, float] = {}
    glob: Dict[str, str] = {}
    for tok in body:
        if tok.endswith(":") and "=" not in tok:
            section = tok[:-1]
            continue
        if "=" not in tok:
            continue
        k, v = tok.split("=", 1)
        if k in ("print_time", "buffer_time", "sd_pos", "print_stall"):
            glob.setdefault(k, v)
        elif section == "mcu":
            try:
                mcu[k] = float(v)
            except ValueError:
                pass

    def f(key):
        try:
            v = float(glob[key])
        except (KeyError, ValueError):
            return None
        return v if math.isfinite(v) else None

    sd = f("sd_pos")
    return Sample(index=index, timestamp=ts.group(1) if ts else None,
                  eventtime=float(m.group(1)), print_time=f("print_time"),
                  buffer_time=f("buffer_time"),
                  sd_pos=int(sd) if sd is not None and sd >= 0 else None, mcu=mcu)


def _parse_gcode_state(text: str) -> Dict[str, object]:
    out: Dict[str, object] = {}
    for m in re.finditer(r"(\w+)=(\[[^\]]*\]|\S+)", text):
        k, v = m.group(1), m.group(2)
        if v.startswith("["):
            try:
                out[k] = [float(x) for x in v[1:-1].split(",") if x.strip()]
            except ValueError:
                out[k] = v
        elif v in ("True", "False"):
            out[k] = v == "True"
        else:
            try:
                out[k] = float(v)
            except ValueError:
                out[k] = v
    return out


def analyze(lines: List[str]) -> Evidence:
    """Parse a Klippy log (docker stdout with timestamps or plain klippy.log)."""
    ev = Evidence()
    for i, line in enumerate(lines):
        if "Stats " in line:
            s = parse_stats(line, i)
            if s is not None:
                ev.samples.append(s)
                continue
        m = _SHUTDOWN.search(line)
        if m:
            ev.shutdown_reason = m.group(1).strip()
            ev.shutdown_index = i
            ts = _TS.match(line)
            ev.shutdown_timestamp = ts.group(1) if ts else None
            ev.exit_position = None
            continue
        m = _EXIT.search(line)
        if m:
            ev.exit_position = int(m.group(1))
            continue
        m = _UPCOMING.search(line)
        if m:
            ev.sdcard_position = int(m.group(1))
        m = _GSTATE.search(line)
        if m:
            ev.gcode_state = _parse_gcode_state(m.group(1))
    if ev.shutdown_index is None:
        ev.notes.append("no shutdown transition in the log")
        return ev
    sd_idx = ev.shutdown_index
    before = [s for s in ev.samples if s.index < sd_idx]
    after = [s for s in ev.samples if s.index > sd_idx]
    lo = max(0, sd_idx - 400)
    ev.reactor_busy_before = sum(1 for ln in lines[lo:sd_idx] if _BUSY.search(ln))
    if after and after[0].print_time is not None:
        ev.p_end, ev.p_end_source = after[0].print_time, "first Stats after shutdown"
    elif before and before[-1].print_time is not None:
        ev.p_end, ev.p_end_source = before[-1].print_time, "last Stats before shutdown"
        ev.notes.append("no Stats line after the shutdown; P_end is a lower bound")
    seqs = [s for s in before if "receive_seq" in s.mcu]
    good = None
    for k in range(len(seqs) - 1, 0, -1):
        if seqs[k].mcu["receive_seq"] > seqs[k - 1].mcu["receive_seq"]:
            good = k
            break
    if good is None:
        ev.notes.append("receive_seq never advanced in the Stats lines")
        return ev
    if good == len(seqs) - 1:
        ev.notes.append("receive_seq still advanced in the last Stats line "
                        "before the shutdown: no link freeze visible")
        return ev
    ev.good = seqs[good]
    ev.next_after_good = seqs[good + 1]
    r, b = ev.good.mcu["receive_seq"], ev.good.mcu.get("bytes_read")
    frozen = seqs[good + 1:]
    ev.freeze = all(s.mcu["receive_seq"] == r and
                    s.mcu.get("send_seq", 0) > s.mcu["receive_seq"] and
                    s.mcu.get("bytes_read") == b for s in frozen)
    if not ev.freeze:
        ev.notes.append("receive_seq stopped but send_seq/bytes_read do not "
                        "show a clean link freeze")
    return ev


# -- mapping onto the planned timeline --------------------------------------

@dataclass
class Window:
    anchor: str                    # sd | end
    earliest_t: float              # planned file time of the earliest stop
    latest_t: float
    file_end_t: float              # planned end of the last dispatched line
    earliest_line: int
    latest_line: int
    walkback_earliest_s: float     # seconds before file_end_t
    walkback_latest_s: float
    lookahead_bound_s: float
    detail: Dict[str, float] = field(default_factory=dict)


@dataclass
class Choice:
    line: int
    t: float
    xy: tuple
    overlap_s: float
    overlap_mm: float
    snapped: bool
    manual: bool
    refusal: Optional[str] = None   # text for R12 when refused
    note: Optional[str] = None


def _line_index(timed):
    """Sorted line numbers and, per line, (first index, last index)."""
    first, last = {}, {}
    for i, tm in enumerate(timed):
        first.setdefault(tm.line, i)
        last[tm.line] = i
    return sorted(first), first, last


def line_start_t(timed, line) -> Optional[float]:
    lines, first, _ = _line_index(timed)
    k = bisect.bisect_left(lines, line)
    if k >= len(lines):
        return None
    return timed[first[lines[k]]].t_start


def line_at_t(timed, t) -> Optional[int]:
    """Line whose motion is running at planned time t (clamped to the ends)."""
    if not timed:
        return None
    ends = [tm.t_end for tm in timed]
    k = bisect.bisect_right(ends, t)
    k = min(k, len(timed) - 1)
    return timed[k].line


def lookahead_bound(timed, before_line, flush_s=LOOKAHEAD_FLUSH_TIME) -> float:
    """Upper bound for motion dispatched before `before_line` but not yet in
    print_time: Klipper's lookahead flushes once the queued moves add up to
    LOOKAHEAD_FLUSH_TIME of minimum move time, and a lazy flush keeps the
    last move back. Sum planned durations walking back until that is met."""
    idx = [i for i, tm in enumerate(timed) if tm.line < before_line]
    total = acc = 0.0
    moves = 0
    for i in reversed(idx):
        tm = timed[i]
        if tm.kind == "dwell":
            break
        total += tm.duration
        acc += tm.min_move_t
        moves += 1
        if acc >= flush_s and moves >= 2:
            break
    return total


def compute_window(ev: Evidence, timed, last_line: int,
                   sd_line: Optional[int]) -> Optional[Window]:
    """Map the executed-motion window onto planned file time.

    With both anchors the window is their conservative union: the earliest
    possible stop is the earlier of the two earliest stops and the latest
    possible stop is the later of the two latest (S1). The end anchor alone
    is used when there is no sd_pos."""
    if ev.good is None or ev.p_end is None or not timed:
        return None
    pg, bg = ev.good.print_time, ev.good.buffer_time
    if pg is None or bg is None:
        return None
    interval = ev.interval
    _, first, last = _line_index(timed)
    ends = [i for ln, i in last.items() if ln <= last_line]
    if not ends:
        return None
    file_end_t = timed[max(ends)].t_end
    la_end = lookahead_bound(timed, last_line + 1)
    end_early = file_end_t - (ev.p_end - (pg - bg)) - la_end
    end_late = file_end_t - (ev.p_end - min(ev.p_end, pg + interval))
    detail = {"P_good": pg, "B_good": bg, "P_end": ev.p_end,
              "interval": interval, "end_anchor_earliest_t": end_early,
              "end_anchor_latest_t": end_late, "lookahead_end_s": la_end}
    anchor, early, late, la = "end", end_early, end_late, la_end
    if sd_line is not None and sd_line in first:
        ts = timed[first[sd_line]].t_start
        dur_s = timed[last[sd_line]].t_end - ts
        la_sd = lookahead_bound(timed, sd_line)
        sd_early = ts - la_sd - bg
        sd_late = ts + dur_s + interval
        detail.update({"sd_line": sd_line, "sd_line_t": ts,
                       "sd_anchor_earliest_t": sd_early,
                       "sd_anchor_latest_t": sd_late,
                       "lookahead_sd_s": la_sd})
        anchor = "sd+end"
        early, la = (sd_early, la_sd) if sd_early <= end_early else (end_early, la_end)
        late = max(sd_late, end_late)
    early = max(early, timed[0].t_start)
    late = min(late, file_end_t)
    return Window(anchor=anchor, earliest_t=early, latest_t=late,
                  file_end_t=file_end_t, earliest_line=line_at_t(timed, early),
                  latest_line=line_at_t(timed, late),
                  walkback_earliest_s=file_end_t - early,
                  walkback_latest_s=file_end_t - late,
                  lookahead_bound_s=la, detail=detail)


def anchors_disagree(w: Window) -> Optional[float]:
    """Largest difference between the two anchors' window endpoints, if both
    anchors exist and it exceeds ANCHOR_AGREEMENT_S (R10). Endpoints, not
    midpoints: a window whose midpoints agree can still differ at one end."""
    d = w.detail
    if "sd_anchor_earliest_t" not in d:
        return None
    gap = max(abs(d["sd_anchor_earliest_t"] - d["end_anchor_earliest_t"]),
              abs(d["sd_anchor_latest_t"] - d["end_anchor_latest_t"]))
    return gap if gap > ANCHOR_AGREEMENT_S else None


def _path_mm(timed, t0, t1):
    mm = 0.0
    for tm in timed:
        if tm.kind != "move" or tm.t_end <= t0 or tm.t_start >= t1:
            continue
        seg = math.hypot(tm.end_xy[0] - tm.start_xy[0], tm.end_xy[1] - tm.start_xy[1])
        span = tm.t_end - tm.t_start
        frac = 1.0 if span <= 0 else (min(tm.t_end, t1) - max(tm.t_start, t0)) / span
        mm += seg * max(0.0, min(1.0, frac))
    return mm


def choose_resume(w: Window, timed, layer_start_line: Optional[int],
                  margin_s: float, snap_window_s: float, max_overlap_s: float,
                  manual_line: Optional[int] = None) -> Choice:
    """Pick the resume line. The auto path never returns a line that starts
    after earliest_t - margin_s (a gap); when no such line exists inside the
    cut layer it refuses (R12) instead of clamping to the layer start.
    A manual line is checked against the window (gap and overlap) only."""
    if not (isinstance(margin_s, (int, float)) and not isinstance(margin_s, bool)
            and math.isfinite(margin_s) and margin_s >= 0):
        raise ValueError("margin_s must be a finite number >= 0 (got %r)" % (margin_s,))
    lines, first, _ = _line_index(timed)
    layer_t = None
    if layer_start_line is not None:
        layer_t = line_start_t(timed, layer_start_line)

    def start_of(line):
        return timed[first[line]].t_start

    def xy_at(line):
        return tuple(timed[first[line]].start_xy)

    if manual_line is not None:
        t = line_start_t(timed, manual_line)
        if t is None:
            return Choice(manual_line, float("nan"), (None, None), float("nan"),
                          float("nan"), False, True,
                          refusal="line %d has no planned motion" % manual_line)
        k = bisect.bisect_left(lines, manual_line)
        line = lines[k]
        ch = Choice(line, t, xy_at(line), w.latest_t - t,
                    _path_mm(timed, t, w.latest_t), False, True)
        if t > w.earliest_t + 1e-6:
            ch.refusal = ("manual line %d starts %.2f s after the earliest "
                          "possible stop (a gap is possible)"
                          % (manual_line, t - w.earliest_t))
        elif ch.overlap_s > max_overlap_s:
            ch.refusal = ("manual line %d overlaps %.2f s > max %.1f s"
                          % (manual_line, ch.overlap_s, max_overlap_s))
        return ch

    target = w.earliest_t - margin_s
    if layer_t is not None and w.earliest_t < layer_t - 1e-6:
        return Choice(layer_start_line, layer_t, (None, None), float("nan"),
                      float("nan"), False, False,
                      refusal="the stop window starts before the cut "
                              "layer (line %d)" % layer_start_line)
    if layer_t is not None and target < layer_t - 1e-9:
        return Choice(layer_start_line, layer_t, (None, None), float("nan"),
                      float("nan"), False, False,
                      refusal="no line of the cut layer (from line %d) starts at or "
                              "before the earliest possible stop minus the %.2f s "
                              "margin" % (layer_start_line, margin_s))
    line = line_at_t(timed, target)
    t = start_of(line)
    snapped = False
    floor_t = w.latest_t - max_overlap_s
    lo = max(t - snap_window_s, floor_t)
    if layer_t is not None:
        lo = max(lo, layer_t)
    for tm in reversed(timed):
        if tm.t_start > t or tm.t_start < lo:
            continue
        if tm.travel or tm.unretract:
            if tm.line != line:
                line, t, snapped = tm.line, start_of(tm.line), True
            break
    ch = Choice(line, t, xy_at(line), w.latest_t - t,
                _path_mm(timed, t, w.latest_t), snapped, False)
    if t > target + 1e-9:
        ch.refusal = ("no line starts at or before the earliest possible stop "
                      "minus the %.2f s margin" % margin_s)
    elif ch.overlap_s > max_overlap_s + 1e-9:
        ch.refusal = ("resuming at the earliest possible stop needs %.2f s of "
                      "overlap > max %.1f s" % (ch.overlap_s, max_overlap_s))
    return ch


def planner_agreement(ev: Evidence, timed, line_of_byte, samples: int = 5):
    """Offsets c = print_time - planned start of the dispatched line for the
    last good samples. A steady c means planner.py tracks Klipper's own
    timing there; returns (spread_s, [(sd_line, c), ...]) or None."""
    if ev.good is None:
        return None
    goods = [s for s in ev.samples if s.index <= ev.good.index and
             s.sd_pos is not None and s.print_time is not None]
    pts = []
    for s in goods[-samples:]:
        ln = line_of_byte(s.sd_pos)
        t = line_start_t(timed, ln) if ln is not None else None
        if t is not None and ln >= timed[0].line:
            pts.append((ln, s.print_time - t))
    if len(pts) < 2:
        return None
    cs = [c for _, c in pts]
    return max(cs) - min(cs), pts


_URB = re.compile(r"^(\w{3} \d\d \d\d:\d\d:\d\d) .*(ch341|ftdi|cp210x|cdc_acm|usb).*"
                  r"urb stopped: (-\d+)")


def kernel_usb_stalls(lines: List[str]) -> List[tuple]:
    """(local timestamp, error code) of USB serial read stalls in kernel
    lines. -32 (EPIPE) on the read URB is a CH340 read stall (its root
    cause is unknown): the host stops hearing the MCU. Kernel time is the host's local time."""
    out = []
    for ln in lines:
        m = _URB.search(ln)
        if m:
            out.append((m.group(1), int(m.group(3))))
    return out
