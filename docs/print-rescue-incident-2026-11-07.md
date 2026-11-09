# Incident 2026-11-07: USB link stall during a print, manual resume

Factual log of a print that Klipper shut down after 2 h 21 min, and of how it
was resumed. Times are UTC unless marked local (the host's kernel log is in
local time, UTC+1). The scrubbed evidence is in `print-rescue/fixtures/`
(see its `SOURCES.md`); the procedure that came out of it is in
[print-rescue.md](print-rescue.md).

## Summary

- **Symptom (established)**: the kernel logged `usb_serial_generic_read_bulk_callback - urb
  stopped: -32` on the read side of the USB serial adapter (CH340,
  `ch341-uart`). The host stopped hearing the MCU, and after Klipper's 5 s
  timeout it shut down with `Lost communication with MCU 'mcu'`.
- **Diagnosis (established from the Klippy `Stats` lines)**: `receive_seq`
  froze while `send_seq` advanced and `bytes_read` stopped. There was no
  `Reactor ... busy` warning before the shutdown.
- **Root cause: UNKNOWN.** Nothing here shows why the read URB stopped: cable,
  EMI, USB power, the adapter or the host controller are all still open.
- **Not host load**: no `Reactor ... busy` warning before the shutdown (the
  only one came after it), system load 0.23, about 500 MB free, Pi at 59 C.
- The same kernel message had appeared once before in this print, at 07:51:48
  local (06:51:48 UTC); the print continued. The captured Klippy log starts
  at 08:40 and does not cover it.
- The part was finished with a hand-built resume file. The analysis below
  shows that the resume started later in the file than the earliest possible
  stop.

## The print

OrcaSlicer 2.4.2, 255 layers, estimated 3 h 15 min, PLA (220 C nozzle after
the first layer, 60 C bed), one tool (T0), relative extrusion (M83), pressure
advance 0.08, `SET_VELOCITY_LIMIT ACCEL=2500 ACCEL_TO_DECEL=1750`, one object
(`EXCLUDE_OBJECT_DEFINE`). The part is a slim diagonal wall mount; its
extrusion box (including the first-layer skirt) is X/Y 29.8-188.4. A G-code
Z offset of 0.025 mm (baby-step) was active.

## Timeline

| Time | Event |
|------|-------|
| 07:43:44 local | CH340 attached as the printer's serial port (printer powered on through the job queue). |
| 06:43:53 | Klippy ready. |
| 06:44:01 | Print started. |
| 07:51:48 local | First `ch341-uart ... urb stopped: -32`. |
| 09:09:45.73 | Last `Stats` line where the MCU's `receive_seq` advanced (493669 = `send_seq`). `print_time` 8761.661, `buffer_time` 1.037, `sd_pos` 5398158 = line 202133 (layer 158, Z 20.6, outer wall). |
| 10:09:45 local | `ch341-uart ... urb stopped: -32`. |
| 09:09:46.73 | `send_seq` 493672, `receive_seq` still 493669, `bytes_read` frozen at 4945189; retransmit timeout growing (0.8, 1.6, 3.2 s). Only 181 more bytes (3 messages) were written; after that only retransmissions. The host kept planning: `sd_pos` reached line 202238 (09:09:46.7), 202325 (09:09:47.7), 202394 (09:09:48.7). |
| 09:09:50.736 | `Timeout with MCU 'mcu'`, `Transition to shutdown state: Lost communication with MCU 'mcu'`. |
| 09:09:50.80 | First `Stats` after the shutdown: `print_time` 8766.891. `gcode state`: `last_position` (184.976, 143.031, 20.625), `base_position` Z 0.025, speed 100 mm/s, M83. |
| 09:09:51.00 | `Exiting SD card print (position 5404182)`: the last dispatched line is 202396, a 153 mm inner-wall move ending at (184.976, 143.031). |
| 09:09:50.84 | Moonraker: Klippy has shut down. Job recorded as `klippy_shutdown`, print time 8456.5 s. |
| 09:25:32 | First restart: `Unable to issue reset command on MCU 'mcu'`; the new Klippy reported `Can not update MCU 'mcu' config as it is shutdown`. |
| 09:25:43 | Second restart; Klippy ready at 09:25:51.9. |
| 09:53:51 | Hand-built `resume_ps2.gcode` started: original file from line 202282, nozzle (154.017, 111.598), Z 20.6. |

## Stop analysis

What the log proves without any timing model: after the 09:09:45.73 sample
the host sent 3 more messages and then only retransmitted them. Motion for
G-code lines dispatched after that moment never reached the MCU. At that
sample the SD card was dispatching line 202133, and the MCU was about 1.04 s
(`buffer_time`) behind the planned motion.

`P_end - P_good` = 8766.891 - 8761.661 = **5.23 s** of motion was planned
after the last confirmed contact.

`print-rescue/plan.py` on this evidence (line positions below are the
**end** of each line, as in the planner's report; the window is the
conservative union of both anchors):

| Quantity | Value |
|----------|-------|
| End anchor (plan formula) walk-back | 4.23 s (latest) to 7.87 s (earliest): 6.27 s plus the 1.61 s lookahead bound, because line 202396 is a single long move that may never have been counted in `print_time` |
| sd anchor walk-back | 4.85 s (latest, line 202238) to 7.32 s (earliest, line 202002) |
| Planner vs Klipper | Klipper's `print_time` advanced 1.7 % faster than the planned time between the dispatched lines over layer 158; offset spread over the last 5 good samples 0.28 s |
| Anchor endpoints | earliest 0.56 s apart, latest 0.62 s apart: under the 1 s R10 limit |
| **Stop window (union)** | earliest possible stop: line 201951, end (171.5, 131.7), 7.87 s before the end of line 202396. Latest possible stop: line 202295, end (161.7, 119.1), 4.23 s before the end. |
| Most likely stop | about `P_good`: lines 202095-202135, near (78-91, 38-51) |
| Resume candidate (tool, default budget) | line 201925, layer 158, commanded Z 21.0 (the layer is at Z 20.6); overlap to the latest possible stop **4.83 s, 426 mm**. Maximum allowed is 3.0 s, so the tool **refused with R12** (at the time; R12 is a warning now, see the live validation record). |
| Probe pin at boot | over the part for every position in the window |

Note: the table is the record of the first planner version, which mapped
`ACCEL_TO_DECEL` to a cruise ratio. The pinned fork ignores that parameter, so
the planner no longer models it; on the same evidence it now gives lines
201962-202298 and an overlap of 4.95 s for the same resume line 201925.

The tool's first version, before the window was combined, chose line 201955
(nozzle (170.7, 131.2) at its start, 2.98 s of overlap, 216 mm). That choice
used the sd anchor alone. The end anchor's earliest possible stop is 0.5 s
earlier, so line 201955 started 0.54 s after the earliest stop minus the
margin, that is 0.05 s after the earliest possible stop itself. The fix uses
the earlier of the two earliest stops, and the overlap grows to 4.83 s.

The manual walk-back used a timing model (accel 2500, square corner velocity
5) that gave 4.6 s for line 202335, 5.2 s for 202314 and 6.1 s for 202282.
Klipper's own samples show about 85 lines per second in this region (lines
202238 to 202325 in 1.02 s), so 202282 to 202314 took about 0.37 s, not
0.9 s: the manual model was about 2.5 times too slow. Against the combined
window, line 202282 starts 0.14 s **inside** the latest possible stop, and
lines 202314 and 202335 start 0.21 s and 0.44 s after it. The same model put
the physical stop at (170-183, 128-140); the log places it on the other side
of the part.

If the stop was at about `P_good`, the hand-built resume left a gap of about
**106 mm of planned path**: 105.7 mm from the start of line 202133 (the last
confirmed sample, at (79.6, 37.1)) to the start of line 202282 (154.0, 111.6),
which is 105.3 mm in a straight line. It ran from the lower-left of the part's
outer wall to the upper-right. **This has not been checked on the part.** A
complete wall there would mean the timing or the `print_time` semantics are
wrong, and that must be resolved before phase 2.

## What worked

- Rebuilding the state in the resume file: `G90`/`G21`/`M83`, the
  `EXCLUDE_OBJECT_DEFINE` line, pressure advance, the velocity limit, fan,
  nozzle and bed temperatures, `G92 E0`.
- Approaching from 3 mm above the resume Z, then lowering, then priming
  2.5 mm (the retraction before the travel) + 0.8 mm (ooze compensation).
- Two restarts brought Klippy back; the first could not reset the shut-down
  MCU, as described in the printer-ops skill.

## Mistakes and lessons

- A `docker logs --since/--until` call without a `Z` suffix used local time
  and the first capture missed the window. Always pass UTC with `Z`.
- The resume point came from a hand timing estimate. Use Klipper's own
  `Stats` (`sd_pos` with `print_time` and `buffer_time`) and a planner that
  matches Klipper's; prefer overlap to a gap. The tool's earliest-stop check
  must use both anchors, not one.
- The resume file had no `EXCLUDE_OBJECT_START`, so the slicer's next
  `EXCLUDE_OBJECT_END` produced a "does not match NAME=None" warning. The
  builder now emits `EXCLUDE_OBJECT_START NAME=<current object>`.
- The resume file did not restore the G-code Z offset (0.025 mm) or load the
  bed mesh; whether that was done by hand before starting is not recorded.
  The builder now restores the offset from the shutdown dump, and its preview
  loads the bed mesh (`BED_MESH_PROFILE LOAD=`).
- The CR Touch pin sits 23 mm left and 14.5 mm in front of the nozzle; at the
  stop it was over the part. Check where it lands before any restart.
- Two USB stalls in one print point at the link, but the cause is not known.
  Repeat stalls should be investigated before long prints.
