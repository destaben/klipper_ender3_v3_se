#!/usr/bin/env python3
"""print-rescue phase 1: read-only resume planner.

    python3 plan.py --gcode FILE --log KLIPPY_LOG --profile PROFILE.json
                    [--printer-cfg PATH] [--history JSON] [--queue JSON]
                    [--kernel-log PATH] [--line N]
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
     (R13), so motion is blocked; the report and preview are still produced
  2  refused: a hard refusal, including R18 when no resume line can be chosen
     (no 'Exiting SD card print' position, no usable Stats lines, no line)
  1  usage or input error (bad arguments, unreadable or malformed input, an
     invalid margin_s); nothing was planned
Standard library only.
"""

import argparse
import datetime
import json
import os
import stat
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import gcode_scan as gs  # noqa: E402
import geometry  # noqa: E402
import planner  # noqa: E402
import machine_profile as prof  # noqa: E402
import refusals as rf  # noqa: E402
import resume_file  # noqa: E402
import stop_point as sp  # noqa: E402

R11_HOURS = 2.0
PIN_SAMPLES = 25
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


def _load_json(path, what):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError, RecursionError) as e:
        raise InputError("%s %s: %s" % (what, os.path.basename(path), e))


def _read_text(path, what):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except OSError as e:
        raise InputError("%s %s: %s" % (what, os.path.basename(path), e.strerror or e))


def _is_number(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


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
    for job in candidates:
        if job.get("status") in ("klippy_shutdown", "error", "cancelled"):
            return job
    return candidates[0] if candidates else None


def _queued(data):
    if not isinstance(data, dict):
        raise InputError("queue: not a JSON object")
    result = data.get("result", data)
    if not isinstance(result, dict):
        raise InputError("queue: 'result' is not an object")
    jobs = result.get("queued_jobs", [])
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
    return d.replace(tzinfo=datetime.timezone.utc)


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
    except (ValueError, ArithmeticError, OSError, LookupError, RecursionError) as e:
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
                add("R07", "modified time differs from the history")
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
    if args.printer_cfg:
        cfg = prof.parse_printer_cfg(_read_text(args.printer_cfg, "printer.cfg"))
        for mm in prof.fingerprint_mismatches(p, cfg):
            add("R14", mm)

    if ev.shutdown_reason is None:
        add("R09", "no shutdown found in the log")
    elif not ev.comm_loss:
        add("R09", "shutdown reason: %s" % ev.shutdown_reason)

    say("== print-rescue plan (read-only, phase 1) ==")
    for item in not_checked:
        say("NOT CHECKED: %s" % item)
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
        res = gs.scan(records(), lim, until_byte=ev.exit_position, keep_layers=2)
        timed = planner.plan(res.moves, lim)
        last_line = gs.last_processed_line(res, ev.exit_position)
        if res.total.empty:
            add("R18", "no extrusion before the exit position: the part box is unknown")
        else:
            part = geometry.Part.from_bbox(res.total)
    elif ev.exit_position is None:
        add("R10", "no 'Exiting SD card print' position in the log")
        add("R18", "the last dispatched line is unknown (no exit position)")

    if res is not None:
        say("last dispatched line: %s (exit position %d)" % (last_line, ev.exit_position))
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
        choice = sp.choose_resume(window, timed, layer_start, se["margin_s"],
                                  se["snap_window_s"], se["max_overlap_s"],
                                  manual_line=args.line)
        if choice.refusal:
            add("R12", choice.refusal)
    elif args.line is not None:
        # N3: a manual line is only ever checked against a window.
        add("R18", "manual --line %d refused: there is no stop window to check it against"
            % args.line)

    if choice is not None and choice.line is not None and res is not None:
        state = gs.state_at(records(), choice.line, lim)
        say("")
        say("-- resume candidate (%s) --"
            % ("refused, see R12" if choice.refusal else "chosen"))
        say("resume line %d at X%.3f Y%.3f Z%.3f (layer %d, Z %.2f)%s%s"
            % (choice.line, state.pos[0], state.pos[1], state.pos[2], state.layer,
               state.layer_z or 0.0, " snapped to travel/unretract" if choice.snapped else "",
               " (manual)" if choice.manual else ""))
        if choice.overlap_s == choice.overlap_s:
            say("overlap to the latest possible stop: %.2f s, %.1f mm"
                % (choice.overlap_s, choice.overlap_mm))
        tail = gs.tail_findings(res, choice.line, state.tool)
        for ln, cmd in tail["forbidden"]:
            add("R17", "line %d: %s (%s)" % (ln, cmd, gs.FORBIDDEN_TAIL.get(cmd, "")))
        for ln, cmd in tail["tool_change"]:
            add("R17", "line %d: tool change %s (printing T%s)" % (ln, cmd, state.tool))
        for ln, _ in tail["m82"][:1]:
            add("R05", "line %d" % ln)
        for ln, why in (tail["vase"] + tail["non_monotonic_z"])[:1]:
            add("R06", "line %d: %s" % (ln, why))
        if state.e_absolute:
            add("R05", "extrusion mode at the resume line is absolute")
        if state.layer < se["min_resume_layer"]:
            add("R08", "layer %d < %d" % (state.layer, se["min_resume_layer"]))
        near = geometry.path_near_zones(
            p, [(state.pos[0], state.pos[1])], cl["travel_clearance"])
        if near:
            add("R03", "resume point within %.0f mm of %s"
                % (cl["travel_clearance"], ", ".join(near)))

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
    good_spots = 0
    reasons = []
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
        say("%s: %s %s" % (name, "ok" if v.ok else "REJECTED", "; ".join(v.reasons)))
        if v.ok:
            good_spots += 1
        else:
            reasons.append("%s: %s" % (name, v.reasons[0]))
    if not spots:
        say("(none in the profile)")
    if good_spots == 0:
        add("R02", "; ".join(reasons) if reasons else "the profile has no measured probe spots")

    if window is not None and part is not None and timed:
        pts = [tm.end_xy for tm in timed
               if window.earliest_t <= tm.t_end <= window.latest_t and tm.kind == "move"]
        step = max(1, len(pts) // PIN_SAMPLES)
        pts = pts[::step] or [timed[-1].end_xy]
        pin = geometry.check_pin_at_boot(pts, part, p)
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
    return _finish(findings, say, choice)


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
            refusal_codes=codes, open_codes=codes)
    except resume_file.ResumeRefused as e:
        # S1: a generated line that the allow-list refuses is a hard refusal.
        # R17 is the existing forbidden-command code; the detail says what failed.
        findings.append(rf.Finding("R17", "resume preview refused: %s" % e))
        say("preview not written: %s" % e)
        return
    try:
        _write_preview(args.preview, text)
    except OSError as e:
        say("preview not written: %s" % (e.strerror or e))
        return
    say("preview written (NOT VERIFIED, do not print): %s" % os.path.basename(args.preview))


def _write_preview(path, text):
    """Replace path with text atomically: write a temp file in the same
    directory, then os.replace it over path. A rename never writes through a
    symlink, and a failed write leaves the old file intact and no temp file."""
    directory = os.path.dirname(os.path.abspath(path))
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".print-rescue-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
        if os.path.exists(path):
            mode = stat.S_IMODE(os.stat(path).st_mode)
        else:
            umask = os.umask(0)
            os.umask(umask)
            mode = 0o666 & ~umask
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _finish(findings, say, choice) -> int:
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
    say("RESULT: resumable (confirm every warning before any motion)")
    return EXIT_RESUMABLE


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--gcode", required=True)
    ap.add_argument("--gcode-map", help="excerpt map (test fixtures only)")
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
    return ap


def main(argv=None) -> int:
    try:
        args = build_parser().parse_args(argv)
    except SystemExit as e:
        return EXIT_USAGE if e.code else 0
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
