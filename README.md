# Ender 3 V3 SE Klipper

This repository contains everything needed to manage an Ender 3 V3 SE with Klipper, Moonraker, Mainsail, and extra utilities, using Docker Compose and automated scripts.

Includes:

- Scripts to build and flash Klipper firmware (`build_firmware.sh`)
- Automated setup script for Docker and dependencies (`setup_services.sh`)
- Pre-made configuration for Ender 3 V3 SE
- Docker Compose for Klipper, Moonraker, Mainsail, Traefik, and utilities
- Integrations: [Shake&Tune](https://github.com/Frix-x/klippain-shaketune), [KlipperMaintenance](https://github.com/3DCoded/KlipperMaintenance), [Moonraker Timelapse](https://github.com/mainsail-crew/moonraker-timelapse), and [Chopper Resonance Tuner](https://github.com/MRX8024/chopper-resonance-tuner)

> **Multicolor printing:** See the [English Installation Guide](english_install_guide.md) or the [Guía de instalación en español](spanish_install_guide.md) for a complete walkthrough on setting up Klipper with Pico MMU for multicolor printing. If you are using the Creality Nebula Smart Kit, see the [Nebula Smart Kit Setup Guide](nebula_setup_guide.md).

> **Upgrading Klipper or the Pico MMU macros:** read the [Klipper + Pico MMU upgrade notes](docs/klipper-mmu-upgrade.md) first. They cover the changed MMU synchronization (`sync_mode`), how to verify distances before printing, rollback, and a known `PROBE_ZOFFSET` issue.

## Table of Contents

- [Prerequisites](#prerequisites)
- [Quick Start](#quick-start)
- [Prebuilt Docker Images](#prebuilt-docker-images)
- [Services](#services)
- [Repository Structure](#repository-structure)
- [Notes](#notes)
- [FAQ](#faq)
- [VFA (Vertical Fine Artifacts)](#vfa-vertical-fine-artifacts)
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
   - Add your login user to the `docker` group; log out and back in (or run `newgrp docker`) before using `docker` without `sudo` in your own shell.

2. **Build and flash the Klipper firmware:**

   ```bash
   sudo bash build_firmware.sh
   ```

   - The script uses the preconfigured `config.ender3_v3_se` file, which enables both USART1 and USART2 for maximum compatibility.
   - The generated binary will be in `klipper/out/klipper_<timestamp>.bin`. Copy this file to an SD card, insert it into the printer, and power it on to flash.
   - **Important:** The firmware filename must end in `.bin` and must not match the last filename that was flashed.
   - If the screen freezes, wait a few minutes and try again if needed.

3. **Access the web interface:**
   - Mainsail: `http://<host_ip>/`

## Prebuilt Docker Images

Prebuilt multi-architecture images (targeting **`linux/amd64`**, **`linux/arm64`**, and **`linux/arm/v7`**, including Raspberry Pi 3B+ and later) are automatically built and published to the [GitHub Container Registry](https://ghcr.io/destaben/klipper_ender3_v3_se) on every push to `main` and on version tags.

| Service | Image |
|---|---|
| Klipper | `ghcr.io/destaben/klipper_ender3_v3_se/klipper:latest` |
| Moonraker | `ghcr.io/destaben/klipper_ender3_v3_se/moonraker:latest` |
| Klipper Connect | `ghcr.io/destaben/klipper_ender3_v3_se/klipper-connect:latest` |
| MMU fallback (opt-in, two services: `mmu-fallback`, `mmu-virtual`) | `ghcr.io/destaben/klipper_ender3_v3_se/mmu-fallback:latest` |

The table lists the image names. `docker-compose.yaml` pins the images built from this repository (`klipper`, `moonraker`, `klipper-connect`, `mmu-fallback`) to a `sha-<short_sha>` tag (see [Available tags](#available-tags)), not to `latest`. The MMU fallback image is used only by the opt-in `mmu-fallback` compose profile, see [docs/virtual-mmu.md](docs/virtual-mmu.md). The Klipper Connect image is published by CI from `main` like the others. To build it locally instead, run `docker compose build klipper-connect`.

### Available tags

- `latest` — tracks the current `main` branch
- `sha-<short_sha>` — pinned to a specific commit
- `v<version>` / `<major>.<minor>` — published on version tag pushes (e.g. `v1.0.0`)

### Using prebuilt images (no local build required)

This is the recommended approach for Raspberry Pi 3B+ and later deployments where building from source is slow.

```bash
git clone https://github.com/destaben/klipper_ender3_v3_se.git
cd klipper_ender3_v3_se
# Clone required plugin dependencies
sudo bash setup_services.sh   # clones the plugin dependencies; see the script for details
# Pull prebuilt images from the registry
docker compose pull klipper moonraker
# Start everything
docker compose up -d
```

> **Note:** On first pull, Docker will automatically select the correct architecture (`arm64` on Raspberry Pi 3B+ and later with a 64-bit OS, `arm/v7` on any Pi running a 32-bit OS, `amd64` on x86 machines).

### Building images locally

If you prefer to build the images yourself (or need to customise the Dockerfiles):

```bash
docker compose up -d --build
```

### CI/CD pipeline

The [Build and Publish Docker Images](.github/workflows/docker-publish.yml) workflow runs on every push to `main` and on version tags. It:

1. Builds each image per platform (`linux/amd64`, `linux/arm64`, `linux/arm/v7`) in parallel jobs. `arm64` and `arm/v7` both build on a native Arm runner; `arm/v7` runs under QEMU emulation unless that runner's CPU executes 32-bit code.
2. Pushes each platform image by digest, then merges them into one multi-arch tag at `ghcr.io/destaben/klipper_ender3_v3_se/<service>`.
3. Applies `latest`, commit-SHA, and version tags automatically.

BuildKit layer cache is stored in the registry (`<service>:buildcache-<arch>` tags), not in the GitHub Actions cache, which is capped at 10 GB and evicts entries. `Dockerfile-klipper` installs the Python dependencies in a layer keyed only on the requirements files, so bumping the Klipper or Shake&Tune commit does not reinstall them. On `arm/v7` it pulls prebuilt wheels from [piwheels](https://www.piwheels.org/) (this is why the image is based on Debian bookworm, Python 3.11) instead of compiling numpy and matplotlib under emulation.

The [Klipper Connect patch tests](.github/workflows/klipper-connect-test.yml) workflow runs on pull requests and on pushes to `main` that touch `patches/klipper-connect/`, `Dockerfile-klipper-connect`, or the workflow file. It builds the `klipper-connect` image (which fails if the local patch no longer applies to the pinned upstream commit) and runs the patch's unit tests in it. The commands are in [Upgrading the pinned commit](docs/prusa-connect.md#upgrading-the-pinned-commit).

The [Virtual Printer Boot](.github/workflows/sim-boot.yml) workflow runs on pull requests and on pushes to `main` that touch `config/`, `sim/`, `mmu-fallback/`, or the workflow file itself. It boots `config/` in a virtual printer (a Klipper host and two virtual MCUs) and fails if Klippy does not reach `ready`. It does not simulate heating or motion. The same workflow unit-tests the `mmu-fallback` watcher.

The [Print Rescue Tests](.github/workflows/print-rescue-test.yml) workflow runs on pull requests and on pushes to `main` that touch `print-rescue/`, anything under `config/`, or the workflow file itself. It runs the standard-library unit tests of the read-only print-rescue planner. The planner, its refusal catalogue and the manual rescue procedure are described in [docs/print-rescue.md](docs/print-rescue.md); the [incident](docs/print-rescue-incident-2026-11-07.md), the [live validation](docs/print-rescue-live-validation-2026-11-07.md) and the [phase 2 design](docs/print-rescue-p2-design.md) have their own pages.

## Services

All services are defined in `docker-compose.yaml` and managed via Docker Compose.

### 1. Klipper

- **Container Name**: `klipper`
- **Image**: Custom build from `Dockerfile-klipper` (based on the [0xD34D/klipper_ender3_v3_se](https://github.com/0xD34D/klipper_ender3_v3_se) fork)
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
- **Image**: `traefik:3.6`
- **Ports**:
  - `80:80`: Reverse proxy routing HTTP traffic to Mainsail, Moonraker, and monitoring services.

### 5. Node Exporter

- **Container Name**: `node-exporter`
- **Image**: `prom/node-exporter:v1.9.1`
- Exposes host-level metrics (CPU, memory, disk) for monitoring.
- Also serves Raspberry Pi health metrics (throttling, voltage, temperature, clock, Wi-Fi signal) from `textfile/rpi.prom`, written every minute by `scripts/rpi-metrics.sh` via a user crontab line that `setup_services.sh` installs. See [docs/rpi-metrics.md](docs/rpi-metrics.md).

### 6. cAdvisor

- **Container Name**: `cadvisor`
- **Image**: `gcr.io/cadvisor/cadvisor:v0.52.0`
- Exposes per-container resource usage metrics.

### 7. Klipper Connect (optional)

- **Container Name**: `klipper-connect`
- **Image**: Custom build from `Dockerfile-klipper-connect`, pinned to a commit of [amd989/klipper-connect](https://github.com/amd989/klipper-connect) (a community bridge, not affiliated with Prusa)
- Connects the printer to Prusa Connect. It is in the `prusa-connect` compose profile, so a plain `docker compose up -d` does not start it. Setup and the security notes are in [docs/prusa-connect.md](docs/prusa-connect.md).

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
│   ├── klipper_connect.conf.example  # Prusa Connect bridge config template (copy to klipper_connect.conf)
│   └── helpers/               # Helper configuration files
├── klipper-connect/           # Prusa Connect token and job state (runtime, gitignored)
├── Dockerfile-klipper         # Dockerfile for the Klipper container
├── Dockerfile-moonraker       # Dockerfile for the Moonraker container
├── Dockerfile-klipper-connect # Dockerfile for the Prusa Connect bridge container
├── Dockerfile-mmu-fallback    # Dockerfile for the optional virtual Pico MMU sidecar
├── mmu-fallback/              # Virtual Pico MMU sidecar (mmu_link.py, run_virtual.sh and unit tests)
├── print-rescue/              # Read-only print rescue planner (phase 1) and its unit tests; see docs/print-rescue.md
├── serial-links/              # Stable serial path for the Pico MMU (default: symlink to the real device)
├── docs/                      # Upgrade notes, Prusa Connect and virtual MMU setup, and VFA docs (klipper-mmu-upgrade.md, prusa-connect.md, virtual-mmu.md, vfa-diagnosis.md, vfa-log.md, print-rescue.md, print-rescue-incident-2026-11-07.md, print-rescue-p2-design.md, print-rescue-live-validation-2026-11-07.md)
├── sim/                       # Virtual printer boot check (Dockerfile-sim, run.sh, check_ready.py, gen_sim_config.py, patches/)
├── docker-compose.yaml        # Docker Compose service definitions
├── config.ender3_v3_se        # Pre-configured firmware build config
├── build_firmware.sh          # Script to build Klipper firmware
├── setup_services.sh          # Script to install Docker and start services
├── set_static_wifi.sh         # Script to set a static WiFi IP
├── english_install_guide.md   # Multicolor install guide (English)
├── spanish_install_guide.md   # Multicolor install guide (Spanish)
├── nebula_setup_guide.md      # Creality Nebula Smart Kit setup guide
├── patch_configfile.sh        # Helper for patching the Klipper config
└── .github/                   # CI workflows and AI agent/skill definitions
```

## Notes

- Requires Docker and Docker Compose (installed automatically by `setup_services.sh`).
- The `config.ender3_v3_se` file is already prepared for the Ender 3 V3 SE and enables both serial ports (USART1 and USART2). You can edit it if your hardware only uses one of them.
- The `build_firmware.sh` script automates the firmware build and uses the included configuration.
- Traefik acts as a reverse proxy, exposing the services via HTTP on port 80.
- The Klipper container uses the [0xD34D/klipper_ender3_v3_se](https://github.com/0xD34D/klipper_ender3_v3_se) fork. This fork does not include the stock Ender 3 V3 SE display support that the previous `jpcurti/ender3-v3-se-klipper-with-display` fork provided (on-screen filament-lane macros, etc.).
- **Home Assistant integration**: `config/moonraker.conf` contains a `[power printer]` section that requires a Home Assistant long-lived access token. Copy `config/moonraker.secrets.example` to `config/moonraker.secrets` and replace `YOUR_HOMEASSISTANT_TOKEN_HERE` with your own token before starting the services. `docker-compose.yaml` mounts that file, and Moonraker will not start without it. Generate one in Home Assistant under **Profile → Long-Lived Access Tokens**. Never commit real tokens to version control. The same section powers the printer on when a job is queued in Mainsail (**Add to queue**): Moonraker switches the plug on, restarts Klipper 12 s later (`restart_delay`, a fixed timer: if the MCU takes longer to appear on USB, Klippy stays in "Unable to connect" and needs a manual `FIRMWARE_RESTART`), and the `[job_queue]` section (`load_on_startup`) starts the queued job when Klipper is ready. That start happens on every Klipper ready, so a restart while jobs are still queued also starts the next one; empty the queue to avoid it. With the plug already on, queuing does not restart Klipper. "Upload and print" from a slicer (Orca, PrusaSlicer, Cura) goes through the same queue: `[file_manager] queue_gcode_uploads` queues the file when it cannot start (printer off, or already printing). A **Print** click in Mainsail does not go through the queue, so it does not power the printer on. A start from Prusa Connect does: the Klipper Connect image carries a small patch that queues it when the printer is off (see [docs/prusa-connect.md](docs/prusa-connect.md)). A slicer upload sent while a print is running is queued instead of started; when the queue then runs it is up to Moonraker's job queue (not verified on this printer).
- **Automatic power-off**: `END_PRINT` calls `PRINTER_SHUTDOWN` (`config/macros/PRINTER_SHUTDOWN.cfg`), which switches the printer off through that Home Assistant switch once the hotend fan has stopped and 5 more minutes have passed. `START_PRINT` cancels a pending shutdown. If Klipper has unsaved `SAVE_CONFIG` changes, they are saved first and the printer powers off 30 s after the restart reaches `ready`, but only if Klipper is still `ready` then; run `CANCEL_PRINTER_SHUTDOWN` within those 30 s to stay on. A manual restart (for example `FIRMWARE_RESTART`) inside that window re-arms the 30 s timer, so cancel again afterwards. The save is skipped, and the power-off aborted, while a print is running or paused or a heater target (hotend or bed) is above 0. This uses the `shutdown_after_save` flag in `[save_variables]` (defined in `config/macros/sp_mmu.cfg`). The flag is set before `SAVE_CONFIG` runs, so if `SAVE_CONFIG` fails or the restart never reaches `ready`, it stays set and the next successful start powers off 30 s after `ready`. Clear it with `SAVE_VARIABLE VARIABLE=shutdown_after_save VALUE=0` or `CANCEL_PRINTER_SHUTDOWN`.

- **Starting without the Pico MMU (optional)**: an opt-in sidecar can back `[mcu PICO_MMU]` with a virtual MCU while the Pico is unplugged, and restart Klipper for you when it is plugged in or removed. Off by default. See [docs/virtual-mmu.md](docs/virtual-mmu.md).

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

## VFA (Vertical Fine Artifacts)

Diagnosis checklist, test protocol and investigation log for vertical fine artifacts: [docs/vfa-diagnosis.md](docs/vfa-diagnosis.md) and [docs/vfa-log.md](docs/vfa-log.md).

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
