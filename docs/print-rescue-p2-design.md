# Print rescue, phase 2 design: a Moonraker-only interactive runner

Status: **design only. Nothing in this page is implemented or has been run by
code.** Phase 1 ([print-rescue.md](print-rescue.md)) is a read-only planner.
This page describes the runner that phase 2 would add, derived from the manual
rescue of 2026-11-07 ([live validation](print-rescue-live-validation-2026-11-07.md)),
in which every step was typed by hand through Moonraker's `gcode/script`, one
command at a time, with the owner watching and holding the emergency stop.

What is untested, plainly: every state below except the manual steps in the
live run, which were done by a person; the runner as software; the gates as
code; the upload and start of a generated resume file (the live run uploaded
and started a hand-built file); and the UI metadata behaviour (see the
pre-checks). No part of this design has been validated on a sacrificial print.

## Principles

- **Moonraker only.** The runner talks to Moonraker (HTTP and `gcode/script`),
  never to the MCU or to Klipper's socket. It runs on a machine that is not the
  printer's host, or at least is not required to survive a Klippy restart.
- **A state machine with gates.** Each state has entry checks, one action, and
  an exit check. Nothing advances on a timeout. Every gate that precedes motion
  needs a human confirmation, and the runner prints the relevant excerpt of
  [print-rescue.md](print-rescue.md) at that gate.
- **One small command at a time**, the result shown, the owner confirming what
  they see (gap in mm, clear of the part, pin out). Steps of at most 5 mm near
  the part, then 2, 1 and 0.5 mm at slow feed. The owner keeps the emergency
  stop in hand. The runner never asks the same question twice.
- **After any errored, timed-out or cancelled command**, re-read the toolhead
  position, `homed_axes`, the probe state and the print state before the next
  move.
- **Abort means emergency stop through Moonraker** (`M112`), never a macro
  abort, because `CANCEL_PRINT` and friends run end-of-print G-code that moves.
- **The resume file is state plus tail only.** It contains no motion, no
  heating, no mesh load and no probing: `CANCEL_PRINTER_SHUTDOWN`, `M117`
  (clear), `G90`, `G21`, `M83`, the object definitions and exclusions, pressure
  advance, velocity limits, the last fan value, the bed and nozzle targets,
  `G92 E0`, `EXCLUDE_OBJECT_START`, `G17`, `SET_PRINT_STATS_INFO`, and then the
  original lines. All motion is done by the runner before the file is started,
  so the phase 1 `GATE_STOP` trick (KI-14, KI-19) is not needed and is removed.
  The live run's file had exactly this shape and ran normally.

## Hard pre-checks (before the first state)

The runner refuses to start unless all of these hold:

1. **R13 does not block motion.** Every safety-critical profile field is
   `measured` with a date and a method, including every head footprint tier.
   The owner's estimate (source `user`) is not enough. In the live run an
   override of this block with the placeholder footprint put the fan over the
   part and needed the emergency stop; it is a hard rule, with no override
   flag.
2. **The planner exits 0** with `--history`, `--queue` and `--printer-cfg`
   given, and every warning (R12 overlap, R20 mesh extrapolation, `PIN_AT_BOOT`,
   R03, R10, R16) has been shown and confirmed.
3. **CR Touch real-`PROBE` test** from a high point, after a power cycle of at
   least 20 s (below). The documented sequence is `BLTOUCH_DEBUG COMMAND=reset`,
   `pin_down`, `touch_mode`, `QUERY_PROBE`, but `QUERY_PROBE` alone is **not
   evidence** of a broken sensor: in the live run it read `TRIGGERED` with a red
   blinking LED repeatedly, `PROBE` failed with "Probe triggered prior to
   movement", and after another power cycle `PROBE` worked normally. The cause
   is unknown. The test is a real `PROBE` from a high point, which aborts before
   moving when the sensor is latched.
4. **`PIN_AT_BOOT` resolved.** If the planner predicts the pin would deploy onto
   the part, the head is made clear (raised, or moved off) before any
   `FIRMWARE_RESTART` or power cycle. Whether the pin deploys at Klippy start is
   still **TO VERIFY**.
5. **The job queue is paused and empty** (`/server/job_queue/status`), before
   any restart. Every queued job starts with `START_PRINT`, which homes into the
   part. Also nothing pending from a Prusa Connect start.
6. **Print state is known** (`print_stats.state`, expected `paused`, `error` or
   `standby` after a shutdown), and no other job is running.
7. **A `START_PRINT` lock** is armed for the duration of the rescue (a flag the
   macro checks and refuses on), so that no start can home into the part.
8. **`idle_timeout` is extended** (`SET_IDLE_TIMEOUT TIMEOUT=14400` held it in
   the live run; memory only, a restart resets it). The default idle gcode turns
   the heaters off and runs `M84`, which also clears homing.
9. **Evidence captured before any restart**: Klippy log, kernel log, Moonraker
   history, queue, `printer.cfg` and the G-code file. Docker logs rotate; in the
   live run the Klippy log was captured about 1.5 hours after the stop, just in
   time.
10. **UI metadata check on the printer (TO VERIFY).** Before relying on a
    generated resume file, check how Moonraker and Mainsail derive layer, slicer,
    thumbnail and estimate from the file (Moonraker's file metadata of a
    generated resume file, and whether `SET_PRINT_STATS_INFO` is honoured). The
    live run's hand-built file dropped the slicer header: the job panel showed
    "Layer 0 of 0", no slicer, no thumbnail, an incoherent estimate and a stale
    "shutdown cancelled" message. The phase 1 preview keeps the header,
    thumbnails and footer, scales the estimates to what remains and sets the
    layer counter; whether that is enough is unverified.

## The state machine

```
EVIDENCE -> PAUSE_QUEUE -> POWER_CYCLE -> DECLARE_Z_LIFT -> HOME_XY ->
PROBE_SPOT -> PROBE_STROKES -> DECLARE_Z -> PAPER_TEST -> PARK_HEAT_PURGE ->
LIFTED_TRAVEL -> LOWER_PRIME -> UPLOAD -> START -> (done)
```

Any state can go to `ABORT` (emergency stop) on a human command or on an
unexpected result. There is no automatic retry of a motion state.

1. **EVIDENCE.** Capture and store the evidence listed above; run the planner;
   show the stop window, the resume line, the overlap with its extra material,
   the part box, and the findings. Gate: the human confirms the resume line
   and the overlap budget (`--max-overlap-s` is the explicit parameter).
2. **PAUSE_QUEUE.** Pause and empty the queue, arm the `START_PRINT` lock.
   Exit check: queue empty and paused.
3. **POWER_CYCLE.** Recovery from an MCU shutdown. `FIRMWARE_RESTART` does not
   reset the MCU; a Moonraker power-device cycle does: device off, wait at least
   20 s (15-20 s worked), on. Klippy was `ready` about 25 s later. After it,
   position, homing and heaters are all lost and the print state goes from
   `paused` to `standby`. Gate: the `PIN_AT_BOOT` pre-check is resolved and the
   queue is paused and empty. Then re-arm the `SET_IDLE_TIMEOUT` extension.
4. **DECLARE_Z_LIFT.** `SET_KINEMATIC_POSITION Z=<provisional> SET_HOMED=z`
   (only Z becomes homed, observed), with a provisional Z the human confirms
   from the nozzle-to-part gap (feeler gauge or paper; the Z axis uses T8
   leadscrews that can back-drive with the motors off). Declare **before** any
   `G28 X` or `G28 Y`: `safe_z_home` with Z not homed sets Z=0 and fakes the
   reference. Then lift with relative moves only (`G91`, 10 mm steps, F300-600)
   to the planner's pass-1 height or higher (part top + clearance + the 5 mm
   pin + margin). Exit check: position reads back.
5. **HOME_XY.** `G28 X`, then `G28 Y`, one command each, at height, Z declared
   at least `z_hop`. They home only their own axis (observed X -6, Y -14).
   `G28 Y` moves the bed, so the part sweeps under the head: the height gate
   comes first and the swept path is checked against the part box and the pin.
6. **PROBE_SPOT.** Move to a spot chosen by the planner's search mode for this
   part (`--suggest-spots`, verified with `--probe-spot`), at height: the
   probe point on the bed, the nozzle inside the envelope and not beyond
   `x_max_safe`, the head footprint at probe height clear of the part box plus a
   margin. For the live part this was the rear (nozzle X134 Y215); every front
   spot puts the X crossbar over the part. Outside the bed mesh is allowed with
   the R20 warning.
7. **PROBE_STROKES.** Descend toward the bed **only** with
   `PROBE PROBE_SPEED=5`, each stroke's physical length bounded by (declared Z
   minus `z_min`, -3). Re-declare Z higher at the same physical spot to start
   the next stroke (98 mm, 10 mm, 15 mm in the live run). Plain `G1` descents
   toward the bed are never allowed. After a probe the next move is up. The
   output (`// probe: at X,Y bed will contact at z=4.9125`) gives
   `last_z_result` (the trigger Z) and the contact Z.
8. **DECLARE_Z.** `SET_KINEMATIC_POSITION Z=<z_offset> SET_HOMED=z`, since the
   nozzle is `z_offset` above the bed at the trigger, plus the mesh difference
   between the probe spot and the home point (under 0.01 mm in the live run).
   `z_offset` comes from the live `printer.cfg`. State the known error sources
   with the result: bed temperature (50 C against 60 C when printing, about
   0.05 mm) and mesh extrapolation outside the meshed area. Whether a second
   probe spot detects a tilted gantry is **TO VERIFY**; the live run used one.
9. **PAPER_TEST.** With the **bed mesh loaded** (loading it after the paper test
   shifted a commanded 53.0 to a reported 52.957), approach the part in
   decreasing gaps (5, 2, 1, 0.5 mm) and paper-rub at the resume Z. The human
   confirms. Gate: no extrusion until this passes.
10. **PARK_HEAT_PURGE.** Move to the off-part park spot (the live run: nozzle
    X134 Y215, bare bed; it must be measured clear of the part, the pin and the
    cutter), heat to the **file header's** temperatures (bed 60, nozzle 220 in
    the live run, not the `START_PRINT` first-layer values), load the bed mesh,
    purge 10 mm, retract 2.5 mm. The thermal and drip rules apply; the open
    conflict between "standby temperature over the part" and "heat only off the
    part" is still undecided (see [print-rescue.md](print-rescue.md) step 10).
11. **LIFTED_TRAVEL.** Lift to 3 mm over the part, travel to the resume XY
    retracted, without dwelling hot over the part (30 s limit).
12. **LOWER_PRIME.** Lower to the resume Z (53.0 in the live run), prime
    (retracted amount plus 0.8 mm, 3.3 mm in the live run), `G92 E0`.
13. **UPLOAD.** Upload the state-and-tail resume file **without** the print
    flag. Check the metadata Moonraker derived (pre-check 10).
14. **START.** `POST /printer/print/start?filename=...`. Release the
    `START_PRINT` lock when the print is running. Watch the first layers.

## Mistakes of the live run that became checks

| Mistake | Check |
|---------|-------|
| Overrode an R13 refusal (placeholder footprint); the fan hit the part | Pre-check 1: motion blocked unless every tier is `measured`; no override |
| Did `FIRMWARE_RESTART` and a power cycle before checking `PIN_AT_BOOT` and pausing the queue | Pre-checks 4 and 5 come before POWER_CYCLE |
| Read `pin_down` plus `QUERY_PROBE` = TRIGGERED as a fault, although the repo docs say it is normal | Pre-check 3: the documented sequence, and a real `PROBE` from a high point; `QUERY_PROBE` alone is non-evidence |
| Blind `G1` descents toward the bed | State 7: bounded `PROBE` strokes only |
| Did not read the docs and skills first | The runner prints the relevant excerpt at each gate |
| The resume file lost the slicer header | The file keeps header, thumbnails and footer; pre-check 10 |
| Stale "shutdown cancelled" message left in the UI | `M117` without text after `CANCEL_PRINTER_SHUTDOWN` |
| Restarted without capturing evidence first | Pre-check 9 and state 1 |

## Not in this design

- Automatic recovery without a human. Every motion state has a human gate.
- Anything that talks to the MCU or Klipper outside Moonraker.
- The cause of the USB link stall (`urb stopped: -32`), which is unknown and
  is the reason rescues are needed.
- Measuring the head footprint, the cutter limit and the park spot; that is
  phase 3 and a prerequisite of pre-check 1.
