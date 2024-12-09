# Docker Services for 3D Printer Setup

This repository contains a Docker Compose configuration for setting up multiple services to manage a 3D printer using Klipper, Moonraker, Mainsail, Mobileraker, and other related services.

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

### 5. Moonraker Obico
- **Container Name**: `mobileraker_obico`
- **Image**: `ghcr.io/thespaghettidetective/moonraker-obico:latest`

### 6. Spoolman
- **Container Name**: `spoolman`
- **Image**: `ghcr.io/donkie/spoolman:latest`
- **Ports**:
  - `8000:8000`: Web interface for spool management

### 7. Traefik
- **Container Name**: `traefik`
- **Image**: `traefik:3.2`
- **Ports**:
  - `80:80`: Exposing Traefik's HTTP service

## Usage

1. **Clone the repository**:
   ```bash
   git clone https://your-repo-url.git
   cd your-repo-directory ´´´

2. **Modify the configuration files**:
   - In some application configuration files, you may need to replace the IP 192.168.1.222 with your own local IP address.

3. **Set file permissions for proper functionality**:
   - sudo chown -R $USER:$USER <REPO_FOLDER>
   - sudo chmod -R 755 <REPO_FOLDER>

    Replace <REPO_FOLDER> with the path to your cloned repository folder.

4. **Start the services using Docker Compose**:
   ```bash
   docker-compose up -d´´´

5. **Access the interfaces**:
   - Mainsail: http://<host_ip>/
   - Spoolman: http://<host_ip>:8000

6. **Stopping the services**:
   ```bash
   docker-compose down´´´

## Notes

- This setup requires Docker and Docker Compose installed on your system.
- Ensure that the printer's configuration is properly set in the klipper and moonraker service volumes.
- Traefik is set up to handle HTTP routing and expose the services through a reverse proxy.

## Customization

- You can adjust the configuration of the services by modifying the corresponding volumes and labels in the docker-compose.yml file.