#!/bin/bash

set -e  # Stop the script if an error occurs

if ! command -v docker &> /dev/null; then
    echo "Installing Docker..."
    curl -fsSL https://get.docker.com -o get-docker.sh
    sudo sh get-docker.sh
    rm get-docker.sh
    sudo usermod -aG docker "$USER"
    echo "Docker installed successfully. Please log out and log back in to apply group changes."
else
    echo "Docker is already installed."
fi

if ! docker compose version &> /dev/null; then
    echo "Docker Compose plugin not found. Installing..."
    sudo apt install -y docker-compose-plugin
    echo "Docker Compose plugin installed successfully."
else
    echo "Docker Compose plugin is already available."
fi

# Aditional dependencies
declare -A repos=(
    ["chopper-resonance-tuner"]="https://github.com/MRX8024/chopper-resonance-tuner.git"
    ["KlipperMaintenance"]="https://github.com/3DCoded/KlipperMaintenance.git"
    ["moonraker-timelapse"]="https://github.com/mainsail-crew/moonraker-timelapse.git"
)

for repo in "${!repos[@]}"; do
    if [ -d "$repo" ]; then
        echo "$repo already exists. Updating..."
        cd "$repo" && git pull && cd ..
    else
        git clone "${repos[$repo]}"
    fi
done

if [ -f "KlipperMaintenance/maintain.py" ]; then
    sed -i 's|http://localhost:7125|http://moonraker:7125|g' KlipperMaintenance/maintain.py
fi

echo "Starting Docker containers..."
docker compose up -d

echo "Docker setup and services started successfully."