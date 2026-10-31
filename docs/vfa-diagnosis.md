# VFA diagnosis guide (Ender 3 V3 SE)

A working checklist for finding, and ruling out, the possible sources of
**VFA (Vertical Fine Artifacts)** on this printer. It is meant to be followed
step by step in a session: change one thing, print the same test, record the
result in [`vfa-log.md`](vfa-log.md), and mark the source as *ruled out*,
*confirmed* or *still open*.

- [Ground rules](#ground-rules)
- [Printer facts that shape the diagnosis](#printer-facts-that-shape-the-diagnosis)
- [Phase 0 — Triage: is it really VFA?](#phase-0--triage-is-it-really-vfa)
- [Test protocol](#test-protocol)
- [Reading the pattern](#reading-the-pattern)
- [Catalog of sources](#catalog-of-sources)
- [Recommended order](#recommended-order)
- [Measurement tooling](#measurement-tooling)
- [Applied before this guide](#applied-before-this-guide)
- [References](#references)

## Ground rules

1. **One variable at a time.** If two things change between prints, neither
   result means anything.
2. **Always print the same test, with the same settings, and photograph it the
   same way.** See [Test protocol](#test-protocol).
3. **Record every test** in `vfa-log.md`, including the ones that changed
   nothing. A change that did nothing is a *ruled-out* source, which is progress.
4. **Expect to reduce VFA, not to eliminate it.** Any belt-driven printer shows
   some. The goal is "not visible at normal viewing distance on the speeds I
   actually print", not a perfect wall.
5. **Cheap and reversible first**, parts and hardware swaps last. The order is in
   [Recommended order](#recommended-order).
6. **The sources disagree.** Prusa attributes its VFAs to belt/pulley engagement,
   resonance and acceleration, and ruled out the motors; Orca Slicer's wiki and
   others treat motor resonance rippling (MRR) as a main cause. Both could be
   true on different machines, so this guide does not rank causes by theory.
   Each entry carries a confidence tag (see [References](#references)).
7. **Restart after config edits** (`FIRMWARE_RESTART`, or "Save & Restart" in
   Mainsail) and re-run `DUMP_TMC` to confirm the change is live. After any
   mechanical change (belt re-tension, new pulley, motor swap, rail mod),
   re-run the input shaper measurement before judging results.
8. **Moving the real printer needs the owner's go-ahead.** Anything marked
   "moves the machine" below must follow the safety rules of the `printer-ops`
   skill (check `print_stats.state` first, one reversible step at a time).

## Printer facts that shape the diagnosis

| Fact | Where it comes from | Consequence |
|---|---|---|
| **Cartesian bed-slinger** (`kinematics: cartesian`): X moves the toolhead, Y moves the bed | `config/printer.cfg` | Ellis' and Prusa's CoreXY procedures (A/B belts, differential belt graph, 45° squares to isolate A/B) do **not** transfer directly. X and Y are independent, which makes isolation *easier*: a wall along X and a wall along Y are driven by different motors, belts and guides. |
| X/Y: 16 microsteps, `rotation_distance: 40`, 200 steps/rev | `config/printer.cfg` | 40 mm of travel per motor revolution, which equals a 20-tooth pulley × 2 mm pitch **if the pulleys are 20T and the belts GT2** (neither is stated in the repo: count the teeth, read the belt marking, and note both in `vfa-log.md`). One full step = 0.2 mm. |
| X/Y in SpreadCycle (`stealthchop_threshold: 0`), `interpolate: False`, chopper `TBL 2 / TOFF 3 / HSTRT 2 / HEND 0` | `config/printer.cfg` | Set earlier (see [Applied before](#applied-before-this-guide)); how they were derived cannot be verified from the repo. Verify the *live* values before trusting them. |
| `run_current`: X 0.60 A, Y 0.80 A | `config/printer.cfg` | Not explained anywhere in the repo. May be deliberate if the two motors differ; check the labels. |
| `max_velocity: 200`, `max_accel: 4000`, `square_corner_velocity: 5.0` | `config/printer.cfg` | Baseline motion limits. |
| `[input_shaper]` MZV, X 49.6 Hz, Y 38.4 Hz | `config/printer.cfg` | Claimed as measured with the LIS2DW; see the measurement precondition below. |
| **`[include lis2dw.cfg]` is commented out** | `config/printer.cfg:27` | With it off there is no accelerometer, no `[resonance_tester]` and no `[shaketune]` section active. Nothing in [Measurement tooling](#measurement-tooling) works until it is re-enabled. Do that in a separate config PR. |
| `lis2dw.cfg` defines a single `accel_chip: lis2dw` for all axes | `config/lis2dw.cfg` | Klipper's docs provide `accel_chip_x` / `accel_chip_y` for bed-slingers (the accelerometer belongs on the moving part: toolhead for X, bed for Y). If Y was measured with the sensor on the toolhead, the Y shaper value (38.4 Hz) is suspect. **Ask how it was mounted.** |
| **Not from the repo — reported by others, unverified on this printer:** the Y axis rides on two ~8 mm smooth rods with linear bearings, and the stock X carriage runs on wheels | Creality forum Y-grinding thread; retailer listings for X rail kits | Rod/bearing and wheel condition are candidates on this model, nothing more. Confirm what is actually fitted (X rail mod? Y rail mod?) in `vfa-log.md` before planning around it. *(Confidence: M/L)* |
| Pi 1 GB runs the stack | `printer-ops` skill | Heavy work on the Pi can trigger `Timer too close`. Keep Shake&Tune's `measurements_chunk_size` at its default and avoid builds while measuring. |

Arithmetic used below (derived from the config plus **assumed** hardware specs:
GT2 belts with 2 mm pitch, 20T pulleys and a 1.8° motor step angle, none of
which the repo states — treat it as a hypothesis generator):

| Quantity | Value |
|---|---|
| Belt tooth pitch | 2 mm (assumed GT2) |
| Motor revolution | 40 mm of travel, from `rotation_distance: 40` (once-per-revolution defects repeat every 40 mm; it matches 20T × 2 mm only if the pulleys are 20T GT2) |
| Motor electrical cycle (4 full steps; 50 per revolution, assuming a 1.8° motor) | 0.8 mm |
| Z lead screw | 8 mm per revolution (`rotation_distance: 8`) |
| Tooth-mesh frequency at speed *v* | *v* / 2 mm. At 100 mm/s this is 50 Hz, right on the X shaper frequency (49.6 Hz); at ~77 mm/s it is 38.4 Hz, the Y one. |
| Motor electrical-cycle frequency at speed *v* | *v* / 0.8 mm. 38.4 Hz would be ~31 mm/s on Y, 49.6 Hz ~40 mm/s on X. |

Those coincidences give concrete predictions to check against the speed tower
([A1](#a1-speed-bands-resonance-windows)): if the worst bands sit near 77 and
100 mm/s the belt-mesh/resonance combination is the main suspect; near 31–40 mm/s
it points at the motor.

## Phase 0 — Triage: is it really VFA?

No changes to the printer in this phase. Answer these, write the answers in
`vfa-log.md`.

| # | Question | How to check | What it tells you |
|---|---|---|---|
| T1 | **Spacing** of the marks | Calipers across 10+ marks, divide | ~2 mm → belt/pulley/idler teeth. ~0.8 mm → motor electrical cycle. ~40 mm → once per motor revolution (pulley run-out). ~8 mm *and* wraps around the whole part → Z lead screw ([E1](#e1-z-banding-8-mm-lead-screw)), not VFA. Irregular or speed-dependent → resonance. |
| T2 | **Shape** | Print a part with irregular geometry (not only a cube) | VFA stays **vertical or diagonal** regardless of geometry. Wood-grain or marks that follow the shape are extrusion patterns, not VFA ([F](#f-extrusion-and-material)). |
| T3 | **Which axis** | Look at walls along X and walls along Y on the same print | Only X walls → toolhead/X belt/X motor path. Only Y walls → bed/Y belt/Y motor path. Both → common causes (settings, driver config, extruder, environment). On a cartesian printer this separates the two motion systems cleanly. |
| T4 | **Speed dependence** | Orca VFA speed tower ([A1](#a1-speed-bands-resonance-windows)) | Strongly speed-dependent → resonance excited at specific speeds. Constant at every speed → a fixed mechanical defect. |
| T5 | **Ringing or VFA?** | Ringing sits next to sharp features and fades away from them; it scales with acceleration. VFA runs along flat walls. | Ringing is handled by acceleration and the input shaper; VFA is not. Don't mix the two. |
| T6 | **Material and temperature** | Same part in a second filament | PLA shows VFA clearly; PETG can exaggerate them and ABS may hide them. Material changes *visibility*, not the cause. |
| T7 | **When did it start?** | Last print without it; what changed since | A recent change (belt re-tension, new part, firmware/Klipper upgrade — see `docs/klipper-mmu-upgrade.md`) is the first suspect. |

## Test protocol

The same test every time, so results are comparable.

**Test pieces**

1. **VFA speed tower** from Orca Slicer (*Calibration → VFA*). Prints a tower at
   increasing speeds and shows which bands are bad. Set the range to cover the
   outer-wall speeds you really use (for example 20–200 mm/s); keep "auto-scale
   for nozzle" and "auto-adjust to max volumetric speed" on; check that the
   filament's max volumetric speed is not capping the top of the range.
2. **A fixed test part with long straight walls along X and Y** (a rectangle or
   the [VFA test cube](https://www.printables.com/model/224847-vfa-test-cube)),
   printed at the one speed you care about, in the same bed position every time.
   Add a rotated copy when you need to separate X and Y.

**Fixed print settings** (write them into the log once, then do not touch them):
same filament and spool, same nozzle temperature, 0.2 mm layers, 0.4 mm line
width, same outer-wall speed and acceleration, fan setting, bed position
(centre) and orientation, pressure advance as configured.

**Photo method:** one camera angle and distance, raking light from one side
(a phone torch at a low angle), one photo per wall, a ruler in frame. Keep the
files named with the log entry number.

**Config snapshot per session** (so a result can be tied to a state):

```bash
git log -1 --format='%h %s'   # run from the repo root
```

and, on the printer console (no motion): `DUMP_TMC STEPPER=stepper_x`,
`DUMP_TMC STEPPER=stepper_y`. `DUMP_TMC` prints **driver register fields**, not
config options: paste `irun`, `ihold`, `mres`, `intpol`, `en_spreadcycle`,
`toff`, `tbl`, `hstrt` and `hend` into the log (`mres` is the microstep
resolution as the driver sees it: `256 >> mres` microsteps). Klipper's
`DUMP_TMC` leaves out fields whose value is 0, so an absent field means 0 (with
the current config, `hend` and `intpol` will not appear): write 0 in the log
rather than leaving a blank. The exception is `mres`: it is a log2 code, so an
absent `mres` means 256 microsteps, not 0. With 16 microsteps it reads `mres 4`.
Take `run_current` and `microsteps` from the loaded config (`printer.cfg`, or Moonraker's
`configfile.settings`); they are not register fields.

## Reading the pattern

| Observation | Most likely sources |
|---|---|
| ~2 mm spacing, any speed | [B4](#b4-belt-quality-and-tooth-meshing), [B5](#b5-pulleys), [B6](#b6-idlers-and-bearings), [B7](#b7-debris), [B1/B2](#b1-x-belt-tension) |
| ~2 mm spacing, only at certain speeds | A mesh frequency (speed/2 mm) that lands on a frame resonance: [A1](#a1-speed-bands-resonance-windows) + [B1/B2](#b1-x-belt-tension) + [D](#d-structure-and-guides) |
| Fine pattern that changes with speed, finer than 2 mm | Motor/driver: [C3](#c3-microsteps-and-interpolation) – [C6](#c6-motor-cogging-and-motor-resonance-rippling) |
| Only X walls | X belt, X pulley/idler, X carriage ([D1](#d1-x-carriage-and-gantry)), X motor |
| Only Y walls | Y belt, Y pulley/idler, Y rods and bearings ([D2](#d2-y-rods-bearings-and-bed-play)), Y motor |
| Both axes, constant | Common to both: extruder ([F1](#f1-extruder-gear-and-slip)), filament ([F2](#f2-filament-quality)), driver config ([C](#c-motors-and-drivers)), PSU/MCU ([G](#g-electronics-firmware-and-host)) |
| Repeats every ~8 mm in **height**, around the full circumference | Z axis ([E1](#e1-z-banding-8-mm-lead-screw)) |
| Disappears at high speed | Often normal: resonance windows. Print in the clean band ([A1](#a1-speed-bands-resonance-windows)). |
| Same marks regardless of material, speed and axis | Something outside the motion system: extrusion ([F](#f-extrusion-and-material)) or the surface under the printer ([D6](#d6-environment-and-placement)) |

## Catalog of sources

Every entry has: **Mechanism**, **Signature** (how you would recognise it),
**Test** (how to confirm or discard it), **Fix**, **Here** (what this repo or
printer has today), and a **Status**. Status values: `open`, `ruled out`,
`confirmed`, `applied` (kept in sync with `vfa-log.md`). Confidence tags are
explained in [References](#references).

### A. Slicer and motion planning

Free and reversible. Do these before touching hardware.

#### A1. Speed bands (resonance windows)
- **Mechanism:** certain feed rates excite natural frequencies of the axes or the
  belt-mesh frequency (*v* / 2 mm), amplifying a tiny vibration into a visible
  pattern. *(H: Prusa, Orca wiki)*
- **Signature:** bad and clean speed bands; artifacts vary with speed.
- **Test:** Orca VFA speed tower (see [Test protocol](#test-protocol)). Compare
  the bad bands with the predicted ones (~77 mm/s and ~100 mm/s for the belt-mesh
  coincidence, ~31–40 mm/s for the motor cycle). Optional second view:
  Shake&Tune `CREATE_VIBRATIONS_PROFILE` ([Measurement tooling](#measurement-tooling)).
- **Fix:** set Orca's *Resonance Avoidance Speed Range* to skip the bad bands;
  or lower `max_velocity` for quality prints (the old README suggested 150 mm/s
  as a temporary test).
- **Here:** `max_velocity: 200`; no avoidance range known.
- **Status:** open

#### A2. Acceleration and corner velocity
- **Mechanism:** abrupt velocity changes excite vibrations that propagate onto
  adjacent walls. Klipper's Resonance Compensation page says to pick the highest
  `max_accel` where ringing and smoothing are both acceptable, and not to raise
  `square_corner_velocity` above the default 5.0 (it adds smoothing; use higher
  acceleration instead). *(H)* `SHAPER_CALIBRATE` also prints a theoretical
  `max_accel` ceiling per shaper. It depends on the measured frequency (the
  Klipper doc's example is 3500 mm/s² for MZV at 34.6 Hz, which is not a limit
  for this printer), so until the accelerometer is active it is unknown
  whether 4000 over-smooths here.
- **Signature:** artifacts strongest near corners and after direction changes.
- **Test:** print the test part at `max_accel` 4000, 2500, 1500 (via
  `SET_VELOCITY_LIMIT ACCEL=…`, no restart needed). Also check what the
  slicer actually emits: Orca's jerk/junction-deviation settings may add their
  own acceleration/limit commands on top of Klipper's configured values.
- **Fix:** lower `max_accel` to what the shaper supports (compare against the
  `SHAPER_CALIBRATE` ceiling for the real X/Y frequencies); check the slicer isn't
  emitting its own accel/jerk overrides.
- **Here:** `square_corner_velocity: 5.0` (was 18.0), `max_accel: 4000`.
- **Status:** applied (corner velocity); acceleration open

#### A3. Input shaper correctness
- **Mechanism:** a wrong shaper leaves ringing that gets mistaken for VFA.
  Note the limit: input shaping acts on direction changes, so it does **not**
  fix constant-speed 2 mm VFA. *(M: Klipper forum; H: Klipper docs on what shaping targets)*
- **Signature:** artifacts that decay with distance from features and scale with
  acceleration (that's ringing, not VFA).
- **Test:** ringing tower per the Klipper docs (with PA off), or `SHAPER_CALIBRATE`
  once the accelerometer is active, **with the sensor on the toolhead for X and
  on the bed for Y**. Fans off during the test.
- **Fix:** re-measure Y with the sensor on the bed; use `accel_chip_x` /
  `accel_chip_y` in `[resonance_tester]`. Repeat the measurement after any
  mechanical change. After `SHAPER_CALIBRATE`, edit `[input_shaper]` in
  `printer.cfg` yourself: `SAVE_CONFIG` alone does not take effect for any option
  that `printer.cfg` also sets, and it sets all four here (see [Measurement tooling](#measurement-tooling)).
- **Here:** MZV, X 49.6 Hz, Y 38.4 Hz; accelerometer include is currently off;
  single `accel_chip`. Y calibration validity unknown.
- **Status:** open

#### A4. Pressure advance and extrusion consistency
- **Mechanism:** mis-tuned PA or flow changes leave bulges and a wood-grain look,
  which gets reported as VFA but is an extrusion pattern. *(M)*
- **Signature:** marks follow geometry (T2); not vertical.
- **Test:** PA tower; then compare walls at PA 0 vs 0.08 vs tuned.
- **Fix:** re-tune PA after the shaper is settled (Klipper's Resonance
  Compensation page: PA "may need to be re-tuned", and Klipper must be
  restarted before tuning it).
- **Here:** `pressure_advance: 0.08` (a starting value, not tuned for this printer).
- **Status:** open

### B. Belts, pulleys and idlers

#### B1. X belt tension
- **Mechanism:** both too loose and too tight hurt. Loose belts add slop and
  lower resonances; over-tight belts load bearings and have been linked to
  fine banding. Prusa's CORE One finding was improper belt tension. *(M: Prusa's
  article was not readable; this is from press and forum summaries)*
- **Signature:** 2 mm pattern or speed-dependent bands on X walls only.
- **Test:** pluck the belt over a fixed span, record the note with a phone
  spectrum app (frequency in Hz). Record it **before and after** every belt
  change. There is no authoritative target for this printer: Voron's ~110 Hz over
  150 mm is a CoreXY/Gates figure and is only a reference point. Do not try to
  match it blindly.
- **Fix:** adjust in small steps, re-print the test after each step.
- **Here:** unknown. First reading goes into `vfa-log.md`.
- **Status:** open

#### B2. Y belt tension
Same as B1, for the bed belt. A Creality forum thread titled "Y axis belt
loosening" exists for this model; it was only seen in search results and not read *(L)*.
- **Signature:** 2 mm / speed bands on Y walls only; may also show as bed motion
  slop.
- **Status:** open

#### B3. Belt rub and misalignment
- **Mechanism:** belt contacting a pulley flange or frame part, or running at an
  angle through the pulley path, adds a periodic disturbance. There is a known
  SE/KE X-belt rub issue at the three pulleys under the left Z stepper, with
  printed spacers as the community fix. *(M: Printables "Ender 3 V3 SE KE Belt Rub Fix"; page not readable at research time — description came from a search summary)*
- **Signature:** shiny/frayed belt edge, dust under the belt path, noise.
- **Test:** inspect both belts along the whole path, look for wear dust.
- **Fix:** realign pulleys, add spacers, replace a damaged belt.
- **Status:** open

#### B4. Belt quality and tooth meshing
- **Mechanism:** tooth engagement and disengagement at the pulley is widely
  proposed as the source of the 2 mm pattern (one study cited on Prusa's forum
  found it the major source of vibration, with pulley eccentricity and belt
  non-uniformity secondary). Forum results from belt swaps are mixed. *(M)*
- **Signature:** very regular ~2 mm spacing.
- **Test:** swap the suspect belt for a known-good one (this is a part swap, so
  do it after the free checks).
- **Fix:** quality GT2 6 mm belt; avoid twisting during install.
- **Status:** open

#### B5. Pulleys
- **Mechanism:** loose grub screw (pulley slips on the shaft), wrong height
  (belt rides a flange), eccentric or cheap pulley (once-per-revolution error,
  40 mm period). *(M: Ellis; Prusa forum)*
- **Signature:** 40 mm periodic component; clicks; flat spot on the motor shaft
  ignored by the grub screw.
- **Test:** power off, spin the motor pulley by hand and watch for wobble; check
  both grub screws sit on the shaft flat; check pulley height vs belt path.
- **Fix:** re-seat, thread-lock, replace with a precision 20T GT2 pulley.
- **Status:** open

#### B6. Idlers and bearings
- **Mechanism:** smooth idlers force a toothed belt to conform to a smooth
  surface; worn or gritty bearings add periodic drag. Ellis ranks toothed Gates
  idlers first, then smooth idlers on two F695 bearings, then budget toothed ones. *(M)*
- **Signature:** artifacts tied to the axis the idler serves. Creality sells the
  SE/KE tension assembly with a smooth idler for 6 mm GT2 *(L: retailer listing)*.
- **Test:** slacken the belt, spin each idler by hand: it must turn smoothly and
  quietly.
- **Fix:** replace gritty bearings; consider a toothed idler.
- **Status:** open

#### B7. Debris
- **Mechanism:** dust or filament bits compressed in belt/pulley teeth or the
  extruder gear change the effective pitch. *(M)*
- **Test:** inspect and clean pulleys, belts, idlers and the extruder gear.
- **Cost:** minutes. Do it before anything else physical.
- **Status:** open

### C. Motors and drivers

> ⚠️ **Advanced.** Wrong driver values can cause missed steps, overheating or
> lower accuracy. Change one field at a time, stay within the motor's rated
> current, and keep a copy of the working values.

How each setting can be tested:

| Setting | Live (no restart, reverts on restart) | Needs config edit + restart |
|---|---|---|
| Run current | `SET_TMC_CURRENT STEPPER=stepper_x CURRENT=<amps>` | `run_current` |
| Chopper (`TBL`, `TOFF`, `HSTRT`, `HEND`) | `SET_TMC_FIELD STEPPER=… FIELD=… VALUE=…` | `driver_TBL`, … |
| **Microsteps, `interpolate`** | **Never.** `SET_TMC_FIELD` on `mres`/`intpol` changes only the driver; Klipper still computes step distance from the configured `microsteps`, so the axis moves a wrong distance (for example, if the driver is set to 8 microsteps, `mres 5`, while Klipper still assumes 16, the axis moves twice as far) and can hit the frame. | `microsteps`, `interpolate` |

Stay on SpreadCycle for X/Y: Klipper's docs measure about 0.15 mm of extra
positional lag in stealthChop at constant velocity. *(H)*
Leave Z (stealthChop is fine for a slow axis) and the extruder (16 microsteps,
tune with pressure advance rather than chopper values) out of the driver
experiments. Remove `hold_current` on X/Y if it is ever set: the 3dwork.io
guide links it to positioning inaccuracy. *(M)*

#### C1. Motor wiring and coil order
- **Mechanism:** swapped or crossed coil pairs cost precision and cause noise
  and vibration at medium/high speed. *(M: 3dwork.io)*
- **Test:** compare the cable to the board pinout and the motor datasheet
  (A1/A2/B1/B2); look for a motor that is noisier or hotter than its twin.
- **Status:** open

#### C2. Motor current and temperature
- **Mechanism:** Klipper's docs: higher current improves positional accuracy
  and torque at the cost of heat, so prefer higher values as long as the motor
  does not get too hot (warm is fine, painful to touch is not) and the driver
  reports no warnings or errors. *(H)*
- **Signature:** artifacts worse on the axis with the lower current.
- **Test:** read the motor labels (the two motors may differ — a retailer lists
  different X and Y motor lengths for the SE/KE, *L*, which could explain 0.6 vs
  0.8 A). Print the test at X/Y current ±0.1 A, set live with
  `SET_TMC_CURRENT STEPPER=stepper_x CURRENT=0.7` (it reverts at restart, so
  note the value in the log), never above the motor's rating. Check motor
  temperature by touch after a long print.
- **Fix:** match the current to the motor rating, then retest.
- **Here:** X 0.60 A, Y 0.80 A. No `hold_current` on X/Y.
- **Status:** open

#### C3. Microsteps and interpolation
- **Mechanism:** with `interpolate: True` the driver introduces a small
  systematic position error (about 0.006 mm in Klipper's example at 16
  microsteps, 40 mm rotation_distance). Klipper's docs recommend SpreadCycle with
  `interpolate: False` and **raising the microstep count** (64 or 128) to get
  noise similar to interpolation without the error. *(H)* Higher counts cost
  torque and load. *(M)*
- **This reverses the former README advice** ("16 microsteps recommended; higher
  counts increase load and reduce torque without clear quality gains"). Both
  views have support, so treat 16 as the control and 64 as the experiment.
- **Test:** compare 16 (current), 32, 64 with `interpolate: False`, same
  everything else. Config edit and restart per variant (microsteps cannot be
  changed live, see the table above).
- **Caution (inference, not from a source):** more microsteps means a much higher
  step rate on this STM32F103 (at 200 mm/s and 64 microsteps ≈ 64,000 steps/s per
  motor). Watch for `Timer too close` and stop if it appears.
- **Here:** 16 microsteps, `interpolate: False`.
- **Status:** open

#### C4. StealthChop vs SpreadCycle (verify the live state)
- **Mechanism:** see above; stealthChop is less accurate. *(H)*
- **Test:** `DUMP_TMC STEPPER=stepper_x` and check the driver reports
  SpreadCycle active. Optionally compare one print in stealthChop to confirm the
  effect on this machine (Ellis suggests testing both).
- **Here:** `stealthchop_threshold: 0` on X/Y.
- **Status:** open (config says correct; live state unverified)

#### C5. Chopper parameters (TBL / TOFF / HSTRT / HEND)
- **Mechanism:** the chopper shapes the current waveform; badly matched values
  leave ripple that the motor turns into vibration. Optimal values depend on the
  motor/driver pair and cannot be copied blindly. *(M: 3dwork.io, MRX8024; Shake&Tune notes an oscilloscope is the gold standard)*
- **Test options, cheapest first:**
  1. Compare the current values with Klipper's defaults (TBL 2, TOFF 3, HEND 0,
     HSTRT 5: the built-in defaults in Klipper's `tmc2208.py`, also quoted in the
     Shake&Tune vibration-profile docs).
  2. Try the Ellis TMC2209 set for LDO motors: TBL 2, TOFF 2, HEND 1, HSTRT 4,
     PWM_LIM 8, PWM_GRAD 8, PWM_FREQ 1, PWM_REG 4 together with 32 microsteps.
     *(M: written for Voron V0; may not apply)*
  3. Recalculate with the Trinamic chopper spreadsheet from the motor datasheet
     (phase resistance, inductance, rated current; VM 24 V; fCLK 12 MHz; adjust
     CS until "RSENSE using VSENSE=1" equals the board's sense resistor — this
     repo uses 0.150 Ω; read HSTRT/HEND from the CHOPCONF cells; run_current
     from Icoil RMS).
  4. Accelerometer-driven search with the Chopper Resonance Tuner
     ([Measurement tooling](#measurement-tooling)).
- **Ranges when adjusting by hand:** `driver_TBL` 0–3 (start at 1–2; one
  guide (3dwork.io) uses 3 for higher capacitive loads, unverified; 0 only when
  StealthChop is active, so not on X/Y), `driver_TOFF` 1–15 (start at 3; try 1–2 at very
  high speeds with a short TBL; **never 0**, which turns the driver's output
  stage off and the motor loses torque), `driver_HSTRT` 0–7 and `driver_HEND` 0–15
  (take them from the spreadsheet). Klipper writes the raw field value, so use
  the datasheet's register value, not the offset-adjusted one. *(H/M)*
- **Here:** TBL 2, TOFF 3, HSTRT 2, HEND 0 on X and Y. The former README says
  they were calculated from the stock motors' datasheet with the Trinamic
  spreadsheet; that cannot be verified from the repo, and the same set is used
  on both axes even though their `run_current` differs. Not verified against
  measurements.
- **Status:** applied (calculated); effect unmeasured

#### C6. Motor cogging and motor resonance rippling
- **Mechanism:** the stepper's detent torque produces a ripple tied to the rotor
  teeth (0.8 mm of travel per electrical cycle here). Orca's docs call this MRR.
  **Contested:** Prusa ruled motors out on its machines. *(M / contested)*
- **Signature:** pattern finer than 2 mm, speed-dependent, may differ per axis.
- **Test:** only after C1–C5 and B-checks are clear. If artifacts differ between
  X and Y walls with identical belts/driver settings, swap the X and Y motors
  (cables and mounts allowing) and see whether the problem follows the motor.
- **Fix:** a different motor model. Last resort.
- **Status:** open

### D. Structure and guides

#### D1. X carriage and gantry
- **Mechanism:** play or uneven wheel pressure lets the toolhead rock as it
  moves, imprinting the carriage's periodic stick-slip. A dusty guide wheel is
  reported by owners. *(L)* X-axis linear-rail upgrades exist and are a
  well-known mod. *(M)*
- **Test:** with power off, wiggle the toolhead by hand (vertical and
  horizontal); roll it along the whole travel and feel for tight/loose spots;
  check the gantry uprights are parallel and the screws are tight.
- **Fix:** adjust wheel tension if the design allows, clean wheels/rails,
  re-square the gantry, or fit a linear rail.
- **Status:** open

#### D2. Y rods, bearings and bed play
- **Mechanism:** the Y axis (bed) is reported to slide on ~8 mm rods with linear
  bearings (unverified on this printer, see [Printer facts](#printer-facts-that-shape-the-diagnosis)).
  The Creality forum documents gritty bearings, gouged rods, and a loose steel
  ball in a bearing as sources of Y grinding on this model; rotating the rod 90°
  in the bearing helped, as did new bearings or a third-party Y rail. *(M)*
- **Signature:** Y-wall artifacts, grinding noise when moving the bed by hand.
- **Test:** power off, move the bed by hand along its full travel; feel for
  grit; lift the front/back of the bed to feel for play; check the bed springs
  and leveling screws are not loose.
- **Fix:** clean and re-grease, rotate the rods, replace bearings/rods.
- **Status:** open

#### D3. Frame squareness and fasteners
- **Mechanism:** a racked frame or loose fasteners shift resonances and add
  rattle; one guide notes ghosting from non-parallel vertical extrusions and
  suggests loosening the top frame bolts, raising the gantry, and retightening. *(L)*
- **Test:** check every visible fastener; measure frame diagonals; look for a
  rattle with a hand on the frame during a fast move.
- **Status:** open

#### D4. Toolhead mass, cables and filament drag
- **Mechanism:** cable loops, a spool dragging filament, or a loose hotend
  assembly add periodic loads to the carriage. *(L; a vertical spool guide
  exists for the SE)*
- **Test:** print with the filament feeding from a freely turning spool,
  cables clear of the frame; check hotend/shroud screws.
- **Status:** open

#### D5. Cooling fans
- **Mechanism:** a fan with a wobbling blade shakes the carriage; Klipper's
  docs say to disable fans for resonance measurements for this reason. *(H)*
- **Test:** print the test with the part fan off (use a material and geometry
  that tolerate it) and compare.
- **Status:** open

#### D6. Environment and placement
- **Mechanism:** an unstable table or vibrating neighbour; the printer's feet
  and the surface it sits on affect resonances and noise. *(M)*
- **Test:** move the printer to a different, rigid surface (or add a damping
  pad / paving slab underneath) and reprint.
- **Status:** open

### E. Z axis

#### E1. Z-banding (8 mm lead screw)
- **Mechanism:** a bent lead screw, a loose coupler grub screw (reported as
  the most common offender), binding, or an eccentric nut produce a bulge that
  repeats once per screw revolution. *(M)*
- **Signature:** repeats every 8 mm in height (`rotation_distance: 8`), wraps
  around the whole part. Not VFA (T1).
- **Test:** mark the lead screw and coupling, print a tall part, compare the
  mark spacing to 8 mm. Roll the lead screw on a flat surface to check it's
  straight.
- **Fix:** tighten/replace the coupler, straighten/replace the screw, clean and
  lubricate.
- **Status:** open

### F. Extrusion and material

#### F1. Extruder gear and slip
- **Mechanism:** an eccentric or dirty drive gear, or filament slipping, makes a
  periodic flow error. Wevolver lists extruder-gear slippage among the sources
  of irregular multi-frequency artifacts. *(M)*
- **Signature:** both axes equally, fixed relation to filament length rather
  than to speed.
- **Test:** inspect and clean the gear; mark the filament at the extruder
  inlet and check for slip over a long extrusion.
- **Status:** open

#### F2. Filament quality
- **Mechanism:** diameter variance, moisture, or a bad batch change extrusion.
  Material changes how *visible* VFAs are, not their cause. *(M)*
- **Test:** measure diameter at several points with calipers; reprint with a
  second spool of a different brand; dry the filament.
- **Status:** open

#### F3. Temperature, flow and cooling
- **Mechanism:** under-extrusion or temperature swings produce banding that
  mimics VFA. *(M)*
- **Test:** print the test piece at ±10 °C and with a flow check.
- **Status:** open

### G. Electronics, firmware and host

Lower probability; check after the mechanical and driver items.

#### G1. MCU timing and host load
- **Mechanism:** step scheduling jitter from an overloaded host or USB link
  could show as periodic roughness. This is a hypothesis, not a documented
  VFA cause. *(L, inference)*
- **Test:** look for `Timer too close`, `Lost communication` or
  `MCU … stalled` in `klippy.log` after a long print; check host CPU load
  during printing.
- **Status:** open

#### G2. Power supply and voltage
- **Mechanism:** a sagging or noisy 24 V rail changes motor current regulation. *(L, inference)*
- **Test:** measure the voltage at the board under load if you have a meter.
- **Status:** open

#### G3. Klipper fork or version changes
- **Mechanism:** this repo pins a Klipper fork; the Klipper step generation was
  reworked in September 2025 (see `docs/klipper-mmu-upgrade.md`). If VFA appeared
  or changed after an image bump, compare against the previous pinned image. *(L)*
- **Test:** answer T7; if relevant, diff the pinned commits.
- **Status:** open

## Recommended order

Stop at any point when the artifact stops being visible at normal viewing
distance. Anything that helps is recorded as a *confirmed* source in the log;
keep going only if it's still not good enough.

| Phase | What | Cost | Entries |
|---|---|---|---|
| 0 | Triage and baseline: measure spacing, axis, speed dependence; take baseline photos; fill in the hardware inventory | None | T1–T7 |
| 1 | Free checks: clean everything, tighten every fastener, check pulley set screws, check wiring, power-off hand tests of both axes | Minutes | B5, B7, C1, D1–D3, E1 |
| 2 | Speed tower and software settings: find the clean speed bands, retest acceleration | An hour of printing | A1, A2, A4 |
| 3 | Belt tension and alignment: measure, adjust, reprint | Short | B1–B3, B6 |
| 4 | Driver experiments: current and chopper live (`SET_TMC_CURRENT`, `SET_TMC_FIELD`); microsteps and interpolate only by config edit + restart | A few restarts | C2–C5 |
| 5 | Measurement-based work: re-enable the accelerometer (separate PR), shaper with correct sensor mounting, vibration profile, chopper tuner | A session each | A3, [Measurement tooling](#measurement-tooling) |
| 6 | Parts: belts, pulleys, idlers, bearings, rods; then motors; X rail | Money | B4–B6, D1, D2, C6 |
| 7 | Long tail: extruder, filament, environment, electronics | Varies | D4–D6, F, G |

Phases 1 and 2 come first because they cost nothing and eliminate most of the
catalog quickly; phase 4 is tested live and reversible; phase 6 is last
because it costs money and several of its entries (motors) are the most
contested.

## Measurement tooling

None of this works until `[include lis2dw.cfg]` is re-enabled in
`config/printer.cfg` (currently commented out). Do that in its own config PR,
and run the sim boot check from `CLAUDE.md` since it touches `config/`.

| Tool | What it does | Moves the machine? | Notes |
|---|---|---|---|
| `ACCELEROMETER_QUERY` | Checks the sensor responds | No | Run first. |
| `MEASURE_AXES_NOISE` | Baseline noise; expected roughly 1–100; ≥1000 suggests sensor, power or fan noise | No | Fans off. |
| `TEST_RESONANCES AXIS=X` / `AXIS=Y` | Resonance sweep (disables input shaping while running) | **Yes** | Sensor on the toolhead for X, on the bed for Y. `M112` is an **emergency stop**, not a graceful abort: it leaves Klipper shut down and needs `FIRMWARE_RESTART` and re-homing. |
| `SHAPER_CALIBRATE` | Computes shaper type and frequency and prints recommendations | **Yes** | **Copy the values into `[input_shaper]` in `printer.cfg` by hand.** `SAVE_CONFIG` writes them to the auto-save block and restarts Klipper, but at load Klipper comments out any auto-saved field that is also set in the regular config (checked in upstream `configfile.py`, not the pinned fork), so for every option `printer.cfg` also sets (all four shaper keys today) the saved values are ignored and the old ones stay live. |
| Shake&Tune `AXES_SHAPER_CALIBRATION` | Graphs for the shaper | **Yes** | |
| Shake&Tune `CREATE_VIBRATIONS_PROFILE` | Vibration energy vs speed and angle; flags bad speed bands; defaults `SIZE=100`, `MAX_SPEED=200`, `SPEED_INCREMENT=2`, `ACCEL=3000`, `TRAVEL_SPEED=120` | **Yes** (fast moves) | Run last, after the shaper is set. Single sensor on the toolhead → Y data is indirect (inference). If the sensor sits under or near the nozzle, raise `Z_HEIGHT` (default 20) and check clearance first, or the sensor can hit the bed. Pick `MAX_SPEED`/`TRAVEL_SPEED` the machine can safely do. |
| Shake&Tune `EXCITATE_AXIS_AT_FREQ` | Holds one excitation frequency to find what is resonating | **Yes** | Useful to locate a part that rings. |
| Shake&Tune `COMPARE_BELTS_RESPONSES` | Differential belt graph | — | **Not applicable**: CoreXY / CoreXZ only. Use B1/B2 (pluck test) on this cartesian printer. |
| [Chopper Resonance Tuner](https://github.com/MRX8024/chopper-resonance-tuner) | Accelerometer-assisted search of TBL/TOFF | **Yes** | Supports cartesian. Its README claims large vibration reductions from its own tests; independent confirmation not found. *(L)* The tool is gitignored: it lives on the printer, not in a fresh clone. Its command reference was not available at research time: read its `wiki/` before use. |

Heavy work on the Pi can disconnect the MCU. Don't build images or compile
firmware while measuring.

## Applied before this guide

These were done earlier (previously documented in the README) and have no
before/after photos recorded, so their effect is unmeasured.

| Change | Rationale | Status |
|---|---|---|
| `square_corner_velocity` 18.0 → 5.0 | The high value allowed large instantaneous velocity changes at corners; 5.0 is the Klipper default | Applied |
| Explicit `[input_shaper]` section in `printer.cfg` (MZV, X 49.6 Hz, Y 38.4 Hz) | Made the calibrated values visible instead of living only in `SAVE_CONFIG` | Applied. Validity of the Y value unknown (see A3). |
| X/Y SpreadCycle, `interpolate: False`, chopper `TBL 2 / TOFF 3 / HSTRT 2 / HEND 0` | Reported (by the former README) as calculated from the stock motors' datasheet with the Trinamic spreadsheet, following the 3dwork.io guide; not verifiable from the repo | Applied |

## References

**Confidence tags:** **H** = official documentation or manufacturer; **M** =
widely repeated guide or community consensus; **L** = single anecdote, retailer
text, unreadable source, or my own inference. Where a fetch failed during
research, that is noted.

**Klipper and tools**
- [Klipper — Measuring Resonances](https://www.klipper3d.org/Measuring_Resonances.html) (H)
- [Klipper — Resonance Compensation](https://www.klipper3d.org/Resonance_Compensation.html) (H)
- [Klipper — TMC drivers](https://www.klipper3d.org/TMC_Drivers.html) (H)
- [Shake&Tune](https://github.com/Frix-x/klippain-shaketune) and its [macro list](https://github.com/Frix-x/klippain-shaketune/blob/main/docs/README.md) (H)
- [Shake&Tune — `CREATE_VIBRATIONS_PROFILE`](https://github.com/Frix-x/klippain-shaketune/blob/main/docs/macros/create_vibrations_profile.md) (H)
- [Chopper Resonance Tuner](https://github.com/MRX8024/chopper-resonance-tuner) (L: claims from its own tests)
- [Trinamic SpreadCycle app note AN001](https://www.trinamic.com/fileadmin/assets/Support/AppNotes/AN001-SpreadCycle.pdf), [TMC2209 datasheet](https://www.trinamic.com/fileadmin/assets/Products/ICs_Documents/TMC2209_Datasheet_V103.pdf) (H)
- [3dwork.io — Advanced TMC VFA guide (Spanish)](https://klipper.3dwork.io/klipper/empezamos/ajustes-avanzados-tmc-vfa) (M)

**VFA theory and procedures**
- [OrcaSlicer wiki — VFA calibration](https://www.orcaslicer.com/wiki/calibration/vfa_calib) (H for the procedure; its "MRR" causal claim is M)
- [Ellis' Print Tuning Guide — VFAs](https://ellis3dp.com/Print-Tuning-Guide/articles/troubleshooting/vfas.html) (M; written for Voron CoreXY — procedures about A/B belts do not transfer)
- [Fabbaloo — Prusa Research identifies causes of VFA](https://www.fabbaloo.com/news/prusa-research-identifies-causes-of-vertical-fine-artifacts-and-offers-fixes) (M, secondhand: Prusa's own article returned 404 and the Fabbaloo page 403 at fetch time; claims taken from search summaries)
- [Wevolver — 3D printing VFA](https://www.wevolver.com/article/3d-printing-vfa-vertical-fine-artifacts-and-how-to-control-them) (M)
- [Klipper forum — forbidden velocities proposal](https://klipper.discourse.group/t/proposal-forbidden-velocities-for-printer-steppers/3995) (M; page returned 403, read via search summary)
- [Prusa forum — VFA artifacts on XY straight edges](https://forum.prusa3d.com/forum/prusa-core-one-hardware-firmware-and-software-help/vfa-artifacts-on-xy-straight-edges/paged/3) (L; anecdotal)
- [Obico — VFA speed test in OrcaSlicer](https://www.obico.io/blog/speed-test-in-orcaslicer-vfa-a-comprehensive-guide/) (M)

**Ender 3 V3 SE specifics**
- [Creality forum — Y axis grinding noise](https://forum.creality.com/t/ender-3-v3-se-y-axis-grinding-noise-fix/6187) (M)
- [Printables — Ender 3 V3 SE/KE belt rub fix](https://www.printables.com/model/955864-ender-3-v3-se-ke-belt-rub-fix) (L; page returned 403)
- [Dremc support — replacing X/Y belts on the V3 SE](https://support.dremc.com.au/support/solutions/articles/51000365366-how-to-replace-x-y-belts-on-ender-3-v3-se) (L; listed in search results, not read)

**Z-banding**
- [All3DP — Z wobble / Z banding](https://all3dp.com/2/z-banding-z-wobble-fix/) (M)
- [Wevolver — Z banding](https://www.wevolver.com/article/z-banding-rooting-out-the-cause-and-fixing-the-3d-printing-defect) (M)

**Test files**
- [VFA test cube (Printables)](https://www.printables.com/model/224847-vfa-test-cube)
