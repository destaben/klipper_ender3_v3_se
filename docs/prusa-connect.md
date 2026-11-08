# Prusa Connect bridge (klipper-connect)

[klipper-connect](https://github.com/amd989/klipper-connect) is a community bridge
that makes this printer show up in [Prusa Connect](https://connect.prusa3d.com).
It is MIT-licensed and is **not made by, endorsed by, or affiliated with Prusa
Research**. It runs as the `klipper-connect` container and talks to Moonraker
over the Compose network. Moonraker's port is not published directly, but Traefik
publishes host port 80 and routes the Moonraker API paths to it, so the API is
reachable from the LAN. See the security note below.

The bridge reports the printer to Connect as an **MK3S** (`type: MK3S` in
`config/klipper_connect.conf`), one of the printer types the Connect SDK knows.
The Ender 3 V3 SE is not a Prusa, so some things will not map (see
[Limitations](#limitations)).

The upstream commit is pinned by the `KLIPPER_CONNECT_COMMIT` argument in
[`Dockerfile-klipper-connect`](../Dockerfile-klipper-connect). Upstream has no
Dockerfile and no releases, so the commit pin is the only upstream version record.
The same file also pins the base image by digest and `requests` and `pycryptodome`
to exact versions. The SDK's other transitive dependencies still float, so rebuilds
are not bit-for-bit reproducible (see [Upgrading the pinned commit](#upgrading-the-pinned-commit)).

## What it does, and what Connect can do

Once registered, Connect shows the printer's state, temperatures, fans, and
progress. It can also **control the printer**. The bridge accepts these commands
from Connect:

- Start, pause, resume, and stop prints.
- Upload files to the `gcodes/` folder, which is shared with Moonraker.
- Delete files and folders in that folder.
- Run arbitrary G-code (`cmd_gcode` in upstream `bridge.py`). That includes
  heaters and motion.
- Run the load and unload filament macros, and reset the printer.

**The Connect token protects only the remote path through Prusa Connect.** It does
not protect Moonraker on the LAN. Traefik routes the Moonraker API on host port 80,
and `trusted_clients` in `config/moonraker.conf` includes the private ranges, so
any LAN client can already control the printer through Moonraker without a key.
Anyone who has the token or a Connect account linked to the printer can start,
stop, and upload prints remotely, and can also move the printer, heat it, and
delete its files. Treat `klipper-connect/prusa_connect.ini` like a password:

- Do not commit it, paste it into an issue, or put it in `config/`.
- Use a strong password and 2FA on the Prusa account that owns the printer.
- Stop the container when you do not need remote access:
  `docker compose stop klipper-connect`.

Uploads from Connect are written to `gcodes/`, which is mounted read-write. The
bridge does not cap upload size, so a large upload can fill the host disk, and a
full disk also affects Moonraker and Klipper. Keep an eye on free space (`df -h`)
after large uploads.

## Starting a print with the printer powered off

The printer's plug (`[power printer]` in `config/moonraker.conf`) is switched off
after a print. A patch on top of the pinned upstream commit
(`patches/klipper-connect/0001-queue-start-when-printer-off.patch`, applied in
`Dockerfile-klipper-connect`) makes a **Start print** from Connect work then:

1. The bridge sees Klippy not `ready` and the Moonraker power device `printer`
   `off`. Only both together count as "printer off".
2. Instead of rejecting the command, it queues the file with
   `POST /server/job_queue/job`.
3. Moonraker switches the plug on, restarts Klipper 12 s later and starts the
   queue when Klippy is `ready` (see the **Home Assistant integration** note under
   [Notes in the README](../README.md#notes)).

If the plug is on but Klippy is down (a real fault), the command is still
rejected: queuing would start a print unattended after the next restart.

The queued job starts on **every** Klippy `ready` until it is removed from the
queue. If Klippy does not reach `ready` after the power-on restart (the MCU took
longer than 12 s to appear, or there is a fault), the job stays queued and starts
on the next successful `ready`, possibly hours later and with nobody at the
printer. Check the queue (`/server/job_queue/status`, or Mainsail's queue panel)
and empty it if the print is no longer wanted. The bridge only checks Klippy and
the plug, not the bed or a previous interrupted print: pressing **Start** in
Connect is taken as the operator's decision. The power
device name is `printer`; set `KLIPPER_CONNECT_POWER_DEVICE` in the container
environment if yours differs.

With the printer powered off, the bridge does not report `ERROR` to Connect
(`Bridge._off_state`). In a 2026-11-05 test with the plug off, Connect showed the
**Start print** button even while the bridge reported `ERROR`, but the command
never arrived: the bridge's DEBUG log showed a `204` for every telemetry request
and no command. The bridge now reports `IDLE` while Klippy is not ready and the
power device is off, and `READY` once Connect has sent `SET_PRINTER_READY`, so
that Connect can deliver the Start command. A printer that is on but faulty still
shows `ERROR`. The power state is cached for 5 s, so Home Assistant is not queried
every second. Temperatures are empty while the printer is off, because there is
no data.

Result of the end-to-end test from Printables Easy Print with the plug off
(2026-11-05): Connect sends `SET_PRINTER_READY`, then `START_PRINT`. The bridge
reports READY (not IDLE) once Connect has marked the printer ready, queues the
job, Moonraker powers the plug on and restarts Klipper, and the print starts
(about 30 s). Connect may resend `START_PRINT` during the restart. In that test
the resend showed ERROR in Connect briefly, then the state recovered. Whether a
resend can queue the same job a second time has not been tested.

`START_PRINT` is not sent by Connect itself: the sliced file's own start G-code
calls it with `BED` and `HEATER`. The macro refuses a missing, empty or non-numeric
`BED` or `HEATER` (for example an unexpanded slicer placeholder such as
`{first_layer_bed_temperature}`) before heating anything, so the print aborts with
an error instead of heating to 0 °C. Values outside 0–400 (and `nan`) are refused too; `0` is accepted.

## Limitations

- **Not a Prusa.** Bed size, the MMU, filament sensors, and other features may
  not match what Connect expects for an MK3S. Some values shown in Connect may
  be wrong or missing.
- **Load and Unload filament buttons will fail.** The bridge's defaults are
  `LOAD_FILAMENT` and `UNLOAD_FILAMENT`. This repository has no macros with those
  names, and the Pico MMU's `SP_LOAD_*` and `SP_UNLOAD_*` macros are not mapped to
  them. The buttons keep failing until you point `load_filament` and
  `unload_filament` at macros that exist in `config/`.
- **Serial number is not a real printer serial.** The Ender 3 V3 SE has no Prusa
  sticker, so `serial_number` is a `CZPX...`-format value you choose. Connect
  identifies the printer by it. Pick a unique value: two installs that share one
  serial collide in Connect.
- **Registration is not verified on this printer.** The real Connect register
  flow (the link, the `/add` page, the 30-minute expiry) has not been tested
  against this printer. If the screens differ, follow what Connect shows and
  check the logs.
- **Binary G-code must be off.** Prusa slicers can write binary G-code
  (`.bgcode`), which Klipper cannot run. Turn off the slicer's binary G-code
  option (PrusaSlicer: "Supports binary G-code"). Plain-text G-code only.
- **Prusa-only commands.** Klipper does not know Prusa firmware commands such as
  the `M862.x` model checks. A file that contains them fails with
  `Unknown command` and the print stops at that line.
- **Bed leveling.** Connect's "Start MBL" sends a plain `G28`, and the bridge
  appends `mbl_gcode` after it. This printer has no `G80`, so `mbl_gcode` is
  left empty.
- **Protocol changes.** Prusa can change the Connect protocol. The bridge then
  breaks until upstream fixes it. Re-check the pin when that happens.

## Setup

Assumes the stack already runs (`docker compose ps moonraker` shows it up). You
also need a serial number in the `CZPX...` format. The Ender 3 V3 SE has no Prusa
sticker, so you choose this value yourself (see [Limitations](#limitations)).

1. **Create the config file.** It is gitignored.

   ```bash
   cp config/klipper_connect.conf.example config/klipper_connect.conf
   ```

   Edit `config/klipper_connect.conf`:

   - Set `serial_number` to a `CZPX...`-format value of your choice. The example
     value is a placeholder and must be replaced with your own unique value. Two
     installs that share a serial collide in Prusa Connect. Connect's acceptance of
     a chosen serial is not verified (see [Limitations](#limitations)).
   - Check the `[macros]` section. Confirm that the macros it names exist:

     ```bash
     grep -rn -E "gcode_macro (LOAD_FILAMENT|UNLOAD_FILAMENT)" config/
     ```

     If that prints nothing, set `load_filament` and `unload_filament` to macros
     that exist, or accept that the load and unload buttons fail.

2. **Make the folders writable by UID 1000.** The container runs as UID 1000, the
   same user as the Moonraker container. It must be able to write `klipper-connect/`
   (token and job state) and `gcodes/`. Check the owner:

   ```bash
   ls -ldn klipper-connect gcodes   # owner column should be 1000
   ```

   On Linux hosts where your login user is not UID 1000, chown `klipper-connect/`
   before step 4. `gcodes/` already has to be writable by UID 1000 for Moonraker, so
   chown it the same way if it is not:

   ```bash
   sudo chown -R 1000:1000 klipper-connect
   ```

   After this, `klipper-connect/` belongs to UID 1000, so removing files in it needs
   `sudo`.

3. **Get the image.** The compose file pins the image CI published from `main`.
   Pull it:

   ```bash
   docker compose pull klipper-connect
   ```

   To build it from `Dockerfile-klipper-connect` instead (for example after
   editing it), run `docker compose build klipper-connect`.

4. **Register the printer.** This prints a link and waits.

   ```bash
   docker compose run --rm -it klipper-connect register
   ```

   Open the link in a browser that is logged in to Prusa Connect and confirm the
   printer. Or enter the printed code at `https://connect.prusa3d.com/add`. This
   flow is not yet verified on this printer (see [Limitations](#limitations)). The
   command exits 0 when Connect confirms, and it writes the token to
   `klipper-connect/prusa_connect.ini`. The link expires after 30 minutes.

5. **Start the service.**

   ```bash
   docker compose up -d klipper-connect
   ```

   A plain `docker compose up -d` does **not** start this service. It is behind
   the `prusa-connect` compose profile, which keeps a plain `up -d` from failing
   when the config is absent.

6. **Check the logs.**

   ```bash
   docker compose logs -f klipper-connect
   ```

   A healthy start logs a `Bridge running:` line with the serial number and the
   Connect URL (upstream's wording, which may change with the pinned commit).
   The printer should then show as online in Connect.

If Moonraker is down, the bridge logs `Moonraker unreachable` and retries. It
does not exit. If the token is missing, it exits with
"doesn't exist - run `klipper-connect register` first". The restart policy then
retries it, so run step 4 before step 5.

## Where state lives

| On the host | In the container | Contents |
|---|---|---|
| `config/klipper_connect.conf` | `/data/config/klipper_connect.conf` (read-only) | Settings. Gitignored. |
| `klipper-connect/prusa_connect.ini` | `/data/klipper_connect/prusa_connect.ini` | Connect token, mode 600. Gitignored. **Secret.** |
| `klipper-connect/state.json` | `/data/klipper_connect/state.json` | Last job ID and the active job. Gitignored. |
| `klipper-connect/camera.json` | `/data/klipper_connect/camera.json` | Camera token, only if a camera is enabled. Gitignored. |
| `gcodes/` | `/data/gcodes` | G-code files. Shared with Moonraker. |

The container runs as UID 1000, the same user as the Moonraker container. It
must be able to write `klipper-connect/` and `gcodes/`. Setup step 2 checks the
ownership and shows the `chown`. The container also has a read-only root
filesystem, `/tmp` on tmpfs, all capabilities dropped, and `no-new-privileges`, so
the two bind mounts and `/tmp` are the only places it can write. If a future bridge version needs
another writable path, add a mount rather than dropping these settings.

### Backup

Back up `klipper-connect/` to a private location. It contains the token. Do not
copy it into the repository.

### Re-register

Use this when you move the printer to another Connect account, or when the
token is lost or compromised:

```bash
docker compose stop klipper-connect
sudo rm klipper-connect/prusa_connect.ini
docker compose run --rm -it klipper-connect register
docker compose up -d klipper-connect
```

To also reset the job ID counter and forget the active job, delete
`klipper-connect/state.json` (with `sudo rm`) before starting. Remove the old printer entry in
Connect yourself.

## Upgrading the pinned commit

Upstream has no releases, so review the commits before bumping the pin:

1. Read the upstream commit log between the old and new commit:

   ```bash
   gh api "repos/amd989/klipper-connect/compare/<old_sha>...<new_sha>" --jq '.commits[].commit.message'
   ```

2. Check `klipper_connect.conf.example` upstream for new or renamed keys, and
   update `config/klipper_connect.conf.example` if needed.
3. Change `KLIPPER_CONNECT_COMMIT` in `Dockerfile-klipper-connect`. If upstream's
   requirements changed, update the `REQUESTS_VERSION` and `PYCRYPTODOME_VERSION`
   pins too. Refresh the base image digest when you want newer base image
   contents (see the comment at the top of the Dockerfile). Transitive dependencies
   of the SDK are not pinned, so a rebuild can still pick up newer versions of them.
4. The build applies the local patch with `patch -F0`, so it fails if upstream
   changed the code the patch touches (`Moonraker` and `Bridge._start`). Rebase the
   patch onto the new commit. Then build the test image from the repository root
   (the Dockerfile copies `patches/`), and run its tests in it:

   ```bash
   docker build -f Dockerfile-klipper-connect -t klipper-connect-test .
   docker run --rm --entrypoint python -v "$PWD/patches/klipper-connect:/t:ro" \
     -e PYTHONPATH=/t -w /t klipper-connect-test -m unittest test_queue_start -v
   ```
5. Rebuild and confirm the bridge still starts:

   ```bash
   docker compose build klipper-connect
   docker compose up -d klipper-connect
   docker compose logs -f klipper-connect
   ```

6. After the change merges to `main` and CI publishes the image, update the
   `image:` tag of `klipper-connect` in `docker-compose.yaml` to the new
   `sha-<short>` tag, as for the other services.

## Troubleshooting

- **`bind source path does not exist`** (`config/klipper_connect.conf`): create the
  config file (step 1). Compose does not create it, by design.
- **"doesn't exist - run `klipper-connect register` first"**: the token is
  missing. Run step 4.
- **`Moonraker unreachable`**: check `docker compose ps moonraker`. The bridge
  retries on its own.
- **`Serial number ... doesn't look like a Prusa serial`**: the value in
  `[printer] serial_number` is not in the `CZPX` format. Set it to a `CZPX...`
  value (Setup step 1). There is no sticker to copy it from.
- **Connect buttons fail with unknown command**: a macro named in `[macros]` is
  missing. See the Setup step 1.
