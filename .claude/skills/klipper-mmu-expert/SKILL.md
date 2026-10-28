---
name: klipper-mmu-expert
description: Use when the user asks questions about Klipper firmware/configuration (printer.cfg, G-code macros, Jinja2 templating, the printer.* object model, calibration, error diagnosis) or about the Pico MMU / LH Stinger multi-material macros in this repo (sp_mmu.cfg, sp_mmu_code.cfg, filament loading/unloading, tip forming, cutting, pause/resume, runout, purge). Trigger on questions like "why does T1 fail", "how do I tune the MMU load distances", "explain _SP_TOOL_CHANGE", or any request for advice/troubleshooting on Klipper or MMU behavior — not only on requests to edit files.
---

# Klipper & Pico MMU (LH Stinger) Expert

You are advising on a real, running **Ender 3 V3 SE** printer managed from this repo (Docker Compose stack: Klipper, Moonraker, Mainsail, Traefik). Full hardware specs live in `.github/copilot-instructions.md` — read it if you need calibration values, wiring, or stack details you don't already have.

This skill covers two things: **general Klipper expertise** and **deep knowledge of this repo's Pico MMU (LH Stinger) macro system**. Ground every specific/numeric answer in the actual files below — defaults drift once a user tunes their printer, so re-read the live file before telling someone what a value "is".

## Ground rules before advising

1. **Read before you answer.** If the question involves a specific variable, macro, or behavior, grep/read the real file (`config/printer.cfg`, `config/macros/*.cfg`) rather than relying on memory of this skill text — values here are examples/snapshots, not the live source of truth.
2. **Never tell the user to edit the `#*# <--- SAVE_CONFIG --->` block** in `printer.cfg` — Klipper owns it (input shaper, PID, Z-offset, bed mesh are saved there via `SAVE_CONFIG`).
3. **Config changes → `FIRMWARE_RESTART`** in Mainsail. Only structural changes (new include files, volume-mounted file renames) need `docker compose restart klipper`.
4. Prefer pointing the user at **`sp_mmu.cfg`** (user-tunable variables and hook macros) over **`sp_mmu/sp_mmu_code.cfg`** (vendored core logic, 80k+ chars, upgraded wholesale from upstream LH Stinger releases — don't suggest hand-editing it unless the user is explicitly patching/upgrading the vendored code).
5. When a macro name or variable you need isn't in this file, run `grep -n "^\[gcode_macro" config/macros/sp_mmu/sp_mmu_code.cfg` (or `sp_mmu.cfg`) to confirm it exists and find its line before describing its behavior.

## General Klipper knowledge to apply

- **Object model**: macros read live state via `printer.<object>.<field>` (e.g. `printer.toolhead.position`, `printer.toolhead.homed_axes`, `printer.extruder.temperature`, `printer.gcode_move.gcode_position`). `printer['gcode_macro NAME']` reads another macro's `variable_*` fields (Jinja dict syntax is required when the name has special chars, e.g. `printer['gcode_macro _SP_VARS']`).
- **Jinja2 macro params**: `{% set x = params.X | default(10) | float %}` — always check `|int`/`|float`/`|string` filters match intended use; a missing `|default()` throws `KeyError` if the caller omits the param.
- **Persisted macro state**: `SET_GCODE_VARIABLE MACRO=<name> VARIABLE=<var> VALUE=<v>` mutates a macro's `variable_*` for the session; `[save_variables]` + `SAVE_VARIABLE`/`printer.save_variables.variables.<name>` persists across restarts to the file in `filename:` (here `~/printer_data/sp_mmu_data.cfg`).
- **Common failure classes** to recognize and triage: `Timer too close` (scheduling overload — usually too many rapid short moves/macro calls), `MCU 'xxx' shutdown` (check the relevant MCU's serial/USB connection — this rig has two MCUs: the main board and `PICO_MMU`), `Move queue overflow` / `Exceeded max accel`, `Must home axis first` before any extrude-only-adjacent cut/park move that includes X/Y, `Unknown command` (macro/alias not loaded — check `[include ...]` order in `printer.cfg`, `config/printer.cfg:20-23` globs `helpers/*.cfg` and `macros/*.cfg`).
- **Where to look for logs**: `klippy.log` via Moonraker/Mainsail, or `docker compose logs klipper` from the host.

## Pico MMU (LH Stinger) architecture

Source/vendor: https://github.com/lhndo/LH-Stinger/wiki/Pico-MMU — this is a **4-lane** build (`variable_lanes: 4`) using a second MCU named `PICO_MMU` (a BTT EBB42-class board) that drives one `manual_stepper` (the shared feed motor) and one `servo` (lane selector + optional filament stop), plus a single `filament_switch_sensor` used as both runout sensor and loading endstop.

Files:
- **`config/macros/sp_mmu.cfg`** — hardware pin mapping (`[mcu PICO_MMU]`, `[servo sp_servo]`, `[manual_stepper sp_motor]`, `[tmc2209 manual_stepper sp_motor]`, `[filament_switch_sensor sp_sensor_runout]`), all user-tunable `variable_*` in `_SP_VARS` and `_SP_TIP_FORMING_DEFAULTS`, the three **user hook macros** (`_SP_BEFORE_CHANGE`, `_SP_AFTER_CHANGE`, `_SP_TIP_FORM`), the `T0`–`T5` tool macros, and `[include sp_mmu/sp_mmu_code.cfg]` at the very end.
- **`config/macros/sp_mmu/sp_mmu_code.cfg`** — vendored core logic (kept out of the `macros/*.cfg` glob so it loads once, via `sp_mmu.cfg`) (~90 macros, all `SP_*`/`_SP_*`). Don't suggest editing it for routine tuning; routine tuning lives in `sp_mmu.cfg`'s `_SP_VARS`.
- **`config/macros/servo_cutter.cfg`** — separate servo (`my_servo`, `PICO_MMU: PB3`) driving the filament cutter blade, plus `CUT_COUNT_INCREMENT`/`CUT_COUNT_STATUS`/`RESET_CUT_COUNT` blade-life tracking (`variable_blade_max_cuts: 5000`, warns every 500 cuts and errors at the max — tell users to run `RESET_CUT_COUNT` only after actually swapping the blade).
- **`config/macros/MMU_SELECT_PURGE.cfg`** — simple manual test macros (`CHANGE0`–`CHANGE3`): select a lane, heat to 220°C, purge 100 mm. Useful for quick bench testing, not part of the automatic toolchange path.
- **`config/macros/KAMP_Settings.cfg`** + **`LINE_PURGE.cfg`** — KAMP's adaptive purge-line macro (`LINE_PURGE`), independent of the MMU's own purge mechanism. If a user has both KAMP's `LINE_PURGE` in their slicer's "before layer change"/purge G-code *and* `variable_park_purge: 1` in `_SP_VARS`, they're purging twice — flag that when diagnosing excess purge/oozing complaints.
- Slicer side (Orca Slicer): tool changes are emitted as `_SP_CHANGE T=[next_extruder]` in the "Change filament G-code" field, which maps to `_SP_TOOL_CHANGE LANE=<n>` (same as calling `T0`..`T3` directly). LH Stinger renamed `SP_CHANGE` to `_SP_CHANGE`; `sp_mmu.cfg` keeps a `SP_CHANGE` alias for old slicer profiles.
- Synced MMU + extruder moves depend on `variable_sync_mode` in `_SP_VARS` (2 = current Klipper, 1 = older Klipper, 0 = `FORCE_MOVE`, the pre-0.93 behavior). If loads/unloads land at wrong distances with no error, run `SP_TEST_SYNC SYNC_MODE=0|1|2` and pick the mode where both motors move together. See `docs/klipper-mmu-upgrade.md`.
- `PRTOUCH_PROBE_ZOFFSET` (the `PROBE_ZOFFSET` macro) is known-broken against the pinned Klipper's probe API; do not recommend running it. Details and how to check Z in `docs/klipper-mmu-upgrade.md`.

### Filament path & key distance variables (`_SP_VARS`, `sp_mmu.cfg`)

The physical path is: **MMU lane → hub → filament sensor → extruder gears → meltzone → park/cut position**. Each segment has a matching `variable_dist_*` and `variable_speed_*`:

| Variable | Segment |
|---|---|
| `dist_mmu_to_hub` / `speed_mmu_to_hub` | MMU exit → hub park position |
| `dist_hub_to_sensor` | hub park → filament sensor trigger |
| `dist_sensor_to_extruder` / `speed_hub_to_extruder` | sensor → extruder gears |
| `dist_sensor_to_synced_move` / `speed_sync_moves` | point where MMU motor and extruder move in tandem |
| `dist_extruder_to_meltzone` / `speed_coldzone_loading` | extruder gears → meltzone entry |
| (meltzone itself) `speed_meltzone_loading` | through the meltzone — lower this first if the extruder skips during loading |
| `dist_filament_park` | meltzone → park position (= **cut position** if a filament cutter is in use) |
| `end_of_load_filament_move` | fine-tune move after reaching the meltzone — negative = retract slightly (no-ooze tuning), positive = purge |

Calibration order when something's off: 1) `rotation_distance` on `[manual_stepper sp_motor]` (use `SP_FORCE_MOVE_LANE` to drive known distances and measure actual extrusion) → 2) `backlash` (bowden slack compensation) → 3) the `dist_*` chain above, segment by segment, from MMU outward to the hotend → 4) `overshoot_factor` / `overshoot_hub_distance` only if sensor-triggered moves aren't landing (and warn: Klipper can reject extreme overshoot values with a crash, per the inline comment).

**Cutter setups — don't confuse "where it cuts" with "how deep it parks":** `cut_position_x/y` (in `_SP_TIP_FORM`, `sp_mmu.cfg`) is only the toolhead XY where the physical blade triggers — independent of filament depth. The depth (mm from the extruder gears) at which the filament tip ends up *after* the cut is controlled by `variable_dist_filament_park` via the formula `fil_cut_position = dist_extruder_to_meltzone - dist_filament_park - tip_length_below_cut` (computed in `_SP_TIP_FORM`, `sp_mmu.cfg`). Its own inline comment says it directly: *"For Filament Cutter: set this to the cut position."* So if a user reports the cut height/position is fine but the parked tip ends up too close to (or too far from) the extruder entrance after cutting, adjust `dist_filament_park` (or its sibling `tip_length_below_cut`) — a `variable_*` change, no gcode edit — rather than touching the hardcoded post-cut retract/wiggle moves (`G1 E-1.0`, the `E2`/`E-2` pairs) inside `_SP_TIP_FORM`. Those hardcoded moves exist for blade clearance and tip cleanup, not for tuning park depth, and editing them was a wrong first instinct — don't repeat it. Increasing `dist_filament_park` *reduces* the pre-cut retract (less filament pulled back before the XY cut move), so the severed gear-side stub ends up *deeper/farther* from the extruder entrance; decreasing it does the opposite — same cut-mechanism XY, different parked depth.

**Symptom → cause, confirmed case:** "cut height/position is correct, but after the cut the filament ends up right at/in the hotend entrance (the cold-zone→heatbreak transition), so the next load's fresh tip collides tip-to-tip with the old stub right at that narrow transition and deflects/jams instead of pushing straight through." Root cause: `dist_filament_park` is parking the stub exactly at (or too close to) that throat transition, leaving no clearance past the choke point for a clean tip-to-tip meeting. Fix: increase `dist_filament_park` by the desired clearance (e.g. +2mm) so the stub parks just past the narrow entrance, in the straight bore beyond it — cut mechanism (`cut_position_x/y`) untouched.

### Tool-change pipeline

`T0..T5` → `_SP_TOOL_CHANGE LANE=n` → unload current lane (`_SP_UNLOAD_EXTRUDER`/`_SP_UNLOAD_HOTEND`/`_SP_UNLOAD_LANE`) → tip-forming (`_SP_TIP_FORM` user hook, default calls `_SP_TIP_FORMING` or, if a cutter is configured, performs the cut sequence directly in `_SP_TIP_FORM` using `cut_position_x/y`, `clear_position_x/y`, `SET_SERVO_ANGLE`, then `CUT_COUNT_INCREMENT`) → select new lane (`_SP_LANE_SELECT`, moves `sp_servo` to the matching angle from `variable_angles`) → load new lane (`_SP_LOAD_LANE` → `_SP_LOAD_HUB` → `_SP_LOAD_EXTRUDER` → `_SP_LOAD_HOTEND`). `_SP_BEFORE_CHANGE`/`_SP_AFTER_CHANGE` fire right around the park, for Z-hop, wipe, or purge customization — point users there instead of editing core unload/load macros.

### Pause / Resume / Runout

`_SP_PAUSE` / `_SP_RESUME` (and the overridden `RESUME` macro in `sp_mmu/sp_mmu_code.cfg`, which renames the built-in `RESUME` to `RESUME_BASE` and wraps Mainsail's `_MAINSAIL_RESUME`, which calls `RESUME_BASE`) handle park, fan control (`pause_fan_speed`), and small retract/push (`pause_retract`) around any pause — manual or filament-change-triggered. `_SP_SENSOR_IN`/`_SP_SENSOR_OUT` feed the `sp_sensor_runout` filament sensor into `_SP_RUNOUT` → `_SP_RUNOUT_CHECK*` (a 10-step polling chain, `_SP_CHECK_1`..`_SP_CHECK_10`/`_SP_CHECK_END`, each waiting longer before giving up — this is the "why did my MMU wait ~30s then pause" mechanism). `variable_infinite_spool: 1` auto-advances to the next lane on runout instead of pausing (guest lanes excluded; always forces a pause-park during the swap).

### Debug & test tools

- `variable_debug: 1` in `_SP_VARS` turns on verbose `_SP_NOTIFY`/status messages — first thing to enable when diagnosing.
- `SP_TEST_SERVO`, `SP_TEST_MMU`, `SP_TEST_LOAD_UNLOAD_EXTRUDER`, `SP_TEST_MANUAL_TIP_FORMING` — safe bench-test macros, recommend these before a full print when a user changed hardware/calibration.
- `SP_FORCE_MOVE_LANE` / `SP_FORCE_SET_LOADED_LANE` — manual motor moves and state override for calibration without going through the full load/unload logic.
- `SP_CLEAR_ERROR_CODES` / `_SP_SET_EXIT_CODE` — clears MMU error state after a manual fix.
- `_CHECK_SETTINGS_VERSION_SP` compares `_SP_VARS.version` against the code's required version — if a user updates `sp_mmu/sp_mmu_code.cfg` from upstream LH Stinger and gets a version-mismatch error/notify, tell them to diff upstream's current `sp_mmu.cfg` template against theirs for new/renamed variables before bumping `variable_version`.
- Persisted MMU state (e.g. cut count, loaded-lane memory) lives in `~/printer_data/sp_mmu_data.cfg` via `[save_variables]` — don't suggest changing its `filename:` path casually, other macros assume it.

## How to answer

- Cite the macro/variable and its file:line when giving concrete advice (e.g. "`end_of_load_filament_move` in `config/macros/sp_mmu.cfg`").
- If the user describes a symptom (oozing after load, extruder skipping, pause on every color change, grinding at the lane switch), map it to the relevant segment/variable above before suggesting a value change, and suggest changing **one variable at a time** then re-testing with `SP_TEST_*` macros.
- If asked to implement a change, still only do so when the user asks for an edit — this skill is for advice/explanation by default.
