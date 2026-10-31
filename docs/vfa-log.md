# VFA log

Living record for the investigation described in
[`vfa-diagnosis.md`](vfa-diagnosis.md). The guide is the stable catalog; this
file changes every session. Keep photos outside the repo (or in an issue/PR) and
reference them here by name.

- [Printer inventory](#printer-inventory)
- [Fixed test settings](#fixed-test-settings)
- [Status board](#status-board)
- [Test log](#test-log)
- [Entry template](#entry-template)

## Printer inventory

Fill this in during the first session. Unknowns stay `?` until checked.

| Item | Value |
|---|---|
| X axis guide | `?` stock wheels / linear rail mod |
| Y axis guide | `?` stock rods + bearings / rail mod |
| X motor (label) | `?` |
| Y motor (label) | `?` |
| X / Y pulleys | `?` stock / replaced; tooth count `?` (the guide assumes 20T) |
| X / Y belts | `?` stock / replaced / age; marking `?` (the guide assumes GT2, 2 mm pitch) |
| X belt pluck frequency | `?` Hz, span `?` mm |
| Y belt pluck frequency | `?` Hz, span `?` mm |
| Accelerometer mounting for X test | `?` |
| Accelerometer mounting for Y test | `?` (toolhead / bed) |
| Extruder | `?` |
| Filament in use for tests | `?` brand, material, diameter spread |
| Printer surface / placement | `?` |
| When VFA first noticed / what changed (T7) | `?` |

## Fixed test settings

Write these once, then do not change them between tests.

| Setting | Value |
|---|---|
| Slicer and version | `?` |
| Filament, nozzle temp, bed temp | `?` |
| Layer height / line width | `?` / `?` |
| Outer-wall speed / acceleration | `?` / `?` |
| Part fan | `?` |
| Test part and bed position | `?` |
| Photo setup (angle, light, distance) | `?` |

## Status board

One row per catalog entry. Update the status whenever a test closes it.
`open` = not yet examined, `ruled out` = tested and not the cause (or not a
significant one), `confirmed` = a contributor, `applied` = changed and kept.

| ID | Source | Status | Evidence (log entry) |
|---|---|---|---|
| A1 | Speed bands | open | |
| A2 | Acceleration / corner velocity | applied (corner velocity); acceleration open | Corner velocity 18 → 5 done before the log; effect not measured |
| A3 | Input shaper correctness | open | Y sensor mounting unknown; accelerometer include currently off |
| A4 | Pressure advance / extrusion | open | |
| B1 | X belt tension | open | |
| B2 | Y belt tension | open | |
| B3 | Belt rub / misalignment | open | |
| B4 | Belt quality / tooth meshing | open | |
| B5 | Pulleys | open | |
| B6 | Idlers and bearings | open | |
| B7 | Debris | open | |
| C1 | Motor wiring | open | |
| C2 | Motor current / temperature | open | X 0.60 A vs Y 0.80 A unexplained |
| C3 | Microsteps / interpolation | open | 16, `interpolate: False` already |
| C4 | StealthChop vs SpreadCycle | open | Config says SpreadCycle; live state unverified |
| C5 | Chopper parameters | applied (calculated) | Effect unmeasured |
| C6 | Motor cogging / motor model | open | |
| D1 | X carriage / gantry | open | |
| D2 | Y rods / bearings / bed play | open | |
| D3 | Frame squareness / fasteners | open | |
| D4 | Toolhead mass / cables / filament drag | open | |
| D5 | Cooling fans | open | |
| D6 | Environment / placement | open | |
| E1 | Z-banding | open | |
| F1 | Extruder gear / slip | open | |
| F2 | Filament quality | open | |
| F3 | Temperature / flow / cooling | open | |
| G1 | MCU timing / host load | open | |
| G2 | Power supply | open | |
| G3 | Klipper fork / version | open | |

## Test log

Newest entry at the bottom. Number entries so photos and the status board can
point to them.

| # | Date | Catalog ID | Change (one variable) | Test | Result | Verdict |
|---|---|---|---|---|---|---|
| 0 | | baseline | None | Speed tower + test part, current config | | Baseline for comparison |

## Entry template

Copy a row into the table above:

```
| N | YYYY-MM-DD | ID | What changed (before → after) | which test | what the walls look like on X / Y, spacing in mm, which speeds | ruled out / confirmed / inconclusive |
```

For config changes also note the commit (`git log -1 --format='%h %s'`) and, if
the driver was touched, the `DUMP_TMC` fields before and after.
