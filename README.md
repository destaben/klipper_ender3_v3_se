# Ender 3 V3 SE Klipper

This repository contains everything needed to manage an Ender 3 V3 SE with Klipper, Moonraker, Mainsail, and extra utilities, using Docker Compose and automated scripts.

Includes:

- Scripts to build and flash Klipper firmware (`build_firmware.sh`)
- Pre-made configuration for Ender 3 V3 SE
- Docker Compose for Klipper, Moonraker, Mainsail, Traefik, and utilities

## Services

### 1. Klipper

- **Container Name**: `klipper`
- **Image**: Custom build from the current directory

### 2. Moonraker

- **Container Name**: `moonraker`
- **Image**: `mkuf/moonraker:v0.9.3-3-gccfe32f`
- **Ports**:
  - `7125:7125`: Exposing Moonraker API to be able to connect from applications

### 3. Mainsail

- **Container Name**: `mainsail`
- **Image**: `ghcr.io/mainsail-crew/mainsail:v2.12.0`

### 4. Traefik

- **Container Name**: `traefik`
- **Image**: `traefik:3.2`
- **Ports**:
  - `80:80`: Exposing Traefik's HTTP service

## Quick Start

1. **Clone the repository and start the services:**

   ```bash
   git clone https://github.com/destaben/klipper_ender3_v3_se.git
   cd klipper_ender3_v3_se
   sudo bash setup_services.sh
   ```

2. **Build and flash the Klipper firmware:**

   ```bash
   sudo bash build_firmware.sh
   ```

   - The script uses the preconfigured `config.ender3_v3_se` file, which enables both USART1 and USART2 for maximum compatibility.
   - The generated binary will be in `klipper/out/klipper_<date>.bin`. Copy this file to an SD card, insert it into the printer, and power it on to flash.
   - If the screen freezes, wait a few minutes and try again if needed.

3. **Access the web interfaces:**
   - Mainsail: http://<host_ip>/

## Notes

- Requires Docker and Docker Compose.
- The `config.ender3_v3_se` file is already prepared for the Ender 3 V3 SE and enables both serial ports (USART1 and USART2). You can edit it if your hardware only uses one of them.
- The `build_firmware.sh` script automates the build and uses the included configuration.
- Traefik exposes the services via HTTP.

## FAQ

- If you have connectivity issues with the printer, check that the `serial` value in `config/printer.cfg` is correct:

   ```ini
   [mcu]
   serial: /dev/serial/by-id/usb-1a86_USB_Serial-if00-port0
   ```

   You can list connected devices with:

   ```bash
   ls /dev/serial/by-id/
   ```

- The configuration is set up to use a lis2dw accelerometer. If you don't have one, simply comment out references in `printer.cfg` or `lis2dw.cfg`.

## Pico MMU Control Panel

An SVG-based Mainsail custom panel is included for controlling the Pico MMU directly from the web interface.

### Features

- **SVG Visualization**: Real-time schematic of the 4-lane MMU showing motor, gears, servo cam, filament hub, sensor, extruder, and hotend
- **Lane Selection**: Click to select which lane (T0–T3) to target for operations; the active/loaded lane is highlighted
- **Operations**: One-click buttons for Home, Load/Unload Lane, Load/Unload Hub, Load/Unload Extruder, Load/Unload Hotend, Purge, and Test MMU
- **Advanced Controls**: Test Servo angle, Force Move, Force Set Loaded Lane, Clear Errors, full Filament Change, and Custom G-code commands
- **Real-time Status**: Displays loaded lane, filament sensor state, servo angle, exit code, change count, and error count — all updated live via Moonraker WebSocket
- **Activity Log**: Timestamped console showing SP: messages from Klipper with color-coded severity
- **Confirmation Dialogs**: Destructive actions (Init MMU, Purge, Filament Change) require confirmation before executing
- **Dark Theme**: Matches Mainsail's visual style for seamless integration

### Setup

The panel files are located in `config/.theme/`:

- `navi.json` — Adds a "Pico MMU" entry to the Mainsail sidebar navigation
- `pico-mmu-panel.html` — The self-contained SVG control panel

To activate the panel, ensure the `.theme` folder is inside your Klipper configuration directory (typically `~/printer_data/config/.theme/`). Mainsail will automatically detect the `navi.json` file and add the sidebar link.

## Customization

- You can adjust the services by editing the volumes and labels in `docker-compose.yaml`.
- The `.gitignore` file already ignores binaries, logs, and temporary configs.

## Static IP for WiFi (Raspberry Pi / Linux)

You can use the included script to set a static IP for your WiFi:

```sh
sudo bash ./set_static_wifi.sh "<connection_name>" <ip>/<cidr> <gateway> "<dns1> <dns2>"
# Example:
sudo bash ./set_static_wifi.sh "MIWIFI_XXXX" 192.168.1.225/24 192.168.1.1 "192.168.1.1 8.8.8.8"
```

This will automatically configure the IP, gateway, and DNS for your WiFi connection.
