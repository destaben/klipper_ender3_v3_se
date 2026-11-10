#!/bin/sh
# Write Raspberry Pi health metrics for node-exporter's textfile collector.
#
# Usage: scripts/rpi-metrics.sh <textfile-dir>
# Writes <textfile-dir>/rpi.prom atomically (temp file + rename), so the
# collector never reads a half-written file. Run it from a user crontab
# (setup_services.sh installs the line). The Pi shares one USB/SDIO bus, so
# this makes at most two vcgencmd calls per run (get_throttled, measure_volts)
# when the sysfs files exist;
# temperature, ARM clock and Wi-Fi come from sysfs and /proc. vcgencmd is
# used for temperature and clock only if the sysfs file is missing.
#
# A metric whose source is unavailable is left out instead of reported as 0.
#
# Test overrides (environment): RPI_THERMAL_FILE, RPI_CPUFREQ_FILE,
# RPI_WIRELESS_FILE, RPI_WIFI_IFACE.

set -eu

dir=${1:-}
if [ -z "$dir" ] || [ ! -d "$dir" ]; then
    echo "usage: $0 <existing textfile-dir>" >&2
    exit 2
fi

thermal_file=${RPI_THERMAL_FILE:-/sys/class/thermal/thermal_zone0/temp}
cpufreq_file=${RPI_CPUFREQ_FILE:-/sys/devices/system/cpu/cpu0/cpufreq/scaling_cur_freq}
wireless_file=${RPI_WIRELESS_FILE:-/proc/net/wireless}
iface=${RPI_WIFI_IFACE:-wlan0}
case $iface in
    ''|*[!A-Za-z0-9_.-]*) iface="" ;; # unsafe as a label value: skip Wi-Fi
esac

have_vcgencmd=0
if command -v vcgencmd >/dev/null 2>&1; then
    have_vcgencmd=1
fi

tmp=$(mktemp "$dir/.rpi.prom.XXXXXX")
trap 'rm -f "$tmp"' EXIT
trap 'exit 1' INT TERM
# mktemp creates 0600; the collector runs as another user inside the container.
chmod 644 "$tmp"

{
    # --- throttling bits (vcgencmd get_throttled -> "throttled=0x50005") ---
    if [ "$have_vcgencmd" -eq 1 ]; then
        raw=$(vcgencmd get_throttled 2>/dev/null || true)
        hex=${raw#throttled=}
        case $hex in
            0x*[!0-9a-fA-F]*|0x) ;;
            0x[0-9a-fA-F]*)
                bits=$((hex))
                echo "# HELP rpi_throttled_bitmask Raw vcgencmd get_throttled bitmask."
                echo "# TYPE rpi_throttled_bitmask gauge"
                echo "rpi_throttled_bitmask $bits"
                echo "# HELP rpi_throttled_flag 1 if the get_throttled flag is set."
                echo "# TYPE rpi_throttled_flag gauge"
                for entry in 0:under_voltage 1:arm_freq_capped 2:throttled 3:soft_temp_limit \
                    16:under_voltage_occurred 17:arm_freq_capped_occurred \
                    18:throttled_occurred 19:soft_temp_limit_occurred; do
                    bit=${entry%%:*}
                    name=${entry#*:}
                    echo "rpi_throttled_flag{flag=\"$name\"} $(((bits >> bit) & 1))"
                done
                ;;
        esac

        raw=$(vcgencmd measure_volts core 2>/dev/null || true)
        volts=${raw#volt=}
        volts=${volts%V}
        case $volts in
            ''|*[!0-9.]*|.|*.*.*) ;;
            *)
                echo "# HELP rpi_core_volts Core voltage in volts."
                echo "# TYPE rpi_core_volts gauge"
                echo "rpi_core_volts $volts"
                ;;
        esac
    fi

    # --- CPU temperature (millidegrees in sysfs, fallback vcgencmd) ---
    temp=""
    if [ -r "$thermal_file" ]; then
        milli=$(cat "$thermal_file" 2>/dev/null || true)
        case $milli in
            ''|*[!0-9]*) ;;
            *) temp=$(awk -v m="$milli" 'BEGIN { printf "%.1f", m / 1000 }') ;;
        esac
    elif [ "$have_vcgencmd" -eq 1 ]; then
        raw=$(vcgencmd measure_temp 2>/dev/null || true)
        temp=${raw#temp=}
        temp=${temp%\'C}
        case $temp in
            ''|*[!0-9.]*|.|*.*.*) temp="" ;;
        esac
    fi
    if [ -n "$temp" ]; then
        echo "# HELP rpi_cpu_temp_celsius SoC temperature in degrees Celsius."
        echo "# TYPE rpi_cpu_temp_celsius gauge"
        echo "rpi_cpu_temp_celsius $temp"
    fi

    # --- ARM clock (kHz in sysfs, fallback vcgencmd measure_clock arm) ---
    hz=""
    if [ -r "$cpufreq_file" ]; then
        khz=$(cat "$cpufreq_file" 2>/dev/null || true)
        case $khz in
            ''|*[!0-9]*) ;;
            *) hz=$((khz * 1000)) ;;
        esac
    elif [ "$have_vcgencmd" -eq 1 ]; then
        raw=$(vcgencmd measure_clock arm 2>/dev/null || true)
        hz=${raw#*=}
        case $hz in
            ''|*[!0-9]*) hz="" ;;
        esac
    fi
    if [ -n "$hz" ]; then
        echo "# HELP rpi_arm_clock_hz Current ARM core clock in hertz."
        echo "# TYPE rpi_arm_clock_hz gauge"
        echo "rpi_arm_clock_hz $hz"
    fi

    # --- Wi-Fi (/proc/net/wireless: iface: status link level noise ...) ---
    if [ -n "$iface" ] && [ -r "$wireless_file" ]; then
        line=$(awk -v i="$iface:" '$1 == i { print $3, $4; exit }' "$wireless_file" 2>/dev/null || true)
        if [ -n "$line" ]; then
            # Trailing dots mark the driver's "updated" flags: "60." -> 60.
            link=${line% *}
            link=${link%.}
            level=${line#* }
            level=${level%.}
            case $link in ''|*[!0-9-]*|-|*?-*) link="" ;; esac
            case $level in ''|*[!0-9-]*|-|*?-*) level="" ;; esac
            if [ -n "$level" ]; then
                echo "# HELP rpi_wifi_signal_dbm Wi-Fi signal level in dBm."
                echo "# TYPE rpi_wifi_signal_dbm gauge"
                echo "rpi_wifi_signal_dbm{interface=\"$iface\"} $level"
            fi
            if [ -n "$link" ]; then
                echo "# HELP rpi_wifi_link_quality Wi-Fi link quality as reported by the driver (usually 0-70)."
                echo "# TYPE rpi_wifi_link_quality gauge"
                echo "rpi_wifi_link_quality{interface=\"$iface\"} $link"
            fi
        fi
    fi

    echo "# HELP rpi_metrics_last_run_timestamp_seconds Unix time of the last rpi-metrics.sh run."
    echo "# TYPE rpi_metrics_last_run_timestamp_seconds gauge"
    echo "rpi_metrics_last_run_timestamp_seconds $(date +%s)"
} >"$tmp"

mv -f "$tmp" "$dir/rpi.prom"
trap - EXIT INT TERM
