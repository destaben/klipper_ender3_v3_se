#!/bin/sh
# Run the virtual Pico MMU (the Klipper "linux" firmware) as its own process,
# separate from the watcher, so recreating the watcher does not kill the MCU
# Klippy is connected to. See docs/virtual-mmu.md.
set -eu

link_dir=${LINK_DIR:-/run/klipper}

if [ "${PRINTER_MODE:-real}" = real ]; then
    echo "PRINTER_MODE=real: virtual MCU not started"
    exec sleep infinity
fi

# Never create the pty inside the tracked default (./serial-links): the watcher
# refuses that directory too, see docs/virtual-mmu.md.
if [ -e "$link_dir/.tracked-default" ]; then
    echo "$link_dir is the tracked default (./serial-links). Set MMU_LINK_DIR in .env to a separate directory" >&2
    exit 1
fi

# A stale link from a previous run would make the watcher think the MCU is up
rm -f "$link_dir/virtual-mmu"
exec /opt/mmu-fallback/klipper.elf -I "$link_dir/virtual-mmu"
