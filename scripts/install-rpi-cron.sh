#!/bin/sh
# Install the rpi-metrics crontab line for the invoking user (SUDO_USER when
# run through sudo). Usage: scripts/install-rpi-cron.sh <repo-dir>
#
# - Creates <repo-dir>/textfile (owned by the cron user when run as root);
#   refuses it if it is a symlink or not a directory.
# - Refuses a repo path with characters that are special to cron or the shell
#   ($ ` % " ' \ ; whitespace, ...): the path is written into the crontab, and
#   '$(...)' or backticks would run every minute, '%' truncates the line.
# - Replaces any earlier rpi-metrics line, keeps the other jobs, and rewrites
#   the crontab only if the old one was read or does not exist yet.
# Failures print a WARNING and exit 0 so they never abort the caller.

set -eu

repo=${1:-}
if [ -z "$repo" ] || [ ! -d "$repo" ]; then
    echo "usage: $0 <existing repo-dir>" >&2
    exit 2
fi
repo=$(cd "$repo" && pwd)

case $repo in
    /*) ;;
    *) echo "WARNING: repo path is not absolute; not installing the rpi-metrics cron line." >&2; exit 0 ;;
esac
case $repo in
    *[!A-Za-z0-9_./+-]*)
        echo "WARNING: repo path '$repo' has characters unsafe for a crontab line (allowed: A-Z a-z 0-9 _ . / + -); not installing the rpi-metrics cron line." >&2
        exit 0
        ;;
esac

if ! command -v crontab >/dev/null 2>&1; then
    echo "WARNING: crontab not found; install cron (sudo apt-get install -y cron) and re-run to enable Pi metrics." >&2
    exit 0
fi

user=${SUDO_USER:-${USER:-$(id -un)}}

# Never follow a symlink here: as root, chown on a symlinked textfile/ would
# hand an arbitrary directory to the cron user. A plain file is refused too.
if [ -L "$repo/textfile" ] || { [ -e "$repo/textfile" ] && [ ! -d "$repo/textfile" ]; }; then
    echo "WARNING: $repo/textfile is a symlink or not a directory; not installing the rpi-metrics cron line." >&2
    exit 0
fi
if ! mkdir -p "$repo/textfile"; then
    echo "WARNING: could not create $repo/textfile; not installing the rpi-metrics cron line." >&2
    exit 0
fi
if [ "$(id -u)" -eq 0 ]; then
    chown -h "$user" "$repo/textfile" 2>/dev/null || true
    set -- -u "$user"
else
    set --
fi

line="* * * * * $repo/scripts/rpi-metrics.sh $repo/textfile"

if current=$(LC_ALL=C crontab -l "$@" 2>&1); then
    :
elif printf '%s' "$current" | grep -q '^no crontab for'; then
    current=""
else
    echo "WARNING: could not read the crontab of $user; not installing the rpi-metrics line." >&2
    exit 0
fi

kept=$(printf '%s\n' "$current" | grep -vF 'scripts/rpi-metrics.sh' || true)
if { [ -z "$kept" ] || printf '%s\n' "$kept"; printf '%s\n' "$line"; } | crontab "$@" -; then
    echo "Installed rpi-metrics crontab line for $user."
else
    echo "WARNING: failed to write the crontab of $user." >&2
fi
