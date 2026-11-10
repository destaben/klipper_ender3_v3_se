"""print-rescue: the worker side of the Mainsail rescue button (issue #115). It PREPARES a plan and then stops.

    python3 print-rescue/rescue_worker.py run     --host <moonraker-host> --work-dir <dir> --rate-bps 1543 [--key-file <file>]
    python3 print-rescue/rescue_worker.py confirm --host <moonraker-host> --job-dir <dir>/jobs/<job> [--probe-spot 134,215]

`run` loops: it keeps snapshots of the Klippy and Moonraker logs (so the evidence of a stop survives a log rotation and the daily
restart of this host), and polls the RESCUE_REQUEST macro (print-rescue/printer-macro/rescue_request.cfg). On a new request it builds the
job and the resume file with rescue_job.py into a private job directory, copies the evidence next to them, writes PENDING.json and
stops. `confirm` asks a person to type an exact phrase that names the prepared job; it then prints the rescue_run.py command for
the person to run. THIS MODULE NEVER STARTS THE RUNNER and has no code path that moves, heats, uploads or starts anything:
every request it makes is a GET (ReadOnlyClient has no other method and an allow-list of paths). Standard library only.

Safety properties kept here (see docs/print-rescue-worker.md):
  * the phrase must name the job id and a hash of the job and resume files; an edited or stale job is refused;
  * the printed command is a --dry-run and never carries the override flag; the planner's R13 refusal is untouched;
  * a log that does not cover the stop (no exit line, or a gap after the snapshot that holds it) is refused, never guessed;
  * the job says plainly that no kernel journal was kept (cause evidence incomplete).
"""

import argparse
import gzip
import hashlib
import json
import os
import re
import shlex
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Callable, Dict, List, Optional

import machine_profile as mp
import rescue_job as rj

MACRO = "gcode_macro RESCUE_REQUEST"
MACRO_PATH = "/printer/objects/query?" + urllib.parse.quote(MACRO) + "&print_stats"
HISTORY_PATH = "/server/history/list?limit=1"
PRINT_STATS_PATH = "/printer/objects/query?print_stats"
INFO_PATH = "/printer/info"
KLIPPY_LOG = "logs/klippy.log"
LOGS = (KLIPPY_LOG, "logs/moonraker.log")
# Job statuses that mean "the print stopped abnormally". A job that completed or was cancelled by a person is not a rescue.
ABNORMAL = ("klippy_shutdown", "klippy_disconnect", "error", "interrupted", "server_exit")
ALLOWED_PREFIXES = ("/printer/info", "/printer/objects/query", "/server/history/list", "/server/files/gcodes/", "/server/files/logs/",
                    "/server/info")
EVIDENCE_NOTE = "cause evidence incomplete: no kernel journal was kept (keep `journalctl -k -f` on this host and add it)"
PHRASE = "CONFIRM %s AT PRINTER"


class WorkerError(RuntimeError):
    pass


# -- plumbing ----------------------------------------------------------------------------------------------------------
def private_dir(path: str) -> str:
    os.makedirs(path, mode=0o700, exist_ok=True)
    os.chmod(path, 0o700)
    return path


def write_private(path: str, data: bytes) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(data)


def atomic_json(path: str, obj) -> None:
    tmp = path + ".tmp"
    write_private(tmp, json.dumps(obj, indent=1, sort_keys=True).encode())
    os.replace(tmp, path)


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """A redirect would carry the API key header to another host and skip the path allow-list: never follow one."""

    def redirect_request(self, *a, **k):
        return None


_OPEN = urllib.request.build_opener(_NoRedirect).open


class ReadOnlyClient:
    """HTTP GET to Moonraker and nothing else. The API key (optional, trusted_clients may be enough) comes from a file that
    must be private; it is sent as a header and never put into a message."""

    def __init__(self, host: str, key_file: Optional[str] = None, opener: Callable = _OPEN, timeout: int = 30):
        self.host, self.opener, self.timeout = host, opener, timeout
        self._key = None
        if key_file:
            try:
                st = os.stat(key_file)
                with open(key_file) as f:
                    self._key = f.read().strip()
            except OSError:
                raise WorkerError("key file missing or unreadable")
            if st.st_mode & 0o077:
                raise WorkerError("key file must be private (chmod 600): it is readable by others")
            if not self._key:
                raise WorkerError("key file is empty")

    def _get(self, path: str) -> bytes:
        if (not path.startswith("/") or path.startswith("//") or ".." in path or not path.startswith(ALLOWED_PREFIXES)
                or any(x in path.lower() for x in ("%2e", "%2f", "%5c", "%00"))):
            raise WorkerError("refused path (read-only allow-list): %s" % path.split("?")[0])
        headers = {"X-Api-Key": self._key} if self._key else {}
        req = urllib.request.Request("http://%s%s" % (self.host, path), method="GET", headers=headers)
        try:
            with self.opener(req, timeout=self.timeout) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            raise WorkerError("HTTP %s for %s" % (e.code, path.split("?")[0]))
        except Exception as e:  # URLError, timeout, reset: never echo the text (it could carry request data)
            raise WorkerError("request failed (%s) for %s" % (type(e).__name__, path.split("?")[0]))

    def get_bytes(self, path: str) -> bytes:
        return self._get(path)

    def get_json(self, path: str):
        try:
            return json.loads(self._get(path).decode())
        except ValueError:
            raise WorkerError("not JSON: %s" % path.split("?")[0])


def _status(client, path: str, key: str) -> dict:
    try:
        return client.get_json(path)["result"]["status"][key]
    except (KeyError, TypeError):
        raise WorkerError("unexpected answer for %s" % key)


# -- request polling -----------------------------------------------------------------------------------------------------
class Poller:
    """Watches the macro's counter. The token is "<seq>@<stamp>": a Klippy restart resets seq, and the stamp (print time) usually
    differs, so a new press is not mistaken for an old one. KNOWN LIMIT: the print time is about 0 after a restart until something
    moves, so two idle presses across a restart can both give "1@0.0" and the second is ignored (docs/print-rescue-worker.md, known
    limits; a better stamp is an owner decision). Handled tokens are kept on disk so a worker restart does not replay a request.
    A request is only taken while print_stats is "standby": a paused, errored, cancelled or complete job is not a rescue moment."""

    def __init__(self, client, state_path: str):
        self.client, self.path = client, state_path

    def _handled(self) -> List[str]:
        try:
            with open(self.path) as f:
                h = json.load(f)["handled"]
            return [str(x) for x in h]
        except (OSError, ValueError, KeyError, TypeError):
            return []

    def mark_handled(self, token: str) -> None:
        atomic_json(self.path, {"handled": (self._handled() + [token])[-50:]})

    def check(self) -> Optional[str]:
        try:
            st = self.client.get_json(MACRO_PATH)["result"]["status"]
            m, ps = st[MACRO], st["print_stats"]["state"]
            seq, stamp = int(m["seq"]), m["stamp"]
        except (WorkerError, KeyError, TypeError, ValueError):
            return None   # Klippy not ready / macro not installed: nothing to do
        if seq <= 0 or ps != "standby":
            return None
        token = "%d@%s" % (seq, stamp)
        return None if token in self._handled() else token


# -- log keeping ---------------------------------------------------------------------------------------------------------
def _tail_hashes(text: bytes, n: int = 3) -> List[str]:
    lines = [l for l in text.splitlines() if l.strip()]
    return [sha256(l) for l in lines[-n:]]


def _contains_seq(text: bytes, seq: List[str]) -> bool:
    if not seq:
        return True
    hs = [sha256(l) for l in text.splitlines() if l.strip()]
    k = len(seq)
    return any(hs[i:i + k] == seq for i in range(len(hs) - k + 1))


class LogKeeper:
    """Periodic full snapshots of GET-able logs into a private, size- and age-capped store, with a manifest that records whether
    each snapshot still overlaps the previous one. A snapshot with `gap: true` means log lines may be missing before it."""

    def __init__(self, client, store_dir: str, names=LOGS, max_bytes: int = 512 * 1024 * 1024, max_age_s: float = 14 * 86400,
                 clock: Callable = time.time):
        self.client, self.dir, self.names = client, private_dir(store_dir), list(names)
        self.max_bytes, self.max_age_s, self.clock = max_bytes, max_age_s, clock
        self.manifest_path = os.path.join(self.dir, "manifest.jsonl")

    def entries(self) -> List[dict]:
        out = []
        try:
            with open(self.manifest_path) as f:
                for l in f:
                    try:
                        if l.strip():
                            out.append(json.loads(l))
                    except ValueError:
                        continue   # one damaged line must not orphan the other snapshots
        except OSError:
            return []
        return out

    def _write(self, entries: List[dict]) -> None:
        tmp = self.manifest_path + ".tmp"
        write_private(tmp, ("\n".join(json.dumps(e, sort_keys=True) for e in entries) + "\n").encode())
        os.replace(tmp, self.manifest_path)

    def read(self, entry: dict) -> bytes:
        with gzip.open(os.path.join(self.dir, entry["file"])) as f:
            return f.read()

    def snapshot_once(self) -> List[dict]:
        out = []
        entries = self.entries()
        for name in self.names:
            try:
                data = self.client.get_bytes("/server/files/" + name)
            except WorkerError as e:
                print("log keeper: %s" % e, file=sys.stderr)
                continue
            prev = [e for e in entries if e["name"] == name]
            digest = sha256(data)
            if prev and prev[-1]["sha256"] == digest:
                continue
            gap = bool(prev) and not _contains_seq(data, prev[-1]["tail"])
            now = self.clock()
            fname = "%s.%d.%s.gz" % (name.replace("/", "_"), int(now), digest[:8])
            write_private(os.path.join(self.dir, fname), gzip.compress(data))
            e = {"name": name, "file": fname, "taken_at": now, "size": len(data), "sha256": digest,
                 "first": not prev, "gap": gap, "tail": _tail_hashes(data), "pruned": False}
            entries.append(e)
            out.append(e)
        self._write([e for e in self._prune(entries) if not e["pruned"]])
        return out

    def _prune(self, entries: List[dict]) -> List[dict]:
        now = self.clock()
        newest = {}
        for e in entries:
            if not e["pruned"]:
                newest[e["name"]] = e
        live = [e for e in entries if not e["pruned"]]

        def drop(e):
            try:
                os.unlink(os.path.join(self.dir, e["file"]))
            except OSError:
                pass
            e["pruned"] = True

        for e in live:
            if newest[e["name"]] is not e and now - e["taken_at"] > self.max_age_s:
                drop(e)
        live = [e for e in entries if not e["pruned"]]
        total = 0
        for e in live:
            try:
                total += os.path.getsize(os.path.join(self.dir, e["file"]))
            except OSError:
                pass
        for e in sorted(live, key=lambda x: x["taken_at"]):
            if total <= self.max_bytes:
                break
            if newest[e["name"]] is e:
                continue
            try:
                total -= os.path.getsize(os.path.join(self.dir, e["file"]))
            except OSError:
                pass
            drop(e)
        return entries


# -- preparing a plan ----------------------------------------------------------------------------------------------------
def _safe_name(name: str) -> str:
    base = os.path.basename(name)
    return re.sub(r"[^A-Za-z0-9._-]", "_", base) or "job.gcode"


def _check_filename(name: str) -> None:
    if not name or name.startswith("/") or ".." in name.split("/") or "\\" in name or "\x00" in name:
        raise WorkerError("unsafe file name in the job history")


def _require_idle_ready(client) -> None:
    ps = _status(client, PRINT_STATS_PATH, "print_stats").get("state")
    if ps != "standby":
        raise WorkerError("print state is %r, not standby (a running, paused or just-finished job is refused)" % ps)
    try:
        state = client.get_json(INFO_PATH)["result"]["state"]
    except (KeyError, TypeError):
        raise WorkerError("unexpected Klippy info")
    if state != "ready":
        raise WorkerError("Klippy is %s, not ready" % state)


CLOCK_SKEW_S = 300.0   # the worker and the printer clocks are not synchronised: look for a baseline this much earlier


def _exit_count(text: str) -> int:
    return len(rj.EXIT_RE.findall(text))


def _pick_klippy_log(keeper: LogKeeper, job_start: float) -> dict:
    """The kept Klippy snapshot that holds THIS job's exit line. Klippy may append to its log across restarts, so an exit line is
    attributed to the job only if it is new since a snapshot taken before the job started, with no gap on the way."""
    ents = [e for e in keeper.entries() if e["name"] == KLIPPY_LOG and not e["pruned"]]
    base = [i for i, e in enumerate(ents) if e["taken_at"] <= job_start - CLOCK_SKEW_S]
    if not base:
        raise WorkerError("no kept Klippy snapshot from before the job started: an exit line cannot be attributed to this job")
    bi = base[-1]
    try:
        base_n = _exit_count(keeper.read(ents[bi]).decode("utf-8", "replace"))
    except (OSError, EOFError):
        raise WorkerError("the baseline Klippy snapshot from before the job started is unreadable")
    if any(e["gap"] for e in ents[bi + 1:]):
        raise WorkerError("a gap follows the baseline log snapshot: lines may be missing, refusing to guess")
    for i in range(len(ents) - 1, bi, -1):
        try:
            text = keeper.read(ents[i]).decode("utf-8", "replace")
        except (OSError, EOFError):
            continue
        if _exit_count(text) > base_n:
            return ents[i]
    raise WorkerError("no new 'Exiting SD card print' line since before the job started: the evidence of this stop is not in the kept logs")


def prepare(client, keeper: LogKeeper, jobs_dir: str, rate_bps: Optional[float], token: str, profile_path: str,
            probe_spot: Optional[str] = None, overlap_s: float = 3.0) -> str:
    if not rate_bps:
        raise WorkerError("need a file rate in bytes/s (--rate-bps or --polls)")
    _require_idle_ready(client)
    try:
        job = client.get_json(HISTORY_PATH)["result"]["jobs"][0]
        fname, status, job_id = job["filename"], job["status"], str(job["job_id"])
    except (KeyError, IndexError, TypeError):
        raise WorkerError("no job in the Moonraker history")
    try:
        job_start = float(job["start_time"])
        want_size = int(job["metadata"]["size"])
    except (KeyError, TypeError, ValueError):
        raise WorkerError("the history job has no start time or file size: the stop cannot be tied to this job")
    _check_filename(fname)
    if status not in ABNORMAL:
        raise WorkerError("the last job ended with status %r, not a stop that needs a rescue" % status)
    if _safe_name(fname).startswith("resume_"):
        raise WorkerError("the last job is itself a resume file: build this one by hand with --exit-file (runbook, step 3)")
    keeper.snapshot_once()   # newest evidence first; a failed fetch is tolerated, the kept snapshots are still used
    entry = _pick_klippy_log(keeper, job_start)
    gcode = client.get_bytes("/server/files/gcodes/" + urllib.parse.quote(fname))
    if len(gcode) != want_size:
        raise WorkerError("the G-code on the printer has size %d, the job that printed had size %d: it is not the file that printed" % (len(gcode), want_size))

    d = private_dir(os.path.join(jobs_dir, "%s-%s" % (re.sub(r"[^0-9A-Za-z]", "", job_id) or "x", sha256(token.encode())[:10])))
    local = _safe_name(fname)
    resume_name = "resume_" + local if local.endswith(".gcode") else "resume_" + local + ".gcode"
    gpath, lpath = os.path.join(d, local), os.path.join(d, "klippy.log")
    write_private(gpath, gcode)
    write_private(lpath, keeper.read(entry))
    try:
        jb, resume = rj.build_job(gpath, lpath, None, None, overlap_s, float(rate_bps), resume_name)
    except (rj.JobError, OSError) as e:
        raise WorkerError("job builder refused: %s" % e)
    jbytes = json.dumps(jb, indent=2).encode()
    rbytes = "\n".join(resume).encode("utf-8", "surrogateescape")
    write_private(os.path.join(d, "job.json"), jbytes)
    write_private(os.path.join(d, resume_name), rbytes)
    ev = private_dir(os.path.join(d, "evidence"))
    write_private(os.path.join(ev, "klippy.log.gz"), gzip.compress(keeper.read(entry)))
    for e in reversed(keeper.entries()):
        if e["name"] != KLIPPY_LOG and not e["pruned"]:
            write_private(os.path.join(ev, _safe_name(e["name"]) + ".gz"), gzip.compress(keeper.read(e)))
            break
    tag = "%s-%s" % (job_id, sha256(jbytes + b"\0" + rbytes)[:8])
    atomic_json(os.path.join(d, "worker.json"), {
        "history_job_id": job_id, "history_status": status, "token": token, "klippy_snapshot": entry["file"],
        "klippy_snapshot_taken_at": entry["taken_at"], "evidence": {"kernel_journal": False, "note": EVIDENCE_NOTE},
        "probe_spot": probe_spot})
    atomic_json(os.path.join(d, "PENDING.json"), {
        "tag": tag, "token": token, "job_sha256": sha256(jbytes), "resume_sha256": sha256(rbytes), "resume_file": resume_name})
    return d


def confirm(job_dir: str, typed: str, client, profile_path: str, probe_spot: Optional[str] = None) -> dict:
    """Check the typed phrase against the prepared job, then return the command a PERSON runs. Runs nothing."""
    if os.path.exists(os.path.join(job_dir, "CONFIRMED.json")):
        raise WorkerError("this job was already confirmed (single use): prepare a new request")
    try:
        with open(os.path.join(job_dir, "PENDING.json")) as f:
            p = json.load(f)
        with open(os.path.join(job_dir, "job.json"), "rb") as f:
            jbytes = f.read()
        with open(os.path.join(job_dir, os.path.basename(p["resume_file"])), "rb") as f:
            rbytes = f.read()
    except (OSError, ValueError, KeyError):
        raise WorkerError("no pending job here")
    if sha256(jbytes) != p["job_sha256"] or sha256(rbytes) != p["resume_sha256"]:
        raise WorkerError("the job or resume file changed since it was prepared: prepare it again")
    if typed != PHRASE % p["tag"]:
        raise WorkerError("the phrase does not match. Type exactly: %s" % (PHRASE % p["tag"]))
    _require_idle_ready(client)
    profile = mp.load_profile(profile_path)
    blocked = bool(mp.motion_ready(profile))
    cmd = "python3 print-rescue/rescue_run.py --host %s --profile print-rescue/profiles/creality-ender3-v3-se.json --job %s --probe-spot %s --dry-run" % (
        shlex.quote(str(getattr(client, "host", "<host>"))), shlex.quote(os.path.join(job_dir, "job.json")), shlex.quote(probe_spot or "<X,Y>"))
    notes = []
    if blocked:
        notes.append("the profile head box is still owner-stated (R13): rescue_run.py will refuse to move. Measure the head; "
                     "this worker never adds the override flag.")
    notes.append("upload the resume file to the printer yourself (runbook, step 3), run the dry run, then run without --dry-run "
                 "with a finger on the emergency stop.")
    try:   # O_EXCL: two concurrent confirms cannot both succeed
        fd = os.open(os.path.join(job_dir, "CONFIRMED.json"), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        raise WorkerError("this job was already confirmed (single use): prepare a new request")
    with os.fdopen(fd, "w") as f:
        json.dump({"tag": p["tag"], "confirmed_at": time.time(), "dry_run_command": cmd}, f, indent=1)
    return {"command": cmd, "notes": notes, "motion_blocked_by_profile": blocked}


def watch_once(client, state_path: str, keeper: LogKeeper, jobs_dir: str, rate_bps: Optional[float], profile_path: str,
               probe_spot: Optional[str] = None) -> Optional[str]:
    poller = Poller(client, state_path)
    token = poller.check()
    if token is None:
        return None
    try:
        d = prepare(client, keeper, jobs_dir, rate_bps, token, profile_path, probe_spot)
    except Exception as e:
        poller.mark_handled(token)   # no hot retry loop: the person presses the button again after fixing the cause
        if isinstance(e, WorkerError):
            raise
        raise WorkerError("unexpected %s while preparing" % type(e).__name__)
    poller.mark_handled(token)
    return d


# -- command line --------------------------------------------------------------------------------------------------------
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Prepare a print rescue on request; never move the machine.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--host", required=True, help="Moonraker host[:port]")
    common.add_argument("--key-file", help="Moonraker API key file, mode 600 (optional when trusted_clients covers this host)")
    common.add_argument("--profile", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "profiles",
                                                           "creality-ender3-v3-se.json"))
    common.add_argument("--probe-spot", help="nozzle X,Y of the bare-bed probe spot, only used in the printed command")
    r = sub.add_parser("run", parents=[common], help="keep logs and prepare plans on request")
    r.add_argument("--work-dir", required=True, help="private directory for logs/, jobs/ and state.json")
    r.add_argument("--rate-bps", type=float)
    r.add_argument("--polls", help="watcher poll CSV for the file rate")
    r.add_argument("--poll-interval", type=int, default=5)
    r.add_argument("--log-interval", type=int, default=300)
    r.add_argument("--max-log-mb", type=int, default=512)
    r.add_argument("--max-log-days", type=float, default=14)
    r.add_argument("--once", action="store_true")
    c = sub.add_parser("confirm", parents=[common], help="type the confirmation phrase for a prepared job")
    c.add_argument("--job-dir", required=True)
    a = ap.parse_args(argv)
    try:
        client = ReadOnlyClient(a.host, a.key_file)
        if a.cmd == "confirm":
            try:
                with open(os.path.join(a.job_dir, "PENDING.json")) as f:
                    tag = json.load(f)["tag"]
            except (OSError, ValueError, KeyError):
                raise WorkerError("no pending job here")
            print("Prepared job %s. Read worker.json and job.json first. Someone must be at the printer with a finger on the "
                  "emergency stop.\nType exactly: %s" % (tag, PHRASE % tag))
            out = confirm(a.job_dir, input("> ").strip("\n"), client, a.profile, a.probe_spot)
            print(json.dumps(out, indent=2))
            return 0
        work = private_dir(a.work_dir)
        keeper = LogKeeper(client, os.path.join(work, "logs"), max_bytes=a.max_log_mb * 1024 * 1024, max_age_s=a.max_log_days * 86400)
        jobs, state = private_dir(os.path.join(work, "jobs")), os.path.join(work, "state.json")
        last_keep = 0.0
        while True:
            if time.time() - last_keep >= a.log_interval:
                try:
                    keeper.snapshot_once()
                except OSError as e:   # a full disk must not stop the poller; it is reported and retried next interval
                    print("log keeper: %s" % type(e).__name__, file=sys.stderr, flush=True)
                last_keep = time.time()
            try:
                rate = rj.file_rate(a.polls) if a.polls and not a.rate_bps else a.rate_bps
                d = watch_once(client, state, keeper, jobs, rate, a.profile, a.probe_spot)
                if d:
                    print("PREPARED %s\nNothing has moved. Review it, then: rescue_worker.py confirm --host %s --job-dir %s" % (d, a.host, d), flush=True)
            except (WorkerError, rj.JobError, OSError) as e:
                print("REFUSED:", e, file=sys.stderr, flush=True)
            if a.once:
                return 0
            time.sleep(a.poll_interval)
    except WorkerError as e:
        print("REFUSED:", e, file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
