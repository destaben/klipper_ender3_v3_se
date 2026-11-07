# Print rescue: resuming a print after Klipper shut down

When Klipper shuts down in the middle of a print (most often `Lost
communication with MCU`), the part is still on the bed, the heaters are off and
the toolhead no longer knows where it is. This page describes how to continue
such a print safely, and the tool in `print-rescue/` that plans it.

The procedure was written after the [2026-11-07 incident](print-rescue-incident-2026-11-07.md),
where a hand-built resume worked but several steps were riskier than they had
to be.

Items marked **TO VERIFY** are claims that have not been checked on the
hardware or in the Klipper fork source. They are listed again in
[To verify](#to-verify-against-the-klipper-fork).

## Scope today (phase 1)

`print-rescue/plan.py` is a **read-only planner** (read-only towards the printer: it sends nothing
to it; its only write is the optional `--preview` file). It reads the print file, the
Klippy log, a machine profile and, optionally, the job history, the job queue,
`printer.cfg` and the kernel log, and prints:

- the window in which the toolhead physically stopped, mapped to G-code lines;
- the resume line, XY, Z and the planned overlap (seconds and mm of path);
- the part's bounding box and height;
- the verdict for every probe spot in the profile;
- whether the CR Touch pin would land on the part at boot;
- every refusal and warning as a console-ready line;
- optionally a resume G-code **preview** (`--preview`), labelled NOT VERIFIED.
  It is written only when nothing is refused.

It never talks to the network, never sends G-code and never moves anything.
Uploading and starting the resume, and the interactive steps, are later
phases (see [Phases](#phases)). Until then the procedure below is manual.

There is no reliable database of printer geometry (head footprint, probe
offsets, where a cutter lever sits), so every safety-relevant value is
measured on this machine and carries its provenance in the profile.

## Manual procedure (corrected)

Rules that apply to every step:

- **Abort = emergency stop through Moonraker**:
  `curl -s -X POST http://localhost/printer/emergency_stop` (M112). Never a
  macro abort: `CANCEL_PRINT` and friends run end-of-print G-code that moves.
  **TO VERIFY:** what state Klipper is left in after M112, and whether
  `FIRMWARE_RESTART` is then the only way out.
- **Confirm before every move.** One command at a time, read the result.
- **Short moves near the part**: at most 5 mm per command, each followed by
  `M400`, then look.
- After any errored, timed-out or cancelled command, **re-read**
  `toolhead.position`, `toolhead.homed_axes` and `probe.last_z_result` before
  the next move. A cancel in the UI does not stop a `PROBE` that is already
  running.

Steps:

1. **Do not restart anything yet.** Save the evidence first: the Klippy log
   (`docker logs --timestamps klipper`; pass `--since`/`--until` with a `Z`
   suffix, without it they are local time), the kernel log
   (`journalctl -k`), Moonraker's job history, and a copy of the print file.
2. **Pause Moonraker's job queue** and empty it
   (`POST /server/job_queue/pause`, check `/server/job_queue/status`).
   **TO VERIFY:** that `[job_queue] load_on_startup: True` starts the next
   queued job on every Klippy `ready` on this version, as assumed here. Every
   job starts with `START_PRINT`, which homes all axes into the part. A Prusa
   Connect start while the printer looks "off" is queued too (klipper-connect
   patch), so make sure nothing is pending there.
3. **Run `plan.py`** (below) on the saved evidence. Stop on any non-zero exit:
   2 (refused), 3 (motion blocked by an unverified profile field) or 1 (input
   rejected). Only exit 0 may go on, and then only after every warning is
   confirmed. Exit 0 is not yet a complete safety verdict (see
   [Known issues](#known-issues-and-pending-work-end-of-phase-1)).
4. **Check the probe pin at boot.** The CR Touch sits at nozzle + (-23, -14.5).
   If `plan.py` reports `PIN_AT_BOOT`, the pin is predicted to deploy onto the
   part when it self-tests. **TO VERIFY:** when the pin deploys at
   `FIRMWARE_RESTART` (see [To verify](#to-verify-against-the-klipper-fork)).
   Make that point clear before `FIRMWARE_RESTART`.
5. **`FIRMWARE_RESTART`** only with the queue paused and empty. After it no
   axis is homed.
6. **Never `G28 Z`**: Z is probed at the `safe_z_home` point (134, 123), which
   is on most parts. **Never a `G28` without axis letters.** Homing X and Y is
   only allowed as step 8 describes.
7. **Z is unknown.** The user measures the nozzle-to-part gap (feeler gauge or
   paper) and confirms it. Declare the position from that measurement, then
   only make **relative upward** moves until Z has been probed. The Z axis
   uses T8 leadscrews (`rotation_distance: 8`), which can back-drive under the
   gantry's weight while the motors are off: re-measure the gap right before
   the first move. The preview never declares Z and never moves Z down before
   the gate in step 9; the operator does the declaration by hand.
8. **Lift to the pass-1 height** (`plan.py` prints it: part top + travel
   clearance + whatever hangs below the nozzle). Only above it, home X and Y,
   **one axis per command**: `G28 X`, then `G28 Y`. Before each homing move,
   check on paper that its swept path (at the lifted height) clears the part
   box and the probe pin. **TO VERIFY:** that `safe_z_home` lifts Z by `z_hop`
   (10 mm) first when Z is not homed, also for an X or Y home. Make sure that
   lift has room.
9. **Re-reference Z at measured probe spots only.** A spot is accepted only
   if it is verified (source `measured`, with `measured_on` and `method`), the
   nozzle is inside the envelope and at most X=205 (cutter lever), the probe
   point is on the meshed area and at least 5 mm from the part, and the head
   footprint at probe height clears the part box + 3 mm (`plan.py` checks all
   of this). Probe **two** spots to detect a tilted gantry (one Z leadscrew
   turned while the motors were off). After a `PROBE` the next move is **up**.
   The preview's lowering to the resume height comes only after this step.
10. **Thermal and drip rules.** The preview's order is:
    1. retract to `retract_before_travel` (2.5 mm) before any move;
    2. lift relatively (`G91`) by 3 mm;
    3. travel at that height to the off-part park spot (profile `park.xy`);
    4. only there: load the bed mesh, heat the bed and the nozzle to print
       temperature (`M190`, `M109`);
    5. travel over the part to the resume XY at the lifted height, filament
       retracted;
    6. stop at the gate (`; GATE:` line): Z is not re-referenced;
    7. after step 9: lower to the resume Z, prime, continue.

    Never dwell hot over the part for longer than `max_hot_dwell_over_part_s`
    (30 s).

    **Open conflict, not decided here (for the council):** the drip rules also
    say that over the part the nozzle stays at standby temperature (PLA 150 C),
    and that heating and priming happen off the part. A resume on the part at
    print temperature cannot satisfy all three. The preview heats off the part
    and therefore travels over the part at print temperature, retracted and
    without dwelling; it primes after lowering, on the part. The council must
    choose before phase 2.
11. **Restore the print state** before resuming. The preview restores the bed
    mesh (`BED_MESH_PROFILE LOAD=<bed_mesh_profile>`, `default` in the shipped
    profile), the G-code Z offset (baby-steps; the shutdown dump's
    `homing_position` Z), fan, pressure advance, acceleration and
    temperatures.
12. **Paper test** at the resume point before the first extrusion.
13. **Overlap, never a gap**: resume at the earliest possible stop minus a
    margin. A few seconds of re-printed path is a small blemish; a gap in a
    wall is a weak spot.

## Stop analysis

Klippy logs a `Stats` line every second. The main MCU's `send_seq`,
`receive_seq` and `bytes_read`, and the toolhead's `print_time` (end of the
motion handed to the step generator), `buffer_time` (`print_time` minus the
MCU's current time) and the SD card's `sd_pos` (byte of the line being
dispatched) are what the analysis needs.

When the USB link stalls, `receive_seq` freezes while `send_seq` still
advances and `bytes_read` stops. From the last sample where `receive_seq`
advanced (`P_good`, `B_good`, `sd_good`) and the first `Stats` line after the
shutdown (`P_end`):

- the MCU executed motion at least up to `print_time = P_good - B_good` (its
  own clock at that sample) and at most up to `P_good + 1 s` (data sent before
  the next sample may have arrived);
- that window is mapped onto the planned timeline of the file by
  `planner.py`, a port of Klipper's lookahead arithmetic.

Two anchors tie `print_time` to the file:

| Anchor | Window in planned time | Notes |
|--------|------------------------|-------|
| `sd` (used when present) | `[t(S) - L(S) - B_good, t(S) + d(S) + 1 s]`, S = line at `sd_good` | `L` = lookahead bound: moves dispatched before S but still in Klipper's lookahead queue (up to `LOOKAHEAD_FLUSH_TIME` = 0.25 s of minimum move time, plus the move a lazy flush keeps back). `d(S)`: `print_time` may already include S itself. |
| `end` (the plan's formula; fallback and cross-check) | walk back from the end of the last dispatched line by `[P_end - min(P_end, P_good + 1), P_end - (P_good - B_good)]`, plus the lookahead bound at the end on the early side | Moves still in the lookahead queue at shutdown were never counted in `P_end`. When the last line is a long move this bound is large. |

With both anchors the **stop window is their conservative union**: the
earliest possible stop is the earlier of the two earliest stops, and the latest
possible stop is the later of the two latest. With no `sd_pos`, the end anchor
alone is used.

`plan.py` compares the two anchors' window **endpoints** (earliest with
earliest, latest with latest). If either differs by more than 1 s it warns
(R10). It also reports how steady `print_time - t(line at sd_pos)` was over
the last good samples; more than 0.5 s of spread means the timing model does
not match this log (R10).

Resume policy (council):

- the target is the earliest possible stop minus `margin_s` (0.5 s), and the
  resume line must start at or before the target;
- snap back to a travel or unretract within `snap_window_s` (1 s) if the
  overlap budget allows;
- never before the start of the cut layer. If the window starts before the
  layer, refuse. If the layer starts too close to the earliest stop for any of
  its lines to meet the margin, refuse as well (R12); the margin is never
  quietly reduced;
- refuse (R12) if the overlap from the resume point to the latest possible
  stop exceeds `max_overlap_s` (3 s);
- a manual `--line` needs the window (refused with R18 without one). It is
  refused (R12) if it starts after the earliest possible stop (a gap) or
  overlaps too much. The margin is not applied to a manual line.

### Planner fidelity

`planner.py` reproduces Klipper's `Move`/`LookAheadQueue.flush` arithmetic:
cruise speed from F x M220 and `max_velocity`, Z and extrude-only limits,
junction speed from `square_corner_velocity` (junction deviation, centripetal
half-move limit, extruder `instant_corner_velocity`), `minimum_cruise_ratio`
(`ACCEL_TO_DECEL` is mapped to it as recent Klipper does), and arcs split like
`gcode_arcs` (resolution 1.0). Approximations:

- the stream is planned as one continuous lookahead; Klipper's forced flushes
  when its buffer runs low (each one a full stop) are not modelled. In the
  incident log Klipper's `print_time` advanced 1.7 % faster than the plan over
  the cut layer; near single long moves it ran up to about 0.9 s ahead;
- `G4`, `M400`, `M109`, `M190` end a planning segment; temperature waits count
  as zero time;
- input shaping and pressure advance do not change Klipper's move times and
  are not modelled.

Errors of this kind make Klipper slower than the plan, which moves the
computed window earlier in the file, toward more overlap.

## Resume file (preview)

`print-rescue/resume_file.py` builds the preview. Its generated part is checked
against an **allow-list** of exact command names and parameters
(`check_generated`, the `ALLOWED` table; no wildcards): a command that is not
listed, such as `G28`, `G29`, `G30`, `G32`, `HOME`, `START_PRINT`,
`FORCE_MOVE`, `SET_KINEMATIC_POSITION` or `PROBE`, is refused.
The check runs over the generated text split into lines, so a newline inside a
value (such as a source name) cannot hide a command. Any control or non-ASCII
character in the generated part is refused as well. Before the gate, the only Z
motion allowed is a relative lift up. A refused generated part is a hard
refusal (R17) with exit 2 and no preview. The original file's lines after the
resume point are copied as they are; R17 refuses the plan if they contain a
forbidden command.

The preview is never written when the plan is refused. An existing file is not
overwritten without `--force-preview`. `--preview` is also refused (exit 1,
before anything is read) when it names an input file, a symbolic link, or a hard
link to an input. With `--force-preview` an existing regular file is replaced by
a rename from a temp file in the same directory, so a failed write leaves the old
file in place and never a truncated preview. Its first line says NOT VERIFIED and
lists the findings that were open when it was made.

## Refusal catalogue

Hard refusals stop the rescue. R13 blocks motion only: plan mode still runs.
Warnings need a human confirmation before any motion (later phases).
Each finding prints as one ASCII line, safe inside
`RESPOND TYPE=error MSG="..."` (quotes and semicolons replaced).

| Code | Kind | Meaning |
|------|------|---------|
| R01 | refused | Part too tall: part top + `min_lift` > `z_max` - `z_top_margin`. |
| R02 | refused | No valid **verified** probe spot (each spot's reasons are listed), or no part geometry. |
| R03 | warning | Resume point within `travel_clearance` of a no-go zone. The park spot and the approach moves are not checked yet (KI-11). |
| R04 | warning | Multi-tool print (or lane mismatch) and `multi_tool_resume` is false. |
| R05 | warning | Absolute extrusion (M82) at the resume line or in the tail. |
| R06 | warning | Vase mode (extrusion moves that change Z) or non-monotonic layer Z. |
| R07 | refused | G-code file missing or not a regular file, or name/size/mtime differ from the job history. |
| R08 | warning | Cut before `min_resume_layer`. |
| R09 | refused | The shutdown was not an MCU communication loss. |
| R10 | warning | Insufficient log evidence, manual line needed (anchor endpoints more than 1 s apart, planner timing off by more than 0.5 s, no clean freeze). |
| R11 | warning | Long time since the shutdown (part may have cooled loose). |
| R12 | refused | The stop window needs more overlap than `max_overlap_s`, starts before the cut layer, has no line in the cut layer that meets the margin, or a manual line would leave a gap or overlap too much. |
| R13 | refused (motion) | A safety-critical profile field is not verified on this machine (source is not `measured` with `measured_on` and `method`). Exit 3 when it is the only refusal. |
| R14 | refused | The profile's `config_fingerprint` does not match `printer.cfg`. |
| R15 | refused | Moonraker's job queue is not empty (a queued job starts with `START_PRINT` on the next ready; **TO VERIFY**). |
| R16 | warning | Unknown previously excluded objects: exclusions made in the UI are not in the log (several objects). |
| R17 | refused | Forbidden command in the resumed tail: any `G28` form or another command in `FORBIDDEN_TAIL` in `print-rescue/gcode_scan.py` (homing, probing, print start, MMU homing macros, stop/restart, pause/cancel and shutdown commands; `G92` on X/Y/Z; exact command names after normalising spaces and `N` line numbers; other macros are not matched, KI-08/KI-17), or a change to another tool. |
| R18 | refused | No usable stop evidence, so no resume line can be chosen: no `Exiting SD card print` position, no usable `Stats` lines, no planned motion before the exit position, or a manual `--line` without a window. Also used when no line was chosen at all. |
| PIN_AT_BOOT | warning | The probe pin self-test at Klippy start would land on the part (**TO VERIFY**: when the pin deploys). |

## Profile

`print-rescue/profiles/creality-ender3-v3-se.json` is versioned
(`"schema": "print-rescue-profile", "version": 1`). Safety-critical leaves
carry `{"value", "unit", "source", "safety_critical": true}` and optionally
`method`, `measured_on`, `note`; `source` is one of `measured`, `config`,
`user`, `vendor`, `community`, `default`. Non-safety values are plain.

A safety-critical value is **verified** only when it is `source: measured`
and carries a `measured_on` date (`YYYY-MM-DD`) and a `method`. Values from
`config` (taken from `printer.cfg` and compared by R14), values stated by a
person (`user`) and `vendor`, `community` or `default` values are not
verified. Each unverified one is listed as R13 and blocks motion.

The shipped profile takes offsets, limits and `safe_z_home` from
`config/printer.cfg` (source `config`, compared with the live file as the
`config_fingerprint`, R14), the cutter limit X=205 from the user, and leaves
the head footprint, `z_top_margin` and `park.xy` as unmeasured placeholders
and the probe spot list empty. It therefore **fails closed** (R13 and R02)
until phase 3 measures them. The cutter is also a no-go zone (`frame: gantry`,
X >= 205) that only `_SP_TIP_FORM` may enter: `config/macros/sp_mmu.cfg` cuts
at X=225 on purpose.

Two further fields are used by the preview:

- `park`: the off-part spot where the preview heats and primes (`park.xy`,
  placeholder `[15, 205]`, unmeasured). It must be clear of the part, the probe
  pin and the cutter.
- `bed_mesh_profile`: the name for `BED_MESH_PROFILE LOAD=` (`default`). It
  must match the `[bed_mesh <name>]` section in `printer.cfg`; R14 does not
  compare it yet.

The profile is validated before anything is planned: section types, finite
numbers, ranges (for example `margin_s`, `snap_window_s` and `max_overlap_s`
must be finite and not negative) and required keys. A profile without
`envelope.probe_area` is invalid. A malformed profile exits 1 with the list
of problems. The spot check also refuses any spot when no probe area is
given, so the check is never skipped.

`test_machine_profile.py` checks that `config/printer.cfg` still matches the
fingerprint, so a change to probe offsets, axis limits, `safe_z_home` or the
SAVE_CONFIG `z_offset` must come with a profile update.

## Running plan.py

Standard library only, no network:

```bash
python3 print-rescue/plan.py \
  --gcode saved/print.gcode --log saved/klippy.log \
  --profile print-rescue/profiles/creality-ender3-v3-se.json \
  --printer-cfg config/printer.cfg --history saved/history.json \
  --queue saved/job_queue.json --kernel-log saved/kernel.log \
  --preview saved/resume_preview.gcode
```

- `--log`: Klippy log, either `docker logs --timestamps klipper` output or a
  plain `klippy.log`.
- `--history`, `--queue`: saved JSON of `/server/history/list` and
  `/server/job_queue/status`.
- `--line N`: manual resume line. It is checked against the window and refused
  without one.
- `--preview FILE`: write the NOT VERIFIED preview, only when nothing is
  refused. `--force-preview` is needed to replace an existing file, and is a
  usage error (exit 1) without `--preview`. FILE is refused if it is an input
  (or a symlink or hard link to one), and a symlink is refused even with
  `--force-preview`. A preview that is not written does not change the exit
  code (KI-03).
- `--gcode-map MAP` exists only for the test fixture excerpts (KI-02).
- `--now`: the current time for R11 (default: now). It must be ISO 8601 UTC.

Exit codes:

| Exit | Meaning |
|------|---------|
| 0 | Resumable: no refusal and every safety-critical profile field verified. Confirm every warning before any motion. Any `NOT CHECKED:` line means that evidence was not supplied and the operator must check it by hand (KI-04). |
| 3 | Resumable on paper only: the only refusal is R13 (a profile field is not verified). Motion is blocked; the report and the preview are still produced. |
| 2 | Refused: at least one hard refusal (R01-R18 except R13 alone). Never 0 without a chosen resume line. |
| 1 | Usage or input error (bad arguments, unreadable or malformed input, an invalid `margin_s`). Nothing was planned. |

On the Pi, run it under `nice -n 19` while Klipper is connected. A full 8 MB
file takes about 35 s.

Unit tests (also in CI, `.github/workflows/print-rescue-test.yml`):

```bash
python3 -m unittest discover -s print-rescue -v
```

## Phases

| Phase | Content |
|-------|---------|
| P1 (code complete; review pass open, see KI list) | Read-only planner, resume preview, refusal catalogue, profile with provenance, incident fixtures. Pending items: [Known issues](#known-issues-and-pending-work-end-of-phase-1). |
| P2 | Resume builder that uploads and starts the resume file; pauses the job queue; a `START_PRINT` lock while a rescue is active. Starts only after the P2-gate items are closed ([Known issues](#known-issues-and-pending-work-end-of-phase-1)). |
| P3 | Cold, empty-bed calibration: envelope corners; cutter approach in 0.5 mm steps to measure `x_max_safe`; head footprint tiers; probe spots; re-reference accuracy against homed truth (the user's dry-run idea: run the whole procedure on an empty bed and compare with a real `G28`). |
| P4 | Interactive runner: confirm before every move, short moves, emergency stop, state re-read after every error. Gantry-tilt check from two probe points. |
| P5 | Recorder/witness (periodic position and state snapshots during prints), start-of-print check. |
| P6 | Moonraker component. |

## Hardware validation order

Do these on a cold, empty bed unless noted. Each step must pass before the
next one.

| # | Check | Pass | Fail |
|---|-------|------|------|
| 1 | `plan.py` on the incident fixtures and on a new capture | Same numbers as the tests; every input accepted | Any difference: stop |
| 2 | Does the probe pin deploy on `FIRMWARE_RESTART` (nozzle at Z 10, paper under the pin)? | Behaviour observed and documented | Unclear: keep `PIN_AT_BOOT` as a pre-check |
| 3 | `SET_KINEMATIC_POSITION` and `toolhead.homed_axes` on this Klipper fork | Only the intended axes are marked homed | Other axes marked: procedure step 7 must change |
| 4 | `G28 X`, then `G28 Y`, with Z not homed | Lifts by `z_hop` (10 mm) first if that is how the fork behaves, then homes X and Y | Moves in another way: stop |
| 5 | Cutter approach: X in 0.5 mm steps toward 205 with the servo stop up and down | No lever contact below 205 + 5 mm margin; contact point recorded | Contact earlier: lower `x_max_safe` |
| 6 | Head footprint tiers measured (calipers/blocks at the probe height) | Profile tiers updated, source `measured` | - |
| 7 | Probe spots: `PROBE_ACCURACY` at each spot | Range <= 0.02 mm | Larger: reject the spot |
| 8 | Re-reference dry run with a gauge block as the "part" | Re-referenced Z within 0.05 mm of a homed Z; X/Y exact after `G28 X` and `G28 Y` | Larger: stop |
| 9 | Two-point tilt detection with one leadscrew turned by a known amount | Tilt detected | Not detected: the procedure cannot detect tilt |
| 10 | Sacrificial print, simulated stop (unplug USB), full procedure | Resume with overlap and no gap | Anything else: back to the failing step |
| 11 | Park spot, heat and drip rules (step 10) | Park spot measured clear; the open conflict resolved by the council | - |

## Known issues and pending work (end of phase 1)

- **KI-01**: phase 1 is a read-only planner. Phases 2+ (motion, upload/start)
  are not implemented and must not be started before the hardware validation
  order above has been followed. No motion code has ever been run on hardware.
- **KI-02**: `--gcode-map` exists only for the test fixture excerpts.
- **KI-03**: a preview that is not written does not change the exit code.
- **KI-04**: the evidence inputs `--history` (R07), `--queue` (R15) and
  `--printer-cfg` (R14) are optional. When one is missing the plan prints
  `NOT CHECKED: ...` but does not refuse. P2 must require all three before it
  acts on an exit code.
- **KI-08**: the R17 tail check matches command names exactly (the list is in
  `print-rescue/gcode_scan.py`, including the MMU homing macros). A macro
  that homes but is not listed is not caught. `G92` on X, Y or Z in the tail
  is refused (R17). P2 must check the tail against
  the macros of the live configuration before it uploads anything.
- **KI-09**: `safety_critical` in the profile is a self-declared flag, so
  clearing it removes a field from R13. The profile is reviewed in git; P2
  should derive the critical set from code instead.
- **KI-10**: an object name containing a space in `EXCLUDE_OBJECT_DEFINE` is
  refused (R17, exit 2, no preview). Klipper splits parameters on whitespace,
  so such a line is not valid for Klipper either; the refusal fails closed.
- **KI-11**: the park spot and the approach moves are not checked against the
  no-go zones (only the resume point is, R03).
- **KI-12**: `--line` (manual resume line) is accepted when it starts at or
  before the earliest possible stop, without the safety margin the automatic
  choice keeps. It is an explicit operator override and still has to fit the
  overlap budget (R12); a test pins this behaviour.
- **KI-13**: the preview preamble always sets `G90`. A tail that resumes in
  relative mode (`G91`) is not translated, and `G91` in the tail is not
  refused. P2 must emit the mode active at the resume line before it runs a
  preview.
- **KI-14**: the `GATE_MARK` line in the preview is only a comment. It does
  not pause anything by itself; the next line, `GATE_STOP`, is deliberately
  not a valid command, so Klipper stops the SD print there with "Unknown
  command" until the operator removes it after step 9. P2 must implement the real gate (re-reference on two
  verified probe spots) in the runner; R02 today only requires at least one
  verified spot.
- **KI-15**: a last dispatched line that is a dwell (`G4`, `M109`) has no
  end position in the planner and can raise R10 falsely (a warning, fails
  safe).
- **KI-16**: a non-ASCII source file name is refused through the R17 path;
  the code is misleading but the refusal fails closed.
- **KI-17**: the R17 tail check is a deny-list (it includes print-ending and
  pausing commands such as `CANCEL_PRINT`, `PAUSE`, `M600` and
  `PRINTER_SHUTDOWN`; `END_PRINT` is accepted only as the end of the file, and
  refused when extrusion follows it), and the copied tail is not run
  through the allow-list. A harmful command that is not in the list (and not
  normalised to a listed name) is copied into the preview. P2 must check the
  tail with an allow-list or the live macro set before upload.
- **KI-18**: `--log` and `--gcode-map` are read whole into memory, and
  `--preview --force-preview` may replace any regular file the user names that
  is not an input. Both are explicit operator inputs; no size limit is applied.
- **KI-19**: the preview lifts by a fixed relative amount and travels over the
  part at that height. The planner computes the safe pass-1 height but does not
  pass it to the preview, and Z is unknown before re-referencing. The preview
  must not be run (the gate stops it); P2 must decide how Z is known and use
  the computed height.
- **KI-20**: smaller gaps, all warnings or phase-2 items: `motion_ready` checks
  only profile leaves (plain-number safety values do not reach R13); G91 and
  non-G17 arcs in the tail are not refused; `M84`/`M18`, `BLTOUCH_DEBUG`,
  `M290` and `SET_GCODE_OFFSET` are not on the tail deny-list; the pin-at-boot
  check samples about 25 move endpoints; `--line` is not checked against the
  cut layer; a missing `size` or `modified` in the history skips R07 silently.
- The review pass over the planner (findings S1, S2, N1-N6, N8, N11, N14,
  N17-N20) was interrupted before it was recorded as complete. The IDs are
  cited in code comments; treat any without a matching test as open.
- Every item under "To verify against the Klipper fork" below is open.

## To verify against the Klipper fork

These behaviours matter for P2-P4 and were not checked against the source of
the Klipper fork in the `klipper` image:

- **TO VERIFY (pin at boot):** whether `[bltouch]` deploys the pin on Klippy
  connect, and when. The CR Touch's own power-on self-test only runs when the
  probe is powered. The `PIN_AT_BOOT` check assumes it deploys at the nozzle +
  offset position.
- **TO VERIFY (job queue):** whether `[job_queue] load_on_startup: True` starts
  the next queued job on every Klippy `ready`, and whether a paused queue
  survives a restart.
- **TO VERIFY (after M112):** the state Klipper and the MCU are left in after
  an emergency stop, and what a later `FIRMWARE_RESTART` does to the toolhead
  position and the homed axes.
- `SET_KINEMATIC_POSITION`: which axes it marks homed (all, or only the ones
  given), and whether a later `G28 X Y` keeps Z homed.
- `PROBE` parameters (`SPEED`, `LIFT_SPEED`, `SAMPLES`) and whether a probe
  error leaves the toolhead where it triggered.
- `probe.last_z_result`: toolhead Z at trigger, with or without `z_offset`.
- `safe_z_home` `z_hop` when Z is not homed (lift by setting Z=0 first, then
  clearing the Z homed state), and whether it applies to an X or Y home.
- `BED_MESH_PROFILE LOAD=`: that it only loads the mesh and does not move.
- Toolhead `print_time` semantics near long moves (why it ran about 0.9 s
  ahead of the planned stream in the incident log), `LOOKAHEAD_FLUSH_TIME`
  and lazy flushing.
- `virtual_sdcard` `file_position` at shutdown (taken here as the start of
  the line being dispatched, consistent with `Upcoming (N)` and
  `Exiting SD card print (position N)`).
- `G2/G3` with `P` (ignored) and the full-circle rule; `SET_VELOCITY_LIMIT
  ACCEL_TO_DECEL` vs `MINIMUM_CRUISE_RATIO`; `RESPOND TYPE=error`.

## Fixtures and privacy

`print-rescue/fixtures/` holds scrubbed excerpts of the incident; see
`print-rescue/fixtures/SOURCES.md` for what each file contains. After adding
or changing fixtures, check that no personal data slipped in (both commands
must print nothing):

```bash
# The words are split ('tok''en') so this page does not match its own pattern.
pat='/home/[a-z]|@[a-z0-9-]+\.|tok''en|api''_key|<hostname>|<usb serial>|ttyUSB.*serial'
grep -rnEi "$pat" print-rescue/
zcat print-rescue/fixtures/gcode-excerpt.gcode.gz | grep -nEi "$pat"
```

Replace `<hostname>` and `<usb serial>` with the host name and the USB
serial numbers of your devices.
