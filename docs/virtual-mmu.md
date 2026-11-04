# Virtual Pico MMU fallback (optional)

Klipper will not reach `ready` while a configured MCU is missing, and it cannot
add or drop an MCU while it runs. With the Pico MMU unplugged, Klippy therefore
stops in an error state (Moonraker and Mainsail stay up).

This optional sidecar, `mmu-fallback`, lets Klipper start without the Pico MMU
by pointing `[mcu PICO_MMU]` at a **virtual MCU** (the Klipper `linux` firmware
with the virtual GPIO patch from `sim/patches`) when the real device is absent,
and restarts Klipper for you when the Pico is plugged in or removed. The
mainboard is never simulated.

It is **off by default**. Without a `.env` file nothing changes.

## How it works

- `[mcu PICO_MMU]` uses the stable path `/run/klipper/mmu` (`config/macros/sp_mmu.cfg`).
- By default that path is the tracked symlink `serial-links/mmu`, which points at
  the real Pico (`/dev/serial/by-id/usb-Klipper_stm32g0b1xx_...`). This is the
  same device as before, so the stack behaves as it always did.
- With the sidecar enabled, `mmu_link.py` writes the links in `MMU_LINK_DIR`
  instead:
  - Real Pico present: `mmu` -> the by-id device.
  - Absent: `mmu` -> `virtual-mmu`, the pty of a `klipper.elf` it runs.
- `[temperature_sensor Pico_MMU]` is kept in `mmu_temp.cfg` in the same
  directory and included from `printer.cfg`, because the virtual MCU has no
  temperature sensor. The sidecar writes the sensor for the real MCU and an
  empty file for the virtual one.
- When the choice changes, the sidecar sends one `printer/firmware_restart` to
  Moonraker. Klipper cannot avoid this restart; the sidecar only does it for you.

### When it acts

The sidecar changes the link and restarts Klipper only when **all** of these hold:

- The change has been stable for the debounce time (3 s to real, 10 s to virtual).
  The longer one for "absent" stops the Pico re-enumerating after a
  `FIRMWARE_RESTART` from looking like an unplug.
- Klippy is `ready` with `print_stats.state` not `printing` or `paused` and
  hotend and bed targets at 0 (a restart switches heaters off), or Klippy is in
  `error`/`shutdown`.
- No print was running when the MCU disappeared. If the real Pico drops during a
  print, Klipper stays in its error state; it is **not** replaced by the virtual
  MCU. This is remembered until Klippy is `ready` again with no active print.
- Moonraker answers. If it does not, the sidecar waits.
- At least 60 s passed since its last restart (20 s quiet period right after one).

A failed restart request is retried every 5 s. `PRINTER_MODE=real` never starts a
virtual MCU and never restarts.

## Enable

```bash
sudo install -d -o 1000 -g 1000 /run/klipper-mmu   # host tmpfs: cleared on reboot
cp .env.example .env
docker compose up -d
```

`/run/klipper-mmu` is on tmpfs, so create it again after a reboot (or add it to
`setup_services.sh` / a tmpfiles.d entry). `MMU_LINK_DIR` must not stay at the
tracked `./serial-links` in `auto`/`sim` mode, because the sidecar would
overwrite those files.

| `PRINTER_MODE` | Behaviour |
| --- | --- |
| `real` (default) | Link manager only: always the real device, never restarts. Kill switch. |
| `auto` | Virtual MCU when the Pico is absent, automatic restart on change. |
| `sim` | Always virtual. Never restarts after the initial link. |

To revert, set `PRINTER_MODE=real`, or delete `.env` and run `docker compose up -d`.

## What the user sees

- `MMU_MODE` prints `MMU: REAL` or `MMU: SIMULATED`. It also runs 5 s after every
  Klipper start, and `START_PRINT` warns when the MMU is simulated.
- With a simulated MMU, `T1`-`T5` and `SP_CHANGE` to a lane other than 0 raise an
  error before anything moves (`config/macros/mmu_mode.cfg`). `T0` still works, so
  single-filament prints are fine. `CHANGE1`-`CHANGE3` fail through `T1`-`T3`.
- Sidecar log: `docker logs mmu-fallback`.

## Known limits

- Any restart cancels pending delayed G-code, including a running auto
  power-off countdown (`PRINTER_SHUTDOWN`).
- The virtual MCU does not simulate heaters or motion of the MMU. It only makes
  Klipper start.
- The Klipper commit used to build the virtual MCU is pinned in four places:
  `Dockerfile-klipper`, `sim/Dockerfile-sim`, `Dockerfile-mmu-fallback` and
  `build_firmware.sh`. Keep them in sync.
- The compose file pulls `mmu-fallback:latest` until the follow-up change pins a
  `sha-` tag, as was done for `klipper-connect`.

## To verify on the printer

These cannot be checked in CI. Defaults are configurable through `.env`
(`MMU_ABSENT_DEBOUNCE`, `MMU_PRESENT_DEBOUNCE`, `MMU_RESTART_COOLDOWN`,
`MMU_QUIET_AFTER_RESTART`).

1. **Pty access across containers.** Klippy (uid 1000) must open the pty created
   by the sidecar. It relies on the sidecar mounting host `/dev` and running as
   uid 1000. If `docker logs klipper` shows a permission error on
   `/run/klipper/mmu`, check the pty owner.
2. **Re-enumeration time.** Measure how long the by-id device is gone after a
   `FIRMWARE_RESTART` (`ls /dev/serial/by-id` in a loop). `MMU_ABSENT_DEBOUNCE`
   (10 s) and `MMU_QUIET_AFTER_RESTART` (20 s) must be longer.
3. **Virtual MCU after `FIRMWARE_RESTART`.** `restart_method: command` on the
   virtual MCU should return to `ready`; the sidecar respawns the process if it
   exits.
4. **Acceptance:** with the Pico unplugged Klipper reaches `ready` showing
   `SIMULATED`; plugging it in while idle causes exactly one restart and then
   `REAL`; unplugging it while idle does the reverse; unplugging it mid-print
   causes no restart and no fallback; without `.env` nothing changes.
