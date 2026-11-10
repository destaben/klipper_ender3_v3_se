# Raspberry Pi health metrics

`scripts/rpi-metrics.sh` writes Pi health metrics to `textfile/rpi.prom`,
which node-exporter's textfile collector serves at `/nodeexporter/metrics`.

| Metric | Source |
|---|---|
| `rpi_throttled_bitmask` | `vcgencmd get_throttled` (raw bitmask) |
| `rpi_throttled_flag{flag=...}` | same call; one gauge per bit: `under_voltage`, `arm_freq_capped`, `throttled`, `soft_temp_limit` and their `_occurred` (sticky since boot) variants |
| `rpi_core_volts` | `vcgencmd measure_volts core` |
| `rpi_cpu_temp_celsius` | `/sys/class/thermal/thermal_zone0/temp` (fallback `vcgencmd measure_temp`) |
| `rpi_arm_clock_hz` | cpufreq `scaling_cur_freq` (fallback `vcgencmd measure_clock arm`) |
| `rpi_wifi_signal_dbm`, `rpi_wifi_link_quality` | `/proc/net/wireless`, interface `wlan0` (override with `RPI_WIFI_IFACE`) |
| `rpi_metrics_last_run_timestamp_seconds` | `date`; alert on staleness |

The Pi shares one USB/SDIO bus, so a run makes only two `vcgencmd` calls
(`get_throttled`, `measure_volts`); everything else is read from sysfs and
`/proc`. A metric whose source is missing or unparsable is omitted, never
reported as 0. The file is replaced atomically (temp file in the same
directory, then `mv`).

## Install / deploy

`setup_services.sh` creates `textfile/` and installs a user crontab line:

```
* * * * * <repo>/scripts/rpi-metrics.sh <repo>/textfile
```

To deploy on an existing printer, only while it is idle:

```bash
git pull
mkdir -p textfile
(crontab -l 2>/dev/null | grep -vF scripts/rpi-metrics.sh; echo "* * * * * $PWD/scripts/rpi-metrics.sh $PWD/textfile") | crontab -
docker compose up -d node-exporter
```

Verify (HTTP only): `curl -s http://<host_ip>/nodeexporter/metrics | grep rpi_`

## Rollback

```bash
crontab -l | grep -v 'scripts/rpi-metrics.sh' | crontab -
git revert <merge-commit>   # or git checkout the previous docker-compose.yaml
docker compose up -d node-exporter   # recreates it without the flag and mount
rm -rf textfile
```

## Tests

```bash
python3 -m unittest discover -s scripts -v
```

Uses a PATH-stubbed `vcgencmd`. If `promtool` is installed (or `RPI_PROMTOOL`
names a command such as `docker run --rm -i --entrypoint promtool prom/prometheus:v2.55.1`),
the output is also checked with `promtool check metrics`. CI runs it in
`.github/workflows/rpi-metrics-test.yml`.
