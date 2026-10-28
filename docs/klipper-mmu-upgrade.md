# Klipper + Pico MMU upgrade notes (2026-08 fork / LH Stinger 0.94)

This documents the upgrade of the pinned Klipper fork and the vendored Pico MMU
(LH Stinger) macros, what changed in how distances are computed, how to verify
the result on the printer, how to roll back, and one **known issue that was
deliberately left unfixed** (Z-offset via `PRTOUCH_PROBE_ZOFFSET`).

| | Before | After |
|---|---|---|
| Klipper fork (`0xD34D/klipper_ender3_v3_se`) | `38604d84` (2025-07-18, upstream base 2025-07-11) | `7e61c1cc` (2026-08-17, upstream base 2026-08-13) |
| LH Stinger `sp_mmu_code.cfg` | template 0.91 (April 2025 code) | template 0.94 (LH Stinger `main`, April 2026) |

The commit is pinned in three places that must always match:
`Dockerfile-klipper`, `sim/Dockerfile-sim`, `build_firmware.sh`.

## What changed and why it matters

### 1. Synchronized MMU + extruder moves (`sync_mode`)

The 0.91 code synchronized the MMU motor and the extruder with two independent
commands (`_SP_MOTOR SYNC=0` followed by `FORCE_MOVE STEPPER=extruder`). Klipper
reworked step generation and toolhead scheduling in September 2025, and LH
Stinger changed its defaults for that reason. The new code has a `sync_mode`
setting in `_SP_VARS` (`config/macros/sp_mmu.cfg`):

| `sync_mode` | Method | Meant for |
|---|---|---|
| `2` (default here) | one `G1 E.. P..` move with the MMU motor as an extra G-code axis | current Klipper |
| `1` | `G92 E0` + `G1 E..` after an unsynchronized MMU move | older Klipper |
| `0` | `FORCE_MOVE` on the extruder (the 0.91 behavior) | Klipper 0.12–0.13 / Kalico |

If the two motors do not move together during loading or unloading, distances
are wrong **without any error in the log**. That is the failure mode to rule
out before printing (see "Verifying the MMU" below).

### 2. Other MMU changes

- `SP_CHANGE` was renamed `_SP_CHANGE`. A compatibility alias `SP_CHANGE`
  is kept in `sp_mmu.cfg` so existing Orca Slicer profiles keep working.
  New profiles should use `_SP_CHANGE T=[next_extruder]`.
- `_SP_PURGE` is now a user hook in `sp_mmu.cfg` (it was inline code in 0.91).
- New `_SP_VARS` options: `sync_mode`, `legacy_endstop_msg`,
  `retrigger_guest_lane`, `skip_first_park`.
- `STOP_ON_ENDSTOP` is sent with the new names (`home`, `try_home`, ...).
  The old integers still work in Klipper (they map to the same behavior) but are
  deprecated. If Klipper reports an error about the stop type, flip
  `legacy_endstop_msg`.
- Local patches kept on top of the vendored `sp_mmu_code.cfg`:
  `RESUME` wraps `_MAINSAIL_RESUME`, the Z safety check uses
  `toolhead.position` and pauses on a large gcode-vs-toolhead Z difference,
  `_SAVE_POS` is called before the sidestep, and `_SP_RESUME` reads its saved
  state into local variables. Re-apply them on every vendor update.
- Your tuned values (`dist_*`, cutter positions, `dist_filament_park`,
  `has_guest_lane`, ...) are preserved; only the four new options and the
  version number were added.

### 3. Klipper changes that do not need action (checked)

- `MANUAL_STEPPER STOP_ON_ENDSTOP=1/2/-2` map to identical semantics.
- `printer.gcode_move.gcode_position.e` (used by `timelapse.cfg`) is deprecated
  but still works.
- `[force_move] enable_force_move: true` is still required and present.
- `max_stepper_error` / `max_accel_to_decel` are not used in this config.
- Probe console output now reports Z relative to the nominal bed instead of
  relative to the probe `z_offset`. Numbers printed by `PROBE` and
  `PROBE_ACCURACY` therefore differ from before by the configured `z_offset`.
  That is a reporting change, not a calibration change.

## Applying the upgrade

1. Keep the previous firmware `.bin` files and the previous Docker image tag
   so you can roll back (see below).
2. Rebuild the Klipper image (`docker compose build klipper`, or pull the image
   published by CI for this commit).
3. **Phase 1 – new host, current MCU firmware (no flashing).** Klipper still
   supports older MCU firmware: an MCU built before 2026-02 that lacks
   `STEPPER_STEP_BOTH_EDGE` only produces a deprecation warning
   ("Recompiling and flashing is recommended"). Run the whole verification
   below in this state first. Rolling back is then just switching the image and
   config back, with no firmware involved.
4. **Phase 2 – flash the MCUs (recommended, optional).** Only after Phase 1 is
   verified. Build both with the same fork commit:
   - Main board: `sudo bash build_firmware.sh` (or build in Docker without
     touching the host), then flash via SD card as before.
   - Pico MMU (STM32G0B1): see the EBB42 DFU steps in `nebula_setup_guide.md`.
   Keep the previous `.bin` for each MCU so the firmware can be rolled back.
5. `docker compose up -d klipper`, then check `klippy.log` for `Klippy state:
   Ready` and any deprecation warnings.
6. Back up the MMU memory file before recreating the Klipper container: the
   `[save_variables]` file (`~/printer_data/sp_mmu_data.cfg` inside the
   container) is **not** on a volume, so recreating the container resets the
   remembered loaded lane. Copy it out with
   `docker cp klipper:/home/klipper/printer_data/sp_mmu_data.cfg .` and copy it
   back into the new (not yet started) container with `docker cp`.
7. In Orca Slicer, change "Change filament G-code" to
   `_SP_CHANGE T=[next_extruder]` (the alias works in the meantime).

## Verifying the MMU (do this before any print)

Run these in order with no print running, and stop at the first failure.

1. **Extruder distance.** Mark 120 mm on the filament above the extruder, heat
   the hotend, run `G91`, `M83`, `G1 E100 F60`. You should see 100 mm
   consumed (±1 mm). If not, the extruder `rotation_distance` is wrong, which is
   unrelated to this upgrade.
2. **MMU motor distance.** With `SP_FORCE_MOVE_LANE`, drive a known distance on
   a lane and measure it. The calibration procedure is in the comment above
   `rotation_distance` in `[manual_stepper sp_motor]`.
3. **Synchronization.** Run `SP_TEST_SYNC SYNC_MODE=0`, then `1`, then `2`, and
   follow the on-screen instructions. **Set `sync_mode` to the value where both
   motors visibly move slowly and together** (for example, no filament buckling
   between the MMU and the extruder, and no extruder grinding). Reload with
   `FIRMWARE_RESTART`.
4. **Distance chain.** Load and unload each lane once with `variable_debug: 1`.
   Compare each segment against your previous calibration
   (`dist_mmu_to_hub` -> `dist_hub_to_sensor` -> `dist_sensor_to_extruder` ->
   `dist_extruder_to_meltzone`). The sensor must trigger within the expected
   window; a sensor that triggers much earlier or later than before points to a
   sync or `rotation_distance` problem, not a distance setting to retune.
5. **Cut and park.** Run one full tool change with the cutter and confirm the
   post-cut stub is parked where it was before (`dist_filament_park`).
6. Only then run a short multicolor print.

## Rolling back

- Set the three pins back to `38604d84318dd4af1ab2d0adc26b9bb303bf382c`.
- Only if you flashed in Phase 2: flash the previous firmware `.bin` back on
  the MCU(s) you updated. If you stayed in Phase 1, no firmware step is needed.
- Restore `config/macros/sp_mmu.cfg` and `config/macros/sp_mmu/sp_mmu_code.cfg`
  from the commit before this upgrade (`git log -- config/macros/sp_mmu.cfg`).
- The saved `z_offset` in `printer.cfg` is not modified by the upgrade or by the
  rollback.

## Known issue left unfixed: `PRTOUCH_PROBE_ZOFFSET`

**Status:** not fixed on purpose. The `PROBE_ZOFFSET` macro calls the fork's
`PRTOUCH_PROBE_ZOFFSET`, and the fork has not been adapted to Klipper's
2025-12 probe rework. Do not run `PROBE_ZOFFSET` after this upgrade until
this is resolved.

**Cause** (`klippy/extras/prtouch.py` in the fork):

- Before: `probe.run_single_probe()` returned a mutable list `[x, y, z]` where
  `z` was the raw trigger height.
- Now: it returns a `ProbeResult` named tuple (immutable) whose `bed_z` field is
  `trigger_z - z_offset`.
- `prtouch.py` assigns `z_probe[2] = ...`, which raises
  `TypeError: 'probe_result' object does not support item assignment`, and its
  arithmetic assumes the old meaning of index 2.

**Why it is safe to leave:** the failure happens before `configfile.set(...)`,
so no wrong value is written. The `z_offset` already stored in the
`#*# <--- SAVE_CONFIG --->` block is untouched by the upgrade, so normal prints
and `G28` behave as before. The `PROBE_ZOFFSET` macro calls the command without
parameters. The only side effect possible before the error is calling
`PRTOUCH_PROBE_ZOFFSET APPLY_Z_ADJUST=1` directly, which applies a temporary
`SET_GCODE_OFFSET` for the session; `FIRMWARE_RESTART` clears it.

### How to tell whether Z is OK or wrong

**A. After the upgrade, without touching `PROBE_ZOFFSET` (expected: OK)**

1. Note the `z_offset` under `[bltouch]` in the `SAVE_CONFIG` block before and
   after the upgrade; it must be identical.
2. `G28`, then `G1 Z0 F300` and do the paper test. The drag must feel the same
   as before the upgrade. If it does, Z is fine.
3. Print a single-layer first-layer test. Compare squish to a print from before
   the upgrade.

**B. What the bug looks like if you do run `PROBE_ZOFFSET`**

- Console shows a Python error and `klippy.log` contains
  `'probe_result' object does not support item assignment` with a traceback in
  `prtouch.py`. No `SAVE_CONFIG` prompt appears. Result: **bad but harmless**.
  Do not save anything.

**C. If someone patches `prtouch.py` later, how to check the patch**

A naive fix that only makes the tuple writable produces a wrong value, because
index 2 changed meaning. Check it before saving:

1. Before the upgrade (or from the old image), run `PRTOUCH_PROBE_ZOFFSET`
   and write down the `Calculated z_offset` it prints. Do **not** `SAVE_CONFIG`.
2. After patching, run the same command from the same state (nozzle clean, bed
   hot, same Z gcode offset of 0).
3. **Good:** the two values agree within about ±0.05 mm and are close to the
   saved `z_offset`.
4. **Bad:** the new value differs from the old one by roughly the configured
   `z_offset`. That means the offset is being counted twice.
5. Before trusting a saved value, run `Z_OFFSET_APPLY_PROBE` with the
   paper test (standard Klipper, independent of prtouch) and confirm the two
   methods agree. Print a first-layer test.

A proper fix must replace `z_probe[2]` with the result's `bed_z` (or
`test_z`), build a new `ProbeResult`/list instead of mutating it, and rework
`probe_calibrate_finalize` accordingly; upstream's equivalent is
`probe_calibrate_finalize` in `klippy/extras/probe.py`.

### Community status (checked 2026-10)

The same failure is reported in the fork's tracker:
[0xD34D/klipper_ender3_v3_se#32](https://github.com/0xD34D/klipper_ender3_v3_se/issues/32)
(open). Several users published workarounds in that thread (their own forks of
`prtouch.py`), and one posted the changes inline: restore the raw trigger height
by adding the start `z_offset` back to `z_probe[2]`, stop assigning into the
result, and write the new `z_offset` directly. That description of the root
cause matches the analysis above (`create_probe_result()` now subtracts
`z_offset`, and the result is an immutable named tuple).

None of these patches has been validated on this printer, and the thread itself
warns that a wrong one can store a bad or negative offset (one user reports
gouging the bed). If you try one, do it with a hand on the power switch, run
`PRTOUCH_PROBE_ZOFFSET` without `APPLY_Z_ADJUST` first, and compare against the
paper test as described above before running `SAVE_CONFIG`. A separate
user-reported issue in that thread, a roughly constant 0.1 mm "too low" bias in
PRTouch, predates this upgrade and has not been verified here.

## Hardware and reference configuration

The probe hardware on this printer is a **CR Touch** (BLTouch compatible,
`[bltouch]`), which does both Z homing and bed mesh. The **PRTouch** load cell
(`[prtouch]`, `[hx711s]`, `[dirzctl]`) is used only for the automatic Z-offset
calibration. This matches the fork author's own
[reference configuration](https://github.com/0xD34D/ender3-v3-se-klipper-config),
which lists "CR touch probe" as working and the load cell as requiring the fork.

`[bltouch]`, `[safe_z_home]` and `[stepper_z]` in `config/printer.cfg` are
identical to that reference (same pins, `probe_with_touch_mode: True`,
`stow_on_each_sample: False`, `z_hop: 10`). Other published Ender 3 V3 SE
configurations use the same `[bltouch]` block. Differences from the reference
are tuning only (acceleration, PID, mesh area, TMC2209 instead of TMC2208, and
the calibrated `z_offset`).

## Troubleshooting: `Probe triggered prior to movement` on `G28`

**What happened on this printer (2026-10).** A `G28` was interrupted in the
middle of the Z probing move by a `Timer too close` shutdown (see the
KlipperMaintenance note in the PR). From then on every `G28` aborted at the start
of the Z descent with `Probe triggered prior to movement`, the CR Touch LED
blinked red after `pin_down`, and `pin_down` followed by `touch_mode` read
`QUERY_PROBE` = `TRIGGERED`. `FIRMWARE_RESTART`, `BLTOUCH_DEBUG COMMAND=reset`
and re-seating the connectors did not clear it. **Switching the printer fully
off and on at the power switch did.** After that, `G28` worked on the previous
Klipper and on the new one with the same hardware, config and MCU firmware (three
consecutive homes, trigger positions repeating within about 0.02 mm).

So the probe was latched in an alarm state, not broken and not misconfigured, and
the new Klipper was not the cause. The reference configuration of the fork's
author and the other published Ender 3 V3 SE configurations use the same
`[bltouch]` block.

**How to test the probe.** Use the sequence `G28` itself uses, with `touch_mode`:

```
BLTOUCH_DEBUG COMMAND=reset
BLTOUCH_DEBUG COMMAND=pin_down
BLTOUCH_DEBUG COMMAND=touch_mode
QUERY_PROBE        ; healthy: probe: open   (stuck in alarm: probe: TRIGGERED)
BLTOUCH_DEBUG COMMAND=pin_up
BLTOUCH_DEBUG COMMAND=reset
```

Do not use `pin_down` followed directly by `QUERY_PROBE` as the test: on this CR
Touch that reads `TRIGGERED` even when the probe is healthy and homing works. See
also Klipper's [BLTouch documentation](https://www.klipper3d.org/BLTouch.html).

**If `G28` reports `Probe triggered prior to movement`:**

1. Power the printer off at the switch for about 15 s and back on, then
   `FIRMWARE_RESTART` (twice if the first leaves the MCU in `shutdown`) and retry.
   This is what fixed it here.
2. If it still fails, check that the pin moves freely with the printer off and
   has no dirt or bend, that the head cable is not squeezed by the hotend cover,
   and re-seat the probe cable and the nozzle adapter ribbon at both ends.
3. If the probe still reads `TRIGGERED` after `touch_mode`, suspect the probe
   itself (clean or adjust it per Klipper's documentation, check the 5 V supply,
   or replace it).

Disabling `probe_with_touch_mode` does not clear an alarm.
