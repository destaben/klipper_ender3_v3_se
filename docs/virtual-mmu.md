# Virtual Pico MMU fallback (optional)

Klipper will not reach `ready` while a configured MCU is missing, and it cannot
add or drop an MCU while it runs. With the Pico MMU unplugged, Klippy therefore
stops in an error state (Moonraker and Mainsail stay up).

These optional services let Klipper start without the Pico MMU by pointing
`[mcu PICO_MMU]` at a **virtual MCU** (the Klipper `linux` firmware with the
virtual GPIO patch from `sim/patches`) when the real device is absent, and
restart Klipper for you when the Pico is plugged in or removed. The mainboard is
never simulated.

There are two services, built from the same image (`Dockerfile-mmu-fallback`):

- `mmu-fallback`: the watcher (`mmu_link.py`). It manages the links and asks for
  restarts.
- `mmu-virtual`: the virtual MCU (`run_virtual.sh`). It is a separate container
  so that recreating or restarting the watcher (for example `docker compose up
  -d` after editing `.env`) does not kill the MCU Klippy is connected to, which
  would shut Klippy down in the middle of a print. Docker respawns it if it
  exits.

It is **off by default**. Without a `.env` file nothing changes.

## How it works

- `[mcu PICO_MMU]` uses the stable path `/run/klipper/mmu` (`config/macros/sp_mmu.cfg`).
- By default that path is the tracked symlink `serial-links/mmu`, which points at
  the real Pico (`/dev/serial/by-id/usb-Klipper_stm32g0b1xx_...`). This is the
  same device as before, so the stack behaves as it always did.
- With the sidecar enabled, `mmu_link.py` writes the links in `MMU_LINK_DIR`
  instead:
  - Real Pico present: `mmu` -> the by-id device.
  - Absent: `mmu` -> `virtual-mmu`, the pty of the `mmu-virtual` container's
    `klipper.elf`.
- `[temperature_sensor Pico_MMU]` is kept in `mmu_temp.cfg` in the same
  directory and included from `printer.cfg` (`[include /run/klipper/mmu_temp.cfg]`),
  because the virtual MCU has no temperature sensor. The sidecar writes the
  sensor for the real MCU and an empty file for the virtual one.
  - This include is a hard dependency: Klippy does not start if the file is
    missing. Without the sidecar, the klipper container mounts the tracked
    `serial-links/` directory at `/run/klipper`, whose `mmu_temp.cfg` holds the
    real sensor, so the default stack needs nothing. If `MMU_LINK_DIR` is set but the
    profile is disabled (or the directory is empty), Klippy fails to start with
    a missing include: unset `MMU_LINK_DIR` or point it at a directory that
    holds `mmu` and `mmu_temp.cfg`. If you run Klipper some
    other way, create `/run/klipper/mmu_temp.cfg` (a copy of that file) first.
- When the choice changes, the sidecar sends one `printer/firmware_restart` to
  Moonraker. Klipper cannot avoid this restart; the sidecar only does it for you.

### When it acts

The watcher changes the link and restarts Klipper only when **all** of these hold:

- The change has been stable for the debounce time (3 s to real, 10 s to virtual).
  The longer one for "absent" stops the Pico re-enumerating after a
  `FIRMWARE_RESTART` from looking like an unplug.
- Moonraker answers and the print state is known. If not, the watcher waits.
- Either Klippy is `ready`, `print_stats.state` is not `printing` or `paused`,
  and the hotend and bed targets are 0 (a restart switches heaters off), or
  Klippy is in `error`/`shutdown`.
- The print latch is not set (below). It applies in the `error`/`shutdown` case
  too.
- At least 60 s passed since its last restart (20 s quiet period right after one).
  This applies to every path: a plug or unplug, the reconcile with what Klippy
  reports and a respawned virtual MCU, so a crash-looping `mmu-virtual` cannot
  restart Klippy more than once a minute. All paths stamp the same
  last-restart time, so a plug right after a respawn restart waits too. Only a
  request that did not go through, because it failed (error or timeout) or was
  postponed by the check below, is repeated sooner, after 20 s.
- Klippy is checked again right before the request is sent, so a print that
  started after the first check postpones the restart. A very small gap remains
  between that check and Klippy acting on the request. The link itself may
  already point at the new device by then, which is harmless: a running Klippy
  keeps the serial port it has open.

### The print latch

If the real Pico drops during a print, Klipper stays in its error state and
`print_stats` becomes unreadable. The watcher remembers that a print was active
and does **not** replace the real MCU with the virtual one. The latch clears
once Klippy is `ready` again and reports no active print (replug the Pico and
restart Klipper by hand). It is stored in `$MMU_LINK_DIR/print_latch`, so it
survives a restart of the watcher, but not a reboot or a full stop of klipper,
mmu-fallback and mmu-virtual (for example `docker compose down`): with the default
`mmu-links` volume and with a host tmpfs path alike, the directory is empty again
afterwards.

Limits:

- It is only recorded while the watcher runs. If the watcher was stopped when a
  print started and the Pico drops during it, the watcher cannot know. On its
  next start it points the link at the virtual MCU. It restarts a Klippy it
  finds in `error` only if it can read which mode Klippy is on and that differs
  from the link; if the mode is unreadable (the usual case in `error`) it does
  nothing, and the next manual restart of Klipper uses the virtual MCU.
- If the latch was stored and the watcher is recreated while the Pico is still
  unplugged, the first decision of the new process (`auto` mode) points the link
  at the real, absent device instead of the virtual MCU, so it is never virtual
  mid-print. The link dangles on purpose: the `mmu-fallback` healthcheck accepts
  it while `print_latch` exists, so `klipper` still starts and reports the
  missing MCU. Replug the Pico and restart Klipper by hand: the watcher does
  not restart automatically while latched, and the latch clears once Klippy is
  `ready` and idle.
- A print that starts and loses the Pico within a single poll (1 s) is never
  seen as `printing` while Klippy is `ready`, so no latch is set. Klippy is then
  in `shutdown` with an unreadable print state, and `firmware_restart` is
  allowed there after the debounce. This cannot be closed from Moonraker, which
  reports no print state in `error`/`shutdown`; the data of such a print is lost
  with the shutdown anyway.
- Deleting `print_latch` has no effect until the watcher restarts, because it is
  only read at startup.

### Other cases

- **Watcher recreated while Klippy kept running.** It compares the link with the
  mode Klippy reports for `[mcu PICO_MMU]` (`mcu_constants.MCU` is `linux` when
  virtual) and asks for the missing restart under the same conditions. Once
  Klippy reports the linked mode the pending restart is dropped, so a request
  whose reply timed out but which took effect is not repeated. A failed request
  or postponed request is retried after 20 s, and a mismatch that persists after a restart is retried
  at most once per cooldown. If Klippy is in `error` and its mode is unknown, the
  watcher does not restart it on its own startup.
- **Virtual MCU respawned.** Every normal restart makes the `linux` MCU exit and
  Docker respawn it, and Klippy reconnects by itself. The watcher notices the
  pty was recreated and waits `MMU_RESPAWN_GRACE` (15 s). If Klippy is still in
  `error`/`shutdown` it requests a restart, under the conditions above, so a
  crashed virtual MCU recovers without a manual `FIRMWARE_RESTART`.
- **Pico enumerating just after the watcher starts.** In `auto` mode the watcher
  waits `MMU_STARTUP_GRACE` (5 s) before the first virtual link. A Pico that
  appears later than that is handled as a normal plug-in: one extra idle
  restart, no print affected.
- **Errors.** A failed write of a link or of the latch is logged and retried on
  the next poll. The watcher does not exit on errors, because restarting its
  container would not help.

## Enable

```bash
cp .env.example .env
docker compose up -d
```

`.env.example` sets `MMU_LINK_DIR=mmu-links`, a Docker tmpfs volume declared in
`docker-compose.yaml`. Docker mounts it when the first container that uses it
starts, so it exists after every reboot with no host directory and no `sudo`.
Its content is lost when all three containers (klipper, mmu-fallback,
mmu-virtual) are stopped, for example by `docker compose down`; the sidecar
writes it again on start. A host path such as `/run/klipper-mmu` also works, but
it is tmpfs on the host and has to be created again after each reboot
(`sudo install -d -o 1000 -g 1000 /run/klipper-mmu`, or a tmpfiles.d entry).
`MMU_LINK_DIR` must not stay at the tracked `./serial-links`: that directory
holds a `.tracked-default` marker and the sidecar exits with an error instead of
overwriting the tracked files.

| `PRINTER_MODE` | Behaviour |
| --- | --- |
| `real` (default) | Always the real device. No virtual MCU runs (`mmu-virtual` idles). No change-triggered restarts; the one reconcile restart applies only if Klippy is still on the virtual MCU. Kill switch. |
| `auto` | Virtual MCU when the Pico is absent, automatic restart on change. |
| `sim` | Always virtual. One reconcile restart if Klippy is still on the real device, none after that. |

To revert, set `PRINTER_MODE=real`, or delete `.env` and stop the services.
Deleting `.env` does not stop containers that are already running:

```bash
docker compose stop mmu-fallback mmu-virtual   # or: docker compose down
```

With the profile off, `docker compose up -d` does not start them again. The
tracked `serial-links/mmu` then points at the real Pico, as before.

## What the user sees

- `MMU_MODE` prints `MMU: REAL` or `MMU: SIMULATED`. It also runs 5 s after every
  Klipper start, and `START_PRINT` warns when the MMU is simulated.
- With a simulated MMU, `T1`-`T5`, `SP_CHANGE`, `_SP_CHANGE`, `SP_LOAD_LANE`,
  `SP_UNLOAD_LANE`, `SP_FORCE_MOVE_LANE`, `SP_LOAD_EXTRUDER`,
  `SP_UNLOAD_EXTRUDER`, `SP_LOAD_HOTEND`, `SP_UNLOAD_HOTEND`, `SP_LOAD_HUB`,
  `SP_UNLOAD_HUB`, `_SP_TOOL_CHANGE`, `_SP_LOAD_LANE`, `_SP_UNLOAD_LANE` and
  `SP_TEST_LOAD_UNLOAD_EXTRUDER` raise an error before anything moves when the lane is not 0 (for the macros
  without a `LANE` parameter, the last loaded lane) (`_MMU_REQUIRE_REAL`, `config/macros/mmu_mode.cfg`).
  `T0` still works, so single-filament prints are fine. `CHANGE1`-`CHANGE3` fail
  through `T1`-`T3`.
- Fourteen of those guards are one-line additions inside the otherwise vendored
  `config/macros/sp_mmu/sp_mmu_code.cfg` (marked `## local`), because a macro
  cannot be wrapped from another file. Re-add them when updating that file.
- The infinite-spool runout path calls `_SP_TOOL_CHANGE` directly. It starts from
  the runout sensor, a pin on the Pico, so a virtual MMU cannot produce a real
  runout. As defence in depth, `runout_gcode` in `sp_mmu.cfg` runs the same guard.
- Logs: `docker logs mmu-fallback` (watcher), `docker logs mmu-virtual`.

### Startup gating of `klipper`

With the profile on, `klipper` waits for `mmu-fallback` to be healthy, so the
link exists before Klippy connects. The check is `test -L /run/klipper/mmu`, and
outside `real` mode also `test -e`, so the virtual pty must exist. In `real` mode
with the Pico unplugged the link dangles on purpose (same as without this
feature), which does not block `docker compose up`; Klippy then fails to open the
Pico as it always did. Use `PRINTER_MODE=auto`, or plug the Pico in, to get
`ready` without it.

The gating has a cost. If the profile is on and `mmu-fallback` never becomes
healthy (for example `mmu-virtual` cannot create its pty), `klipper` is never
started and Moonraker shows no Klippy. Before this feature, Klippy would have
started and reported an error instead. To diagnose:

```bash
docker compose ps                  # mmu-fallback shows "unhealthy" or "starting"
docker logs mmu-fallback           # watcher: link decisions, guard errors
docker logs mmu-virtual            # virtual MCU: pty creation, permission errors
```

To get out of it, set `PRINTER_MODE=real` in `.env` (the pty is no longer
required) or remove `COMPOSE_PROFILES=mmu-fallback`, then run
`docker compose up -d` again.

## Known limits

- Any restart cancels pending delayed G-code, including a running auto
  power-off countdown (`PRINTER_SHUTDOWN`). This matters right after
  `END_PRINT`: the printer is idle with cold heaters, so the watcher's idle
  condition holds during the countdown. If the Pico is unplugged, or stays
  absent for more than 10 s, inside that window, the watcher restarts Klipper
  and the power-off never happens: the printer stays on. Nothing is lost
  (heaters are already off), but power it off by hand. The watcher does not
  skip restarts during the countdown because that state is not exposed in a
  way it can read reliably, so this is documented rather than handled.
- `config/` is no longer standalone. `[mcu PICO_MMU]` uses
  `serial: /run/klipper/mmu` and `printer.cfg` includes
  `/run/klipper/mmu_temp.cfg`, so the directory mounted at `/run/klipper`
  (tracked `serial-links/` by default) must exist, with `mmu` and `mmu_temp.cfg`
  in it, wherever this config is used. The in-repo `klipper` service does it. A
  deployment that only copies `config/` fails at Klippy start with a missing
  include until it provides them or edits those two lines back.
- The virtual MCU does not simulate heaters or motion of the MMU. It only makes
  Klipper start.
- The Klipper commit used to build the virtual MCU is pinned in four places:
  `Dockerfile-klipper`, `sim/Dockerfile-sim`, `Dockerfile-mmu-fallback` and
  `build_firmware.sh`. Keep them in sync.
- Both services, `mmu-fallback` and `mmu-virtual`, are pinned to the same
  `sha-` tag as `klipper` and `moonraker`, so the two sidecars always run the
  same image and `docker compose pull` does not change them on its own. This
  does not cover `./config`, which is bind-mounted: after a merge that changes
  the link contract, bump all four tags and update the config together.
- `/dev` is mounted read-only in `mmu-fallback` (it only checks that the Pico's
  by-id entry exists) and read-write in `mmu-virtual` (it creates the pty there,
  and the pty must come from the host devpts for the `klipper` container to open
  it). Narrowing the second one is item 5 below.
- The check before the restart request cannot close the last gap completely (see
  "When it acts").

## To verify on the printer

These cannot be checked in CI. Defaults are configurable through `.env`
(`MMU_ABSENT_DEBOUNCE`, `MMU_PRESENT_DEBOUNCE`, `MMU_RESTART_COOLDOWN`,
`MMU_QUIET_AFTER_RESTART`).

1. **Pty access across containers.** Klippy (uid 1000) must open the pty created
   by `mmu-virtual`. It relies on that container mounting host `/dev` and running
   as uid 1000. If `docker logs klipper` shows a permission error on
   `/run/klipper/mmu`, check the pty owner.
2. **Re-enumeration time.** Measure how long the by-id device is gone after a
   `FIRMWARE_RESTART` (`ls /dev/serial/by-id` in a loop). `MMU_ABSENT_DEBOUNCE`
   (10 s) and `MMU_QUIET_AFTER_RESTART` (20 s) must be longer.
3. **Virtual MCU after `FIRMWARE_RESTART`.** `restart_method: command` on the
   virtual MCU should return to `ready`. The `linux` MCU may exit on reset;
   Docker then respawns `mmu-virtual` and Klippy should reconnect within
   `MMU_RESPAWN_GRACE`, with no second restart from the watcher. Check
   `docker logs mmu-fallback` for an unexpected extra `firmware_restart`.
4. **Acceptance:** with the Pico unplugged Klipper reaches `ready` showing
   `SIMULATED`; plugging it in while idle causes exactly one restart and then
   `REAL`; unplugging it while idle does the reverse; unplugging it mid-print
   causes no restart and no fallback; without `.env` nothing changes.
5. **Narrower `/dev` mount.** `mmu-virtual` mounts the whole host `/dev` so the
   pty comes from the host devpts. Try `/dev/pts` and `/dev/ptmx` only, with
   `/dev/serial` for the by-id lookup, and check that Klippy can still open the
   pty. Until then the whole tree stays; the container is unprivileged, drops all
   capabilities and cannot create device nodes.
