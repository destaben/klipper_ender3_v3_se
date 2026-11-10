#!/bin/bash

set -euo pipefail  # Stop on error, treat unset variables as errors, propagate pipe failures

if ! command -v docker &> /dev/null; then
    echo "Installing Docker..."
    curl -fsSL https://get.docker.com -o get-docker.sh
    # Sanity-check the download before running it as root. curl -f already
    # aborts on an HTTP error, but a connection cut mid-transfer can still
    # leave a short, truncated-but-valid-looking file; the real install
    # script is tens of KB, so a short file is a reliable truncation signal.
    if [ "$(wc -c < get-docker.sh)" -lt 2000 ] || ! head -n 1 get-docker.sh | grep -q '^#!/'; then
        echo "ERROR: downloaded Docker install script looks invalid or truncated. Aborting." >&2
        rm -f get-docker.sh
        exit 1
    fi
    sudo sh get-docker.sh
    rm -f get-docker.sh
    sudo usermod -aG docker "${SUDO_USER:-$USER}"
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
        if [ "$repo" = "KlipperMaintenance" ]; then
            # Discard only the local maintain.py patch (re-applied below)
            # before pulling, not the whole working tree: a blanket discard
            # would also silently wipe any other local edit in this clone.
            (cd "$repo" && git checkout -- maintain.py && git pull)
        else
            (cd "$repo" && git pull)
        fi
    else
        git clone "${repos[$repo]}"
    fi
done

if [ -f "KlipperMaintenance/maintain.py" ]; then
    sed -i 's|http://localhost:7125|http://moonraker:7125|g' KlipperMaintenance/maintain.py
    if ! grep -q 'http://moonraker:7125' KlipperMaintenance/maintain.py; then
        echo "error: maintain.py patch did not apply (upstream URL changed?)" >&2
        exit 1
    fi
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
if ! id -nG "${SUDO_USER:-$USER}" | grep -qw docker; then
    echo "Adding '${SUDO_USER:-$USER}' to the docker group..."
    sudo usermod -aG docker "${SUDO_USER:-$USER}"
fi
# Create bind-mount host directories for moonraker so the container user can write to them.
# The permissions set inside the Dockerfile are masked by bind mounts, so they must be
# created here before the containers start.
mkdir -p moonraker/logs moonraker/database
# Match ownership to the moonraker container's UID/GID instead of making the
# directories world-writable. Build and run the image directly (bypassing
# `docker compose run`'s volumes) so this works even before config/moonraker.secrets
# exists, since that bind mount has create_host_path: false and would otherwise
# make this whole detection fail on a fresh install. Uses sg so the just-added
# docker group membership above applies to the commands in this script without requiring
# a new login session (your own shell still needs a re-login or `newgrp docker`).
sg docker -c "docker compose build moonraker"
MOONRAKER_IMAGE=$(sg docker -c "docker compose config --images moonraker")
MOONRAKER_IDS=$(sg docker -c "docker run --rm --entrypoint sh '$MOONRAKER_IMAGE' -c 'id -u; id -g'" | tr -d '\r' || true)
if [ "$(printf '%s\n' "$MOONRAKER_IDS" | wc -l)" -ge 2 ]; then
    MOONRAKER_UID=$(printf '%s\n' "$MOONRAKER_IDS" | sed -n '1p')
    MOONRAKER_GID=$(printf '%s\n' "$MOONRAKER_IDS" | sed -n '2p')
    sudo chown -R "${MOONRAKER_UID}:${MOONRAKER_GID}" moonraker/logs moonraker/database
    chmod 770 moonraker/logs moonraker/database
    # Also let the host login user read/write these directly (e.g. `cat
    # moonraker/logs/moonraker.log`) without sudo, instead of falling back
    # to a world-writable chmod 777.
    if ! command -v setfacl &> /dev/null; then
        sudo apt-get install -y acl
    fi
    sudo setfacl -R -m "u:${SUDO_USER:-$USER}:rwX,d:u:${SUDO_USER:-$USER}:rwX" moonraker/logs moonraker/database
else
    echo "WARNING: could not determine moonraker container UID/GID (see build/run output above); falling back to permissive chmod 777."
    chmod 777 moonraker/logs moonraker/database
fi
# Raspberry Pi health metrics for node-exporter's textfile collector: a user
# crontab line runs scripts/rpi-metrics.sh every minute and writes
# textfile/rpi.prom (mounted read-only into node-exporter). Idempotent: any
# earlier rpi-metrics line is replaced. Remove with:
#   crontab -l | grep -vF 'scripts/rpi-metrics.sh' | crontab -   (as the same user)
mkdir -p textfile
if command -v crontab &> /dev/null; then
    REPO_DIR=$(pwd)
    CRON_USER="${SUDO_USER:-$USER}"
    # `crontab -u` needs root; a plain user edits their own crontab directly.
    CRON_ARGS=()
    if [ "$(id -u)" -eq 0 ]; then
        CRON_ARGS=(-u "$CRON_USER")
    fi
    CRON_LINE="* * * * * \"${REPO_DIR}/scripts/rpi-metrics.sh\" \"${REPO_DIR}/textfile\""
    # Only rewrite the crontab when the current one was read successfully or
    # does not exist yet, so an unexpected `crontab -l` failure can never
    # replace the user's other jobs with the metrics line alone.
    if CURRENT_CRON=$(LC_ALL=C crontab -l "${CRON_ARGS[@]}" 2>&1) || printf '%s' "$CURRENT_CRON" | grep -q '^no crontab for'; then
        if ! printf '%s' "$CURRENT_CRON" | grep -q '^no crontab for'; then
            CURRENT_CRON=$(printf '%s\n' "$CURRENT_CRON" | grep -vF 'scripts/rpi-metrics.sh' || true)
        else
            CURRENT_CRON=""
        fi
        { [ -n "$CURRENT_CRON" ] && printf '%s\n' "$CURRENT_CRON"; echo "$CRON_LINE"; } | crontab "${CRON_ARGS[@]}" -
        echo "Installed rpi-metrics crontab line for $CRON_USER."
    else
        echo "WARNING: could not read the crontab of $CRON_USER; not installing the rpi-metrics line." >&2
    fi
else
    echo "WARNING: crontab not found; install cron (sudo apt-get install -y cron) and re-run to enable Pi metrics."
fi
# Use sg to apply docker group membership without requiring a new login session
sg docker -c "docker compose up -d"

echo "Docker setup and services started successfully."
