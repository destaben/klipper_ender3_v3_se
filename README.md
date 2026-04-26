# Ender 3 V3 SE Klipper

This repository contains everything needed to manage an Ender 3 V3 SE with Klipper, Moonraker, Mainsail, and extra utilities, using Docker Compose and automated scripts.

Includes:

- Scripts to build and flash Klipper firmware (`build_firmware.sh`)
- Automated setup script for Docker and dependencies (`setup_services.sh`)
- Pre-made configuration for Ender 3 V3 SE
- Docker Compose for Klipper, Moonraker, Mainsail, Traefik, and utilities
- Integrations: [Shake&Tune](https://github.com/Frix-x/klippain-shaketune), [KlipperMaintenance](https://github.com/3DCoded/KlipperMaintenance), [Moonraker Timelapse](https://github.com/mainsail-crew/moonraker-timelapse), and [Chopper Resonance Tuner](https://github.com/MRX8024/chopper-resonance-tuner)
- **AI print monitoring** via Tapo RTSP camera + [Obico ML API](https://github.com/TheSpaghettiDetective/obico-server) (`print-watcher` service)

> **Multicolor printing:** See the [English Installation Guide](english_install_guide.md) or the [Guía de instalación en español](spanish_install_guide.md) for a complete walkthrough on setting up Klipper with Pico MMU for multicolor printing.

## Table of Contents

- [Prerequisites](#prerequisites)
- [Quick Start](#quick-start)
- [Services](#services)
- [AI Print Monitoring (Tapo Camera)](#ai-print-monitoring-tapo-camera)
- [Repository Structure](#repository-structure)
- [Notes](#notes)
- [FAQ](#faq)
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

### 8. MediaMTX *(optional — AI monitoring)*

- **Container Name**: `mediamtx`
- **Image**: `bluenviron/mediamtx:latest`
- Re-publishes the Tapo RTSP camera stream as HLS (port `8888`) and RTSP (port `8554`) so Mainsail and other clients can display the live feed.
- Set `TAPO_RTSP_URL` in the environment before starting (see [AI Print Monitoring](#ai-print-monitoring-tapo-camera)).

### 9. Obico ML API *(optional — AI monitoring)*

- **Container Name**: `obico-ml-api`
- **Image**: `thespaghettidetective/ml_api:latest`
- Runs the [Obico](https://github.com/TheSpaghettiDetective/obico-server) AI failure-detection model locally. Accepts JPEG frames on `POST /p` and returns a failure probability score.

### 10. Print Watcher *(optional — AI monitoring)*

- **Container Name**: `print-watcher`
- **Image**: Custom build from `Dockerfile-print-watcher` (Python 3.11 + ffmpeg)
- Periodically captures a frame from the Tapo camera, sends it to the Obico ML API, and optionally pauses the print when failures are repeatedly detected.
- Configurable via environment variables (see [AI Print Monitoring](#ai-print-monitoring-tapo-camera)).

## AI Print Monitoring (Tapo Camera)

The stack includes three optional Docker services that together enable AI-based print-failure detection using a **Tapo C200 / C210** (or any RTSP-capable) IP camera.

### How it works

```
Tapo camera (RTSP)
        │
        ├──► mediamtx ──► HLS stream ──► Mainsail camera feed
        │
        └──► print-watcher
                │  (ffmpeg frame capture, every N seconds)
                ▼
          obico-ml-api  (Obico failure-detection model)
                │  failure score 0–1
                ▼
          Moonraker API  (optional: pause print on failure)
```

| Service | Purpose |
|---|---|
| `mediamtx` | Proxies the Tapo RTSP stream to HLS so Mainsail can display it |
| `obico-ml-api` | Runs the Obico AI model locally (spaghetti detection) |
| `print-watcher` | Captures frames, calls the ML API, pauses print on failures |

### Prerequisites

- Tapo camera with RTSP enabled (enable in the Tapo app under **Advanced > Camera Account**)
- RTSP URL format: `rtsp://<camera_user>:<camera_password>@<camera_ip>/stream1`
- The host machine must be on the same LAN as the camera

### Setup

1. **Enable RTSP on the Tapo camera**

   Open the Tapo app → select your camera → **Settings → Advanced Settings → Camera Account**.  
   Create a username/password; the RTSP URL will be:

   ```
   rtsp://<username>:<password>@<camera_ip>/stream1
   ```

   For lower-resolution stream use `/stream2`.

2. **Configure the RTSP URL**

   Create a `.env` file in the repository root (it is already git-ignored):

   ```bash
   # .env
   TAPO_RTSP_URL=rtsp://admin:secret@192.168.1.100/stream1
   ```

   All three AI-monitoring services read this variable automatically via Docker Compose variable substitution.

3. **Start the services**

   ```bash
   docker compose up -d mediamtx obico-ml-api print-watcher
   ```

   On first run, `obico-ml-api` downloads the model weights (~500 MB). Wait until the container logs show it is ready before expecting detection results.

4. **Add the camera to Mainsail**

   Edit `config/moonraker.conf`, uncomment the `[webcam tapo]` block, and replace `<HOST_IP>` with your host machine's LAN IP:

   ```ini
   [webcam tapo]
   location: printer
   enabled: True
   service: hlsstream
   target_fps: 15
   target_fps_idle: 5
   stream_url: http://<HOST_IP>:8888/tapo
   snapshot_url: http://<HOST_IP>:8888/tapo/snapshot.jpg
   flip_horizontal: False
   flip_vertical: False
   rotation: 0
   aspect_ratio: 4:3
   ```

   Then restart Moonraker: `docker compose restart moonraker`.

### Environment variables (print-watcher)

| Variable | Default | Description |
|---|---|---|
| `TAPO_RTSP_URL` | *(empty)* | Full RTSP URL of the camera (takes priority over snapshot URL) |
| `TAPO_SNAPSHOT_URL` | *(empty)* | HTTP snapshot URL (alternative when RTSP is not available) |
| `DETECTION_INTERVAL` | `10` | Seconds between detection runs |
| `CONFIDENCE_THRESHOLD` | `0.3` | Failure score (0–1) above which a failure is reported |
| `ENABLE_AUTO_PAUSE` | `false` | Set to `true` to automatically pause the print on repeated failures |

To enable auto-pause, add to your `.env`:

```bash
ENABLE_AUTO_PAUSE=true
```

### Adjusting sensitivity

- **Lower `CONFIDENCE_THRESHOLD`** (e.g., `0.2`) → more sensitive, may produce false positives
- **Higher `CONFIDENCE_THRESHOLD`** (e.g., `0.5`) → fewer alerts, may miss early failures
- The print is paused only after **2 consecutive** detections above the threshold, reducing false positives

### Hardware notes

| Component | Recommended |
|---|---|
| Host CPU | Arm64 / x86_64 with at least 2 cores |
| RAM | Minimum 2 GB free for `obico-ml-api` (~1 GB) + other services |
| Camera | Tapo C200 or C210 (720p/1080p RTSP) |
| Network | Camera and host on the same LAN (wired preferred for reliability) |

> **Tip:** The `obico-ml-api` container is CPU-only by default. Each call to the ML API (one per `DETECTION_INTERVAL` while printing) consumes roughly 1–2 seconds of CPU time. On low-power hardware (e.g., Raspberry Pi 4) you may need to increase `DETECTION_INTERVAL` to `30` or more so that inference finishes well before the next capture is triggered, avoiding CPU saturation.

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
├── Dockerfile-print-watcher   # Dockerfile for the AI print-watcher container
├── docker-compose.yaml        # Docker Compose service definitions
├── config.ender3_v3_se        # Pre-configured firmware build config
├── build_firmware.sh          # Script to build Klipper firmware
├── setup_services.sh          # Script to install Docker and start services
├── set_static_wifi.sh         # Script to set a static WiFi IP
├── usb_watcher.py             # USB reconnection watcher script
├── print_watcher.py           # AI print failure detection script (Tapo + Obico)
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
