# Ender 3 V3 SE Klipper

This repository contains everything needed to manage an Ender 3 V3 SE with Klipper, Moonraker, Mainsail, and extra utilities, using Docker Compose and automated scripts.

Includes:

- Scripts to build and flash Klipper firmware (`build_firmware.sh`)
- Automated setup script for Docker and dependencies (`setup_services.sh`)
- Pre-made configuration for Ender 3 V3 SE
- Docker Compose for Klipper, Moonraker, Mainsail, Traefik, and utilities
- Integrations: [Shake&Tune](https://github.com/Frix-x/klippain-shaketune), [KlipperMaintenance](https://github.com/3DCoded/KlipperMaintenance), [Moonraker Timelapse](https://github.com/mainsail-crew/moonraker-timelapse), and [Chopper Resonance Tuner](https://github.com/MRX8024/chopper-resonance-tuner)

> **Multicolor printing:** See the [English Installation Guide](english_install_guide.md) or the [Guía de instalación en español](spanish_install_guide.md) for a complete walkthrough on setting up Klipper with Pico MMU for multicolor printing.

## Table of Contents

- [Prerequisites](#prerequisites)
- [Quick Start](#quick-start)
- [Services](#services)
- [Repository Structure](#repository-structure)
- [Notes](#notes)
- [FAQ](#faq)
- [VFA (Vertical Fine Artifacts) Investigation & Fix](#vfa-vertical-fine-artifacts-investigation--fix)
- [Customization](#customization)
- [Static IP for WiFi (Raspberry Pi / Linux)](#static-ip-for-wifi-raspberry-pi--linux)
- [Contributing](#contributing)

## Prerequisites

- A mini PC or Raspberry Pi connected to the Ender 3 V3 SE via USB.
- [Docker](https://docs.docker.com/get-install/) and Docker Compose (the `setup_services.sh` script can install these for you).
- An SD card for flashing the firmware to the printer.

## Quick Start

1. **Clone the repository and start the services:**

   ```bash
   git clone https://github.com/destaben/klipper_ender3_v3_se.git
   cd klipper_ender3_v3_se
   sudo bash setup_services.sh
   ```

   The `setup_services.sh` script will:
   - Install Docker and the Docker Compose plugin if not already present.
   - Clone additional dependencies ([Chopper Resonance Tuner](https://github.com/MRX8024/chopper-resonance-tuner), [KlipperMaintenance](https://github.com/3DCoded/KlipperMaintenance), [Moonraker Timelapse](https://github.com/mainsail-crew/moonraker-timelapse)).
   - Start all Docker containers.

2. **Build and flash the Klipper firmware:**

   ```bash
   sudo bash build_firmware.sh
   ```

   - The script uses the preconfigured `config.ender3_v3_se` file, which enables both USART1 and USART2 for maximum compatibility.
   - The generated binary will be in `klipper/out/klipper_<date>.bin`. Copy this file to an SD card, insert it into the printer, and power it on to flash.
   - **Important:** The firmware filename must end in `.bin` and must not match the last filename that was flashed.
   - If the screen freezes, wait a few minutes and try again if needed.

3. **Access the web interface:**
   - Mainsail: `http://<host_ip>/`

## Services

All services are defined in `docker-compose.yaml` and managed via Docker Compose.

### 1. Klipper

- **Container Name**: `klipper`
- **Image**: Custom build from `Dockerfile-klipper` (based on the [Ender 3 V3 SE display fork](https://github.com/jpcurti/ender3-v3-se-klipper-with-display))
- Includes [Shake&Tune](https://github.com/Frix-x/klippain-shaketune) for resonance analysis.
- Volumes mount the local `config/` directory and additional plugins ([KlipperMaintenance](https://github.com/3DCoded/KlipperMaintenance), [Chopper Resonance Tuner](https://github.com/MRX8024/chopper-resonance-tuner)).

### 2. Moonraker

- **Container Name**: `moonraker`
- **Image**: Custom build from `Dockerfile-moonraker` (based on `mkuf/moonraker:v0.9.3-3-gccfe32f`, adds `ffmpeg` for timelapse support)
- Includes [Moonraker Timelapse](https://github.com/mainsail-crew/moonraker-timelapse) integration.

### 3. Mainsail

- **Container Name**: `mainsail`
- **Image**: `ghcr.io/mainsail-crew/mainsail:v2.12.0`
- Web interface for controlling the printer, accessible via Traefik on port 80.

### 4. Traefik

- **Container Name**: `traefik`
- **Image**: `traefik:3.2`
- **Ports**:
  - `80:80`: Reverse proxy routing HTTP traffic to Mainsail, Moonraker, and monitoring services.

### 5. Node Exporter

- **Container Name**: `node-exporter`
- **Image**: `prom/node-exporter:v1.9.1`
- Exposes host-level metrics (CPU, memory, disk) for monitoring.

### 6. cAdvisor

- **Container Name**: `cadvisor`
- **Image**: `gcr.io/cadvisor/cadvisor:v0.52.0`
- Exposes per-container resource usage metrics.

### 7. USB Watcher

- **Container Name**: `usb-watcher`
- **Image**: `python:3.11-slim`
- Monitors USB device connections and automatically sends a `firmware_restart` to Moonraker when the printer is reconnected.
- Configure monitored USB vendor/product IDs via the `USB_IDS` environment variable in `docker-compose.yaml`.

## Repository Structure

```
.
├── config/                    # Klipper and Moonraker configuration files
│   ├── printer.cfg            # Main Klipper printer configuration
│   ├── moonraker.conf         # Moonraker configuration
│   ├── mainsail.cfg           # Mainsail macros
│   ├── lis2dw.cfg             # Accelerometer configuration (optional)
│   ├── timelapse.cfg          # Timelapse configuration
│   ├── macros/                # Custom G-code macros
│   └── helpers/               # Helper configuration files
├── Dockerfile-klipper         # Dockerfile for the Klipper container
├── Dockerfile-moonraker       # Dockerfile for the Moonraker container
├── docker-compose.yaml        # Docker Compose service definitions
├── config.ender3_v3_se        # Pre-configured firmware build config
├── build_firmware.sh          # Script to build Klipper firmware
├── setup_services.sh          # Script to install Docker and start services
├── set_static_wifi.sh         # Script to set a static WiFi IP
├── usb_watcher.py             # USB reconnection watcher script
├── english_install_guide.md   # Multicolor install guide (English)
└── spanish_install_guide.md   # Multicolor install guide (Spanish)
```

## Notes

- Requires Docker and Docker Compose (installed automatically by `setup_services.sh`).
- The `config.ender3_v3_se` file is already prepared for the Ender 3 V3 SE and enables both serial ports (USART1 and USART2). You can edit it if your hardware only uses one of them.
- The `build_firmware.sh` script automates the firmware build and uses the included configuration.
- Traefik acts as a reverse proxy, exposing the services via HTTP on port 80.
- The Klipper container uses the [jpcurti/ender3-v3-se-klipper-with-display](https://github.com/jpcurti/ender3-v3-se-klipper-with-display) fork, which adds display support for the Ender 3 V3 SE.

## FAQ

**Q: The printer is not connecting. What should I check?**

Check that the `serial` value in `config/printer.cfg` matches your printer's USB device:

```ini
[mcu]
serial: /dev/serial/by-id/usb-1a86_USB_Serial-if00-port0
```

You can list connected devices with:

```bash
ls /dev/serial/by-id/
```

**Q: I don't have an accelerometer. How do I disable it?**

The configuration references a lis2dw accelerometer. If you don't have one, make sure the `[include lis2dw.cfg]` line in `config/printer.cfg` is commented out (it is by default):

```ini
#[include lis2dw.cfg]
```

**Q: The screen froze after flashing. What do I do?**

Wait a few minutes for the firmware to finish flashing. If the screen remains frozen, power-cycle the printer and try again. Make sure the firmware filename on the SD card is different from the last one that was flashed.

**Q: How do I update the services?**

Pull the latest changes and restart the containers:

```bash
git pull
sudo bash setup_services.sh
```

## VFA (Vertical Fine Artifacts) Investigation & Fix

VFA are periodic patterns visible on the surface of printed parts, caused by vibrations or irregular stepper-motor motion that gets imprinted into the extrusion.

### Root Causes Investigated

| Cause | Details |
|---|---|
| TMC2208 interpolation disabled | With `interpolate: False`, the driver outputs raw 16-microstep pulses. Enabling internal 256-step interpolation smooths motion significantly. |
| `square_corner_velocity` too high | The previous value of `18.0 mm/s` was far above the Klipper default of `5.0 mm/s`, causing excessive vibration at corners that propagated into the print walls. |
| Input shaper not documented | Calibrated values only existed in the `SAVE_CONFIG` block and were not visible at a glance. |
| Belt tension / hardware | Check that GT2 belts are tensioned evenly; no software fix compensates for slack or unevenly tensioned belts. |

### Applied Fixes (in `config/printer.cfg`)

#### 1. Enable TMC2208 interpolation for X and Y

The TMC2208 driver can interpolate a 16-microstep signal to an internal 256-microstep waveform, which produces a noticeably smoother current envelope and reduces the periodic forcing that causes VFA.

```ini
[tmc2208 stepper_x]
interpolate: True   # was False

[tmc2208 stepper_y]
interpolate: True   # was False
```

#### 2. Reduce `square_corner_velocity`

```ini
[printer]
square_corner_velocity: 5.0   # was 18.0
```

A value of `18.0 mm/s` allows large instantaneous velocity changes at corners, which excites resonances that appear as vertical artifacts on adjacent walls. `5.0 mm/s` is the Klipper default and provides a good balance between speed and print quality.

#### 3. Add explicit `[input_shaper]` section

The calibrated resonance-compensation values (obtained with Shake&Tune / LIS2DW) are now visible in the main config and documented inline. Klipper's `SAVE_CONFIG` will continue to update them automatically after each calibration run.

```ini
[input_shaper]
shaper_type_x: mzv
shaper_freq_x: 49.6   # Hz – calibrated with LIS2DW
shaper_type_y: mzv
shaper_freq_y: 38.4   # Hz – calibrated with LIS2DW
```

### Further Tuning Tips

- **Re-run input shaper calibration** after any mechanical change (belt retension, stepper replacement, etc.) using the LIS2DW accelerometer and the `SHAPER_CALIBRATE` macro.
- **Check belt tension**: Use the [Shake&Tune belt tension test](https://github.com/Frix-x/klippain-shaketune) (`AXES_MAP_CALIBRATION` / belt resonance graphs). Both X and Y belts should have similar and consistent resonance profiles.
- **Pressure advance**: The current value of `0.08` is a reasonable starting point; fine-tune it if bulging corners coincide with the VFA pattern.
- **Print speed**: Lowering the print speed reduces the vibration energy available to create VFA. If artifacts persist, try reducing `max_velocity` to `150 mm/s` temporarily for quality prints.

### References

- [Klipper Resonance Compensation](https://www.klipper3d.org/Resonance_Compensation.html)
- [VFA Test Cube (Printables)](https://www.printables.com/model/224847-vfa-test-cube)
- [TMC2208 datasheet – interpolation](https://www.trinamic.com/fileadmin/assets/Products/ICs_Documents/TMC2208_datasheet_rev1.09.pdf)

## Customization

- You can adjust the services by editing the volumes and labels in `docker-compose.yaml`.
- The `.gitignore` file already ignores binaries, logs, and temporary configs.
- To add or remove Klipper plugins, edit the volumes in the `klipper` service and rebuild with `docker compose up -d --build`.

## Static IP for WiFi (Raspberry Pi / Linux)

You can use the included script to set a static IP for your WiFi:

```sh
sudo bash ./set_static_wifi.sh "<connection_name>" <ip>/<cidr> <gateway> "<dns1> <dns2>"
# Example:
sudo bash ./set_static_wifi.sh "MIWIFI_XXXX" 192.168.1.225/24 192.168.1.1 "192.168.1.1 8.8.8.8"
```

This will automatically configure the IP, gateway, and DNS for your WiFi connection using NetworkManager (`nmcli`).

## Contributing

Contributions are welcome! If you see something that can be improved, feel free to open an issue or submit a pull request.
