#!/bin/bash

set -e  # Stop the script if an error occurs

# Variables
KLIPPER_REPO="https://github.com/0xD34D/klipper_ender3_v3_se.git"
TIMESTAMP=$(date +"%Y%m%d_%H%M%S")
CONFIG_FILE="config.ender3_v3_se"

# Install dependencies
sudo apt update
sudo apt install -y git make gcc-arm-none-eabi binutils-arm-none-eabi libnewlib-arm-none-eabi \
    libusb-1.0-0-dev dfu-util python3 python3-pip python3-virtualenv python3-dev

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
echo "Please manually copy the firmware file to the SD card:"
echo "$PWD/$FIRMWARE_FILE"
echo "Then insert the SD card into the printer and restart to flash."