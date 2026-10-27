---
name: printer-ops
description: Use when asked to check, restart, diagnose, or send G-code to the live Ender 3 V3 SE running from this repo's Docker stack (Klipper + Moonraker behind Traefik). Covers reading state, FIRMWARE_RESTART, sending G-code, reading logs, and the safety rules for a real machine. Trigger on "restart klipper", "send G28", "why did it shut down", "check the printer", "home it", "FIRMWARE_RESTART".
---

# Operating the live printer

The Pi that runs this repo's stack is wired to the real printer. Anything you send moves metal or heats a nozzle. Everything below goes through Moonraker's HTTP API (Traefik forwards `/printer`, `/server`, `/api`, `/machine`, `/access`, `/websocket` on port 80). Moonraker is **not** published on 7125 on the host.

## Safety rules (read first)

1. Only run G-code the user asked for. Do not heat, home, move, extrude or drive the MMU on your own initiative. Reading state, `QUERY_PROBE`, `M115`, and `FIRMWARE_RESTART` of an idle printer are fine once the user has allowed restarts.
2. Before anything that restarts or moves: read `print_stats.state`. If it is `printing` or `paused`, stop and ask. Also check hotend and bed temperatures.
3. The user may be at the printer. Prefer small, reversible steps. After a failure, do not retry in a loop: diagnose first (see below).
4. Never edit the `SAVE_CONFIG` block of `printer.cfg`. Config changes go through a branch/PR; the live checkout is a deployment target.
5. Do not run heavy work (Docker builds, firmware compiles) on this Pi while Klipper is connected. The 1 GB Pi starves the USB serial link and Klipper shuts down with `Lost communication with MCU` or `Timer too close`. Stop Klipper first (`docker stop klipper`).

## Read state

```bash
# Klippy state and message (ready | startup | error | shutdown)
curl -s http://localhost/printer/info | python3 -I -c "import sys,json; r=json.load(sys.stdin)['result']; print(r['state'], '|', r['state_message'][:600])"

# Print state, temperatures, homed axes, position
curl -s 'http://localhost/printer/objects/query?print_stats&webhooks&toolhead&extruder&heater_bed' |
  python3 -I -c "import sys,json; d=json.load(sys.stdin)['result']['status']; print(d['print_stats']['state'], d['webhooks']['state'], round(d['extruder']['temperature']), round(d['heater_bed']['temperature']), repr(d['toolhead']['homed_axes']), d['toolhead']['position'][:3])"

# Config warnings (deprecations etc.)
curl -s 'http://localhost/printer/objects/query?configfile' | python3 -I -c "import sys,json; print(json.load(sys.stdin)['result']['status']['configfile'].get('warnings'))"

# MMU memory (loaded lane) lives INSIDE the klipper container, not on a volume
docker exec klipper cat /home/klipper/printer_data/sp_mmu_data.cfg
```

`printer.cfg` values that Klipper owns are visible under `configfile.settings` (for example `settings.bltouch.z_offset`, `settings.maintain.interval`).

## Send G-code

```bash
# One command. It returns when Klipper finishes it, so long moves (G28) need a long --max-time.
curl -s -X POST --max-time 170 'http://localhost/printer/gcode/script' --data-urlencode 'script=G28'

# Responses/errors printed by Klipper are in the G-code store (filter the noisy maintenance lines):
curl -s 'http://localhost/server/gcode_store?count=20' | python3 -I -c "
import sys,json
for m in json.load(sys.stdin)['result']['gcode_store']:
    if 'Maintenance' not in m['message']: print(m['type'][:7], '|', m['message'][:300])"
```

The HTTP reply to `gcode/script` is `{"result": "ok"}` or `{"error": {"message": ...}}` (for example `Probe triggered prior to movement`). Use `--data-urlencode` for scripts with spaces or `=`.

Safe read-only/diagnostic commands: `QUERY_PROBE`, `M115`, `MAINTAIN_STATUS`, `SP_*` status macros, `BLTOUCH_DEBUG COMMAND=pin_up|pin_down|reset|touch_mode` (moves only the probe pin).

## Restart

```bash
curl -s -X POST --max-time 20 http://localhost/printer/firmware_restart   # = FIRMWARE_RESTART; also reloads printer.cfg
```

Wait for the state to *change*. Polling right away reads the old `shutdown` state. Poll until `ready` (or `error`):

```bash
for i in $(seq 1 45); do s=$(curl -s --max-time 3 http://localhost/printer/info | python3 -I -c "import sys,json; print(json.load(sys.stdin)['result']['state'])" 2>/dev/null); [ "$s" = ready ] && break; sleep 3; done; echo "$s"
```

Gotchas learned on this printer:

- After an MCU `shutdown`, the MCU stays shut down until reset. A freshly started Klippy then reports `error: Can not update MCU 'mcu' config as it is shutdown`. That is expected; send `firmware_restart`.
- If `gcode/script` and `firmware_restart` hang (Moonraker logs `Request 'gcode/script' pending: N seconds`), Klippy is stuck on an earlier request. `docker restart klipper` (the container filesystem, including the MMU memory file, is preserved), wait for `error`, then `firmware_restart`.
- Recreating the container (`docker compose up --force-recreate`) loses `sp_mmu_data.cfg`. Copy it out with `docker cp` first and back in before starting.
- The live stack uses prebuilt images tagged in `docker-compose.yaml`; a local override (`docker-compose.override.yaml`) can point `klipper` at a locally built image.

## Logs and diagnosis

```bash
docker logs --since 10m --timestamps klipper 2>&1 | grep -v 'Stats ' | tail -60   # Klippy log (stdout)
docker logs --since 10m moonraker 2>&1 | tail -30
vcgencmd get_throttled; vcgencmd measure_temp; uptime; free -m    # host health
```

- `Timer too close` / `Lost communication with MCU`: look for `Reactor ... busy for` lines just before it (something blocked Klippy's main loop) and for host load/swap/thermal throttling (`get_throttled` bit `0x8` = soft temperature limit, `0x1`/`0x10000` = under-voltage).
- The KlipperMaintenance timer does blocking HTTP calls to Moonraker inside the reactor; it is disabled (`[maintain] interval: 0`) because it stalled G28.
- Kernel time to wall clock: `dmesg` times are seconds since boot; convert with `/proc/uptime`.
- `Probe triggered prior to movement` during G28: the BLTouch signal was already "triggered" when the Z descent started. Check `BLTOUCH_DEBUG COMMAND=pin_down` then `QUERY_PROBE` (expect `open`), then `touch_mode` then `QUERY_PROBE`. See `docs/klipper-mmu-upgrade.md`.

## Rollback

During the 2026-10 upgrade a `rollback.sh` was kept in a backup directory next to the repo (outside it, not versioned). It restores the previous image and the `main` config, and refuses to run during a print. Reuse the same pattern for future upgrades: tag the running image, back up the config and `sp_mmu_data.cfg`, and keep a script that undoes the switch.

## Host tool quirks (Claude Code)

- Broad Docker filters such as `docker ps --filter ancestor=ubuntu:22.04` also match the Klipper and Moonraker containers (their images derive from it). Address containers by exact ID or name.
- Edits to the shared checkout require a worktree; commit there, push, then fast-forward the live checkout (`git pull --ff-only`).
