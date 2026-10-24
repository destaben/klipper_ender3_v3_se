#!/bin/bash

set -euo pipefail  # Stop on error, treat unset variables as errors, propagate pipe failures

# Variables
KLIPPER_REPO="https://github.com/0xD34D/klipper_ender3_v3_se.git"
# Same commit as KLIPPER_COMMIT in Dockerfile-klipper, so both build paths use one source.
KLIPPER_COMMIT=38604d84318dd4af1ab2d0adc26b9bb303bf382c
TIMESTAMP=$(date +"%Y%m%d_%H%M%S")
CONFIG_FILE="config.ender3_v3_se"

# Install dependencies for firmware build
sudo apt update
sudo apt install -y git make gcc-arm-none-eabi binutils-arm-none-eabi libnewlib-arm-none-eabi \
    libusb-1.0-0-dev dfu-util python3 python3-pip python3-virtualenv python3-dev

# Clone Klipper fork
if [ -d "klipper" ]; then
    echo "Klipper directory already exists. Fetching..."
    # Fetch rather than pull: the checkout below leaves the clone on a detached commit.
    git -C "klipper" fetch origin
else
    git clone "$KLIPPER_REPO" "klipper"
fi
git -C "klipper" checkout "$KLIPPER_COMMIT"
cd "klipper"

# Clean previous build artifacts
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

# Notify user
FIRMWARE_FILE="out/klipper_$TIMESTAMP.bin"
mv "out/klipper.bin" "$FIRMWARE_FILE"
echo "Firmware successfully compiled."

echo "Please manually copy the firmware file to the SD card:"
echo "$PWD/$FIRMWARE_FILE"
echo "Then insert the SD card into the printer and restart to flash."