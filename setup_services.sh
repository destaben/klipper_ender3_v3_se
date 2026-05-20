#!/bin/bash

set -euo pipefail  # Stop on error, treat unset variables as errors, propagate pipe failures

if ! command -v docker &> /dev/null; then
    echo "Installing Docker..."
    curl -fsSL https://get.docker.com -o get-docker.sh
    sudo sh get-docker.sh
    rm -f get-docker.sh
    sudo usermod -aG docker "$USER"
    echo "Docker installed successfully. Please log out and log back in to apply group changes."
else
    echo "Docker is already installed."
fi

if ! docker compose version &> /dev/null; then
    echo "Installing Docker Compose plugin..."
    sudo apt install -y docker-compose-plugin
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
        (cd "$repo" && git pull)
    else
        git clone "${repos[$repo]}"
    fi
done

if [ -f "KlipperMaintenance/maintain.py" ]; then
    sed -i 's|http://localhost:7125|http://moonraker:7125|g' KlipperMaintenance/maintain.py
fi

# Enable cgroup memory limits on Raspberry Pi (required for Docker memory limits)
CMDLINE=/boot/firmware/cmdline.txt
if [ -f "$CMDLINE" ]; then
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
# Use sg to apply docker group membership without requiring a new login session
sg docker -c "docker compose up -d"

echo "Docker setup and services started successfully."
