#!/bin/sh
# Boot the virtual printer with the mounted printer config and check it reaches ready.
#
# The config is read from /printer-config (read-only mount). Runtime files live
# under /tmp and /root/printer_data, so nothing is written to the mount.
set -eu

KLIPPER_DIR=/opt/klipper
SIM_DIR=/opt/sim
SIM_CONFIG=/tmp/sim-config
API_SOCKET=/tmp/klippy_api.sock
LOG_DIR=/tmp/sim-logs
TIMEOUT=${SIM_READY_TIMEOUT:-120}

# Run with the production home so "~/printer_data" in [save_variables] resolves
# to the same path it does in the klipper container (user klipper).
export HOME=/home/klipper
mkdir -p "$LOG_DIR" "$HOME/printer_data"
# Files the printer config expects, created empty as in the production image
touch "$HOME/printer_data/sp_mmu_data.cfg"
echo 42000 > /tmp/sim_cpu_temp
rm -f /tmp/klipper_host_mcu /tmp/klipper_pico_mmu "$API_SOCKET"

python3 "$SIM_DIR/gen_sim_config.py" /printer-config "$SIM_CONFIG"

"$KLIPPER_DIR/out/klipper.elf" -I /tmp/klipper_host_mcu >"$LOG_DIR/mcu.log" 2>&1 &
MCU_PID=$!
"$KLIPPER_DIR/out/klipper.elf" -I /tmp/klipper_pico_mmu >"$LOG_DIR/mcu_pico.log" 2>&1 &
PICO_PID=$!

# Each virtual MCU creates its serial pty link when it starts. Klippy must not
# connect before the link exists, so wait for it instead of sleeping.
wait_for_pty() {
    waited=0
    while [ ! -e "$1" ]; do
        waited=$((waited + 1))
        if [ "$waited" -gt 30 ]; then
            echo "MCU serial port $1 did not appear" >&2
            exit 1
        fi
        sleep 1
    done
}
wait_for_pty /tmp/klipper_host_mcu
wait_for_pty /tmp/klipper_pico_mmu

KLIPPY_PID=""
cd "$KLIPPER_DIR"
/opt/klippy-env/bin/python klippy/klippy.py "$SIM_CONFIG/printer.cfg" \
    -l "$LOG_DIR/klippy.log" -a "$API_SOCKET" >"$LOG_DIR/klippy.out" 2>&1 &
KLIPPY_PID=$!

status=0
/opt/klippy-env/bin/python "$SIM_DIR/check_ready.py" "$API_SOCKET" "$TIMEOUT" "$KLIPPY_PID" || status=$?

# Only exercise the macros once the printer is ready
if [ "$status" -eq 0 ]; then
    /opt/klippy-env/bin/python "$SIM_DIR/smoke.py" "$API_SOCKET" || status=$?
fi

if [ "$status" -ne 0 ]; then
    echo "--- klippy.log (errors and last lines)" >&2
    grep -E "Config error|Traceback|Error|error" "$LOG_DIR/klippy.log" | grep -v "RESPOND\|{" | tail -30 >&2 || true
    tail -20 "$LOG_DIR/klippy.log" >&2 || true
    echo "--- virtual MCU logs (last lines)" >&2
    tail -5 "$LOG_DIR/mcu.log" "$LOG_DIR/mcu_pico.log" >&2 || true
fi

kill "$KLIPPY_PID" "$MCU_PID" "$PICO_PID" 2>/dev/null || true
exit "$status"
