# Print rescue: the live run of 2026-11-07 (incident 3)

This page records what a real, manual rescue taught the print-rescue planner.
It is evidence, not a validation: one print, one printer, steps typed by hand
through Moonraker with the owner watching and holding the emergency stop. It
contains no host data; the scrubbed fixtures that back the numbers are listed in
`print-rescue/fixtures/SOURCES.md`.

Honest limits, up front:

- The **joint quality is not yet judged.** The print ran normally for the first
  minutes after the resume; whether the join is sound is not known.
- The **head footprint is the owner's estimate**, not caliper measurements.
- The **root cause of the CR Touch alarm episode is unknown** (below), and so is
  the cause of the shutdown itself.
- Everything was done by hand. No motion code of this repository ran.

## What happened

The part was a vase, 53.00 mm tall at the stop (layer 265 of 500, part box X/Y
71.1-148.9). The shutdown was `Lost communication with MCU 'mcu'`, with
`urb stopped: -32` from the USB serial adapter in the kernel log in the same
minute. It was the fourth shutdown of that kind on the same day; the cause is
still unknown.

Timeline (relative, in order):

1. **Shutdown.** The job ended as `klippy_shutdown`. The job queue was empty and
   paused throughout.
2. **Evidence capture** about 1.5 hours later, just before the Docker log
   rotation would have dropped the Klippy log.
3. **Planner run** on the saved evidence (read-only): see the next section.
4. **First motion attempt** with the placeholder head footprint (plus or minus
   25 mm in X, 20 mm in Y). At nozzle X185 the fan, which reaches 70 mm to the
   left, overlapped the part; descending toward Z10 put the fan on the part and
   the owner hit the emergency stop. The R13 block had been overridden by hand.
5. **CR Touch alarm episode.** After the emergency stop in the middle of a
   descent, `pin_down` and `touch_mode` followed by `QUERY_PROBE` read
   `TRIGGERED` repeatedly, with a red blinking LED; `self_test` ended
   `TRIGGERED`; `PROBE` failed with "Probe triggered prior to movement" and
   "Failed to verify BLTouch probe is raised; retrying". After another power
   cycle and a fresh homing, `PROBE` worked normally (a full stroke with "No
   trigger", then a clean trigger). The earlier conclusion that the sensor was
   broken was wrong; the repo docs already said that `pin_down` plus
   `QUERY_PROBE` reads triggered when healthy. The root cause of the alarm is
   not established (candidates: the emergency stop in the middle of a descent;
   repeated `pin_down` and polling).
6. **Recovery.** A Moonraker power-device cycle (off, at least 15-20 s, on)
   reset the MCU; `FIRMWARE_RESTART` had not. Klippy was `ready` about 25 s
   later; position, homing and heaters were lost and the print state went from
   `paused` to `standby`.
7. **Z re-reference** (details in [print-rescue.md](print-rescue.md), step 7-9):
   a provisional Z declared with `SET_HOMED=z` (only Z homed), a relative lift,
   `G28 X` and `G28 Y` at height, a rear probe spot (nozzle X134 Y215, probe
   point X111 Y200.5), bounded `PROBE PROBE_SPEED=5` strokes (98, 10, then
   15 mm), then Z declared as `z_offset` (2.96).
8. **Paper test on the part** at Z 70, 58 (gap about 5 mm seen), 55 (2 mm), 54
   (1 mm), 53.5 (0.5 mm), 53.0 (the paper rubbed). Loading the bed mesh after
   the paper test shifted the commanded 53.0 to a reported 52.957.
9. **Park, heat, purge, retract, lifted travel, lower, prime** by hand: nozzle
   X134 Y215 on the bare bed; bed 60 and nozzle 220 from the file header (not
   the `START_PRINT` first-layer 230/65); mesh loaded; purge 10 mm, retract
   2.5; lift to Z56; travel to the resume XY; lower to Z53.0; prime 3.3 mm.
10. **Resume file** (state and tail only, no motion, heating, mesh or probing),
    uploaded without the print flag and started through the print-start
    endpoint. It ran normally for the first minutes. In the job panel, "Layer 0
    of 0", no slicer, no thumbnail, an incoherent estimate and a stale
    "shutdown cancelled" message showed: the hand-built file had dropped the
    slicer header.

## What the planner got right

Run read-only on the saved evidence, it refused for reasons that were all
legitimate, and warned of the right danger:

- stop window lines 390902-391125, resume candidate line 390866 (X134.561
  Y137.957 Z53.000), overlap 3.55 s / 104.8 mm;
- R14: the profile's `z_offset` 3.035 against the live 2.96 (the live value was
  right);
- R02 and R13: no measured probe spot, unmeasured profile fields (that is what
  the placeholder footprint override had ignored);
- `PIN_AT_BOOT`: the pin was predicted to deploy over the part at the stop
  position (the owner saw no mark after a boot self-test, which is not proof).

The byte-offset check also held: the end of line 391419 is 8772080, which is
the logged `Exiting SD card print (position 8772080)` and Moonraker's
`file_position`.

## What it got wrong

- **R07 refused on the file's modified time**, although the file had only been
  re-uploaded and its contents were identical. The content check by byte offset
  replaces it; the mtime is now the R19 warning.
- **R12 refused for an overlap of 3.55 s against a 3.0 s budget.** Refusing
  favours a gap in the wall. It is now a warning with the extra material and an
  operator budget; only absurd overlaps refuse.
- **R14 on a stale profile copy** of `z_offset`; the live value from
  `printer.cfg` is used now.
- **The rule "a probe spot must be on the meshed area"** would have rejected the
  working rear spot, which lies outside the mesh (the mesh probe area ends at
  Y190). Mesh deviation there was under 0.05 mm (one observation). Now an R20
  warning with the distance.
- **No spot search**: with the owner's head estimate, only rear spots work for
  this part, and any front spot puts the X crossbar (65 mm back) over the part.
  The planner now has a search mode.
- **The preview** lacked `CANCEL_PRINTER_SHUTDOWN` (`START_PRINT` calls it, and a
  resume skips `START_PRINT`, so an armed auto power-off could cut the power
  mid-print) and `G17`; it mixed parking, heating and motion into the file
  behind an invalid-command gate, while the live run did that motion outside
  the file; and it did not keep the slicer header or set the layer counter.

## Measured and estimated values

| Value | Result | Provenance |
|-------|--------|------------|
| `z_offset` | 2.96 mm | Live `printer.cfg` SAVE_CONFIG block; the profile said 3.035 |
| Probe trigger, `last_z_result` | 7.8725 | Observed `PROBE` output |
| Contact Z (`last_probe_position[2]`) | 4.9125 | Observed; trigger minus `z_offset` |
| Mesh difference, probe spot against home | below 0.01 mm | Observed in the run |
| Mesh deviation at the rear spot | below 0.05 mm | One observation |
| Bed temperature during the probe | 50 C (printing: 60 C) | Observed; about 0.05 mm error source |
| Paper test, commanded to reported Z with the mesh loaded after | 53.0 to 52.957 | Observed |
| Head footprint: left 70, right 100 (lever extended), front 50 | mm from the nozzle tip | **Owner estimate**, not calipers |
| Head footprint: back about 30 to the carriage, about 65 with the crossbar | mm | **Owner estimate** |
| Lowest point of fan and duct | about nozzle level | **Owner estimate**, treated as worst case |
| CR Touch offsets | X -23, Y -14.5 | `printer.cfg` |
| Net filament at the stop | 11098.87 mm | Log `last_position` E and the history, equal |

## Errors made and the checks they became

| Error | Check |
|-------|-------|
| Overrode R13 with a placeholder footprint; the fan hit the part | Motion blocked unless every tier is `measured`; no override |
| `FIRMWARE_RESTART` and a power cycle before checking `PIN_AT_BOOT` and the queue | Both are pre-checks before the power cycle |
| Concluded "sensor broken" from `QUERY_PROBE` | The documented sequence plus a real `PROBE` from a high point; `QUERY_PROBE` alone is non-evidence |
| Blind `G1` descents toward the bed (once, on the owner's instruction) | Only bounded `PROBE` strokes descend toward the bed |
| Did not read the docs and skills first | The runner prints the relevant excerpt at each gate |
| Resume file without the slicer header | The header, thumbnails and footer are kept; layer counter set; estimates scaled; M117 clears the message |

See [the P2 design](print-rescue-p2-design.md) for where each check sits.

## Mapping onto the "Hardware validation order"

| # | Step | Covered by this run? |
|---|------|----------------------|
| 1 | `plan.py` on the fixtures and on a new capture | **Yes.** The numbers above were reproduced on a scrubbed excerpt and match the full file; all inputs accepted. The planner's R07 and R12 verdicts were wrong in kind and were changed. |
| 2 | Pin deployment on `FIRMWARE_RESTART` | **No.** The owner saw no mark after a boot self-test; no experiment with a paper under the pin. Remains. |
| 3 | `SET_KINEMATIC_POSITION` and `homed_axes` | **Yes.** Only the named axis is marked homed (`z`). |
| 4 | `G28 X`, `G28 Y` with Z not homed | **Partly.** Observed with Z declared at least `z_hop`: no hop, each homes its own axis. With Z **not** homed, the fork source (read, not run) fakes Z=0 and lifts `z_hop`. Remains for Z not homed. |
| 5 | Cutter approach in 0.5 mm steps | **No.** Remains. |
| 6 | Head footprint measured | **No.** Only the owner's estimate. Remains, and it is the reason R13 stays on. |
| 7 | `PROBE_ACCURACY` at each spot | **No.** Single probes only. Remains. |
| 8 | Re-reference dry run with a gauge block | **No.** The paper test on the real part agreed with the probed Z (the paper rubbed at 53.0), which is one observation, not a dry run against a homed Z. Remains. |
| 9 | Two-point tilt detection | **No.** One probe spot was used. Remains. |
| 10 | Sacrificial print, simulated stop, full procedure | **No.** It was a real stop on a real part, by hand, and the join is not yet judged. Remains. |
| 11 | Park spot, heat and drip rules | **Partly.** A rear park spot (X134 Y215) was clear of the part; heating at the park spot and a short lifted travel worked. The drip-rule conflict is still undecided, and the park spot is not measured against the cutter. |

## Still unknown

- The root cause of the CR Touch alarm episode.
- The cause of the USB link stall that shut Klipper down four times that day.
- Whether the pin deploys at Klippy start.
- Whether the join is sound.
- How Moonraker and Mainsail derive layer, slicer and estimate from a generated
  file (so whether the header and counters of the new resume file are enough).
