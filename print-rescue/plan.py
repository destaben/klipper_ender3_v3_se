#!/usr/bin/env python3
"""print-rescue phase 1: read-only resume planner.

    python3 plan.py --gcode FILE --log KLIPPY_LOG --profile PROFILE.json
                    [--printer-cfg PATH] [--history JSON] [--queue JSON]
                    [--kernel-log PATH] [--line N] [--max-overlap-s S]
                    [--probe-spot X,Y ...] [--suggest-spots]
                    [--preview OUT [--force-preview]]
                    [--now ISO8601]
                    [--gcode-map MAP]   (only for fixture excerpts)

Prints the stop window, the chosen resume line/XY/overlap, part box and
height, probe-spot verdicts, the probe-pin-at-boot check and every refusal
and warning as a console-ready line. With --preview it writes a resume
G-code preview labelled NOT VERIFIED, and only when nothing is refused; an
existing file is not overwritten without --force-preview, and the preview is
never an input file, a symlink, or a hard link to one. It never talks to
the network and never moves anything.

Exit codes:
  0  resumable: no refusal and every safety-critical profile field verified
     (warnings must still be confirmed before any motion)
  3  resumable on paper only: a safety-critical profile field is not verified
     (R13), or --history, --queue or --printer-cfg was not given (NOT CHECKED),
     so motion is blocked; the report and preview are still produced
  2  refused: a hard refusal, including R18 when no resume line can be chosen
     (no 'Exiting SD card print' position, no usable Stats lines, no line)
  1  usage or input error (bad arguments, unreadable or malformed input, an
     invalid margin_s); nothing was planned
Standard library only.
"""

import argparse
import bisect
import datetime
import errno
import glob
import json
import math
import os
import secrets
import stat
import sys
import zlib

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import gcode_scan as gs  # noqa: E402
import geometry  # noqa: E402
import planner  # noqa: E402
import machine_profile as prof  # noqa: E402
import refusals as rf  # noqa: E402
import resume_file  # noqa: E402
import stop_point as sp  # noqa: E402

R11_HOURS = 2.0
_ABSURD_CAP = 30.0   # --max-overlap-s never exceeds the default R21 limit
EXIT_RESUMABLE = 0
EXIT_USAGE = 1
EXIT_REFUSED = 2
EXIT_MOTION_BLOCKED = 3


class InputError(Exception):
    """Bad usage or unreadable/malformed input: exit 1 with a message."""


def limits_from_profile(p: dict) -> gs.Limits:
    m = p.get("motion", {})
    lim = gs.Limits()
    for k in lim.__dataclass_fields__:
        if k in m:
            setattr(lim, k, float(prof.val(m[k])))
    return lim


def _records_factory(args):
    if args.gcode_map:
        return lambda: gs.iter_excerpt(args.gcode, args.gcode_map)
    return lambda: gs.iter_file(args.gcode)


MAX_TEXT_CHARS = 256 * 1024 * 1024


def _load_json(path, what):
    """Parse a JSON input, with the same size cap as every other text input."""
    text = _read_text(path, what)
    try:
        return json.loads(text)
    except (ValueError, RecursionError) as e:
        raise InputError("%s %s: %s" % (what, os.path.basename(path), e))


def _read_text(path, what):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            text = fh.read(MAX_TEXT_CHARS + 1)
        if len(text) > MAX_TEXT_CHARS:
            raise InputError("%s %s: larger than %d characters"
                             % (what, os.path.basename(path), MAX_TEXT_CHARS))
        return text
    except OSError as e:
        raise InputError("%s %s: %s" % (what, os.path.basename(path), e.strerror or e))


# PIN_AT_BOOT is a warning, so the run can still exit 0. The text must say
# what to DO, not only what to assume.
PIN_UNKNOWN_NOTE = ("the stop window holds no move, so the head position at the stop "
                    "is unknown. Treat the pin as over the part: lift Z by hand and move "
                    "the head off the part before FIRMWARE_RESTART (exit code 0 does not "
                    "clear this warning)")

# The content check compares the exit offset and the logged text at a few
# offsets. It cannot see an edit that keeps the length and lies elsewhere in
# the file (for example F1800 -> F2400 in the last layers); only a byte-exact
# copy of the printed file (--history size plus the operator's own knowledge)
# can. The message says so, so a pass is never read as "identical".
R19_REUPLOAD_NOTE = ("the exit offset and the logged text match, so a re-upload is the "
                     "likely cause; a same-length edit elsewhere in the file cannot "
                     "be ruled out by this check")


def tail_warnings(tail):
    """Warnings (code, detail) for a gs.tail_findings result: absolute
    extrusion (R05), vase or non-monotonic Z (R06), relative XYZ (R23). Only the
    first line of each kind is named."""
    out = []
    for ln, _ in tail["m82"][:1]:
        out.append(("R05", "line %d" % ln))
    for ln, why in (tail["vase"] + tail["non_monotonic_z"])[:1]:
        out.append(("R06", "line %d: %s" % (ln, why)))
    for ln, _ in tail["g91"][:1]:
        out.append(("R23", "line %d: G91 in the resumed tail; the preview restores the "
                    "mode at the resume line, check that the tail returns to G90" % ln))
    return out


MAX_CFG_FILES = 200
_INCLUDE = "include "


def _cfg_with_includes(path):
    """Sections of printer.cfg with its [include] files merged in (Klipper
    follows them), as (sections, missing, external). A relative include must
    stay inside the directory of printer.cfg: one that escapes it, does not
    exist, or comes after MAX_CFG_FILES files is reported as missing, never
    read. An absolute include (for example a file generated at runtime) is
    listed as external and not read. Glob patterns work like Klipper's. Later
    files override earlier options of the same section."""
    root = os.path.realpath(os.path.dirname(os.path.abspath(path)))
    sections, missing, external, seen = {}, [], [], set()
    queue = [os.path.realpath(path)]
    while queue and len(seen) < MAX_CFG_FILES:
        cur = queue.pop(0)
        if cur in seen:
            continue
        seen.add(cur)
        parsed = prof.parse_printer_cfg(_read_text(cur, "printer.cfg"))
        base = os.path.dirname(cur)
        for name, opts in parsed.items():
            if name.lower().startswith(_INCLUDE):
                pattern = name[len(_INCLUDE):].strip()
                if os.path.isabs(pattern):
                    external.append(pattern)
                    continue
                hits = sorted(glob.glob(os.path.join(base, pattern)))
                ok = [h for h in hits if os.path.isfile(h) and
                      os.path.realpath(h).startswith(root + os.sep)]
                if not ok:
                    missing.append(pattern)
                queue.extend(os.path.realpath(h) for h in ok)
            else:
                sections.setdefault(name, {}).update(opts)
    if queue:
        missing.append("(more than %d files)" % MAX_CFG_FILES)
    return sections, missing, external


def _is_number(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _exit_boundary(res, exit_position, size):
    """Content check (R07): Klipper's 'Exiting SD card print (position N)' is
    the byte right after the last dispatched line. In the file that was
    printing, N is therefore the start of a line (or the end of the file).
    A re-uploaded file with the same contents passes whatever its modified
    time says. This alone is weak: a same-length edit always passes, and a
    different file lands on a line start now and then (a few percent), so it
    is paired with _text_check. Returns (ok, text)."""
    offsets, lines = res.line_offsets
    i = bisect.bisect_left(offsets, exit_position)
    if i < len(offsets) and offsets[i] == exit_position:
        prev = lines[i - 1] if i > 0 else None
        return True, ("exit position %d is the end of line %s and the start of line %d"
                      % (exit_position, prev, lines[i]))
    if size is not None and size == exit_position:
        return True, "exit position %d is the end of the file" % exit_position
    return False, ("exit position %d is not a line boundary in this file, so it is not "
                   "the file that was printing" % exit_position)


def _text_check(records, ev, excerpt=False):
    """Stronger half of the content check (R07): the text the log printed for
    'Virtual sdcard (N)' and 'Upcoming (N)' is the file's own text at those
    offsets. Returns (ok, message). Every offset is checked. An offset with no
    text in a real file means the file ends before it, which is not the file
    that was printing; in a fixture excerpt (gaps are expected) it is skipped.
    ok is True when there is nothing to compare."""
    texts = [(p, t) for p, t in ev.sdcard_texts][:4]
    if not texts:
        return True, "no file text in the log to compare"
    got = gs.file_text_at(records(), [(p, len(t)) for p, t in texts])
    compared = 0
    for pos, logged in texts:
        mine = got.get(pos, "")
        if not mine:
            if excerpt:
                continue
            return False, ("the file has no text at offset %d the log shows, so it is "
                           "not the file that was printing" % pos)
        compared += 1
        n = min(len(mine), len(logged))
        if mine[:n] != logged[:n]:
            return False, ("the text the log shows at offset %d differs from the file, so "
                           "it is not the file that was printing" % pos)
    if not compared:
        return True, "the excerpt holds nothing at the logged offsets to compare"
    return True, "the text the log shows at %d offset(s) matches the file" % compared


def _gcode_identity(args):
    """(size, mtime, source name). An excerpt reports its source's size."""
    if args.gcode_map:
        meta = _load_json(args.gcode_map, "excerpt map")
        if not isinstance(meta, dict):
            raise InputError("excerpt map: not a JSON object")
        return meta.get("source_size"), None, str(meta.get("source", ""))
    if not os.path.isfile(args.gcode):
        return None, None, os.path.basename(args.gcode)
    st = os.stat(args.gcode)
    return st.st_size, st.st_mtime, os.path.basename(args.gcode)


def _history_job(data):
    if not isinstance(data, dict):
        raise InputError("history: not a JSON object")
    if "job" in data:
        job = data["job"]
        if not isinstance(job, dict):
            raise InputError("history: 'job' is not an object")
        return job
    result = data.get("result", data)
    if not isinstance(result, dict):
        raise InputError("history: 'result' is not an object")
    jobs = result.get("jobs", [])
    if not isinstance(jobs, list):
        raise InputError("history: 'jobs' is not a list")
    candidates = [j for j in jobs if isinstance(j, dict)]
    # Only a job that ended abnormally can be the interrupted print. Taking the
    # first job of any status would compare size and mtime against an unrelated
    # earlier print; no match is an R07 refusal instead.
    for job in candidates:
        if job.get("status") in ("klippy_shutdown", "klippy_disconnect", "error",
                                 "cancelled", "interrupted"):
            return job
    return None


def _queued(data):
    if not isinstance(data, dict):
        raise InputError("queue: not a JSON object")
    result = data.get("result", data)
    if not isinstance(result, dict):
        raise InputError("queue: 'result' is not an object")
    if "queued_jobs" not in result:
        # An absent key must not read as an empty queue (R15 would fail open
        # for a history or other JSON passed to --queue by mistake).
        raise InputError("queue: no 'queued_jobs' key (is this a "
                         "server.job_queue.status response?)")
    jobs = result["queued_jobs"]
    if not isinstance(jobs, list):
        raise InputError("queue: 'queued_jobs' is not a list")
    return [j.get("filename", "?") if isinstance(j, dict) else "?" for j in jobs]


def _parse_ts(text):
    if not text:
        return None
    t = text.rstrip("Z")
    if "." in t:
        head, frac = t.split(".", 1)
        t = head + "." + frac[:6]
    try:
        d = datetime.datetime.fromisoformat(t)
    except ValueError:
        return None
    if d.tzinfo is None:
        return d.replace(tzinfo=datetime.timezone.utc)
    return d.astimezone(datetime.timezone.utc)


def _same_file(a: str, b: str) -> bool:
    if os.path.realpath(a) == os.path.realpath(b):
        return True
    return os.path.exists(a) and os.path.exists(b) and os.path.samefile(a, b)


def _check_preview_target(args):
    """S2: the preview is never an input (or a link to one), never written
    through a symlink, and an existing file needs --force-preview. Checked
    before anything is read or written."""
    target = args.preview
    name = os.path.basename(target)
    inputs = [("--gcode", args.gcode), ("--gcode-map", args.gcode_map),
              ("--log", args.log), ("--profile", args.profile),
              ("--history", args.history), ("--queue", args.queue),
              ("--printer-cfg", args.printer_cfg), ("--kernel-log", args.kernel_log)]
    for flag, path in inputs:
        if path and _same_file(path, target):
            raise InputError("preview %s is the same file as the %s input; refusing to "
                             "overwrite an input" % (name, flag))
    if os.path.islink(target):
        raise InputError("preview %s is a symbolic link; refusing to write through it" % name)
    if os.path.lexists(target) and not args.force_preview:
        raise InputError("preview %s already exists; pass --force-preview to overwrite it"
                         % name)


def _parse_spot(text):
    try:
        x, y = (float(v) for v in text.split(","))
    except ValueError:
        raise InputError("--probe-spot must look like X,Y (got %r)" % text)
    if not (abs(x) < 1e4 and abs(y) < 1e4):
        raise InputError("--probe-spot %r is not a plausible position" % text)
    return {"x": x, "y": y}


def _check_args(args):
    """Usage errors and the preview target, before anything is read."""
    now = None
    if args.now is not None:
        now = _parse_ts(args.now)
        if now is None:
            raise InputError("--now must be an ISO 8601 UTC time such as "
                             "2026-11-07T09:30:00Z (got %r)" % args.now)
    if args.line is not None and args.line < 1:
        raise InputError("--line must be a line number >= 1")
    if args.gcode_map and os.environ.get("PRINT_RESCUE_ALLOW_GCODE_MAP") != "1":
        raise InputError("--gcode-map is for the test fixtures only; set "
                         "PRINT_RESCUE_ALLOW_GCODE_MAP=1 to use it")
    if args.max_overlap_s is not None and not (args.max_overlap_s > 0 and
                                               args.max_overlap_s < float("inf")):
        raise InputError("--max-overlap-s must be a number > 0")
    if args.max_overlap_s is not None and args.max_overlap_s > _ABSURD_CAP:
        raise InputError("--max-overlap-s %g is above %g s, the limit for any budget; the "
                         "profile's absurd_overlap_s (R21) is not moved by it"
                         % (args.max_overlap_s, _ABSURD_CAP))
    for text in args.probe_spot or []:
        _parse_spot(text)
    if args.force_preview and not args.preview:
        raise InputError("--force-preview needs --preview")
    if args.preview:
        _check_preview_target(args)
    return now


def run(args, out=sys.stdout) -> int:
    """Plan the rescue. Malformed input gives a message and exit 1, never a
    traceback."""
    try:
        return _run(args, out)
    except InputError as e:
        print("error: %s" % e, file=sys.stderr)
        return EXIT_USAGE
    except (ValueError, ArithmeticError, OSError, LookupError, RecursionError,
            EOFError, zlib.error, TypeError, AttributeError, MemoryError) as e:
        print("error: input could not be analysed: %s" % e, file=sys.stderr)
        return EXIT_USAGE


def _run(args, out) -> int:
    findings = []

    def add(code, detail=""):
        findings.append(rf.Finding(code, detail))

    not_checked = []

    def say(text=""):
        print(text, file=out)

    now = _check_args(args)
    try:
        p = prof.load_profile(args.profile)
    except (OSError, ValueError, RecursionError) as e:
        raise InputError("profile: %s" % e)
    lim = limits_from_profile(p)
    se = prof.section_values(p, "stop_estimation")
    th = prof.section_values(p, "thermal")
    cl = prof.section_values(p, "clearances")

    gcode_ok = os.path.isfile(args.gcode)
    if not gcode_ok:
        if os.path.exists(args.gcode):
            add("R07", "G-code path is not a regular file: %s" % os.path.basename(args.gcode))
        else:
            add("R07", "G-code file not found: %s" % os.path.basename(args.gcode))
    ev = sp.analyze(_read_text(args.log, "log").splitlines())

    size, mtime, source_name = _gcode_identity(args)
    mtime_differs = False
    if args.history:
        job = _history_job(_load_json(args.history, "history"))
        if job is None:
            add("R07", "no job in the history file")
        else:
            meta = job.get("metadata") or {}
            if not isinstance(meta, dict):
                raise InputError("history: job metadata is not an object")
            fname = job.get("filename", "")
            if not isinstance(fname, str):
                raise InputError("history: job filename is not a string")
            if os.path.basename(fname) != os.path.basename(source_name):
                add("R07", "history job is %s" % os.path.basename(fname))
            if meta.get("size") is not None and size is not None and meta["size"] != size:
                add("R07", "size %s != %s in history" % (size, meta["size"]))
            if meta.get("modified") is not None and mtime is not None and \
                    (not _is_number(meta["modified"]) or abs(meta["modified"] - mtime) > 1.0):
                mtime_differs = True
            if meta.get("size") is None and meta.get("modified") is None:
                # The name alone is not an identity check; do not pass silently.
                item = "R07 history identity (the job has no size or modified)"
                not_checked.append(item)
    if not args.history:
        not_checked.append("R07 history identity (no --history)")
    if not args.queue:
        not_checked.append("R15 job queue (no --queue)")
    if not args.printer_cfg:
        not_checked.append("R14 printer.cfg fingerprint (no --printer-cfg)")
    if args.queue:
        q = _queued(_load_json(args.queue, "queue"))
        if q:
            add("R15", "%d queued job(s) would start on the next Klippy ready" % len(q))
    live_notes = []
    user_macros = {}
    if args.printer_cfg:
        cfg = prof.parse_printer_cfg(_read_text(args.printer_cfg, "printer.cfg"))
        cfg_all, missing_inc, external_inc = _cfg_with_includes(args.printer_cfg)
        user_macros = gs.dangerous_macros(cfg_all)
        if missing_inc:
            # A named include that was not supplied can hold the macro the
            # tail calls, so the R17 user-macro check is incomplete: NOT
            # CHECKED, exit 3. Save the config directory next to printer.cfg.
            not_checked.append("R17 user macros (include not read: %s)"
                               % ", ".join(sorted(set(missing_inc))[:5]))
        if external_inc:
            live_notes.append("include outside the config directory not read for the "
                              "user-macro check: %s" % ", ".join(sorted(set(external_inc))[:5]))
        live_notes = live_notes + prof.apply_live_config(p, cfg)
        for mm in prof.fingerprint_mismatches(p, cfg):
            add("R14", mm)

    if ev.shutdown_reason is None:
        add("R09", "no shutdown found in the log")
    elif not ev.comm_loss:
        add("R09", "shutdown reason: %s" % ev.shutdown_reason)

    say("== print-rescue plan (read-only, phase 1) ==")
    for item in not_checked:
        say("NOT CHECKED: %s" % item)
    for note in live_notes:
        say("live config: %s" % note)
    say("source: %s" % os.path.basename(source_name))
    say("shutdown: %s at %s" % (ev.shutdown_reason, ev.shutdown_timestamp))
    say("reactor busy warnings before the shutdown: %d" % ev.reactor_busy_before)
    if args.kernel_log:
        stalls = sp.kernel_usb_stalls(_read_text(args.kernel_log, "kernel log").splitlines())
        for ts, err in stalls:
            say("kernel: USB serial read stalled (%d) at %s host local time" % (err, ts))
        if not stalls:
            say("kernel: no USB serial read stall found")

    # Scan the file up to the exit position. Nothing below returns early: the
    # profile checks (R02, R13, R14) and every other check still run, and the
    # refusal is decided at the end in _finish.
    records = _records_factory(args)
    res = timed = part = last_line = None
    if gcode_ok and ev.exit_position is not None:
        res = gs.scan(records(), lim, until_byte=ev.exit_position, keep_layers=2,
                      extra_forbidden=user_macros)
        timed = planner.plan(res.moves, lim)
        last_line = gs.last_processed_line(res, ev.exit_position)
        if res.total.empty:
            add("R18", "no extrusion before the exit position: the part box is unknown")
        else:
            part = geometry.Part.from_bbox(res.total)
    elif ev.exit_position is None:
        add("R10", "no 'Exiting SD card print' position in the log")
        add("R18", "the last dispatched line is unknown (no exit position)")

    if res is None and mtime_differs:
        add("R19", "the contents could not be checked (no exit position or file)")
    if res is not None:
        say("last dispatched line: %s (exit position %d)" % (last_line, ev.exit_position))
        same, why = _exit_boundary(res, ev.exit_position, size)
        say("content check: %s" % why)
        if not same:
            add("R07", why)
        else:
            same, why = _text_check(records, ev, excerpt=bool(args.gcode_map))
            say("content check: %s" % why)
            if not same:
                add("R07", why)
            elif not ev.sdcard_texts:
                # Nothing to compare is not a pass: the check then rests on the
                # exit-offset boundary alone, so the run cannot exit 0.
                item = "R07 file text (the log has no 'Virtual sdcard' or 'Upcoming' text)"
                not_checked.append(item)
                say("NOT CHECKED: %s" % item)
        if same and mtime_differs:
            add("R19", R19_REUPLOAD_NOTE)
        if part is not None:
            say("part box X %.1f-%.1f Y %.1f-%.1f, top %.2f mm (extrusions through line %d)"
                % (part.x_min, part.x_max, part.y_min, part.y_max, part.top, res.bbox_until))
        last_pos = ev.gcode_state.get("last_position")
        if isinstance(last_pos, list) and len(last_pos) >= 2 and timed:
            end_xy = [tm.end_xy for tm in timed if tm.line == last_line]
            if end_xy and (abs(end_xy[-1][0] - last_pos[0]) > 0.01 or
                           abs(end_xy[-1][1] - last_pos[1]) > 0.01):
                add("R10", "log last_position %s does not match line %d" %
                    (last_pos[:2], last_line))

    window = None
    if res is not None:
        if last_line is None or not timed:
            add("R18", "nothing was planned before the exit position: no stop window")
        elif ev.good is None:
            add("R10", "; ".join(ev.notes) or "no usable Stats lines")
            add("R18", "no stop window: the log has no usable Stats evidence")
        else:
            if not ev.freeze:
                add("R10", "receive_seq freeze not clean")
            for note in ev.notes:
                if "lower bound" in note:
                    add("R10", note)
            sd_line = gs.line_at_byte(res, ev.good.sd_pos) if ev.good.sd_pos is not None else None
            window = sp.compute_window(ev, timed, last_line, sd_line)
            if window is None:
                add("R18", "no stop window: the log and the plan do not match")

    choice = None
    state = None
    if window is not None:
        d = window.detail
        say("")
        say("-- stop window (anchors: %s) --" % window.anchor)
        say("P_good %.3f B_good %.3f P_end %.3f (%s), P_end - P_good %.2f s"
            % (d["P_good"], d["B_good"], d["P_end"], ev.p_end_source,
               d["P_end"] - d["P_good"]))
        say("earliest stop: line %d, %.2f s before the end of line %d"
            % (window.earliest_line, window.walkback_earliest_s, last_line))
        say("latest stop:   line %d, %.2f s before the end of line %d"
            % (window.latest_line, window.walkback_latest_s, last_line))
        say("lookahead bound used: %.2f s" % window.lookahead_bound_s)
        say("end-anchor (plan formula) walk-back: %.2f-%.2f s"
            % (window.file_end_t - d["end_anchor_latest_t"],
               window.file_end_t - d["end_anchor_earliest_t"]))
        if "sd_anchor_earliest_t" in d:
            say("sd-anchor walk-back: %.2f-%.2f s"
                % (window.file_end_t - d["sd_anchor_latest_t"],
                   window.file_end_t - d["sd_anchor_earliest_t"]))
        gap = sp.anchors_disagree(window)
        if gap is not None:
            add("R10", "sd and end anchor window endpoints differ by %.2f s" % gap)
        agree = sp.planner_agreement(ev, timed, lambda b: gs.line_at_byte(res, b))
        if agree is not None:
            say("planner vs log offset spread over the last good samples: %.3f s"
                % agree[0])
            if agree[0] > 0.5:
                add("R10", "planner timing disagrees with the log by %.2f s" % agree[0])
        starts = [ls for ls, _, _ in res.layer_starts if ls <= window.latest_line]
        layer_start = max(starts) if starts else None
        budget = args.max_overlap_s if args.max_overlap_s is not None else se["max_overlap_s"]
        absurd = max(se.get("absurd_overlap_s", 30.0), se["max_overlap_s"])
        choice = sp.choose_resume(window, timed, layer_start, se["margin_s"],
                                  se["snap_window_s"], budget,
                                  manual_line=args.line, absurd_overlap_s=absurd)
        if choice.note:
            say("note: %s" % choice.note)
        if choice.refusal:
            add(choice.refusal_code, choice.refusal)
        if choice.warning:
            add("R12", choice.warning)
    elif args.line is not None:
        # N3: a manual line is only ever checked against a window.
        add("R18", "manual --line %d refused: there is no stop window to check it against"
            % args.line)

    if choice is not None and choice.line is not None and res is not None:
        say("")
        say("Z after a restart: not homed. Declare a provisional Z, probe at a spot that "
            "clears the part, set Z = z_offset at the trigger, then do the paper test ON "
            "THE PART with the bed mesh LOADED (docs/print-rescue.md, Z re-reference).")
        state = gs.state_at(records(), choice.line, lim)
        say("")
        say("-- resume candidate (%s) --"
            % ("refused, see %s" % choice.refusal_code if choice.refusal else "chosen"))
        say("resume line %d at X%.3f Y%.3f Z%.3f (layer %d, Z %.2f)%s%s"
            % (choice.line, state.pos[0], state.pos[1], state.pos[2], state.layer,
               state.layer_z or 0.0, " snapped to travel/unretract" if choice.snapped else "",
               " (manual)" if choice.manual else ""))
        if choice.overlap_s == choice.overlap_s:
            say("overlap to the latest possible stop: %.2f s, %.1f mm of path, "
                "%.0f mm of filament (about %.0f mm3)"
                % (choice.overlap_s, choice.overlap_mm, choice.extra_filament_mm,
                   choice.extra_filament_mm * math.pi * (sp.FILAMENT_DIAMETER / 2.0) ** 2))
        for ev_ in res.events:
            if ev_.kind == "forbidden" and ev_.detail in ("G10", "G11") and ev_.line < choice.line:
                add("R10", "firmware retraction (%s, line %d) before the resume line: the "
                    "retracted length is not modelled, so the prime amount is unreliable"
                    % (ev_.detail, ev_.line))
                break
        tail = gs.tail_findings(res, choice.line, state.tool)
        for ln, cmd in tail["forbidden"]:
            add("R17", "line %d: %s (%s)" % (ln, cmd, gs.FORBIDDEN_TAIL.get(cmd)
                                               or user_macros.get(cmd, "")))
        for ln, what in tail["g92_xyz"]:
            add("R17", "line %d: %s redefines the coordinate system" % (ln, what))
        for ln, cmd in tail["tool_change"]:
            add("R17", "line %d: tool change %s (printing T%s)" % (ln, cmd, state.tool))
        for code, detail in tail_warnings(tail):
            add(code, detail)
        if state.e_absolute:
            add("R05", "extrusion mode at the resume line is absolute")
        if state.layer < se["min_resume_layer"]:
            add("R08", "layer %d < %d" % (state.layer, se["min_resume_layer"]))
        near = geometry.path_near_zones(
            p, [(state.pos[0], state.pos[1])], cl["travel_clearance"])
        if near:
            add("R03", "resume point within %.0f mm of %s"
                % (cl["travel_clearance"], ", ".join(near)))
        park = prof.park_xy(p)
        near = geometry.path_near_zones(p, [tuple(park)], cl["travel_clearance"])
        if near:
            add("R03", "park spot %s within %.0f mm of %s"
                % (list(park), cl["travel_clearance"], ", ".join(near)))

    if res is not None:
        mmu = prof.val(p["accessories"].get("mmu", {}))
        if len(res.tools_used) > 1 and not mmu.get("multi_tool_resume", False):
            add("R04", "file uses tools %s" % res.tools_used)
        if len(res.state.objects) > 1:
            add("R16", "%d objects defined; exclusions made in the UI are not in the log"
                % len(res.state.objects))
    if part is not None:
        tall = geometry.too_tall(part, p)
        if tall:
            add("R01", tall)

    say("")
    if part is not None:
        say("-- probe spots (pass-1 travel height %.2f mm) --" % geometry.pass1_height(part, p))
    else:
        say("-- probe spots (no part geometry: pass-1 height unknown) --")
    spots = p.get("probe_spots", [])
    proposed = [_parse_spot(t) for t in args.probe_spot or []]
    good_spots = 0
    reasons = []
    extrapolated = []
    for leaf in spots:
        name = leaf.get("name", "spot")
        if not prof.is_verified(leaf):
            why = prof.unverified_reason(leaf) or "source %s" % leaf.get("source")
            reasons.append("%s: not verified (%s)" % (name, why))
            say("%s: REJECTED not verified (%s)" % (name, why))
            continue
        if part is None:
            reasons.append("%s: no part geometry (the G-code was not read)" % name)
            say("%s: REJECTED no part geometry" % name)
            continue
        v = geometry.check_probe_spot(prof.val(leaf), part, p)
        say("%s: %s %s" % (name, "ok" if v.ok else "REJECTED", "; ".join(v.reasons + v.warnings)))
        if v.ok:
            good_spots += 1
            if v.mesh_extrapolation_mm > 0:
                extrapolated.append((name, v.mesh_extrapolation_mm))
        else:
            reasons.append("%s: %s" % (name, v.reasons[0]))
    # A typed spot is geometry-checked only. It counts toward R02 on that basis;
    # that the head footprint behind the check is unmeasured is R13's job, and
    # R13 blocks motion (docs "Design notes": unmeasured --probe-spot).
    for sp_xy in proposed:
        name = "proposed (%g,%g)" % (sp_xy["x"], sp_xy["y"])
        if part is None:
            reasons.append("%s: no part geometry (the G-code was not read)" % name)
            say("%s: REJECTED no part geometry" % name)
            continue
        v = geometry.check_probe_spot(sp_xy, part, p)
        say("%s: %s %s" % (name, "ok" if v.ok else "REJECTED", "; ".join(v.reasons + v.warnings)))
        if v.ok:
            good_spots += 1
            if v.mesh_extrapolation_mm > 0:
                extrapolated.append((name, v.mesh_extrapolation_mm))
        else:
            reasons.append("%s: %s" % (name, v.reasons[0]))
    if not spots and not proposed:
        say("(none in the profile and none proposed)")
    suggestions = []
    if args.suggest_spots:
        if part is None:
            say("suggested spots: no part geometry (the G-code was not read)")
        else:
            suggestions = geometry.suggest_probe_spots(part, p)
            if suggestions:
                say("suggested nozzle spots, best first (grid search, advice only):")
                for sg, v in suggestions:
                    xo, yo, _ = prof.probe_offsets(p)
                    say("  nozzle X%g Y%g, probe point X%g Y%g, head margin %s mm, mesh "
                        "extrapolation %.1f mm" % (sg["x"], sg["y"], sg["x"] + xo, sg["y"] + yo,
                                                   "n/a" if v.margin_mm is None else "%.1f" % v.margin_mm,
                                                   v.mesh_extrapolation_mm))
            else:
                say("suggested spots: none; no grid position keeps the head clear of the part")
    if extrapolated:
        add("R20", "; ".join("%s is %.1f mm outside the mesh" % (n, d) for n, d in extrapolated)
            + " (the probe works there; mesh error was < 0.05 mm at the live run's rear spot)")
    if good_spots == 0:
        hint = ""
        if suggestions:
            hint = " (candidates exist, check one with --probe-spot X,Y)"
        add("R02", ("; ".join(reasons) if reasons else
                    "the profile has no measured probe spots and none was proposed") + hint)

    if window is not None and part is not None and timed:
        # The head may have stopped anywhere along any move that overlaps the
        # stop window, so the whole path of each such move is tested (not only
        # its end points).
        segs = [(tm.start_xy, tm.end_xy) for tm in timed
                if tm.kind == "move" and tm.t_end >= window.earliest_t and
                tm.t_start <= window.latest_t]
        if not segs:
            add("PIN_AT_BOOT", PIN_UNKNOWN_NOTE)
            say("probe pin at boot: unknown (no move in the stop window)")
        else:
            pin = geometry.check_pin_at_boot_paths(segs, part, p)
            say("probe pin at boot: %s" % ("clear of the part" if pin.ok else pin.reasons[0]))
            if not pin.ok:
                add("PIN_AT_BOOT", "%s, raise Z or move the head off the part before "
                    "FIRMWARE_RESTART" % pin.reasons[0])

    shut = _parse_ts(ev.shutdown_timestamp)
    if now is None:
        now = datetime.datetime.now(datetime.timezone.utc)
    if shut and (now - shut).total_seconds() > R11_HOURS * 3600:
        add("R11", "%.1f h since the shutdown" % ((now - shut).total_seconds() / 3600))

    missing = prof.motion_ready(p)
    if missing:
        say("")
        say("-- profile fields not verified on this machine (R13) --")
        for m in missing:
            say("  " + m)
        add("R13", "%d field(s): %s" % (len(missing), ", ".join(missing)))

    if args.preview:
        _preview(args, p, th, lim, ev, state, choice, records, source_name,
                 findings, say)
    return _finish(findings, say, choice, not_checked)


def _preview(args, p, th, lim, ev, state, choice, records, source_name, findings, say):
    """Write the preview only when nothing is refused (N4). Never overwrites
    an existing file unless --force-preview was given (checked up front)."""
    if rf.refused(findings) or state is None or choice is None or \
            choice.line is None or choice.refusal:
        say("preview not written: the plan is refused, so there is no resume to preview")
        return
    gstate = ev.gcode_state
    offset = gstate.get("homing_position")
    sf = gstate.get("speed_factor")
    ef = gstate.get("extrude_factor")
    overlap_s = choice.overlap_s if choice.overlap_s == choice.overlap_s else 0.0
    overlap_mm = choice.overlap_mm if choice.overlap_mm == choice.overlap_mm else 0.0
    codes = sorted({f.code for f in findings})
    header = resume_file.header_from_records(records())
    try:
        text = resume_file.build(
            state, os.path.basename(source_name), choice.line,
            (t for n, _, t in records() if n >= choice.line),
            overlap_s=overlap_s, overlap_mm=overlap_mm,
            park_xy=prof.park_xy(p), bed_mesh_profile=prof.bed_mesh_profile(p),
            retract_before_travel=th["retract_before_travel"],
            prime_extra=th["prime_extra"],
            gcode_offset_z=offset[2] if isinstance(offset, list) and len(offset) > 2 else None,
            speed_factor=sf * 60.0 * 100.0 if isinstance(sf, float) else None,
            extrude_factor=ef * 100.0 if isinstance(ef, float) else None,
            default_scv=lim.square_corner_velocity,
            default_velocity=lim.max_velocity,
            refusal_codes=codes, open_codes=codes, header_lines=header,
            remaining_fraction=resume_file.remaining_fraction(state, header))
    except resume_file.ResumeRefused as e:
        # S1: a generated line that the allow-list refuses is a hard refusal.
        # R17 is the existing forbidden-command code; the detail says what failed.
        findings.append(rf.Finding("R17", "resume preview refused: %s" % e))
        say("preview not written: %s" % e)
        return
    try:
        _write_preview(args.preview, text, replace=bool(args.force_preview))
    except OSError as e:
        say("preview not written: %s" % (e.strerror or e))
        return
    say("preview written (NOT VERIFIED, do not print): %s" % os.path.basename(args.preview))


def _write_preview(path, text, replace=True):
    """Write path atomically: write a temp file in the same directory, then
    publish it. With replace=True it is os.replace'd over path (a rename never
    writes through a symlink, and a failed write leaves the old file intact and
    no temp file). With replace=False it is hard-linked to path instead, which
    fails with FileExistsError if anything exists there by then, so a file
    created after the pre-run check is never replaced. The temp file is created
    with mode 0666 so the kernel applies the umask (no process-wide umask
    change)."""
    directory = os.path.dirname(os.path.abspath(path))
    tmp = os.path.join(directory, ".print-rescue-%s.tmp" % secrets.token_hex(8))
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                 0o666)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
        if replace:
            if os.path.exists(path):
                os.chmod(tmp, stat.S_IMODE(os.stat(path).st_mode))
            os.replace(tmp, path)
        else:
            try:
                os.link(tmp, path)
            except FileExistsError:
                raise
            except OSError:
                # No hard links here (for example a FAT card): fall back to the
                # pre-run check alone.
                if os.path.lexists(path):
                    raise FileExistsError(errno.EEXIST, "file exists", path)
                os.replace(tmp, path)
                return
            os.unlink(tmp)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _finish(findings, say, choice, not_checked=()) -> int:
    """The verdict. Exit 0 needs a chosen line and no refusal of any kind."""
    chosen = choice is not None and choice.line is not None and not choice.refusal
    if not chosen and not rf.refused(findings):
        findings.append(rf.Finding("R18", "no line was chosen to resume from"))
    say("")
    say("-- findings --")
    hard = [f for f in findings if f.hard]
    warn = [f for f in findings if not f.hard]
    for f in hard + warn:
        say(rf.console_line(f))
    if not findings:
        say("none")
    say("")
    if rf.refused(findings):
        say("RESULT: REFUSED")
        return EXIT_REFUSED
    if any(f.motion_only for f in findings):
        say("RESULT: resumable on paper; motion blocked until R13 is cleared")
        return EXIT_MOTION_BLOCKED
    if not_checked:
        say("RESULT: resumable on paper; evidence not supplied (%d NOT CHECKED line(s)), "
            "so exit 3: supply --history, --queue and --printer-cfg, and a log that "
            "holds the 'Virtual sdcard' text" % len(not_checked))
        return EXIT_MOTION_BLOCKED
    say("RESULT: resumable (confirm every warning before any motion)")
    return EXIT_RESUMABLE


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--gcode", required=True)
    ap.add_argument("--gcode-map", help=argparse.SUPPRESS)  # test fixtures only, KI-02
    ap.add_argument("--log", required=True, help="klippy.log or docker stdout")
    ap.add_argument("--profile", required=True)
    ap.add_argument("--printer-cfg")
    ap.add_argument("--history", help="Moonraker history JSON")
    ap.add_argument("--queue", help="Moonraker job_queue/status JSON")
    ap.add_argument("--kernel-log", help="kernel log lines (journalctl -k / dmesg)")
    ap.add_argument("--line", type=int, help="manual resume line (checked against the window)")
    ap.add_argument("--preview", help="write the NOT VERIFIED resume preview here (never when refused)")
    ap.add_argument("--force-preview", action="store_true",
                    help="overwrite an existing --preview file")
    ap.add_argument("--now", help="current time, ISO 8601 UTC (default: now)")
    ap.add_argument("--max-overlap-s", type=float,
                    help="overlap budget in seconds (default: stop_estimation.max_overlap_s); "
                         "an overlap above it is a warning with the extra material, not a refusal")
    ap.add_argument("--probe-spot", action="append", metavar="X,Y",
                    help="nozzle position of a probe spot to check for this part (repeatable)")
    ap.add_argument("--suggest-spots", action="store_true",
                    help="search mode: list the best candidate probe spots for this part")
    return ap


def main(argv=None) -> int:
    try:
        args = build_parser().parse_args(argv)
    except SystemExit as e:
        return EXIT_USAGE if e.code else 0
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
