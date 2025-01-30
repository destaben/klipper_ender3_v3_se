#!/bin/bash

set -e  # Stop the script if an error occurs

# Variables
KLIPPER_REPO="https://github.com/0xD34D/klipper_ender3_v3_se.git"
TIMESTAMP=$(date +"%Y%m%d_%H%M%S")
CONFIG_FILE="config.ender3_v3_se"
MOBILERAKER_CONF="mobileraker.conf"

# Install dependencies
sudo apt update
sudo apt install -y git make gcc-arm-none-eabi binutils-arm-none-eabi libnewlib-arm-none-eabi \
    libusb-1.0-0-dev dfu-util python3 python3-pip python3-virtualenv python3-dev

# Install Docker
if ! command -v docker &> /dev/null; then
    echo "Installing Docker..."
    curl -fsSL https://get.docker.com -o get-docker.sh
    sudo sh get-docker.sh
    rm get-docker.sh
    sudo usermod -aG docker $USER
    echo "Docker installed successfully. Please log out and log back in to apply group changes."
else
    echo "Docker is already installed."
fi

# Install Docker Compose
if ! command -v docker-compose &> /dev/null; then
    echo "Installing Docker Compose..."
    sudo apt install -y docker-compose
    echo "Docker Compose installed successfully."
else
    echo "Docker Compose is already installed."
fi

# Clone Klipper fork
if [ -d "klipper" ]; then
    echo "Klipper directory already exists. Updating..."
    cd "klipper" && git pull
else
    git clone "$KLIPPER_REPO" "klipper"
    cd "klipper"
fi

# Create Python virtual environment for Klipper
make clean

# Use a pre-configured .config file
if [ -f "../$CONFIG_FILE" ]; then
    echo "Using pre-configured $CONFIG_FILE file."
    cp "../$CONFIG_FILE" .config
else
    echo "Error: Pre-configured $CONFIG_FILE file not found. Please run make menuconfig manually and save it."
    exit 1
fi

# Compile firmware for Ender 3 V3 SE
if ! make 2>&1 | tee build_log.txt; then
    echo "Error: Compilation failed. Check build_log.txt for details."
    exit 1
fi

# Notify user to manually copy firmware to SD card
FIRMWARE_FILE="out/klipper_$TIMESTAMP.bin"

mv "out/klipper.bin" "$FIRMWARE_FILE"
echo "Firmware successfully compiled."

# Clone additional repositories
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

# Start Docker services
echo "Starting Docker containers..."
docker compose up -d

echo "Setup completed successfully."

echo "Please manually copy the firmware file to the SD card:"
echo "$PWD/$FIRMWARE_FILE"
echo "Then insert the SD card into the printer and restart to flash."