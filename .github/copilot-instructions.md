# GitHub Copilot Instructions — Ender 3 V3 SE Klipper

## Project Overview

This repository manages an **Ender 3 V3 SE** 3D printer running **Klipper** firmware with a full Docker Compose stack. The stack includes:

- **Klipper** — firmware host (custom fork with display support: [jpcurti/ender3-v3-se-klipper-with-display](https://github.com/jpcurti/ender3-v3-se-klipper-with-display))
- **Moonraker** — Klipper API server (v0.9.3)
- **Mainsail** — web UI (v2.12.0) exposed via Traefik on port 80
- **Traefik** (v3.2) — reverse proxy and HTTP router
- **Node Exporter** + **cAdvisor** — host and container metrics

## Hardware

| Component | Details |
|---|---|
| Printer | Creality Ender 3 V3 SE |
| MCU board | Creality 4.2.2 (STM32F103 or GD32F303) |
| Display | Stock Ender 3 V3 SE display (managed by the Klipper fork) |
| Probe | BLTouch (offset X:-23.0, Y:-14.5, Z:3.305) |
| Steppers | TMC2209 (UART) on X, Y, Z, E |
| Accelerometer | LIS2DW (optional, disabled by default via `#[include lis2dw.cfg]`) |
| Extruder | Direct drive, rotation_distance: 7.44 |
| Build volume | 230×230×250 mm |
| MMU | Pico MMU (LH Stinger) — 4-lane multicolor, connected via `PICO_MMU` MCU |
| Filament cutter | Servo-based cutter at X:224 |

## Repository Structure

```
.
├── .github/
│   ├── copilot-instructions.md   # This file
│   └── agents/                   # Copilot coding agent prompts
│       ├── klipper-config.md
│       ├── docker-services.md
│       └── firmware.md
├── config/                       # All Klipper/Moonraker config files
│   ├── printer.cfg               # Main printer config (includes all others)
│   ├── moonraker.conf
│   ├── mainsail.cfg
│   ├── lis2dw.cfg                # Accelerometer (disabled by default)
│   ├── timelapse.cfg
│   ├── macros/                   # G-code macros
│   │   ├── START_PRINT.cfg
│   │   ├── END_PRINT.cfg
│   │   ├── M600.cfg
│   │   ├── sp_mmu.cfg            # Pico MMU settings & user macros
│   │   ├── sp_mmu_code.cfg       # Pico MMU core logic
│   │   ├── servo_cutter.cfg      # Servo filament cutter
│   │   └── ...
│   └── helpers/                  # Helper configs
│       ├── maintain.cfg          # KlipperMaintenance
│       ├── prtouch.cfg           # Probe/BLTouch helpers
│       └── shakeandtune.cfg      # Shake&Tune resonance analysis
├── Dockerfile-klipper
├── Dockerfile-moonraker
├── docker-compose.yaml
├── config.ender3_v3_se           # Pre-built firmware .config for make
├── build_firmware.sh             # Builds Klipper .bin for flashing
├── setup_services.sh             # Installs Docker, clones deps, starts stack
├── set_static_wifi.sh            # Sets static WiFi IP via nmcli
```

## Key Configuration Facts

- **MCU serial**: `/dev/serial/by-id/usb-1a86_USB_Serial-if00-port0`
- **Pico MMU MCU serial**: `/dev/serial/by-id/usb-Klipper_stm32g0b1xx_5A00320017504D4636383420-if00`
- **Firmware target**: STM32F103, 28KiB bootloader, USART1 (PA10/PA9) + USART2
- **Bed mesh**: 5×5 bicubic, min (30,30) max (190,190)
- **Input shaper** (calibrated): X=MZV@49.6 Hz, Y=MZV@38.4 Hz
- **PID** (calibrated): extruder kp=20.070 ki=2.193 kd=45.909; bed kp=62.105 ki=0.519 kd=1856.165
- **Pressure advance**: 0.08
- **Firmware retraction**: 0.5 mm at 40 mm/s

## Plugins & Integrations

| Plugin | Purpose | Mount path |
|---|---|---|
| [Shake&Tune](https://github.com/Frix-x/klippain-shaketune) | Resonance analysis | built into Klipper image |
| [KlipperMaintenance](https://github.com/3DCoded/KlipperMaintenance) | Maintenance tracking | `./KlipperMaintenance/maintain.py` |
| [Chopper Resonance Tuner](https://github.com/MRX8024/chopper-resonance-tuner) | TMC chopper tuning | `./chopper-resonance-tuner/` |
| [Moonraker Timelapse](https://github.com/mainsail-crew/moonraker-timelapse) | Print timelapses | `./moonraker-timelapse/component/timelapse.py` |

## Docker Volumes & Paths

| Host path | Container path | Service |
|---|---|---|
| `./config` | `/home/klipper/config` | klipper |
| `./config` | `/opt/printer_data/config` | moonraker |
| `./gcodes` | `/opt/printer_data/gcodes` | klipper, moonraker |
| `./moonraker/database` | `/opt/printer_data/database` | moonraker |
| `./moonraker/logs` | `/opt/printer_data/logs` | moonraker |

## Coding Conventions

- **Config files** use Klipper `.cfg` syntax (INI-like). Section headers are `[section_name]`, G-code macros use Jinja2 templating (`{% %}`, `{{ }}`).
- **Shell scripts** use `bash` with `set -e`. Keep them idempotent.
- **Docker**: Always use `docker compose` (v2 plugin syntax), not `docker-compose`.
- **No secrets** should be committed. USB IDs and IPs are environment variables.
- When editing `printer.cfg`, never modify the `#*# <--- SAVE_CONFIG --->` block — Klipper manages it automatically.
- When adding a new macro file, add `[include macros/<filename>.cfg]` in `printer.cfg`.
- When adding a new helper file, add `[include helpers/<filename>.cfg]` in `printer.cfg`.

## Common Tasks

### Rebuild and restart all services
```bash
sudo bash setup_services.sh
```

### Build firmware
```bash
sudo bash build_firmware.sh
# Output: klipper/out/klipper_<timestamp>.bin — copy to SD card to flash
```

### Restart a single service
```bash
docker compose restart klipper
```

### View Klipper logs
```bash
docker compose logs -f klipper
```

### Apply config changes (no rebuild needed)
Klipper picks up `.cfg` changes on `FIRMWARE_RESTART` or via Mainsail → "Save & Restart".
