# Print rescue: handoff and where to continue (2026-11-08)

Purpose: everything needed to pick this work up from the repository alone, with no chat history and no personal notes. Read in this order:
[print-rescue-runbook.md](print-rescue-runbook.md) (the procedure, every number and its source), this file (state, open items, next steps),
[print-rescue.md](print-rescue.md) (design, refusal catalogue, planner reference), [print-rescue-incident-2026-11-07.md](print-rescue-incident-2026-11-07.md)
and [print-rescue-live-validation-2026-11-07.md](print-rescue-live-validation-2026-11-07.md) (what the first live run showed).
Raw evidence (logs, G-code, printer config) contains host data and is NOT in the repo; it lived on the maintainer's machine and in a private notes repo.

> **Branch `feat/print-rescue-p2`: moved out of PR #114 (read-only phase 1). Do not run `rescue_run.py` against a printer until these review findings (PR #114, review of 5e5a296) are closed:**
> 1. `rescue_run.py` moves a real printer and is not reviewed as safety-critical; `--dry-run` is opt-in.
> 2. `probe_z`, "no trigger" branch: it redeclares `Z=z_offset` with `SET_KINEMATIC_POSITION` although the head stopped at the end of the probe stroke (below `z_offset`), so the next move lands low.
> 3. ~~`rescue_job.py --resume-name` is joined into the output path with no traversal or overwrite check.~~ Fixed 2026-11-10: a plain `*.gcode` name only, and the CLI never overwrites.
> 4. `rescue_job.py build_job` is a second job builder that skips the planner's R07/R09/R14/R15 refusals and uses a byte-based overlap.
> 5. ~~`power_cycle_and_rebuild` swallows a failed `device_power` call~~ (fixed 2026-11-10: it stops the run) and poll errors (still tolerated while waiting for `ready`).
>
> Also fixed 2026-11-10: a refused preflight (a print is active, R13, a cold bed...) used to run the abort cleanup, which switched off the nozzle and raised Z; it now sends nothing until preflight has passed.

## 1. Where things stand

| Area | State |
|---|---|
| Planner (`plan.py`, phase 1) | Read-only. Refuses a power cut (no `shutdown` line in the log) and reads the wrong exit position when the log spans two prints. |
| `rescue_job.py` | Builds `job.json` and the resume G-code from the G-code and the Klippy log. Reproduces the file that printed on 2026-11-08 byte for byte (`--rate-bps 1543`). |
| `rescue_run.py` | One-pass run through Moonraker with a head-vs-part guard. Used live once (a part rescued and finished). Head dimensions are owner-stated: it needs `--allow-unverified` and adds a 10 mm margin. |
| `rescue_watch.py` | Polls, captures evidence at the first failure, detects a host reboot. No unit test yet. |
| Tests | `python3 -m unittest discover -s print-rescue` (468 on 2026-11-10). |
| PR review loop | **Not at zero.** The `review` job has failed or stayed pending on and off (the earliest failures were `is_error:true` with 0 turns and no cost: probably quota or API on the repo side, never confirmed). Rounds up to 25 were pushed by the automated loop. Do not call the PR done until a review reports zero findings or each finding is fixed or justified. |
| Commit metadata | Many commits carry a Mac hostname in the author field. History must not be rewritten; use a squash merge with a clean author (the maintainer's decision). New commits in this session used the GitHub `noreply` address. |

## 2. What happened (the evidence behind the tooling)

All on one printer (Ender 3 V3 SE, Klipper fork, CR Touch, Raspberry Pi 3B+, Docker). Times local.

| # | When | Part | Stop | Recovery |
|---|---|---|---|---|
| 1 | 11-07 10:09 | wall mount, 141 min | `Lost communication with MCU`, one `ch341-uart ttyUSB0: urb stopped: -32` | hand resume, completed |
| 2 | 11-07 15:06 | laundry divider, 150 min | same | part discarded |
| 3 | 11-07 16:31 and 18:59 | vase | same | **first live rescue by hand**, layer 265, Z 53.0, Z by paper on the part |
| 4 | 11-07 23:05 | vase, resumed | same (`-32` at 22:36, 23:02) | owner powered off |
| 5 | 11-08 10:24 | vase from scratch, 26.8 % | **mains power cut** (the router has a battery); Pi, board and a second host went down; the log just ends | resumed by hand at line 261792 |
| 6 | 11-08 14:45 | vase, 68.5 % | same `-32` stall; one `-32` at 14:44:47 | **`rescue_run.py` one pass, line 810522, 2 min 27 s, 10 s hot over the part; the vase then printed to the end** |

Facts worth keeping:

* **The `-32` stalls are not explained.** Kernel `urb stopped: -32` on the CH340 (`ttyUSB`), `dwc_otg` `Timed out waiting for FSM NP transfer` bursts,
  no undervoltage, no thermal throttle other than the soft limit, load below 1. Never seen before 2026-11-07. Moving the cables did not prevent it
  (a 24 min cube ran clean, then two 2 h prints ran and stopped). Retransmit counters grew in bursts of ~187 bytes during long prints
  (21 events in the first vase run, 11 in the second, 0 in the cube) and recovered; the final one did not (`receive_seq` froze, three packets unacked,
  retransmit timeouts doubling, `Lost communication` about 16 s later).
* **Software is not ruled out** (owner's instruction). Changes merged shortly before the first stall: the `klipper-connect` bridge (PRs #96-#112),
  the virtual MMU sidecars `mmu-fallback` / `mmu-virtual` (#102, #103), Moonraker patches that power the printer on for queued jobs (#104, #105),
  a macro that saves config before auto power-off (#98), a `z_offset` change (#100). The stack was recreated about a day before the first stall.
* The kernel journal on the Pi is volatile (a reboot loses it); Docker logs rotate in about an hour. Evidence must be captured first and streamed off the Pi.
* A spontaneous Pi reboot at ~07:56 on 11-08, right after the printer was powered on, was never explained (the journal was lost).
* After a Pi reboot Klippy can start before `mmu-fallback` writes `/run/klipper/mmu_temp.cfg` (fix: `POST /printer/restart`), and the MMU Pico can keep a
  `shutdown` state (fix: `POST /printer/firmware_restart`). `depends_on` in compose does not apply to the Docker restart policy after a reboot.
* The CR Touch alarm (`Probe triggered prior to movement`) appeared after power cuts, an emergency stop and a hand-lifted head; power-cycling >= 25 s clears it.
  Rules are in the runbook ("CR Touch").
* The Klippy log at a `Lost communication` shutdown holds the host's exact state (`last_position`, `Virtual sdcard`, `Upcoming`, `Exiting SD card print`). Use it.
* Estimating print time from feed rates without acceleration was wrong by ~10x on a textured vase (thousands of ~1 mm segments). Use the measured file rate.

## 3. Open items, in the order to do them

1. **Close PR #114.** Check that the `review` job runs (if it fails with `is_error:true` at 0 turns, look at the repository's API key and quota),
   run it to zero findings or justify each one, then squash-merge with a clean author. Do not merge before.
2. **Measure the head with calipers** and put it in the profile as `source: measured` with `method` and `measured_on`: left, right (lever in and out), back,
   front, and the lowest point of the fan duct relative to the nozzle tip. This removes the need for `--allow-unverified` for the head box. The remaining
   safety-critical leaves read from `printer.cfg` (27 listed by R13) need the same treatment or an explicit decision to treat config values as verified.
3. **Teach the planner the power-cut case.** With no `shutdown` line, take the exit position from the last Moonraker poll, use only the log after the last
   print start, and let `rescue_job.py`'s selection (positive-E bead, measured-rate overlap) replace the time-based estimate. Add the "park point must be on a
   printed bead" rule (the first G-code line of a resume can be a travel over the hollow of the part).
4. **Find the cause of the `-32` stalls**, cheapest first, one change at a time, with the kernel stream and `rescue_watch.py` running on another machine:
   * stop `klipper-connect` and the metrics containers during a long print;
   * `dwc_otg.fiq_fsm_enable=0` in the kernel command line (needs a Pi reboot; backup the file first);
   * a short, shielded USB cable with ferrites, the CH340 on its own port, the MMU Pico on another;
   * make the Pi journal persistent so the previous boot survives (the `/var/log/journal` directory existed but stayed empty);
   * then, only if the above change nothing: align the Klipper firmware of the three MCUs with the host (plan in section 4).
5. **Fix the boot race** between Klippy and `mmu-fallback` (make Klippy wait for the include file, or make the include optional) and document it in `docs/`.
6. **Inspect the joins** of the rescued vase (around layers 203-204, 408-410 and 265) and record whether the overlap material and the Z were right.
7. **Add a unit test for `rescue_watch.py`** (reboot detection, frozen position, capture on failure).
8. Issue #115: start a rescue from a Mainsail macro with a worker on another host. A request-only macro and a preparing worker exist (PR #120, not deployed, see `print-rescue-worker.md`); the run itself stays manual.
9. Housekeeping on the printer: remove the uploaded `resume_*.gcode` files and the purge blobs, and decide on the uncommitted `z_offset` edit in the working `printer.cfg`
   (the profile says 2.96, which equals the live value; R14 compares them).

## 4. Plan to align the Klipper version on the three MCUs (do not start before the cube-sized tests above)

Observed (read-only, from the Klippy start log): the main board (stm32f103, serial over a CH340) runs `1.0.0-598-gcecc1bba`; the MMU Pico (stm32g0b1, USB) runs a
build from 2025-03-30 with no tag; a third board with an rp2040 (accelerometer, config include commented out) was not connected. The host image is built from
the fork commit pinned in `Dockerfile-klipper` and `build_firmware.sh`. The Klipper container has no `git`, so read the host version from its start log.
Steps: read each MCU's `mcu_build_versions`; build all three firmwares from the same commit and keep the old `.bin` files and images for rollback; the main board is
flashed from an SD card, the Pico by DFU (see `nebula_setup_guide.md`), the rp2040 in BOOTSEL; back up the MMU save-variables file and `printer.cfg`; never flash
around a print. A host/MCU version gap is a long shot for a USB `-32` stall; it is cheap to remove as a variable.

## 5. How to continue on a new machine (no memory needed)

1. Clone the repo, check out `feat/print-rescue`, run `python3 -m unittest discover -s print-rescue`.
2. Read the runbook end to end; do a `--dry-run` of `rescue_run.py` against the printer with a job built from a real stop.
3. Before any print that may stall, start `rescue_watch.py` and a kernel-log stream on a machine that is not the Pi.
4. When it stops: capture first (runbook section 1), keep the bed hot, never power-cycle with the nozzle a few millimetres above the part, then follow the runbook.
5. Update this file when an item in section 3 is done or when something new is learned.
