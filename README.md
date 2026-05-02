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

> ⚠️ **Advanced tuning** — Only attempt the SpreadCycle section below if you are comfortable editing Klipper configuration and understand the risks of changing motor driver parameters. The quick-fix section is safe to apply to any Ender 3 V3 SE.

VFA are periodic patterns visible on the surface of printed parts, caused by vibrations or irregular stepper-motor motion that gets imprinted into the extrusion.

### Step 0 — Baseline: print a VFA test cube before any changes

Print a calibration cube or a [VFA test cube](https://www.printables.com/model/224847-vfa-test-cube) with your current configuration and photograph all four walls in good lighting. Keep this photo — you will compare it to each subsequent print to track progress.

> 📷 **Photo placeholder — Baseline print (before any changes)**  
> *Replace this line with a photo of your test cube showing VFA on X/Y walls.*

### Root Causes Investigated

| Cause | Details |
|---|---|
| `square_corner_velocity` too high | The previous value of `18.0 mm/s` was far above the Klipper default of `5.0 mm/s`, causing excessive velocity changes at corners that excited resonances propagating into print walls. |
| TMC2209 interpolation & chopper settings | With `interpolate: True` and generic chopper values the driver current waveform is not optimised for the specific motors on this machine. Advanced SpreadCycle tuning (`driver_TBL/TOFF/HSTRT/HEND`) produces a smoother waveform and lower vibration. |
| Input shaper not documented | Calibrated values only existed in the `SAVE_CONFIG` block and were not visible at a glance. |
| Belt tension / hardware | Check that GT2 belts are tensioned evenly; no software fix compensates for slack or unevenly tensioned belts. |

### Quick Fixes (in `config/printer.cfg`)

#### Fix 1 — Reduce `square_corner_velocity`

```ini
[printer]
square_corner_velocity: 5.0   # was 18.0
```

A value of `18.0 mm/s` allows large instantaneous velocity changes at corners, which excites resonances that appear as vertical artifacts on adjacent walls. `5.0 mm/s` is the Klipper default and is the safest starting point for quality tuning. After applying this change, do a `FIRMWARE_RESTART` (or "Save & Restart" in Mainsail), print the test cube, and photograph the result.

> 📷 **Photo placeholder — After Fix 1: square_corner_velocity reduced to 5.0**  
> *Replace this line with a photo of your test cube after lowering square_corner_velocity.*

#### Fix 2 — Copy calibrated `[input_shaper]` values into the main config

The calibrated resonance-compensation values (obtained with Shake&Tune / LIS2DW) typically remain in Klipper's auto-managed `SAVE_CONFIG` block. If you want them to be visible and editable in the main config, copy the current calibrated values into an explicit `[input_shaper]` section outside `SAVE_CONFIG`. Klipper may still write updated values to `SAVE_CONFIG` after later calibration runs, so recopy them if you want the main config section to stay in sync.

```ini
[input_shaper]
shaper_type_x: mzv
shaper_freq_x: 49.6   # Hz – calibrated with LIS2DW
shaper_type_y: mzv
shaper_freq_y: 38.4   # Hz – calibrated with LIS2DW
```

After applying this change, print the test cube and photograph the result.

> 📷 **Photo placeholder — After Fix 2: input_shaper values confirmed**  
> *Replace this line with a photo of your test cube after verifying/applying input shaper calibration.*

### Advanced TMC SpreadCycle Tuning

> ⚠️ **This section is for advanced users.** The changes below directly affect how the TMC2209 drives the stepper motors. Incorrect values can cause missed steps, overheating, or reduced positioning accuracy. Always test and compare results using printed samples.

The [3dwork.io Advanced TMC VFA Guide](https://klipper.3dwork.io/klipper/empezamos/ajustes-avanzados-tmc-vfa) (on which this section is based) describes how tuning the SpreadCycle chopper parameters (`driver_TBL`, `driver_TOFF`, `driver_HSTRT`, `driver_HEND`) for your specific motor model can significantly reduce VFA by optimising the current waveform at the source.

#### Key principles from the guide

- **SpreadCycle mode** (`stealthchop_threshold: 0`) must be active on X/Y axes. StealthChop trades torque and accuracy for silence and can worsen VFA.
- **`interpolate: False`** is the recommended setting when doing advanced chopper tuning. `interpolate: True` lets the driver smooth the waveform dynamically but can introduce micro-positioning imprecision. The current config uses `interpolate: True` as a simpler starting fix — if VFA persists after all quick fixes, switch to `interpolate: False` and apply the chopper values below.
- **Microstepping**: For entry/mid-range machines (Ender 3, Artillery, etc.) 16 microsteps is recommended. Higher counts increase system load and reduce torque without clear quality gains.
- **Z axis**: StealthChop (`stealthchop_threshold: 999999`) is acceptable — Z moves slowly and precision is managed by homing/probing.
- **Extruder**: Keep at 16 microsteps with standard settings. Use [Pressure Advance](https://www.klipper3d.org/Pressure_Advance.html) instead of driver tuning to improve extrusion quality.

#### How to calculate chopper values for your motors

1. Find the **datasheet** for your stepper motors (check the motor label or manufacturer site). You will need:
   - Phase resistance `Rcoil` (Ω)
   - Phase inductance `L` (mH)
   - Rated current (used to derive `Icoil_peak`)

2. Download the **Trinamic chopper spreadsheet** for TMC2209 from the [Trinamic app-notes page](https://www.trinamic.com/support/app-notes/) (look under "Tools & Simulations" on the TMC2209 product page).

3. In the spreadsheet's **Chopper Parameters** sheet, fill in the yellow cells:
   - `fCLK` = 12 MHz (TMC2209 default)
   - `VM[V]` = your supply voltage (typically 24 V)
   - `TBL` = 1 (use 2 for motors above ~1.5 A or high-voltage setups)
   - `L[H]` = motor inductance in H (e.g. 3 mH → `0.003`)
   - `Rcoil[Ohm]` = motor phase resistance
   - `Icoil(peak)[A]` = motor peak current per datasheet
   - `toff` = 3 (initial value)

4. Adjust `CS` (current scale) until the **RSENSE using VSENSE=1** cell equals your sense resistor value (`0.110 Ω` for standard Ender/Creality boards).

5. Read off the **blue CHOPCONF register bits** section:
   - `HSTRT` → `driver_HSTRT`
   - `HEND` → `driver_HEND`
   - Set `driver_TBL: 1` and `driver_TOFF: 3` (adjust as needed — see tuning tips below)
   - Use the **Icoil (RMS)[A]** value as `run_current`

#### Current chopper configuration for this printer

The values below were calculated for the stock Ender 3 V3 SE motors and are already applied in `config/printer.cfg`. They can serve as a starting point if you want to experiment or if you swap to different motors.

```ini
# X and Y motion axes — SpreadCycle, tuned chopper
[tmc2209 stepper_x]
stealthchop_threshold: 0    # SpreadCycle mode
interpolate: True           # set to False for full advanced tuning
driver_TBL: 2
driver_TOFF: 3
driver_HSTRT: 2
driver_HEND: 0

[tmc2209 stepper_y]
stealthchop_threshold: 0    # SpreadCycle mode
interpolate: True           # set to False for full advanced tuning
driver_TBL: 2
driver_TOFF: 3
driver_HSTRT: 2
driver_HEND: 0

# Z axis — StealthChop acceptable (slow, low-vibration axis)
# [tmc2209 stepper_z]
# stealthchop_threshold: 999999
```

After applying or modifying chopper values, print the test cube and photograph the result.

> 📷 **Photo placeholder — After advanced TMC tuning**  
> *Replace this line with a photo of your test cube after applying SpreadCycle chopper values.*

#### Chopper parameter fine-tuning tips

| Parameter | Range | Notes |
|---|---|---|
| `driver_TBL` | 0–3 | Blanking time. Start at 1 (or 2 for >1.5 A motors). Use 0 only when StealthChop is active. |
| `driver_TOFF` | 0–15 | Slow-decay time. Start at 3. Try 1 or 2 at very high speeds. |
| `driver_HSTRT` | 0–7 | Hysteresis start. Use the spreadsheet value. |
| `driver_HEND` | 0–15 | Hysteresis end. Use the spreadsheet value. |

#### Automated alternative: Chopper Resonance Tuner

The [Chopper Resonance Tuner](https://github.com/MRX8024/chopper-resonance-tuner) is already integrated in this repo. It uses accelerometer data to assist with chopper tuning and can partially automate the process described above. It is more labour-intensive than manual calculation but can yield very accurate results for your specific hardware.

### Final Result — Before vs After

> 📷 **Photo placeholder — Before / After side-by-side comparison**  
> *Replace this line with a side-by-side photo of the baseline cube (Step 0) and the final result cube (after all fixes). Include photos of all four walls.*

### Further Tuning Tips

- **Re-run input shaper calibration** after any mechanical change (belt re-tension, stepper replacement, etc.) using the LIS2DW accelerometer and the `SHAPER_CALIBRATE` macro.
- **Check belt tension**: Use the [Shake&Tune belt tension test](https://github.com/Frix-x/klippain-shaketune) (`AXES_MAP_CALIBRATION` / belt resonance graphs). Both X and Y belts should have similar and consistent resonance profiles.

  > 📷 **Photo placeholder — Shake&Tune belt resonance graphs (X and Y)**  
  > *Replace this line with screenshots of the X/Y belt resonance plots from Shake&Tune, showing even and consistent curves.*

- **Motor wiring**: A common and easy-to-miss issue is incorrect motor coil wiring. Cross-wired coil polarity can cause missed steps, excess noise, and VFA at mid/high speeds even when driver settings are correct. Verify your motor wiring matches the board pinout (A1/A2/B1/B2) per the motor datasheet.
- **Pressure advance**: The current value of `0.08` is a reasonable starting point; fine-tune it if bulging corners coincide with the VFA pattern.
- **Print speed**: Lowering the print speed reduces vibration energy. If artifacts persist, try reducing `max_velocity` to `150 mm/s` temporarily for quality prints.

### References

- [Klipper Resonance Compensation](https://www.klipper3d.org/Resonance_Compensation.html)
- [Klipper TMC Drivers documentation](https://www.klipper3d.org/TMC_Drivers.html)
- [Advanced TMC VFA Tuning Guide (3dwork.io)](https://klipper.3dwork.io/klipper/empezamos/ajustes-avanzados-tmc-vfa)
- [Trinamic SpreadCycle App Note (PDF)](https://www.trinamic.com/fileadmin/assets/Support/AppNotes/AN001-SpreadCycle.pdf)
- [TMC2209 datasheet](https://www.trinamic.com/fileadmin/assets/Products/ICs_Documents/TMC2209_Datasheet_V103.pdf)
- [Chopper Resonance Tuner](https://github.com/MRX8024/chopper-resonance-tuner)
- [VFA Test Cube (Printables)](https://www.printables.com/model/224847-vfa-test-cube)

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
