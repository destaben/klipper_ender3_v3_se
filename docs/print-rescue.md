# Print rescue: resuming a print after Klipper shut down

When Klipper shuts down in the middle of a print (most often `Lost
communication with MCU`), the part is still on the bed, the heaters are off and
the toolhead no longer knows where it is. This page describes how to continue
such a print safely, and the tool in `print-rescue/` that plans it.

The procedure was written after the [2026-11-07 incident](print-rescue-incident-2026-11-07.md),
where a hand-built resume worked but several steps were riskier than they had
to be.

**To run a rescue yourself, without AI, follow [print-rescue-runbook.md](print-rescue-runbook.md):** capture, bring the machine back,
`rescue_job.py` (builds the resume file), `rescue_run.py` (one pass, finger on the emergency stop), `rescue_watch.py`. It was validated live
on 2026-11-08. The Mainsail button and the worker that prepares the plan and waits for confirmation (not deployed): [print-rescue-worker.md](print-rescue-worker.md). Where the work stands and what to do next: [print-rescue-handoff-2026-11-08.md](print-rescue-handoff-2026-11-08.md). The sections below are the design and the planner reference.

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
- the verdict for every probe spot in the profile and for every spot proposed
  with `--probe-spot X,Y`, and with `--suggest-spots` a ranked list of
  candidate spots for this part (search mode);
- a content check of the print file (R07): the logged exit position must be the
  end of a line of the file, whatever its modified time says;
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
  Treat the printer as shut down afterwards: in the live run it needed the
  Moonraker power-device cycle (off at least 15-20 s, then on) and
  `FIRMWARE_RESTART` alone did not recover it (see the closed list under "To
  verify"). Save the evidence (step 1) before any restart. **TO VERIFY:** the
  exact MCU state after M112, and whether a restart without the power cycle
  can ever be enough. An emergency stop is still the right abort: the
  alternative moves the head.
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
   (`journalctl -k`), Moonraker's job history, the job queue, `printer.cfg` and
   a copy of the print file. Docker logs rotate; capture them early.
2. **Pause Moonraker's job queue** and empty it
   (`POST /server/job_queue/pause`, check `/server/job_queue/status`).
   **TO VERIFY:** that `[job_queue] load_on_startup: True` starts the next
   queued job on every Klippy `ready` on this version, as assumed here. Every
   job starts with `START_PRINT`, which homes all axes into the part. A Prusa
   Connect start while the printer looks "off" is queued too (klipper-connect
   patch), so make sure nothing is pending there.
3. **Run `plan.py`** (below) on the saved evidence, with `--history`,
   `--queue` and `--printer-cfg`. Stop on any non-zero exit: 2 (refused), 3
   (motion blocked by an unverified profile field, or evidence not supplied)
   or 1 (input rejected). Only exit 0 may go on, and then only after every
   warning is confirmed (an overlap above the budget, R12, is a warning with
   the extra material: choose the budget with `--max-overlap-s`). Exit 0 is not yet a complete safety verdict (see
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
7. **Declare a provisional Z, then lift.** After an MCU shutdown or restart
   nothing is homed. `SET_KINEMATIC_POSITION Z=<provisional> SET_HOMED=z`
   marks **only** Z as homed (observed: `homed_axes` was `z`). Declare Z
   **before** any `G28 X` or `G28 Y`: in this fork `safe_z_home` with Z not
   homed sets Z=0 and lifts `z_hop`, which fakes the reference. Then lift with
   **relative** moves up only (`G91`, steps of 10 mm, F300-600) until the
   nozzle is above the part top + the 5 mm pin + a margin. The Z axis uses T8
   leadscrews (`rotation_distance: 8`) that can back-drive while the motors
   are off: re-measure the gap right before the first move.
8. **Home X and Y at height, one axis per command**: `G28 X`, then `G28 Y`.
   With Z declared at least `z_hop` they home only their own axis (observed:
   X -6, Y -14). `G28 Y` moves the **bed**, so the part sweeps under the head:
   lift first, and check on paper that the swept path clears the part box and
   the probe pin. One axis per command is from the live run (see the closed
   list in [To verify](#to-verify-against-the-klipper-fork)): it keeps a
   surprise small, and the check on paper does not depend on it. If a command
   moves both axes, stop.
9. **Re-reference Z with bounded `PROBE` strokes, at a spot chosen for this
   part.** Use `plan.py --suggest-spots` and check the chosen spot with
   `--probe-spot X,Y`. A spot is accepted when the nozzle is inside the
   envelope and at most X=205 (cutter lever), the probe point is on the bed
   and at least 5 mm from the part, and the head footprint at probe height
   clears the part box + 3 mm. The spot does **not** have to lie inside the
   bed mesh: outside it the planner only warns (R20) and prints the distance.
   With the owner's head estimate (left 70, right 100, front 50, back 65) the
   only possible spots for a part in the middle of the bed are at the **rear**;
   any front spot puts the X crossbar over the part. Descend toward the bed
   **only** with `PROBE PROBE_SPEED=5` whose physical length is bounded by
   (declared Z - `z_min` of -3); declare Z higher at the same physical spot to
   start the next stroke (the live run used 98 mm, 10 mm and 15 mm strokes).
   Plain `G1` descents toward the bed are not allowed. After a `PROBE` the next
   move is **up**. Probe two spots to detect a tilted gantry (**TO VERIFY**,
   not done in the live run).

   Z formula (observed output of this Klipper:
   `// probe: at X,Y bed will contact at z=4.9125`):
   `probe.last_z_result` is the **trigger** Z of the toolhead (7.8725 in the
   live run) and `last_probe_position[2]` is trigger - `z_offset` (4.9125). At
   the trigger point the nozzle is `z_offset` (2.96) above the bed, so after the
   probe declare `SET_KINEMATIC_POSITION Z=<z_offset> SET_HOMED=z`, plus the
   bed-mesh difference between the probe spot and the home point (below
   0.01 mm in the live run). The `z_offset` comes from the live `printer.cfg`
   (it drifts after every `PROBE_CALIBRATE`; the profile copy is replaced by
   the live value). Error sources to state with the result: the bed was 50 C,
   not the 60 C used for printing (about 0.05 mm), and the mesh extrapolation
   outside the meshed area (below 0.05 mm at the live run's rear spot, one
   observation).
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
12. **Paper test on the part, with the bed mesh LOADED**, at the resume point
    before the first extrusion. In the live run, loading the mesh after the
    paper test shifted the commanded Z 53.0 to a reported 52.957 (-0.043 mm).
    Cross-check by approaching in decreasing gaps (5, 2, 1, 0.5 mm), then
    paper-rub at the resume Z. Only then start the resume file.
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
  its lines to meet the margin, refuse as well (R22); the margin is never
  quietly reduced;
- warn (R12) if the overlap from the resume point to the latest possible
  stop exceeds the budget (`max_overlap_s`, 3 s, or `--max-overlap-s`), with
  the extra material; refuse (R21) only above `absurd_overlap_s` (30 s; `--max-overlap-s` above 30 s is a usage error, exit 1, and never moves it);
- a manual `--line` needs the window (refused with R18 without one). It is
  refused (R22) if it starts after the earliest possible stop (a gap), and
  warned (R12) if it overlaps above the budget. The margin is not applied to a
  manual line.

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
value (such as a source name) cannot hide a command. Any control character, and any non-ASCII
character outside a `;` comment line, in the generated part is refused as well. Before the gate, the only Z
motion allowed is a relative lift up. A refused generated part is a hard
refusal (R17) with exit 2 and no preview. The original file's lines after the
resume point are copied as they are; R17 refuses the plan if they contain a
forbidden command.

What a resume skips: **a resume does not run `START_PRINT`**, so everything
`START_PRINT` does for the whole job must be restated in the file. The preview
restates `CANCEL_PRINTER_SHUTDOWN` (an armed auto power-off would otherwise cut
the power mid-print), clears the display message it leaves behind (`M117`
without text), and sets `G17` (the slicer's arc plane) before the original
lines. `SET_IDLE_TIMEOUT TIMEOUT=600` restates the configured `[idle_timeout]`
default so the file does not depend on an earlier change; the longer timeout
the rescue itself needs (the idle gcode turns the heaters off and clears
homing) is a runner step outside the file, the live run used 14400 (memory
only, a restart resets it).

UI metadata (what Mainsail shows). The live run's hand-built file dropped the
slicer header, and the job panel showed "Layer 0 of 0", no slicer, no
thumbnail and an incoherent estimate. The preview therefore keeps the original
file's leading comment block (slicer header and thumbnails) right after its own
first line (the NOT VERIFIED label), keeps the footer with the tail, rewrites
`;TIME:`, `;Filament used:` and `estimated printing time` comments to the
**remaining** values, and emits `SET_PRINT_STATS_INFO TOTAL_LAYER=<n>
CURRENT_LAYER=<k>` at the start and `CURRENT_LAYER` after every
`;LAYER_CHANGE`. The remaining share is the filament still to be printed
(net extrusion up to the resume line against the header's total), so it is
proportional, not a re-simulation; the planner's timeline only covers the
layers around the stop. **TO VERIFY** (we could not query the printer): how
Moonraker derives layer count, slicer and estimate from a file (which part of
the file it scans, whether a leading comment before the header is accepted,
whether `SET_PRINT_STATS_INFO` overrides the metadata), and that Mainsail
shows them for a generated file. This is a P2 pre-check
([P2 design](print-rescue-p2-design.md)). Nothing is changed for a print that
is already running.

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
| R02 | refused | No valid probe spot: none of the profile's spots (which must be verified) and none proposed with `--probe-spot` passes the geometry check for this part, or there is no part geometry. With `--suggest-spots` the report lists candidates. |
| R03 | warning | Resume point within `travel_clearance` of a no-go zone. The park spot and the approach moves are not checked yet (KI-11). |
| R04 | warning | Multi-tool print (or lane mismatch) and `multi_tool_resume` is false. |
| R05 | warning | Absolute extrusion (M82) at the resume line or in the tail. |
| R06 | warning | Vase mode (extrusion moves that change Z) or non-monotonic layer Z. |
| R07 | refused | G-code file missing or not a regular file, name or size differ from the job history, or the content check fails: the logged `Exiting SD card print (position N)` is not the end of a line (or of the file), so this is not the file that was printing. |
| R08 | warning | Cut before `min_resume_layer`. |
| R09 | refused | The shutdown was not an MCU communication loss. |
| R10 | warning | Insufficient log evidence, manual line needed (anchor endpoints more than 1 s apart, planner timing off by more than 0.5 s, no clean freeze). |
| R11 | warning | Long time since the shutdown (part may have cooled loose). |
| R12 | warning | The overlap from the resume line to the latest possible stop is above the budget (`stop_estimation.max_overlap_s`, or `--max-overlap-s`). The line states the path (mm), the filament (mm) and an estimate of the material printed twice (mm3). Refusing would favour a gap in the part, so it only warns. |
| R13 | refused (motion) | A safety-critical profile field is not verified on this machine (source is not `measured` with `measured_on` and `method`). Exit 3 when it is the only refusal. The owner's head estimate has source `user`, so it keeps this block on. |
| R14 | refused | The profile's `config_fingerprint` does not match `printer.cfg`. |
| R15 | refused | Moonraker's job queue is not empty (a queued job starts with `START_PRINT` on the next ready; **TO VERIFY**). |
| R16 | warning | Unknown previously excluded objects: exclusions made in the UI are not in the log (several objects). |
| R17 | refused | Forbidden command in the resumed tail: any `G28` form or another command in `FORBIDDEN_TAIL` in `print-rescue/gcode_scan.py` (homing, probing, print start, MMU homing macros, stop/restart, pause/cancel and shutdown commands; `G92` on X/Y/Z; exact command names after normalising spaces and `N` line numbers; other macros are not matched, KI-08/KI-17), or a change to another tool. |
| R18 | refused | No usable stop evidence, so no resume line can be chosen: no `Exiting SD card print` position, no usable `Stats` lines, no planned motion before the exit position, or a manual `--line` without a window. Also used when no line was chosen at all. |
| R19 | warning | The G-code file's modified time differs from the history while the content check passed (a re-upload). |
| R20 | warning | A probe point lies outside the bed mesh area: the mesh is extrapolated there. The distance is printed. |
| R21 | refused | The overlap is clearly absurd (above `stop_estimation.absurd_overlap_s`, default 30 s; `--max-overlap-s` above 30 s is rejected with exit 1 and never moves this limit): the evidence and the plan do not fit together. |
| R22 | refused | No resume line without a gap: the stop window starts before the cut layer, no line of the cut layer meets the margin, or a manual line starts after the earliest possible stop. |
| R23 | warning | The resumed tail contains `G91` (relative XYZ). The preview restores the mode of the resume line itself; the warning names the first `G91` line so the tail's return to `G90` can be checked. |
| PIN_AT_BOOT | warning | The probe pin self-test at Klippy start could land on the part: the pin path (nozzle path plus probe offset) of every move that overlaps the stop window is tested against the part box plus the probe-spot margin, whole segments, not only their ends (**TO VERIFY**: when the pin deploys). |

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
`config_fingerprint`, R14), the cutter limit X=205 from the user, takes the head
footprint from the owner's estimate (source `user`, see below), and leaves
`z_top_margin` and `park.xy` as unmeasured placeholders and the probe spot
list empty. It therefore **fails closed** (R13 and R02)
until phase 3 measures them. The cutter is also a no-go zone (`frame: gantry`,
X >= 205) that only `_SP_TIP_FORM` may enter: `config/macros/sp_mmu.cfg` cuts
at X=225 on purpose.

Changes after the live rescue of incident 3:

- `head_footprint` now holds the owner's estimate of the whole head: left 70,
  right 100 (cutter lever extended), front 50, back 65 (counting the X
  crossbar, about 30 to the carriage), lowest point at nozzle level. These are
  **estimates, not calipers**: the source is `user`, never `measured`, so R13
  keeps motion blocked until phase 3 measures them. A first attempt with the
  old placeholder footprint put the fan over the part and the emergency stop
  was needed; motion must stay blocked while any footprint tier is not
  `measured`.
- `envelope.bed` is the printable bed (0-220); a probe point outside it is a
  hard reason. `envelope.probe_area` is now the bed **mesh** area; outside it
  is only R20.
- `config_fingerprint.bltouch.z_offset` is marked `"live": true`. It drifts
  after every `PROBE_CALIBRATE` (the profile said 3.035, the live value was
  2.96). With `--printer-cfg` the live value replaces the profile copy in
  memory and R14 does not compare it; without it the profile copy is used and
  R14 is NOT CHECKED. The profile file is never written.
- `stop_estimation.absurd_overlap_s` (default 30) is the R21 limit.

Two further fields are used by the preview:

- `park`: the off-part spot where the preview heats and primes (`park.xy`,
  placeholder `[15, 205]`, unmeasured). It must be clear of the part, the probe
  pin and the cutter.
- `bed_mesh_profile`: the name for `BED_MESH_PROFILE LOAD=` (`default`). It
  must match the `[bed_mesh <name>]` section in `printer.cfg`; R14 does not
  compare it yet.

The profile is validated before anything is planned: section types, finite
numbers, ranges (for example `snap_window_s` and `max_overlap_s` must be
finite and not negative, and `margin_s` greater than 0) and required keys. A profile without
`envelope.probe_area` or `envelope.bed` is invalid. A malformed profile exits 1 with the list
of problems. The spot check also refuses any spot when no bed area is
given, so the check is never skipped.

`test_machine_profile.py` checks that `config/printer.cfg` still matches the
fingerprint, so a change to probe offsets, axis limits, the Z velocity and
acceleration limits, `safe_z_home` or the
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

The preview is a **review artefact** (KI-14, KI-19): do not print it. It stops
at an invalid command on purpose, and its motion is not safe to run.

- `--log`: Klippy log, either `docker logs --timestamps klipper` output or a
  plain `klippy.log`.
- `--history`, `--queue`: saved JSON of `/server/history/list` and
  `/server/job_queue/status`. In a history list the job used is the first one
  whose status is `klippy_shutdown`, `klippy_disconnect`, `error`, `cancelled`
  or `interrupted`; none of those is an R07 refusal (never the first job of
  any status).
- `--line N`: manual resume line. It is checked against the window and refused
  without one. A line without motion resolves to the next line with motion and
  the report says so.
- `--max-overlap-s S`: the overlap budget in seconds (default: the profile's
  `max_overlap_s`). An overlap above it is the R12 warning with the extra
  material; the operator sets the budget knowingly. Above 30 s it is rejected
  with exit 1 (it never moves the R21 limit).
- `--probe-spot X,Y` (repeatable): a nozzle position to check for this part.
  A typed spot is checked by geometry only and has no `measured` provenance of
  its own: the trust sits in the head footprint it is checked against, which R13
  keeps blocked until every tier is `measured`.
- `--suggest-spots`: search mode, a grid of nozzle positions that pass the
  geometry check, ranked by head margin (capped at 15 mm), then by the smaller
  mesh extrapolation, then by the closeness to the part's X centre. Advice only.
- `--preview FILE`: write the NOT VERIFIED preview, only when nothing is
  refused. `--force-preview` is needed to replace an existing file, and is a
  usage error (exit 1) without `--preview`. FILE is refused if it is an input
  (or a symlink or hard link to one), and a symlink is refused even with
  `--force-preview`. A preview that is not written does not change the exit
  code (KI-03).
- `--gcode-map MAP` exists only for the test fixture excerpts (KI-02); it is
  hidden from `--help` and refused unless `PRINT_RESCUE_ALLOW_GCODE_MAP=1`.
- `--now`: the current time for R11 (default: now). It must be ISO 8601 UTC.

Exit codes:

| Exit | Meaning |
|------|---------|
| 0 | Resumable: no refusal, every safety-critical profile field verified, and all evidence inputs supplied. Confirm every warning before any motion. |
| 3 | Resumable on paper only: the only refusals are R13 (a profile field is not verified, motion is blocked) or `--history`, `--queue` or `--printer-cfg` was not given, or the log holds no `Virtual sdcard`/`Upcoming` text for the content check (printed as `NOT CHECKED:`). The report and the preview are still produced. |
| 2 | Refused: at least one refused finding (a hard code in the catalogue; R13 alone is exit 3, warnings never refuse). Never 0 without a chosen resume line. |
| 1 | Usage or input error (bad arguments, unreadable or malformed input, an invalid `margin_s`, an input larger than the size cap). Nothing was planned. |

On the Pi, run it under `nice -n 19` while Klipper is connected. A full 8 MB
file takes about 35 s.

Unit tests (also in CI, `.github/workflows/print-rescue-test.yml`):

```bash
python3 -m unittest discover -s print-rescue -v
```

## Phases

| Phase | Content |
|-------|---------|
| P1 (code complete; review pass open, see the known issues) | Read-only planner, resume preview, refusal catalogue, profile with provenance, incident fixtures. Pending items: [Known issues](#known-issues-and-pending-work-end-of-phase-1). |
| P2 | Moonraker-only interactive runner, see [the P2 design](print-rescue-p2-design.md): pauses the job queue, a `START_PRINT` lock while a rescue is active, uploads a state-and-tail resume file and starts it. Starts only after the P2-gate items are closed ([Known issues](#known-issues-and-pending-work-end-of-phase-1)). |
| P2b | Request-only Mainsail macro and a worker that prepares the plan and waits for a typed confirmation (never runs the runner; not deployed), see [print-rescue-worker.md](print-rescue-worker.md). |
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

The live run of 2026-11-07 covered parts of steps 3, 4 and 8 by hand and
observed the rest only partly; see
[the live validation record](print-rescue-live-validation-2026-11-07.md) for
which steps are covered and which remain.

## Known issues and pending work (end of phase 1)

Numbers are stable references (KI-05 to KI-07 are intentionally unused). A
"Design notes" section below justifies the choices behind the open items.

Open:

- **KI-01**: phase 1 is a read-only planner. Phases 2+ (motion, upload/start)
  are not implemented. No motion code of this repository has ever run on
  hardware; the live rescue of 2026-11-07 was done by hand through Moonraker
  ([live validation](print-rescue-live-validation-2026-11-07.md)).
- **KI-02**: `--gcode-map` exists only for the test fixture excerpts (hidden
  from `--help`, refused without `PRINT_RESCUE_ALLOW_GCODE_MAP=1`; its
  `source_size` is trusted for the R07 size comparison).
- **KI-03**: a preview that is not written does not change the exit code.
- **KI-08**: the R17 tail check is a deny-list of exact command names (after
  normalising spaces and `N` line numbers, with or without digits). The list
  includes every macro in `config/` that reaches a homing, probing,
  force-move or stepper command, directly or through other macros (a test
  derives the transitive set from the files, so a macro added to `config/`
  fails that test until it is listed), plus the MMU load, unload and lane
  macros. With `--printer-cfg` the planner also reads the user's own
  `[gcode_macro]` sections (following `[include]` files inside the config
  directory) and refuses any macro that reaches a listed command, so a macro
  that exists only in the user's config is covered when its file is supplied.
  A macro that moves the head without any listed word (for example a command
  assembled in a way the checks above do not see) or an absolute-path include
  can still pass; the allow-list
  against the live macro set is the P2 answer (design decision for the owner). Job-end
  commands (`END_PRINT`, `M84`, `M18`, `TURN_OFF_HEATERS`) are accepted only
  as the end of the file; `MANUAL_STEPPER` and `STEPPER_BUZZ` are refused; see `FORBIDDEN_TAIL` in
  `print-rescue/gcode_scan.py`. A macro that homes or stops but is not listed
  is not caught, and the copied tail is not run through the allow-list. P2
  must check the tail against the live macro set or an allow-list.
- **KI-09**: `safety_critical` in the profile is a self-declared flag, so
  clearing it removes a field from R13; plain-number safety values
  (`clearances`, `stop_estimation`) never reach R13. The profile is reviewed in
  git. P2 should derive the critical set from code.
- **KI-10**: an object name containing a space in `EXCLUDE_OBJECT_DEFINE` is
  refused (R17); Klipper splits parameters on whitespace, so the refusal fails
  closed.
- **KI-11**: R03 checks the resume point and the park spot against the no-go
  zones. The first travel from the stop position (unknown to the planner) and
  the travel from the park spot to the resume point are not checked; the P2
  runner must check every move it issues.
- **KI-12**: `--line` is accepted when it starts at or before the earliest
  possible stop, without the safety margin of the automatic choice. It is an
  explicit operator override; the overlap warning still applies.
- **KI-13**: the preview runs its own prime in `G90`/`M83`, then restores the
  modes the source was in at the resume line: `M82` plus `G92 E<source E>` for
  absolute extrusion, `G91` for relative XYZ (tests `Build`). Non-G17 arcs are
  not translated or refused.
- **KI-14**: the preview stops at `GATE_STOP`, a deliberately invalid command.
  That Klipper stops the SD print on an unknown command is **TO VERIFY** on the
  printer. The P2 design removes the trick: the resume file holds state and
  tail only, and the gate is the runner's state machine.
- **KI-15**: a last dispatched line that is a dwell has no end position in the
  planner and can raise R10 falsely (a warning).
- **KI-16**: a source file name or copied header line with a control character or a
  Unicode line separator is refused through the R17 path; accented text is kept
  (comment lines only; every other generated line stays printable ASCII).
- **KI-17**: see KI-08 (the deny-list also covers print-ending and pausing
  commands; `END_PRINT` is accepted only as the end of the file).
- **KI-18**: `--preview --force-preview` may replace any regular file the user
  names that is not an input. It is an explicit operator choice. Text inputs
  and unpacked excerpts are capped at 256 million characters; JSON inputs share
  that cap, and a single G-code line longer than 4 MiB is refused.
- **KI-19**: the preview lifts by a fixed relative amount and travels at that
  height; the planner's safe pass-1 height is not used and Z is unknown before
  re-referencing. The preview must not be run; the P2 design replaces it with
  a state-and-tail file and runner-controlled motion.
- **KI-20**: smaller gaps, all warnings or P2 items: `M84`/`M18`,
  `M290` is not on the tail deny-list (`BLTOUCH_DEBUG` and `SET_GCODE_OFFSET ... MOVE=1` are);
  `--line` is not checked against the cut layer; a history job with
  neither `size` nor `modified` is reported `NOT CHECKED` (exit 3); with only
  one of them, that one is compared. `SDCARD_RESET_FILE`,
  `SDCARD_PRINT_FILE`, `M24` and `M32` are not on the tail deny-list either
  (a slicer does not emit them; see "Design notes").
- **KI-21**: when the log has no Stats line after the shutdown, `P_end` is a
  lower bound and the stop window can be biased late (reported as R10).
- **KI-22**: the R07 content check has two parts: the exit position must be a
  line boundary, and the text the log prints for `Virtual sdcard (N)` and
  `Upcoming (N)` must match the file at those offsets (a few hundred
  characters). It cannot see an edit elsewhere in the file that keeps the size
  and those lines; the history `size` comparison (when `--history` is given)
  catches size changes only. The R19 text says so: a pass does not mean the
  file is identical. A log without those text lines leaves the text half
  unchecked, which is reported as `NOT CHECKED` (exit 3), not as a pass.
- **KI-23**: the remaining-time and remaining-filament rewrite of the resume
  file's header is proportional to filament, and how Moonraker/Mainsail use the
  header is TO VERIFY.
- **KI-24**: the head footprint is the owner's estimate (source `user`); the
  rear probe spot and its mesh extrapolation (below 0.05 mm) rest on one
  observation. Both keep R13 on until phase 3 measures them.
- **KI-25**: the CR Touch alarm episode of the live run (pin down / touch mode
  read TRIGGERED with a red blinking LED, `PROBE` failing with "Probe
  triggered prior to movement", cleared after another power cycle) has an
  unknown root cause. QUERY_PROBE alone is not evidence of a broken sensor.
- The review IDs S1, S2, N1-N6, N8, N11, N14, N17-N20 come from a review pass
  of the first (interrupted) session. See "Design notes" for their status.

Closed in this change:

- **KI-04** (evidence inputs optional): a run without `--history`, `--queue`
  or `--printer-cfg` now exits 3, not 0.
- Prime amount with a slicer retraction larger than `retract_before_travel`
  (test `PrimeAmount`); firmware retraction (`G10`/`G11`) and R-form arcs are
  refused in the tail; the pin-at-boot check tests the whole path, not only the ends, of
  every move in the stop window; oversized text input and excerpts are
  refused; a manual `--line` without motion is reported.
- The `z_offset` drift (profile 3.035, live 2.96) is gone by taking the live
  value from `printer.cfg`; the placeholder footprint was replaced by the
  owner's estimate; the "probe spot must be on the meshed area" rule became
  the R20 warning.

## Design notes

Choices behind items that the automated reviews raised repeatedly.

- **Why an overlap above the budget is only a warning (R12).** In the
  incident-3 rescue the planner refused for 3.55 s of overlap against a 3.0 s
  budget. The alternative to an overlap is a gap, which is a weak spot in the
  wall; a few seconds of material printed twice is a blemish. So the budget is
  advice with the cost spelled out (mm, mm3) and the operator can set it
  (`--max-overlap-s`). Only a clearly absurd overlap (R21) or a window that
  forces a gap (R22) refuses.
- **Why the R07 check moved from mtime to content.** The file had been
  re-uploaded, so its mtime differed while the bytes were identical. The
  logged exit position is the byte right after the last dispatched line: in
  the same file it is a line boundary (8772080 is the end of line 391419).
  The check also compares the text the log printed at `Virtual sdcard (N)` and
  `Upcoming (N)` with the file, which catches same-length edits. The mtime is
  kept as the R19 warning. KI-22 states what the check cannot do.
- **Why a late or missing end anchor only warns (KI-21).** When both anchors exist, the earliest stop
  is the earlier of the two (`sd_pos` and the end anchor), so a late
  end anchor alone cannot move the earliest stop later; the window start only
  moves late when the `sd_pos` anchor is late as well, and R10 then asks for a
  manual line. A gap needs both anchors wrong in the same direction.
- **Why a manual `--line` skips the cut-layer and margin checks (KI-12).** It
  is the operator's explicit override for a log that gives no usable evidence.
  The automatic policy cannot run without that evidence, and refusing the
  override would leave no way to resume at all. What stays: R07, R15, R17, the
  overlap warning and R13. The operator chose the line, so the cost of an
  early or late choice is theirs and is printed.
- **Why a missing Stats line only raises R10 (KI-21).** Without a Stats line
  after the shutdown the end anchor is a lower bound, so the earliest stop is
  biased late, the unsafe direction. R10 states it ("manual line needed"), and
  the `sd_pos` anchor limits the exposure. It is a warning rather than a
  refusal because the manual `--line` path exists for exactly this case; the
  overlap shown for the chosen line is what the operator weighs.
- **Why an unmeasured `--probe-spot` counts toward R02.** R02 asks whether a
  spot passes the geometry check (inside the envelope, at least 5 mm from
  the part, head footprint clear). Measuring is a different question and
  belongs to R13: the head footprint the check relies on is unmeasured, and
  R13 blocks motion until it is measured. So a typed spot can satisfy R02
  while R13 still refuses motion; one code each, no double counting. The test
  in `test_incident3.py` depends on this.
- **Why the tail deny-list leaves out `SDCARD_*`, `M24` and `M32` (KI-08,
  KI-20).** A slicer does not emit the SD-card and file-control commands in a
  part file. A plain `SET_GCODE_OFFSET` (a baby-step) only shifts later moves
  and is allowed; `SET_GCODE_OFFSET ... MOVE=1`, which moves at once, and
  `BLTOUCH_DEBUG`, which deploys the pin, are refused (R17). The rest are
  accepted gaps, not claims of safety: a deny-list cannot be complete
  (KI-08), and the P2 allow-list against the live macro set replaces it.
- **Why `KEY=VALUE` words such as `G1 X=10` are not parsed.** Klipper splits
  a line at the letters and keeps `=10` as the value; `float("=10")` raises,
  so the line aborts the print ("Unable to parse move") instead of moving
  (read from `klippy/gcode.py` and `klippy/extras/gcode_move.py`). The scanner
  models what a move does, and that one does nothing; a file with such lines
  does not print on the real machine in the first place.
- **Why `margin_s` must be greater than 0.** The stop window is clamped to the
  moves the scan kept (two layers). With a margin of 0, a window that started
  before the kept moves would be clamped to the first kept move and the
  planner could resume at the start of the oldest kept layer, skipping part of
  the model. A positive margin makes that case refuse (R22) instead. Profile
  validation rejects 0.
- **Why the R14 fingerprint covers only options that `printer.cfg` sets.** It
  compares `max_velocity`, `max_accel`, `max_z_velocity`, `max_z_accel`,
  `square_corner_velocity` and the two `[extruder]` extrude-only limits, which
  feed the timing model. `minimum_cruise_ratio`, `instant_corner_velocity` and
  `arc_resolution` are not set in this repository's `printer.cfg`, so they are
  Klipper defaults and there is nothing to compare; if the file ever sets them
  the profile must follow. The `motion` and `stop_estimation` numbers are
  tuning values, not measured geometry, so they stay outside R13 (KI-09); R10
  catches a timing model that disagrees with the log by more than 0.5 s.
- **Why `PIN_AT_BOOT` stays a warning.** It is a council-ruled catalogue
  entry and the premise is still **TO VERIFY**: whether the pin deploys on
  `FIRMWARE_RESTART` at all (see "To verify"). Escalating it to a refusal would
  make an unverified assumption block every rescue; the text tells the
  operator what to do and that exit 0 does not clear it. Raising it to a
  refusal is a design choice for the owner.
- **Why the review workflow does not accept the `Copilot` bot.** The Copilot
  cloud agent once added `allowed_bots: Copilot` to
  `.github/workflows/claude-pr-review.yml` so its own pushes would be reviewed.
  The job holds `CLAUDE_CODE_OAUTH_TOKEN` and the checkout of a same-repo head
  supplies `.claude/` settings (hooks), so a head pushed by an agent whose
  work can be steered by text (an issue, a comment) could run a hook with the
  token. The line was removed. Pull requests opened by Copilot are reviewed on
  request: an OWNER, MEMBER or COLLABORATOR comments `@claude review`, and that
  path never checks out the PR head.
- **Why zero-padded command numbers (`M0112`, `G028`) are refused.** Klipper
  looks the command up as the literal text (`klippy/gcode.py`), so `M0112` is
  an unknown command there and would not stop anything. The scanner
  normalises the number and refuses it anyway: stricter than Klipper, never
  looser, and covered by a test.
- **How the preview is published.** Without `--force-preview` the temp file is
  hard-linked to the target, which fails if anything exists there by then, so
  a file created after the pre-run check is not replaced. If the file system
  has no hard links (a FAT card) it falls back to the pre-run check alone.
  The temp file is created with mode 0666, so the kernel applies the umask and
  the process-wide umask is never changed.
- **Why `MINIMUM_CRUISE_RATIO=1` and `ACCEL_TO_DECEL=0` are not modelled.**
  Klipper requires `0 <= MINIMUM_CRUISE_RATIO < 1` and `ACCEL_TO_DECEL > 0`
  (`klippy/extras/gcode_move.py` and `klippy/toolhead.py`), so such a line
  errors there and changes nothing. The scanner ignores them and profile
  validation rejects `motion.minimum_cruise_ratio >= 1`; with no deceleration
  headroom the look-ahead port would have no junction to time.
- **Which built-in commands the deny-list names.** Config macros are derived
  from `config/`; the Klipper built-ins that probe, home or sweep the head are
  listed by hand (`G28`, `G29`, `G30`, `G32`, `PROBE*`, `BED_MESH_CALIBRATE`,
  `BED_TILT_CALIBRATE`, `Z_ENDSTOP_CALIBRATE`, `AXIS_TWIST_COMPENSATION_CALIBRATE`,
  `DELTA_CALIBRATE`, `DELTA_ANALYZE`, `TEST_RESONANCES`, `SHAPER_CALIBRATE`,
  `SCREWS_TILT_CALCULATE`, `BED_SCREWS_ADJUST`, `MANUAL_PROBE`, `TESTZ`,
  `MANUAL_STEPPER`, `STEPPER_BUZZ`, `FORCE_MOVE`, `SET_KINEMATIC_POSITION`).
  A built-in added by a future Klipper is not caught until it is listed (KI-08).
- **Why `CLAUDE.md` has the print-rescue test section it has.** Its
  "Print-rescue unit tests" entry mirrors the other test sections: the
  command `python3 -m unittest discover -s print-rescue -v`, the workflow that
  runs it, and the standard-library-only note. The refusal codes, procedure and
  design notes live in `docs/print-rescue.md` and are deliberately not
  repeated there.
- **How user macros reach the deny-list.** `dangerous_macros()` in
  `gcode_scan.py` takes the parsed `printer.cfg` (plus includes) and marks every
  `[gcode_macro]` whose gcode calls, as the first word of a line and at any
  depth, a deny-listed command, or that mentions a hard word (homing, probing,
  stepper, restart: `G28`, `PROBE`, `FORCE_MOVE`, `FIRMWARE_RESTART`...) anywhere
  in its body, a Jinja expression or string included
  (`{printer.gcode.run_script_from_command("G28")}`). Soft names such as
  `PAUSE` count only as the first word of a line, so a message or variable that
  mentions them does not condemn a macro. A macro that runs a command it
  builds at run time (`run_script*`, or a bare `{ ... ~ ... }` expression line,
  whose text becomes a gcode line) is dangerous too, since the command cannot
  be known; `action_*()` calls only print and are ignored. Firmware retraction (`G10`/`G11`) does not make a
  macro dangerous (a timelapse macro retracts and is fine in a tail), tool
  changes `T<n>` are left to the tool-change check, and the end macros keep
  their own rule. Every branch of an `{% if %}` counts, so it fails closed. A
  saved `printer.cfg` without its config directory is the usual evidence, so
  a named include that was not supplied is `NOT CHECKED` (exit 3), because it can
  hold the macro the tail calls; save the config directory next to
  `printer.cfg`. An absolute include (the runtime-generated
  `/run/klipper/mmu_temp.cfg`, which holds the MMU MCU section) cannot be
  supplied as evidence and is only a note.
- **Why the test fixtures set `PRINT_RESCUE_ALLOW_GCODE_MAP` process-wide.**
  `rescue_fixtures.py` sets it on import because the committed excerpts need
  `--gcode-map`, which is otherwise refused. Only test modules import it; a test
  starts a clean interpreter, imports every planning module and fails if
  `rescue_fixtures` is loaded, so production code can never pick the variable up
  from it.
- **Why the author metadata of the early commits stays.** One early commit
  carries a local host name in its author header. It is not a credential, the PR
  body discloses it, and removing it needs a history rewrite, which this
  repository's rules forbid; a squash-merge drops it, and that choice is the
  owner's.
- **Why offsets past the end of a real file are a mismatch.** The log's
  `Virtual sdcard (N)` text must exist in the file at `N`. In a real file a
  missing offset means the file is shorter than the print, so R07 refuses; only
  a fixture excerpt (which has gaps) skips such an offset, and a run that
  compared nothing says so.
- **Why invalid UTF-8 is tolerated in offsets.** Lines are decoded with
  replacement characters, so a stray byte re-encodes longer than it was; text
  offsets therefore use the next line's exact offset as the end of a line. A
  logged offset that falls inside a line holding invalid bytes may compare
  imprecisely, which can only cause a false refusal (R07), never a false pass.
- **A refused run and an existing preview.** With `--force-preview` a refused
  run leaves the existing file alone (it may be the operator's) and prints
  `stale preview: ... NOT updated by this run`. Without the flag, an existing
  file stops the run before anything else (exit 1).
- **Why the `--force-preview` replace and the FAT fallback are not made
  race-proof.** `--force-preview` means "replace whatever is there", so a file
  created between the check and the write cannot be overwritten against the
  operator's intent. The no-flag path is hard-linked and cannot replace. Only a
  file system without hard links (a FAT card) falls back to check-then-replace;
  exploiting that needs a local actor who can already write to the output
  directory.
- **Why exit 3 without evidence.** An exit code is what a runner reads.
  `NOT CHECKED:` in the text is easy to miss, so a run that skipped R07, R14
  or R15 never exits 0.
- **Why the G-code gate is not made real in phase 1.** The preview is a
  review artefact; the P2 design makes the resume file hold state and tail
  only and moves every motion and gate into the runner. Investing in the
  invalid-command trick (KI-14, KI-19) would be wasted. If Klipper kept
  running past the unknown command, the next line would lower Z in absolute
  mode at an unreferenced Z; that is why the file is labelled NOT VERIFIED, why
  phase 1 never runs it, and why no run path exists until the runner enforces
  the gate (P2). Verifying the firmware behaviour is on the "To verify" list
  and is not needed to keep phase 1 safe.
- **Why the deny-list stays a deny-list for now (KI-08).** An allow-list for
  the tail needs the macro set of the live configuration, which the planner
  does not read. The P2 pre-checks require it.
- **Why plain-number safety values do not reach R13 (KI-09).** They are
  tuning values (clearances, budgets), not measured geometry; the schema change
  to make them leaves is part of phase 3.
- **Review IDs.** The IDs S1, S2, N1-N6, N8, N11, N14 each have a code site and
  a test naming the ID. N7, N9, N10, N12, N13, N15, N16 and N17-N20 have no code,
  test or recorded text in the repository: the findings were lost with the
  interrupted session, so they are retired rather than guessed. Anything real
  among them would have to be found again by a review of the current code,
  which the automated PR reviews do on every push.
- **Why the hand-run rescue is evidence, not validation.** See
  [the live validation record](print-rescue-live-validation-2026-11-07.md).

## To verify against the Klipper fork

Closed by observation in the live run of 2026-11-07 (this fork, container
source read and Moonraker queries):

- `SET_KINEMATIC_POSITION Z=<z> SET_HOMED=z` marks **only** Z as homed
  (`homed_axes` was `z`).
- With Z declared at least `z_hop`, `G28 X` and `G28 Y` home only their own
  axis and do no hop (observed X -6, Y -14).
- `safe_z_home` with Z **not** homed sets Z=0 and lifts `z_hop`, which fakes
  the Z reference. Declare Z first.
- `PROBE` output: `// probe: at X,Y bed will contact at z=4.9125`;
  `probe.last_z_result` is the toolhead Z at the trigger (7.8725), and
  `last_probe_position[2]` is that minus `z_offset`. The nozzle is `z_offset`
  above the bed at the trigger.
- A Moonraker power-device cycle (off, at least 15-20 s, on) resets a shutdown
  MCU; `FIRMWARE_RESTART` does not. The print state goes from `paused` to
  `standby` and position, homing and heaters are lost. Klippy was `ready`
  about 25 s later.
- `[idle_timeout] timeout: 600` runs the idle gcode (heaters off, `M84`),
  which also clears homing; `SET_IDLE_TIMEOUT TIMEOUT=14400` held it (memory
  only).
- The shutdown dump's `Exiting SD card print (position N)` is the end of the
  last dispatched line (8772080 = end of line 391419).

Still open:

- **TO VERIFY (pin at boot):** whether `[bltouch]` deploys the pin on Klippy
  connect, and when. The owner saw no mark on the part after a boot
  self-test, which is not proof. `PIN_AT_BOOT` stays a pre-check.
- **TO VERIFY (gantry tilt):** whether two probe points detect a tilt after
  the motors were off.
- **TO VERIFY (KI-14):** that an unknown command stops an SD print.
- **TO VERIFY (job queue):** whether `[job_queue] load_on_startup: True`
  starts the next queued job on every Klippy `ready`, and whether a paused
  queue survives a restart (in the live run the queue was empty and paused
  throughout).
- **TO VERIFY (after M112):** the state Klipper and the MCU are left in after
  an emergency stop (the live run's stop was followed by a power cycle).
- **TO VERIFY (UI metadata):** how Moonraker and Mainsail derive layer, slicer
  and estimate (KI-23).
- `PROBE` parameters other than `PROBE_SPEED` (`LIFT_SPEED`, `SAMPLES`) and
  whether a probe error leaves the toolhead where it triggered.
- `BED_MESH_PROFILE LOAD=`: that it only loads the mesh and does not move (it
  does change the commanded-versus-reported Z by about 0.04 mm, see step 12).
- Toolhead `print_time` semantics near long moves (why it ran about 0.9 s
  ahead of the planned stream in the incident log) and lazy flushing.
- `G2/G3` with `P` (ignored) and the full-circle rule; `SET_VELOCITY_LIMIT
  ACCEL_TO_DECEL` vs `MINIMUM_CRUISE_RATIO`; `RESPOND TYPE=error`.

## Fixtures and privacy

`print-rescue/fixtures/` holds scrubbed excerpts of two incidents (the
`inc3-*` files are the 2026-11-07 vase rescue); see
`print-rescue/fixtures/SOURCES.md` for what each file contains. After adding
or changing fixtures, check that no personal data slipped in (both commands
must print nothing):

```bash
# The words are split ('tok''en') so this page does not match its own pattern.
pat='/home/[a-z]|@[a-z0-9-]+\.|tok''en|api''_key|<hostname>|<usb serial>|ttyUSB.*serial'
grep -rnEi "$pat" print-rescue/
zcat print-rescue/fixtures/gcode-excerpt.gcode.gz | grep -nEi "$pat"
zcat print-rescue/fixtures/inc3-gcode-excerpt.gcode.gz | grep -nEi "$pat"
```

Replace `<hostname>` and `<usb serial>` with the host name and the USB
serial numbers of your devices.
