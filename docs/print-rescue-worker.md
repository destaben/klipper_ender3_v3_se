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

* `print-rescue/printer-macro/rescue_request.cfg`: the macro. It does no motion, heating, restart, file or shell access, and refuses while a
  print runs. It is **opt-in**: it is not under `config/macros/` (which `printer.cfg` includes by glob), so nothing changes until the
  owner copies it to the printer, includes it and restarts Klippy.
* `print-rescue/rescue_worker.py`: `run` (log keeper + poller + prepare) and `confirm`. Standard library only.

## Decisions (council quick, 2026-11-10) and why

| Question | Decision | Reason |
|---|---|---|
| Trigger | A: a macro only bumps `seq`/`stamp`; the worker polls `GET /printer/objects/query` | Smallest printer-side change, no motion, no new Moonraker component. B (`action_call_remote_method`) and C (database item) would couple the macro to a Moonraker component or write path this increment does not need. D (automatic detection) removes the human trigger. |
| Reaching Moonraker | HTTP GET only: history, gcode download, log files via `/server/files/logs/...`. No ssh in this increment | One protocol, no shell on the Pi. The client has no method other than GET and an allow-list of paths (a test fails if any other method is sent). |
| Log retention | The worker snapshots the Klippy and Moonraker logs every 5 min into a private store (gzip, 512 MB and 14 day caps, newest snapshot of each log is never pruned) with a manifest that flags a **gap** when a snapshot does not overlap the previous one | Docker logs rotate in about an hour and the worker host restarts daily. A job built from a log that does not cover the stop is refused. |
| Auth | Optional Moonraker API key in a file that must be mode 600 and is never logged; LAN only | Nothing is exposed to the internet. Honest limit: a Moonraker API key is not scoped, so "GET only" is enforced by this client, not by Moonraker. Prefer narrowing `trusted_clients` to the worker's address and no key at all. |

Council amendments, all implemented: completeness manifest for the logs; poller state on disk (a worker restart does not replay a
request; a Klippy restart resets the counter but the stamp still tells a new press apart); the confirmation is bound to the job id
and a hash of the job and resume files.

## Safety properties (each has a test)

* The macro text contains no motion, heating, probing, restart or shell command.
* The worker module has no `subprocess`, no import of `rescue_run`, and does not contain the override flag at all. The printed command is
  `--dry-run`. If the profile head box is still owner-stated, the output says `rescue_run.py` will refuse (R13); nothing here overrides it.
* The phrase must be exactly `CONFIRM <job id>-<hash8> AT PRINTER`. An edited job or resume file, a second use, a running print, or a Klippy
  that is not `ready` is refused.
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
