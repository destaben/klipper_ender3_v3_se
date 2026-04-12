# Improvements and Recommendations

This document summarizes the analysis of the current Klipper configuration for the Ender 3 V3 SE, compares it with features found in other popular Klipper projects, and lists concrete recommendations for further enhancements.

---

## Current Feature Summary

| Feature | Status |
|---|---|
| Pressure advance | ✅ Configured (0.08) |
| Input shaping (MZV) | ✅ Calibrated (X: 49.6 Hz, Y: 38.4 Hz) |
| Firmware retraction | ✅ Enabled |
| BLTouch auto-leveling | ✅ Configured with 5×5 mesh |
| PID-tuned extruder & bed | ✅ Auto-generated |
| Adaptive purge (KAMP) | ✅ LINE_PURGE macro |
| Timelapse support | ✅ Macro-based |
| Multi-material (Pico MMU) | ✅ 4-lane configuration |
| Filament cutter (servo) | ✅ Servo-based cutter |
| Accelerometer (LIS2DW) | ✅ Optional via lis2dw.cfg |
| Shake & Tune vibration analysis | ✅ ShakeTune integration |
| Maintenance tracker | ✅ XY rods & extruder |
| Idle timeout with safety shutdown | ✅ 600s, heaters off + motors off |
| Docker-based deployment | ✅ 7-service stack |
| Monitoring (Prometheus/cAdvisor) | ✅ Included |
| USB device watcher | ✅ Auto firmware restart |

---

## Improvements Added in This Update

### 1. Filament Load / Unload Macros (`config/macros/filament.cfg`)

Standalone macros for loading and unloading filament without the MMU. These are among the most common macros found in community Klipper configurations and are essential for basic filament changes.

- **LOAD_FILAMENT** — Heats the nozzle and pushes filament into the extruder with a small purge to confirm loading. Parameters: `TEMP` (°C), `LENGTH` (mm), `SPEED` (mm/s), `PURGE` (mm).
- **UNLOAD_FILAMENT** — Heats the nozzle, shapes the filament tip, and retracts it from the extruder. Parameters: `TEMP` (°C), `LENGTH` (mm), `SPEED` (mm/s).

### 2. Calibration Helper Macros (`config/macros/calibration.cfg`)

Convenience wrappers that simplify calibration tasks and can be triggered directly from the Mainsail UI.

- **CALIBRATE_INPUT_SHAPER** — Runs `SHAPER_CALIBRATE` for one or both axes. Parameter: `AXIS` (`x`, `y`, or `all`).
- **TEST_SPEED** — Performs a speed and acceleration stress test by moving the toolhead in a bounding box pattern. Parameters: `SPEED`, `ACCEL`, `ITERATIONS`, `BOUND`.
- **FULL_CALIBRATION** — Runs a complete calibration sequence: home → bed mesh → extruder PID → bed PID. Parameters: `BED_TEMP`, `EXTRUDER_TEMP`.

### 3. Utility Macros (`config/macros/utility.cfg`)

Diagnostics and convenience macros inspired by popular community configurations.

- **PRINT_INFO** — Displays current temperatures, toolhead position, pressure advance value, and active bed mesh profile.
- **BEEP** — Emits a short beep if the beeper pin is enabled in `printer.cfg`. Parameter: `DURATION`.
- **MAINTENANCE_POSITION** — Moves the toolhead to the center of the build volume at a raised height and disables X/Y steppers for easy access during maintenance.
- **LEVEL_BED** — Convenience wrapper for `SCREWS_TILT_CALCULATE` that homes first and provides a user-friendly message.

---

## Recommendations for Future Improvements

The following items are organized by category. Each entry notes whether it requires **software-only** changes or also **hardware** modifications.

### Print Quality

| Recommendation | Type | Notes |
|---|---|---|
| Pressure advance per-filament profiles | Software | Store PA values per filament type and switch via macro parameters in START_PRINT |
| Adaptive bed mesh on every print | Software | Uncomment `BED_MESH_CALIBRATE ADAPTIVE=1` in START_PRINT.cfg for per-print mesh generation |
| Velocity/acceleration profiles | Software | Create macros for different speed profiles (e.g., QUALITY vs SPEED) that set velocity limits |
| Retraction tuning per filament | Software | Extend firmware_retraction settings via macros for PLA/PETG/TPU presets |

### Safety

| Recommendation | Type | Notes |
|---|---|---|
| Filament runout sensor | Hardware + Software | Add a filament switch sensor and configure `[filament_switch_sensor]` in printer.cfg |
| Thermal runaway protection tuning | Software | Klipper has built-in thermal protection; consider tightening `max_temp` and verifying `verify_heater` defaults |
| Smoke/fire detection integration | Hardware | External sensor with GPIO trigger for emergency shutdown |
| Power loss recovery (PLR) | Hardware + Software | Requires UPS or supercapacitor module; Klipper does not natively support PLR but workarounds exist |

### User Experience

| Recommendation | Type | Notes |
|---|---|---|
| Neopixel/LED status indicator | Hardware + Software | Add `[neopixel]` section for visual print status feedback (heating, printing, done, error) |
| Beeper notification on print completion | Software | Enable the `[output_pin beeper]` section (pin PB0) in printer.cfg |
| Display status messages in macros | Software | Add more `M117` status messages in START_PRINT and END_PRINT for display feedback |
| Webcam/camera integration | Hardware + Software | Add camera configuration in moonraker.conf for remote monitoring |

### Hardware Add-ons

| Recommendation | Type | Notes |
|---|---|---|
| Enclosure temperature sensor | Hardware + Software | Add a thermistor inside an enclosure and configure as `[temperature_sensor enclosure]` |
| Chamber heater control | Hardware + Software | Requires heater + relay + thermistor; useful for ABS/ASA printing |
| Secondary fan for electronics cooling | Hardware + Software | Add `[controller_fan]` section to cool stepper drivers based on stepper activity |
| Direct-drive extruder upgrade | Hardware | Improves retraction performance and TPU printing |

### Maintenance and Monitoring

| Recommendation | Type | Notes |
|---|---|---|
| Extend maintenance tracker | Software | Add entries for nozzle replacement, belt tension checks, and lead screw lubrication |
| Print time/filament usage statistics | Software | Already tracked by Moonraker history module; consider adding summary macros |
| Automatic backup of printer.cfg | Software | Add a cron job or macro to copy printer.cfg to a timestamped backup before SAVE_CONFIG |

### Configuration Optimization

| Recommendation | Type | Notes |
|---|---|---|
| TMC driver tuning | Software | Fine-tune StealthChop thresholds and driver registers for noise vs performance |
| Microstepping review | Software | Consider 32 microsteps with interpolation for smoother motion on X/Y |
| Square corner velocity tuning | Software | Current value (18.0) is aggressive; benchmark against print quality |
| Homing speed optimization | Software | Current X/Y homing at 80mm/s is good; ensure second homing pass is slower for accuracy |

---

## References

- [Klipper Configuration Reference](https://www.klipper3d.org/Config_Reference.html)
- [Klipper G-Code Commands](https://www.klipper3d.org/G-Codes.html)
- [KAMP (Klipper Adaptive Meshing & Purging)](https://github.com/kyleisah/Klipper-Adaptive-Meshing-Purging)
- [Shake&Tune for Klipper](https://github.com/Frix-x/klippain-shaketune)
- [Ellis' Print Tuning Guide](https://ellis3dp.com/Print-Tuning-Guide/)
- [Mainsail Documentation](https://docs.mainsail.xyz/)

---

## Integration Checklist

Below is a checklist for integrating the above recommendations incrementally:

- [x] Add LOAD_FILAMENT / UNLOAD_FILAMENT macros
- [x] Add calibration helper macros (input shaper, speed test, full calibration)
- [x] Add utility macros (PRINT_INFO, BEEP, MAINTENANCE_POSITION, LEVEL_BED)
- [ ] Enable beeper pin (uncomment `[output_pin beeper]` in printer.cfg — user decision)
- [ ] Enable adaptive bed mesh in START_PRINT (uncomment line — user decision)
- [ ] Add filament runout sensor configuration (requires hardware)
- [ ] Add Neopixel/LED status configuration (requires hardware)
- [ ] Add per-filament pressure advance profiles
- [ ] Add speed/quality profile macros
- [ ] Extend maintenance tracker entries
- [ ] Add webcam/camera configuration
- [ ] Review and tune TMC driver settings
- [ ] Add controller fan for electronics cooling (requires hardware)
