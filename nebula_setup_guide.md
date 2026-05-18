# Pico MMU on Ender 3 V3 SE — Nebula Smart Kit Setup Guide

> **Note:** This guide covers the specific modifications required when running the Pico MMU on the **Creality Nebula Smart Kit** (Creality's custom Klipper fork, MIPS / Ingenic SoC). If you are using a standard mini PC or Raspberry Pi, follow the [standard installation guide](english_install_guide.md) instead.

Personal build notes documenting every modification needed to get the LH-Stinger Pico MMU (4-lane) running on a Creality Ender 3 V3 SE equipped with the Nebula Smart Kit.

This guide assumes the mechanical Pico MMU is already assembled and you have:

- A BTT EBB42 V1.2 (STM32G0B1) as the MMU control board
- The stock Creality filament runout sensor mounted on the toolhead (re-used instead of the original hub microswitch)
- A rooted Nebula Pad (see below) — **mandatory**
- SSH access to the Nebula Pad (provided by the root firmware)
- WSL2 on Windows with the ARM toolchain (`gcc-arm-none-eabi`)

## Contents

1. [WARNING: Hard requirement — Rooted Nebula firmware](#warning-hard-requirement--rooted-nebula-firmware)
2. [Why these modifications are needed](#why-these-modifications-are-needed)
3. [Step 1 — Compile EBB42 firmware against Klipper v0.11.0](#step-1--compile-ebb42-firmware-against-klipper-v0110)
4. [Step 2 — Flash the EBB42 via DFU](#step-2--flash-the-ebb42-via-dfu)
5. [Step 3 — Patch configfile.py on the Nebula](#step-3--patch-configfilepy-on-the-nebula)
6. [Step 4 — Increase TRSYNC_TIMEOUT in mcu.py](#step-4--increase-trsync_timeout-in-mcupy)
7. [Step 5 — Add the missing servo.py module to Klipper extras](#step-5--add-the-missing-servopy-module-to-klipper-extras)
8. [Step 6 — Re-use the stock Creality runout sensor in sp_mmu.cfg](#step-6--re-use-the-stock-creality-runout-sensor-in-sp_mmucfg)
9. [Step 7 — Disable the original runout pause in printer.cfg](#step-7--disable-the-original-runout-pause-in-printercfg)
10. [Step 8 — Create sp_mmu_data.cfg for \[save\_variables\]](#step-8--create-sp_mmu_datacfg-for-save_variables)
11. [After everything is in place](#after-everything-is-in-place)
12. [Known limitation — Print progress % on the Nebula screen](#known-limitation--print-progress--on-the-nebula-screen)
13. [Quick reference — files modified](#quick-reference--files-modified)

---

## WARNING: Hard requirement — Rooted Nebula firmware

This guide has only been tested on a Nebula Pad flashed with the rooted firmware built from Creality's stock **1.1.0.30** base. The rooted build re-labels itself as **6.1.0.30** and adds persistent root SSH access plus a writable system partition.

The rooting was done with:

- **Tool:** [srvrguy/nebula-pad-firmware-customizer](https://github.com/srvrguy/nebula-pad-firmware-customizer) — "Tool to customize the firmware for the Nebula Pad to allow root SSH access" (BSD-2-Clause).
- **Original Creality firmware fed into the customizer:** stock 1.1.0.30 for the Nebula Pad, downloaded from [Creality Cloud](https://www.crealitycloud.com/downloads/other/type-24?source=1).

Follow the customizer's own README for the exact build/flash procedure — it takes the Creality `.img` as input and produces a modified image you flash on the Nebula via its standard firmware-update routine.

The whole pipeline below depends on root capabilities — without them you cannot:

- Patch `/usr/share/klipper/klippy/configfile.py` (Step 3) — system partition is read-only on stock firmware
- Edit `/usr/share/klipper/klippy/mcu.py` (Step 4) — same reason
- Drop `servo.py` into `/usr/share/klipper/klippy/extras/` (Step 5) — same reason
- Create `/usr/data/printer_data/config/sp_mmu_data.cfg` (Step 8) and edit `sp_mmu.cfg` / `printer.cfg` with the new pin mappings
- Reach the EBB42 serial path under `/dev/serial/by-id/` to wire it into `sp_mmu.cfg`

> Other firmware versions (stock 1.1.0.30 without root, newer Creality releases, alternative roots) have not been tested. If you are on a different base or rooting method, expect at least the file paths and possibly the Klipper version to differ — the protocol-mismatch fix in Step 1 assumes a host Klipper of the v0.11 era, which is true for 1.1.0.30 but may not hold on newer Creality builds.

---

## Why these modifications are needed

The Nebula Pad runs a private Creality Klipper fork pinned to the **v0.11 era**. The stock Pico MMU firmware (BTT EBB42 default) is built against Klipper v0.12, and the two cannot talk to each other — at startup you get:

```
OverflowError: can't convert negative number to unsigned
```

inside `mcu.py` at `trdispatch_mcu_alloc`. The whole pipeline below is the working set of patches that gets host and MCU on the same protocol, gets the runout sensor talking on the right pin and polarity, and keeps the Nebula UI from choking on the extra MMU macros.

---

## Step 1 — Compile EBB42 firmware against Klipper v0.11.0

The single most important step. The default Pico MMU `.bin` will **not** work with the Nebula's Creality Klipper.

In WSL2:

```sh
git clone https://github.com/Klipper3d/klipper.git
cd klipper
git checkout v0.11.0
sudo apt install gcc-arm-none-eabi
make menuconfig
```

In `menuconfig`, set:

- **Micro-controller architecture:** STMicroelectronics STM32
- **Processor model:** STM32G0B1
- **Bootloader offset:** No bootloader
- **Communication interface:** USB (on PA11/PA12)

Then build:

```sh
make
```

The output is `out/klipper.bin`. Copy it to Windows for flashing:

```sh
cp ~/klipper/out/klipper.bin /mnt/c/Users/<your-username>/Desktop/
```

---

## Step 2 — Flash the EBB42 via DFU

The EBB42 V1.2 on this build has no Katapult bootloader, so flashing requires entering DFU mode. You have two equivalent paths — pick one.

### Path A — STM32CubeProgrammer on Windows (simplest)

1. Copy the firmware from WSL2 to your Windows desktop:
   ```sh
   cp ~/klipper/out/klipper.bin /mnt/c/Users/<your-username>/Desktop/klipper_nebula.bin
   ```
2. Disconnect the EBB42 from the Nebula and connect it to the PC via USB-C.
3. Put the board in DFU mode: hold **BOOT**, press and release **RESET**, then release **BOOT**.
4. Open **STM32CubeProgrammer**. In the top-right dropdown choose **USB**, click **Connect** — you should see `USB1` listed.
5. Open file → select `klipper_nebula.bin`.
6. Click **Download**. Wait for "File download complete".
7. Click **Disconnect**, press **RESET** on the EBB42 to exit DFU mode, then plug it back into the Nebula.

### Path B — dfu-util from WSL2 (via usbipd-win)

WSL2 cannot see USB devices natively — you need [usbipd-win](https://github.com/dorssel/usbipd-win) to bridge USB from Windows into WSL2.

**One-time setup on Windows** (run PowerShell as Administrator):

```powershell
winget install usbipd
```

**One-time setup inside WSL2:**

```sh
sudo apt update
sudo apt install linux-tools-generic hwdata dfu-util -y
sudo update-alternatives --install /usr/local/bin/usbip usbip \
    /usr/lib/linux-tools/*-generic/usbip 20
```

**Each time you flash:**

1. Put the EBB42 in DFU mode (BOOT held → tap RESET → release BOOT) and connect it via USB-C to the PC.
2. In PowerShell as Administrator, find the device:
   ```powershell
   usbipd list
   ```
   Look for the row with `STM32 BOOTLOADER` or VID:PID `0483:df11`. Note its `BUSID` (e.g. `2-3`).
3. Bind and attach it to WSL2:
   ```powershell
   usbipd bind --busid 2-3
   usbipd attach --wsl --busid 2-3
   ```
   (`bind` only needs to be done once per physical USB port; `attach` is needed every time.)
4. In WSL2, verify the device is visible:
   ```sh
   lsusb | grep -i "DFU\|0483"
   ```
   You should see something like `STMicroelectronics STM Device in DFU Mode`.
5. Flash:
   ```sh
   dfu-util -a 0 -s 0x08000000:leave -D ~/klipper/out/klipper.bin
   ```
   If you get an address-related error on the STM32G0B1, retry with:
   ```sh
   dfu-util -a 0 -s 0x08000000:mass-erase:force:leave -D ~/klipper/out/klipper.bin
   ```
6. The board will reset automatically after the flash. Disconnect it from the PC and reconnect it to the Nebula.

### After flashing (both paths)

The EBB42 will re-enumerate as `usb-Klipper_stm32g0b1xx_<serial>`. Update the `serial:` line in `sp_mmu.cfg` to match the new `/dev/serial/by-id/...` path. To find it from the Nebula:

```sh
ls /dev/serial/by-id/
```

In the Klipper log, the `PICO_MMU` MCU should now report `v0.11.0-xxx` instead of `v0.12.0-226` — that's the confirmation the protocol mismatch is resolved.

---

## Step 3 — Patch configfile.py on the Nebula

The Pico MMU adds a lot of `[gcode_macro ...]` sections. The Nebula Pad's master-server UI breaks when the configfile object served by Moonraker exceeds its internal buffer — you get a "config too large" popup and the Nebula UI becomes effectively unusable.

The patch modifies `_build_status` in `/usr/share/klipper/klippy/configfile.py` to:

- Skip all `[gcode_macro ...]` sections entirely
- Skip the `gcode` option in any other section (e.g. `delayed_gcode`)

This brings the configfile size back from 200+ KB to roughly 124 KB.

Use the `patch_configfile.sh` script on the Nebula:

```sh
sh patch_configfile.sh status           # show current state
sh patch_configfile.sh apply --dry-run  # preview
sh patch_configfile.sh apply            # apply
/etc/init.d/S55klipper_service restart
```

The script is idempotent, creates timestamped backups, and never overwrites existing ones. Re-run `apply` after any Creality firmware update.

---

## Step 4 — Increase TRSYNC_TIMEOUT in mcu.py

In our setup, the MMU stepper (`sp_motor`) lives on the EBB42 but the homing endstop (the runout sensor — see next step) is on the mainboard. This is cross-MCU homing, and Klipper enforces a strict ~25 ms timeout on the trsync handshake between MCUs. On the Nebula (MIPS, with the Creality Klipper fork) latency occasionally exceeds that window, producing:

```
!! {"code":"key21", "msg":"Communication timeout during homing manual_stepper sp_motor"}
```

which aborts the print mid filament-change.

Edit `/usr/share/klipper/klippy/mcu.py`:

```python
TRSYNC_TIMEOUT = 0.050  # was 0.025
```

Then restart Klipper. Doubling the window is enough on this hardware.

> If you ever need to find the file: `find / -name "mcu.py" 2>/dev/null | grep klipper`

---

## Step 5 — Add the missing servo.py module to Klipper extras

The Creality Klipper fork ships **without** `klippy/extras/servo.py` — Creality strips it because none of their printers use a generic Klipper servo. The Pico MMU defines `[servo sp_servo]` for lane selection, so on first start you will get:

```
Klipper Error: Section 'servo sp_servo' is not a valid config section
```

The fix is to drop in `servo.py` from mainline Klipper. To keep things consistent with Step 1 (EBB42 firmware built against Klipper v0.11.0), pull `servo.py` from the same source tree.

On the Nebula:

```sh
cd /usr/share/klipper/klippy/extras/
wget https://raw.githubusercontent.com/Klipper3d/klipper/d7d9092a920b3bd2bede4b570c66ddaa52df3f19/klippy/extras/servo.py
ls -la servo.py
```

If your Nebula's `wget` is the BusyBox one and refuses HTTPS, fetch the file on the PC and `scp` it over:

```sh
# On the PC:
scp servo.py root@<NEBULA_IP>:/usr/share/klipper/klippy/extras/
```

Reference: [servo.py at that exact commit](https://github.com/Klipper3d/klipper/blob/d7d9092a920b3bd2bede4b570c66ddaa52df3f19/klippy/extras/servo.py)

Then restart Klipper:

```sh
/etc/init.d/S55klipper_service restart
```

To confirm Klipper now picks up the module, `[servo sp_servo]` should validate cleanly at startup and `SET_SERVO SERVO=sp_servo ANGLE=...` (or the LH-Stinger `SP_TEST_SERVO` macro) should move the servo.

> Path verification — if your Klipper install lives elsewhere: `sudo find / -type d -name "klippy" 2>/dev/null`

---

## Step 6 — Re-use the stock Creality runout sensor in sp_mmu.cfg

Instead of building the hub microswitch + 4 mm ball bearing from the LH-Stinger design, we re-use the runout sensor already mounted on the toolhead.

This requires two things:

1. **Re-pinning** the sensor inputs from the EBB42 pin (`PICO_MMU: PB6`) to the mainboard pin (`PC15`).
2. **Inverting polarity** — the Creality sensor is Normally Closed (NC): when filament is present the pin reads LOW. The MMU expects the opposite, so we add `!` to invert the logic. The `^` prefix keeps the software pull-up (harmless to leave in).

Three edits in `sp_mmu.cfg`:

| Line | Section | Change |
|------|---------|--------|
| ~36 | `[duplicate_pin_override]` | `pins: PC15` (was `pins: PICO_MMU: PB6`) |
| ~53 | `[manual_stepper sp_motor]` | `endstop_pin: ^!PC15` (was `endstop_pin: ^PICO_MMU: PB6`) |
| ~65 | `[filament_switch_sensor sp_sensor_runout]` | `switch_pin: ^!PC15` (was `switch_pin: ^PICO_MMU: PB6`) |

> Line numbers reflect the version of `sp_mmu.cfg` in use during this build — they may shift if you regenerate the file from the LH-Stinger repo.

After the edit, verify with:

```
QUERY_FILAMENT_SENSOR SENSOR=sp_sensor_runout
```

With filament inserted it must report `filament detected`; with filament removed, `filament not detected`. If it reads the opposite, the `!` is missing or doubled.

---

## Step 7 — Disable the original runout pause in printer.cfg

`printer.cfg` already has:

```ini
[filament_switch_sensor filament_sensor]
switch_pin: !PC15
pause_on_runout: true
```

The Pico MMU now manages runout itself through `sp_sensor_runout` (which uses the same physical pin). Leaving `pause_on_runout: true` on the original sensor creates a conflict — both handlers fire on the same event.

Change:

```ini
pause_on_runout: false
```

The MMU's internal logic (`_SP_SENSOR_OUT` etc.) takes over runout handling cleanly.

---

## Step 8 — Create sp_mmu_data.cfg for [save_variables]

`sp_mmu.cfg` has a `[save_variables]` section that points to:

```
filename: /usr/data/printer_data/config/sp_mmu_data.cfg
```

Klipper normally creates this file on first write, but on the Nebula's custom filesystem the auto-create occasionally fails on permissions. Pre-create it empty:

```sh
touch /usr/data/printer_data/config/sp_mmu_data.cfg
ls -la /usr/data/printer_data/config/sp_mmu_data.cfg
```

Confirm `sp_mmu.cfg` references that exact path.

---

## After everything is in place

Restart Klipper:

```sh
/etc/init.d/S55klipper_service restart
```

Then run, in order:

1. `SP_HOME` — confirms the sensor reads correctly and homes the selector
2. Servo / lane angle calibration (`SP_TEST_SERVO ANGLE=...` per lane)
3. Rotation distance calibration
4. Distances calibration (hub → extruder, extruder → nozzle)
5. Tip forming calibration (only if no filament cutter is installed; with a cutter, skip)

Refer to the [LH-Stinger Pico MMU wiki](https://github.com/lhndo/LH-Stinger/wiki/Pico-MMU) for the calibration procedures themselves.

---

## Known limitation — Print progress % on the Nebula screen

It is **not currently possible** to show real-time print progress percentage on the Nebula Pad screen when prints are launched from Fluidd / Mainsail (i.e. anything other than the Nebula's own UI).

**Why:** Creality's master-server calculates the progress value internally — and only when it is the process that started the print. `display_status.progress` from Klipper does not influence the Heartbeat field that master-server sends to the screen. Workarounds tried and confirmed not working:

- `M118 Print percentDone:{int(layer_num * 100 / total_layer_count)}` in slicer's Change Layer G-code → reaches master-server and the cloud, but never the local screen.
- Patching `display_status.py` to multiply progress by 100 → updates the value in Klipper's API correctly but is still ignored by the screen.

This is a hard limitation of the Nebula's closed display pipeline. The print still completes normally; the screen just does not animate the percentage.

---

## Quick reference — files modified

| File | Change |
|------|--------|
| EBB42 firmware | Flashed `klipper.bin` built from Klipper v0.11.0 |
| `/usr/share/klipper/klippy/configfile.py` | Patched via `patch_configfile.sh` |
| `/usr/share/klipper/klippy/mcu.py` | `TRSYNC_TIMEOUT = 0.050` |
| `/usr/share/klipper/klippy/extras/servo.py` | Added from Klipper mainline (v0.11.0 commit) |
| `sp_mmu.cfg` line ~36 | `pins: PC15` |
| `sp_mmu.cfg` line ~53 | `endstop_pin: ^!PC15` |
| `sp_mmu.cfg` line ~65 | `switch_pin: ^!PC15` |
| `printer.cfg` | `pause_on_runout: false` |
| `/usr/data/printer_data/config/sp_mmu_data.cfg` | Created (empty) |
