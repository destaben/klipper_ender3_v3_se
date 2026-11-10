# Print rescue: Mainsail button and worker (issue #115)

Status: **implemented and tested with stubs only. Nothing is deployed, no printer was contacted, nothing was applied to the live
printer.** It builds on the one-pass runner ([runbook](print-rescue-runbook.md), [design](print-rescue-p2-design.md)) and changes
none of its safety rules.

## What it is

```
Mainsail button RESCUE_REQUEST  ->  counter+stamp bump in Klipper  ->  worker polls (GET only)
      worker: prepares job.json + resume file + evidence copy in a private dir, writes PENDING.json, STOPS
      person at the worker: types "CONFIRM <job tag> AT PRINTER"
      worker: prints a rescue_run.py --dry-run command. It does not run it.
```

* `print-rescue/printer-macro/rescue_request.cfg`: the macro. It does no motion, heating, restart, file or shell access, and records a
  request only while `print_stats` is `standby` (a running, paused, errored, cancelled or complete job is refused). It is **opt-in**: it is not under `config/macros/` (which `printer.cfg` includes by glob), so nothing changes until the
  owner copies it to the printer, includes it and restarts Klippy.
* `print-rescue/rescue_worker.py`: `run` (log keeper + poller + prepare) and `confirm`. Standard library only.

## Decisions (council quick, 2026-11-10) and why

| Question | Decision | Reason |
|---|---|---|
| Trigger | A: a macro only bumps `seq`/`stamp`; the worker polls `GET /printer/objects/query` | Smallest printer-side change, no motion, no new Moonraker component. B (`action_call_remote_method`) and C (database item) would couple the macro to a Moonraker component or write path this increment does not need. D (automatic detection) removes the human trigger. |
| Reaching Moonraker | HTTP GET only: history, gcode download, log files via `/server/files/logs/...`. No ssh in this increment | One protocol, no shell on the Pi. The client has no method other than GET and an allow-list of paths (a test fails if any other method is sent). |
| Log retention | The worker snapshots the Klippy and Moonraker logs every 5 min into a private store (gzip, 512 MB and 14 day caps, newest snapshot of each log is never pruned) with a manifest that flags a **gap** when a snapshot does not overlap the previous one | Docker logs rotate in about an hour and the worker host restarts daily. A job built from a log that does not cover the stop is refused. |
| Auth | Optional Moonraker API key in a file that must be mode 600 and is never logged; LAN only | Nothing is exposed to the internet. Moonraker speaks plain HTTP here, so a key crosses the LAN in the clear: `--key-file` is refused unless `--accept-plaintext-key` is passed AND the host resolves only to private, loopback or link-local addresses. The key file holds one line, mode 600, never logged. Honest limit: a Moonraker API key is not scoped, so "GET only" is enforced by this client, not by Moonraker. Prefer narrowing `trusted_clients` to the worker's address and no key at all. |

Council amendments, all implemented: completeness manifest for the logs; poller state on disk (a worker restart does not replay a
request; a Klippy restart resets the counter but the stamp still tells a new press apart); the confirmation is bound to the job id
and a hash of the job and resume files.

## Safety properties (each has a test)

* The macro text contains no motion, heating, probing, restart or shell command.
* The worker module has no `subprocess`, no import of `rescue_run`, and does not contain the override flag at all. The printed command is
  `--dry-run`. If the profile head box is still owner-stated, the output says `rescue_run.py` will refuse (R13); nothing here overrides it.
* The phrase must be exactly `CONFIRM <job id>-<hash8> AT PRINTER`. An edited job or resume file, a second use, any print state other than `standby` (running, paused, errored, cancelled, complete), or a Klippy
  that is not `ready` is refused.
* The stop is tied to the history job: the exit line must be NEW since a kept snapshot taken at least 5 minutes before the job started (the worker and printer
  clocks are not synchronised), with no gap in between, because Klippy may append to its log across restarts and an older job's exit line would give the wrong
  resume point. This needs the log keeper to have been running before the job started; otherwise the worker refuses. The G-code downloaded by name must have
  the size the history recorded for the job that printed (a re-uploaded file of the same name is refused). Because a same-length edit keeps the size, the file's modification time on the printer must also equal the one the history recorded; if either is missing, the worker refuses.
* Refused rather than guessed: last job ended normally, last job was itself a resume file (build by hand with `--exit-file`), unsafe file name
  in the history, no `Exiting SD card print` line in any kept log, a gap after the snapshot that holds it, no `--rate-bps`/`--polls`.
* A failed prepare marks the request handled (no hot retry loop): fix the cause and press the button again.

## Known limits (what is NOT done, and what is unverified)

* **Klippy in shutdown cannot run macros.** The button works only after someone brought Klippy back to `ready` (runbook step 2). That is why
  the evidence is kept continuously and not captured by the button.
* **Kernel journal is not kept** (needs ssh). Every prepared job says `cause evidence incomplete: no kernel journal`. Keeping it is the first follow-up.
* **Unverified against a real Moonraker**: that `/server/files/logs/klippy.log` exists in this docker layout (`moonraker/logs` is mounted; check
  that Klipper writes its log there), that the macro's `estimated_print_time` stamp renders as expected, and the real log rotation behaviour.
  Test them read-only before relying on this.
* A request token is `<seq>@<stamp>`. The stamp is `estimated_print_time`, which is about 0 after a Klippy restart until something moves. Two presses
  with no motion between them across a restart can therefore produce the same token (`1@0.0`), and the second is ignored (the worker prints one `SKIPPED request ...` line per token and process, so it is not silent, but it cannot tell the two presses apart). This is more likely on
  an idle printer than "the same millisecond" suggests. Not mitigated (a better stamp, such as a wall-clock time, needs a design choice). If a press
  seems to do nothing, check the worker's output and press again.
* A failed prepare (including a transient Moonraker timeout) marks the request handled, so the person must press again. Chosen over automatic retry to
  avoid a hot loop against a printer that is in trouble; the cause is printed as `REFUSED`.
* Each log snapshot is read into memory whole. Fine for the hourly-rotated logs (a few MB); not for a log grown to hundreds of MB.
* **Checked on the real printer (2026-11-10, owner present by remote consent):** after the printer was powered on from `shutdown` (Moonraker power device, Klippy restarted by `restart_klipper_when_powered`), `print_stats.state` read `standby` with an empty job queue. That is a restart after power loss, not after an MCU-lost shutdown in the middle of a print followed by `FIRMWARE_RESTART`; the latter restarts the Klipper process the same way, but it has not been observed. If it ever stays `error`, the feature refuses in exactly the case it was built for and the gate must be revisited (an owner decision, since it weakens the check).
* A Moonraker `/server/files/` read is limited to the `gcodes/` and `logs/` roots; paths with `..` or encoded dots and slashes are refused.
* The worker does not upload the resume file (that is a POST); the person does it (runbook step 3).
* Where the worker runs, and how it restarts, is the owner's choice: run `rescue_worker.py run` under any supervisor (systemd unit, container
  restart policy). A daily restart is safe: state and logs are on disk.
* The job directories and the log store contain host data (G-code, logs). They are created mode 700/600 and must stay out of git.

## Use (on the worker, after the owner has installed the macro)

```bash
python3 print-rescue/rescue_worker.py run --host <host> --work-dir <private dir> --rate-bps <bytes/s> [--key-file <600 file>]
python3 print-rescue/rescue_worker.py confirm --host <host> --job-dir <private dir>/jobs/<job> --probe-spot 134,215
```

`--rate-bps` (or `--polls <poll.csv>`) is required: bytes are not seconds (runbook, step 3).

## Installing the macro (an owner decision; NOT done by this PR)

1. Copy `print-rescue/printer-macro/rescue_request.cfg` next to `printer.cfg` on the printer (for example `macros/rescue_request.cfg`).
2. Add `[include macros/rescue_request.cfg]` to `printer.cfg` and restart Klippy.
3. Check read-only: `RESCUE_REQUEST` appears in the console list, and `print_stats.state` reads `standby` when idle (see the unverified item above).
Pressing it prints one line and bumps the counter; nothing moves.

## Running and testing

* No supervisor is chosen yet: a systemd unit or a container restart policy both work, since state and logs are on disk.
* Tests: `python3 -m unittest discover -s print-rescue -v` (the worker's are `test_rescue_worker.py`, all against stubs and local sockets).
* Left open after review (low impact): a failed state write after a prepare can repeat the read-only prepare on the next poll (it prepares into a new job directory and moves nothing); the G-code is not re-checked after the download; a transient Moonraker error still consumes the button press.  
