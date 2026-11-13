# Print rescue runbook: resume a stopped print in one pass, without AI

> **Hold: do not run `rescue_run.py` against a printer until the open review findings in
> [print-rescue-handoff-2026-11-08.md](print-rescue-handoff-2026-11-08.md) are closed.** As of 2026-11-10 findings 2 (the "no trigger"
> redeclaration of Z), 4 (the second job builder skips the planner's refusals) and 1 (a safety review of `rescue_run.py`) are still
> open; 3 and 5 are fixed. The procedure below describes what was validated live, not a gate that has been cleared.

Status: **prototype, validated live twice** (2026-11-08, same part, two different stops). It moves the machine. Read
[print-rescue.md](print-rescue.md) for the design, the refusal catalogue (R01-R22) and the known issues; this file is the
procedure with every number and where it came from. Placeholders: `<host>` is the Moonraker host, `<alias>` an ssh alias,
`<dir>` a scratch directory outside the repo (captures contain host data and must not be committed).

**Rules that hold for every step**

1. Someone stands at the printer with a finger on the emergency stop for the whole run. The scripts never replace that.
2. Nothing is restarted or power-cycled before the evidence is captured (step 1).
3. **Heat the bed first** and hold it (`M140 S<bed>` and `SET_IDLE_TIMEOUT TIMEOUT=14400`) as soon as Klippy answers: a part left on a
   cooling bed detaches (the vase did, 2026-11-07).
4. The nozzle is never lowered toward the part with an unmeasured guess: Z comes from a probe at a bare-bed spot, and the head box
   comes from the profile.
5. The CR Touch rules in "CR Touch" below are not optional.

## 0. What you need

| Item | Value on this printer | Source |
|---|---|---|
| Machine | Ender 3 V3 SE, Klipper fork, CR Touch (BLTouch mode), Moonraker + Mainsail | repo |
| Probe offsets from the nozzle | x -23, y -14.5 | `printer.cfg` `[bltouch]` |
| `z_offset` (nozzle height above the bed at the probe trigger) | 2.96 mm (the live value is compared with the profile before moving: R14) | `printer.cfg` SAVE_CONFIG block |
| Axis limits | X -6..230, Y -14..230, Z max 250; `z_min` for probing -3 | `printer.cfg` |
| Bed / nozzle temperatures | the file's own `M140`/`M104` (60 / 220 for the vase), NOT `START_PRINT`'s first-layer 65 / 230 | the G-code |
| Probe spot used | nozzle **X134 Y215** (probe point X111 Y200.5), bare bed, outside the part's bed-mesh area | used live, 2026-11-07 and 2026-11-08 |
| Hot dwell limit over the part | 30 s | profile `thermal.max_hot_dwell_over_part_s` |
| Probe speed | 5 mm/s | profile `probe.rescue_probe_speed` |

### Head footprint (distances from the nozzle tip) - OWNER-STATED, NOT MEASURED WITH CALIPERS

| Direction | mm | Note |
|---|---|---|
| Left (-X) | 70 | cooling fan |
| Right (+X) | 100 | with the cutter lever extended |
| Back (+Y) | 65 | counting the X crossbar (about 30 to the carriage) |
| Front (-Y) | 50 | |
| Lowest point | nozzle level | worst case |
| CR Touch pin deployed | extends `z_offset` (2.96) below the nozzle tip | derived: the nozzle is 2.96 above the bed when the pin triggers |

These live in `print-rescue/profiles/creality-ender3-v3-se.json` (`head_footprint`, source `user`). While they are not measured the planner
refuses motion (R13) and `rescue_run.py` refuses unless `--allow-unverified` is passed; it then adds `--margin` (default 10 mm) on every
side. **Measure them with calipers (left, right with and without the lever, back, front, lowest point of the fan duct vs the nozzle tip)
and enter them as `source: measured` with `method` and `measured_on`; that removes the flag.** A first live attempt with the placeholder
box (+-25/+-20) put the fan on the part: never run on the placeholder.

### Part geometry - computed, not drawn

`rescue_job.py` takes the part box from every extrusion (`G1` with X/Y and positive `E`) of the G-code up to the resume line. For the
2026-11-08 vase: X 71.07-148.93, Y 71.07-148.93 over all layers (a ring of radius 34.9-37.5 around X110 Y110 at the stop height), top at
the current layer Z. Never check a gap or a paper test over a point that is not on a printed bead (the vase is hollow inside).

## 1. Capture the evidence (before any restart)

Docker logs rotate (5 MB x 2: about an hour). Do this first, from any machine that can reach the printer:

```bash
mkdir -p <dir>/capture && cd <dir>/capture
ssh <alias> 'docker logs klipper 2>&1'    > klippy.log
ssh <alias> 'docker logs moonraker 2>&1'  > moonraker.log
ssh <alias> 'journalctl -k --no-pager'    > kernel.log        # volatile across a Pi reboot
curl -s http://<host>/printer/info                          > info.json
curl -s 'http://<host>/printer/objects/query?print_stats&virtual_sdcard&toolhead&heater_bed&extruder' > objects.json
curl -s http://<host>/server/job_queue/status               > queue.json
curl -s 'http://<host>/server/history/list?limit=3'         > history.json
```

Pause or empty the Moonraker job queue (`POST /server/job_queue/pause`) so nothing starts on restart. Keep a kernel stream running for
the next print: `ssh <alias> 'journalctl -k -f --no-pager' >> <dir>/kernel-stream.log &` (a Pi reboot kills it; relaunch it).

## 2. Bring the machine back

| What you see | Cause | Do |
|---|---|---|
| Klippy `shutdown`, `Lost communication with MCU 'mcu'` | the `-32` stall (cause still unknown) | `SET_IDLE_TIMEOUT TIMEOUT=14400` and `M140 S<bed>` if it answers; otherwise power-cycle the printer (Moonraker device `printer` off >= 25 s, on) |
| After a Pi reboot Klippy says `Include file '/run/klipper/mmu_temp.cfg' does not exist` | boot race: Klippy starts before `mmu-fallback` writes it | `POST /printer/restart` |
| `Can not update MCU 'PICO_MMU' config as it is shutdown` | the MMU Pico stays powered by the Pi and keeps its shutdown | `POST /printer/firmware_restart` (about 60 s to `ready`) |
| Pi and board down, then back on their own | mains power cut (the log just ends, no `shutdown` line) | as above, then use `--exit-pos` in step 3 |
| The Moonraker `printer` device reads `off` and its status query errors | the plug is a Home Assistant switch; the query fails when HA/DNS is down | trust it only after the query succeeds |

No `sudo` on the Pi for the ssh user: reboot the Pi with `POST /machine/reboot`.

## 3. Build the job and the resume file

`print-rescue/rescue_job.py` reads the ORIGINAL G-code and the Klippy log of the stop. The log contains the host's exact state at a
`Lost communication` shutdown: `gcode state: ... last_position=[x, y, z, e]`, `Virtual sdcard (N)`, `Upcoming (M)`,
`Exiting SD card print (position P)`. Byte `P` is the end of the last dispatched line.

```bash
python3 print-rescue/rescue_job.py \
  --gcode  <original>.gcode \
  --log    <dir>/capture/klippy.log \
  --exit-file <file that was printing>.gcode \      # omit if it was the original; give the resume file if you resumed before
  --polls  <dir>/poll.csv  \                        # or: --rate-bps <bytes/s>
  --overlap-s 3 \
  --out-dir <dir>/job --resume-name resume_<name>.gcode
```

* **Power cut:** the log has no exit line. Use `--exit-pos <file_position>` from the last Moonraker poll (`poll.csv` column `file_pos`, or
  `virtual_sdcard.file_position`), and add the bytes the file advanced between that poll and the cut (rate x seconds).
* **Overlap:** the MCU keeps running its queue after the link dies, so the physical stop is at or before the dispatched end by at most
  Klipper's lookahead (about 2 s of print time). Bytes are not seconds (the vase's file rate varied 516-5317 B/s), so the overlap is a
  bounded margin: `overlap_s x measured rate` bytes. The default 3 s equals `stop_estimation.max_overlap_s`. The rate depends on the
  window used: the tool uses the last 100 s of `printing` polls. **For incident 6 the run used `--rate-bps 1543`, which gives resume line
  810522 and a file byte-identical to the one that printed; the default window (1843 B/s) would have chosen line 810482.**
* The resume line is always a plain extrusion `G1 X.. Y.. E<positive>` (never a wipe retraction), so the nozzle restarts mid-bead on
  printed material with the filament at its nominal position. The park point is the end of the previous XY move.
* The resume file keeps the slicer header and thumbnails, then a state-only preamble: `CANCEL_PRINTER_SHUTDOWN`, `M117`, `G90 G21 M83`,
  `EXCLUDE_OBJECT_DEFINE`, pressure advance, velocity limit, `M106`, `M140`/`M104` from the file, `G92 E0`, `EXCLUDE_OBJECT_START`, `G17`,
  the last feed rate, `SET_PRINT_STATS_INFO TOTAL_LAYER/CURRENT_LAYER` (and one per later layer). No motion, heating, mesh or probe.
  Moonraker then reads slicer, layer count and thumbnails from it (checked: `OrcaSlicer`, 500 layers, 2 thumbnails); the estimated time
  still shows the full job.

Read `job/job.json` before going on: `stop.exit_line_text` must be the line you expect (a travel after a wipe, or a bead), `resume.text`
a bead, `part` the box you expect, `temps` the file's temperatures. Upload without starting:

```bash
curl -X POST http://<host>/server/files/upload -F file=@<dir>/job/resume_<name>.gcode -F print=false
```

## 4. Run the rescue (finger on the emergency stop)

```bash
python3 print-rescue/rescue_run.py --host <host> \
  --profile print-rescue/profiles/creality-ender3-v3-se.json \
  --job <dir>/job/job.json --probe-spot 134,215 [--allow-unverified] [--dry-run]
```

Do a `--dry-run` first: it reads the printer and prints every command without sending any. What the live run does, in order:

1. **Preflight (stops on any failure):** profile motion-ready or `--allow-unverified`; Klippy `ready`; no print active; live `z_offset` equals
   the profile (R14); bed within 5 C of the job's bed temperature; job queue empty; probe spot clear of the part; park point inside the part
   box; resume file present on the printer.
2. `SET_IDLE_TIMEOUT 14400`, `M140`.
3. **Z reference.** Declare Z = the job's layer Z as a LOWER bound (`SET_KINEMATIC_POSITION Z=<z> SET_HOMED=z`, before any `G28`; the
   fork's `safe_z_home` would otherwise fake Z=0). A hand-lifted head makes the true Z higher, and the lower bound is the safe side in both
   directions. Lift in 10 mm relative steps to 120 (never past the Z maximum minus 5), `G28 X`, `G28 Y` (the bed moves, so the head is high),
   move to the probe spot.
4. **`PROBE` loop.** A stroke is bounded by declared Z minus -3. If a stroke ends without a trigger the pin cannot be below `z_offset`, so
   `SET_KINEMATIC_POSITION Z=2.96` is taken as a lower bound and the stroke repeats (**open finding 2 of the handoff:** the head stopped
   at the end of the stroke, below `z_offset`, so the next move can land low; this has not been fixed or measured) (each repeat descends at most 5.96 mm). On a trigger:
   `SET_KINEMATIC_POSITION Z=2.96`. If `PROBE` answers `Probe triggered prior to movement` or `Failed to verify BLTouch probe is raised`, the
   script powers the printer off >= 25 s, on, restarts Klippy as needed, re-declares the lower bound, re-homes X/Y and probes again.
   (2026-11-08: the first stroke of 123 mm had no trigger, the second triggered 5.14 mm later: the head had been lifted about 11 mm by hand.)
5. **Heat and purge at the probe spot** (bare bed, 10 mm up): `M104`, wait, 12 s soak, `M83`, extrude 10 mm, retract 2.5 mm.
6. Lift to Z 100 (the hot dwell clock starts at the next line), load the bed mesh (profile `bed_mesh_profile`), travel to the park point at
   Z 100: the guard allows it only because the lowest point of the head (Z 100) is above the part top plus 5 mm.
7. **Descent** to the layer Z: Z top+8.4 at 10 mm/s, top+3.0 at 5 mm/s, top at 1 mm/s; each Z is read back (tolerance 0.12 mm: the mesh shifts
   Z by about 0.04). Prime +2.5 mm (nominal filament, since the first resumed line is a bead).
8. Abort if the hot dwell exceeded the limit, else `POST /printer/print/start` of the resume file.

On any failure after the nozzle is hot: `M104 S0` and a 10 mm lift. The head-vs-part guard checks every XY move along its path (every
2 mm) against the head box (+ margin when unverified) whenever the lowest point of the head is below part top + 5 mm; the probe spot is
refused if the probe point or the head box touches the part (the front spot nozzle Y30 is refused because the crossbar would sweep over the
part).

## 5. Watch the print

```bash
python3 print-rescue/rescue_watch.py --host <host> --out <dir>/run --ssh <alias> --kernel-stream <dir>/kernel-stream.log
```

Polls every 10 s into `poll.csv`; on the first failure it writes `FAILURE.txt` plus the last 15 min of kernel log and the Klippy log.
Exit codes: 0 complete, 2 Klippy not ready / paused / error / position frozen 600 s, 3 cancelled, 4 Moonraker unreachable x4, **5 the Pi
rebooted** (a mains power cut looks like this: the log of the previous boot is gone unless the journal is persistent). New `urb stopped`
lines in the kernel stream are counted in `events.txt` without stopping.

## 6. After it starts

* Look at the join (the overlap deposits about 3 s of extra material over printed beads) and at the first beads: a bead that is too thin or
  squashed means the Z is off. Stop (Mainsail cancel or emergency stop) rather than let it run.
* The final Z has no paper test in the one-pass run. The probe reference was confirmed once against the part by paper (agreement 0.0 mm,
  2026-11-07); the join quality of the one-pass run was not inspected when this was written, and the gantry tilt between the probe spot and
  the part is not measured.
* Remove the purge blob left at the probe spot.

## CR Touch (read before touching the probe)

* It works. Do not conclude "broken" from anything except a real `PROBE`.
* The only valid test is `PROBE PROBE_SPEED=5` from a high point over bare bed. `Probe triggered prior to movement` means latched: cut the
  printer power for 20-25 s and probe again. `BLTOUCH_DEBUG COMMAND=reset` did not clear it.
* Do not judge it with `pin_down` + `QUERY_PROBE` (it reads TRIGGERED on a healthy probe). Do not run `PROBE_ZOFFSET` /
  `PRTOUCH_PROBE_ZOFFSET` (broken with this Klipper, see [klipper-mmu-upgrade.md](klipper-mmu-upgrade.md)).
* Hypothesis, unproven: the alarm follows a power cut, an emergency stop or a power-on with the pin deploying against or next to the part.
  Power-cycle with the head high over the bare bed, never a few mm above the part.

## Validation record (what was actually run)

| Date | Part | Stop | Resume line / method | Result |
|---|---|---|---|---|
| 2026-11-07 | Japandi vase, layer 265, Z 53.0 | `-32`, MCU lost | original line 390865, hand-driven through Moonraker one command at a time, Z by paper on the part | printed 71 min more, then stopped again |
| 2026-11-08 | same vase, layer 203, Z 40.6 | mains power cut | original line 261792 (a hop after a wipe), hand-driven | printed 2 h, then stopped (`-32`) at layer ~410 |
| 2026-11-08 | same vase, layer 408, Z 81.6 | `-32`, MCU lost | original line 810522, `--rate-bps 1543`, **`rescue_run.py` one pass in 2 min 27 s, 10 s hot over the part** | resumed on layer 408; the outcome of the print is in the vault log |

## Known gaps (what is NOT done)

* Head dimensions are owner-stated, not calipers (see above). All `safety_critical` profile leaves read from `printer.cfg` or the user stay
  "unverified" by design (27 listed by R13).
* The resume line for a power cut with no exit line is chosen from the last poll; the planner (`plan.py`) still refuses that case.
* The final descent has no gap confirmation or paper test; the gantry tilt is not measured.
* `rescue_run.py` has no Mainsail macro that starts it. A request-only macro plus a worker that prepares the plan and waits for a typed
  confirmation exist (not deployed, stub-tested): see [print-rescue-worker.md](print-rescue-worker.md). The run itself stays manual.
* The cause of the `-32` stalls is not known (kernel `urb stopped: -32` on the CH340, `dwc_otg` FSM warnings, no undervoltage). Cables moved
  and a short test print ran clean; two longer prints stopped. Software is not ruled out (new containers on 2026-11-04/05).
* The Pi journal is volatile: a reboot loses the previous boot's kernel log. Keep the kernel stream running on another machine.

## Reproduce the checks

```bash
python3 -m unittest discover -s print-rescue -v     # includes test_rescue_job.py, test_rescue_run.py (simulated Moonraker) and test_rescue_worker.py
```

`test_rescue_run.py` checks: R13 refusal sends no motion; command order (lower bound declared before `G28`, probe after homing, print start last);
every XY move happens above the clearance or with the head clear of the part; a latched probe power-cycles and recovers; cold bed, wrong
`z_offset`, missing file and queued job are refused; the hot-dwell limit cools and lifts instead of starting; `--dry-run` sends nothing.
Disabling the geometry guard, the dwell check or the R13 check makes these tests fail (checked by hand).
