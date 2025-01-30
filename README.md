# Ender 3 V3 SE Klipper

This repository contains a Docker Compose configuration for setting up multiple services to manage a Ender 3 V3 SE using Klipper, Moonraker, Mainsail, Mobileraker, and other related services.

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

### 4. Mobileraker Companion
- **Container Name**: `mobileraker_companion`
- **Image**: `ghcr.io/clon1998/mobileraker_companion:latest`

### 5. Traefik
- **Container Name**: `traefik`
- **Image**: `traefik:3.2`
- **Ports**:
  - `80:80`: Exposing Traefik's HTTP service

## Usage

1. **Clone the repository and run the script**:
   ```bash
   git clone https://github.com/destaben/klipper_ender3_v3_se.git
   cd klipper_ender3_v3_se
   bash run_klipper.sh
   ```

2. **Flash your printer**
   - In the last step, you will see a path at the end of the script execution. You need to copy that file to the SD card and turn on the printer. After waiting for about 5 minutes, the printer should be flashed. If this does not happen, it is recommended to try multiple times, as there is no way to determine if it was successful or not since the screen will become unusable. The important thing is to proceed to the next step after waiting for 5 minutes, and if it doesn't work, try again. The SD card should contain only the file to be flashed.

3. **Access the interfaces**:
   - Mainsail: http://<host_ip>/

4. **Change snapshot_uri - Optional for timelapse**
   - Set your own IP in mobileraker.conf, change snapshot_uri. Replace 192.168.1.222 (my local IP) by the output of this command:
   ```bash
   hostname -I | awk '{print $1}'
   ```
   (your local IP)

## Notes

- This setup requires Docker and Docker Compose installed on your system.
- Ensure that the printer's configuration is properly set in the klipper and moonraker service volumes.
- Traefik is set up to handle HTTP routing and expose the services through a reverse proxy.

## Customization

- You can adjust the configuration of the services by modifying the corresponding volumes and labels in the docker-compose.yml file.