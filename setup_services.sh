#!/bin/bash

set -euo pipefail  # Stop on error, treat unset variables as errors, propagate pipe failures

if ! command -v docker &> /dev/null; then
    echo "Installing Docker..."
    curl -fsSL https://get.docker.com -o get-docker.sh
    # Sanity-check the download before running it as root: it must be a
    # non-empty shell script, not an empty/truncated file from a flaky network.
    if [ ! -s get-docker.sh ] || ! head -n 1 get-docker.sh | grep -q '^#!/'; then
        echo "ERROR: downloaded Docker install script looks invalid or truncated. Aborting." >&2
        rm -f get-docker.sh
        exit 1
    fi
    sudo sh get-docker.sh
    rm -f get-docker.sh
    sudo usermod -aG docker "$USER"
    echo "Docker installed successfully. Please log out and log back in to apply group changes."
else
    echo "Docker is already installed."
fi

if ! docker compose version &> /dev/null; then
    echo "Installing Docker Compose plugin..."
    sudo apt-get update
    sudo apt-get install -y docker-compose-plugin
    echo "Docker Compose installed successfully."
else
    echo "Docker Compose is already installed."
fi

# Additional dependencies
declare -A repos=(
    ["chopper-resonance-tuner"]="https://github.com/MRX8024/chopper-resonance-tuner.git"
    ["KlipperMaintenance"]="https://github.com/3DCoded/KlipperMaintenance.git"
    ["moonraker-timelapse"]="https://github.com/mainsail-crew/moonraker-timelapse.git"
)

for repo in "${!repos[@]}"; do
    if [ -d "$repo" ]; then
        echo "$repo already exists. Updating..."
        # Discard the local maintain.py patch (applied below) before pulling,
        # otherwise git pull aborts on local changes to a tracked file.
        (cd "$repo" && git checkout -- . && git pull)
    else
        git clone "${repos[$repo]}"
    fi
done

if [ -f "KlipperMaintenance/maintain.py" ]; then
    sed -i 's|http://localhost:7125|http://moonraker:7125|g' KlipperMaintenance/maintain.py
fi

# Enable cgroup memory limits on Raspberry Pi (required for Docker memory limits).
# Location depends on the OS: Raspberry Pi OS Bookworm+ uses /boot/firmware/,
# older Raspberry Pi OS releases use /boot/ directly.
CMDLINE=""
for candidate in /boot/firmware/cmdline.txt /boot/cmdline.txt; do
    if [ -f "$candidate" ]; then
        CMDLINE="$candidate"
        break
    fi
done
if [ -n "$CMDLINE" ]; then
    if ! grep -q "cgroup_enable=memory" "$CMDLINE"; then
        echo "Enabling cgroup memory in $CMDLINE..."
        sudo cp "$CMDLINE" "${CMDLINE}.bak"
        sudo sed -i 's/$/ cgroup_enable=memory cgroup_memory=1/' "$CMDLINE"
        echo "WARNING: cgroup memory parameters added. A reboot is required before starting containers."
        echo "Please run: sudo reboot"
        exit 0
    else
        echo "cgroup memory already enabled in $CMDLINE."
    fi
fi
echo "Starting Docker containers..."
# Add user to docker group if missing (requires sudo)
if ! id -nG "$USER" | grep -qw docker; then
    echo "Adding '$USER' to the docker group..."
    sudo usermod -aG docker "$USER"
fi
# Create bind-mount host directories for moonraker so the container user can write to them.
# The permissions set inside the Dockerfile are masked by bind mounts, so they must be
# created here before the containers start.
mkdir -p moonraker/logs moonraker/database
# Match ownership to the moonraker container's UID/GID instead of making the
# directories world-writable. Falls back to a permissive chmod if detection fails
# (e.g. first run before the image can be built). Uses sg so the just-added
# docker group membership above applies without requiring a new login session.
MOONRAKER_IDS=$(sg docker -c "docker compose run --rm --no-deps --build --entrypoint sh moonraker -c 'id -u; id -g'" 2>/dev/null | tr -d '\r' || true)
if [ "$(printf '%s\n' "$MOONRAKER_IDS" | wc -l)" -ge 2 ]; then
    MOONRAKER_UID=$(printf '%s\n' "$MOONRAKER_IDS" | sed -n '1p')
    MOONRAKER_GID=$(printf '%s\n' "$MOONRAKER_IDS" | sed -n '2p')
    sudo chown -R "${MOONRAKER_UID}:${MOONRAKER_GID}" moonraker/logs moonraker/database
    chmod 770 moonraker/logs moonraker/database
else
    echo "WARNING: could not determine moonraker container UID/GID; falling back to permissive chmod 777."
    chmod 777 moonraker/logs moonraker/database
fi
# Use sg to apply docker group membership without requiring a new login session
sg docker -c "docker compose up -d"

echo "Docker setup and services started successfully."
