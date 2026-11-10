import gzip
import io
import json
import os
import re
import stat
import tempfile
import threading
import unittest
import http.server
import urllib.error

import rescue_worker as rw
from test_rescue_job import synth_gcode

HERE = os.path.dirname(os.path.abspath(__file__))
MACRO_CFG = os.path.join(HERE, "printer-macro", "rescue_request.cfg")
PROFILE = os.path.join(HERE, "profiles", "creality-ender3-v3-se.json")
MACRO_PATH = "/printer/objects/query?gcode_macro%20RESCUE_REQUEST&print_stats"


def macro_status(seq, stamp, printing=False, state=None):
    return {"result": {"status": {
        "gcode_macro RESCUE_REQUEST": {"seq": seq, "stamp": stamp},
        "print_stats": {"state": state or ("printing" if printing else "standby")}}}}


class FakeClient:
    """Stands in for ReadOnlyClient: routes map a path to JSON (dict) or bytes. Only get_* exist, like the real one."""

    def __init__(self, routes=None):
        self.routes = dict(routes or {})
        self.calls = []

    def _get(self, path):
        self.calls.append(path)
        if path not in self.routes:
            raise rw.WorkerError("HTTP 404 for %s" % path)
        v = self.routes[path]
        return v() if callable(v) else v

    def get_json(self, path):
        return self._get(path)

    def get_bytes(self, path):
        v = self._get(path)
        return v if isinstance(v, bytes) else json.dumps(v).encode()


def klippy_log(extra=""):
    return ("Stats 100.0: gcodein=0\n"
            "gcode state: absolute_coord=True last_position=[20.0, 20.0, 0.8, 5.0]\n"
            "Exiting SD card print (position %d)\n"
            "Lost communication with MCU 'mcu'\n" % EXIT_POS) + extra


def make_gcode_bytes():
    return "\n".join(synth_gcode()).encode()


def exit_pos_of(lines):
    idx = max(i for i, l in enumerate(lines) if l.startswith("G1 X20 Y20 Z"))
    return len(("\n".join(lines[:idx + 1]) + "\n").encode())


EXIT_POS = exit_pos_of(synth_gcode())
HIST = {"result": {"jobs": [{"job_id": "000042", "filename": "part.gcode", "status": "klippy_shutdown",
                             "start_time": 3000.0, "end_time": 4000.0, "metadata": {"size": len(make_gcode_bytes()), "modified": 1700000000.5}}]}}


def hist(**kw):
    j = dict(HIST["result"]["jobs"][0])
    for k, v in kw.items():
        if v is None:
            j.pop(k, None)
        else:
            j[k] = v
    return {"result": {"jobs": [j]}}
LOGS = ["logs/klippy.log", "logs/moonraker.log"]


class TestMacro(unittest.TestCase):
    """The Klipper side is request-only: it must not be able to move or heat anything."""

    def setUp(self):
        with open(MACRO_CFG) as f:
            self.text = f.read()
        self.body = "\n".join(l for l in self.text.splitlines() if not l.strip().startswith("#"))

    def test_macro_exists_and_is_named(self):
        self.assertIn("[gcode_macro RESCUE_REQUEST]", self.body)

    def test_macro_has_no_motion_heating_or_restart_commands(self):
        banned = r"\b(G0|G1|G2|G3|G28|G29|G91|G90|M104|M109|M140|M190|M106|M84|M112|M999|PROBE|BED_MESH\w*|"
        banned += r"SET_KINEMATIC_POSITION|SET_HEATER_TEMPERATURE|STEPPER_\w+|FIRMWARE_RESTART|RESTART|"
        banned += r"SAVE_CONFIG|SET_IDLE_TIMEOUT|BLTOUCH\w*|QUERY_PROBE|SDCARD_\w+|CANCEL_PRINT|RESUME|PAUSE|"
        banned += r"RUN_SHELL_COMMAND|CANCEL_PRINTER_SHUTDOWN)\b"
        self.assertIsNone(re.search(banned, self.body), re.search(banned, self.body))

    def test_macro_only_records_through_its_own_variables(self):
        for m in re.finditer(r"SET_GCODE_VARIABLE\s+(.*)", self.body):
            self.assertIn("MACRO=RESCUE_REQUEST", m.group(1))
        self.assertIn("variable_seq", self.body)
        self.assertIn("variable_stamp", self.body)

    def test_macro_accepts_a_request_only_when_standby(self):
        self.assertIn('printer.print_stats.state != "standby"', self.body)
        self.assertNotIn('== "printing"', self.body)

    def test_macro_is_not_enabled_by_default(self):
        # printer.cfg includes macros/*.cfg; the opt-in file must not live there and must not be picked up by that glob
        with open(os.path.join(HERE, "..", "config", "printer.cfg")) as f:
            includes = [l.strip() for l in f if l.startswith("[include")]
        self.assertIn("[include macros/*.cfg]", includes)
        self.assertFalse(os.path.exists(os.path.join(HERE, "..", "config", "macros", "rescue_request.cfg")))
        self.assertFalse(any("rescue" in i or "printer-macro" in i for i in includes))
        self.assertNotIn(os.sep + "macros" + os.sep, MACRO_CFG)


def _resolver(*addrs):
    return lambda host, port, *a, **k: [(2, 1, 6, "", (x, 0)) for x in addrs]


KEYOK = {"accept_plaintext_key": True, "resolver": _resolver("192.168.1.5")}


class TestKeyOverPlainHttp(unittest.TestCase):
    """Moonraker speaks plain HTTP: the key is sent in the clear, so it is only allowed on an explicit opt-in to a private address."""

    def key(self):
        d = tempfile.mkdtemp()
        k = os.path.join(d, "key")
        with open(k, "w") as f:
            f.write("sekret-key")
        os.chmod(k, 0o600)
        return k

    def test_key_is_refused_without_the_explicit_opt_in(self):
        with self.assertRaises(rw.WorkerError) as cm:
            rw.ReadOnlyClient("h", key_file=self.key(), resolver=_resolver("192.168.1.5"))
        self.assertIn("plain HTTP", str(cm.exception))

    def test_key_is_refused_for_a_public_or_mixed_address_even_with_the_opt_in(self):
        for addrs in (("8.8.8.8",), ("192.168.1.5", "93.184.216.34"), ("2606:4700::1111",)):
            with self.assertRaises(rw.WorkerError, msg=addrs):
                rw.ReadOnlyClient("h", key_file=self.key(), accept_plaintext_key=True, resolver=_resolver(*addrs))

    def test_key_is_allowed_on_private_loopback_and_link_local_with_the_opt_in(self):
        for addrs in (("192.168.1.5",), ("10.0.0.2",), ("127.0.0.1",), ("169.254.1.1",), ("fe80::1",)):
            rw.ReadOnlyClient("h", key_file=self.key(), accept_plaintext_key=True, resolver=_resolver(*addrs))

    def test_unresolvable_host_with_a_key_is_refused(self):
        def nope(*a, **k):
            raise OSError("no")
        with self.assertRaises(rw.WorkerError):
            rw.ReadOnlyClient("h", key_file=self.key(), accept_plaintext_key=True, resolver=nope)

    def test_no_key_needs_no_opt_in_and_no_resolution(self):
        def boom(*a, **k):
            raise AssertionError("must not resolve")
        rw.ReadOnlyClient("h", resolver=boom)


class TestReadOnlyClient(unittest.TestCase):
    def _opener(self, record, body=b"{}"):
        def opener(req, timeout=None):
            record.append((req.get_method(), req.full_url, dict(req.header_items())))
            return io.BytesIO(body)
        return opener

    def test_only_get_is_ever_sent(self):
        rec = []
        c = rw.ReadOnlyClient("host:7125", opener=self._opener(rec, b'{"a": 1}'))
        c.get_json("/printer/info")
        c.get_bytes("/server/files/logs/klippy.log")
        self.assertEqual({m for m, _, _ in rec}, {"GET"})
        self.assertFalse(any(hasattr(c, n) for n in ("post", "put", "delete", "patch", "gcode", "send", "request")))

    def test_path_must_be_absolute_and_stay_on_the_host(self):
        c = rw.ReadOnlyClient("host:7125", opener=self._opener([]))
        for bad in ("http://evil/x", "//evil/x", "printer/info", "/printer/gcode/script?script=M112",
                    "/server/files/config/printer.cfg", "/server/files/config/moonraker.secrets", "/server/files/gcodes/%2e%2e/x",
                    "/server/files/logs/%2E%2E/config/printer.cfg"):
            with self.assertRaises(rw.WorkerError):
                c.get_json(bad)

    def test_key_file_must_be_private_and_exist(self):
        d = tempfile.mkdtemp()
        with self.assertRaises(rw.WorkerError) as cm:
            rw.ReadOnlyClient("h", key_file=os.path.join(d, "missing"), opener=self._opener([]), **KEYOK)
        self.assertIn("key file", str(cm.exception))
        k = os.path.join(d, "key")
        with open(k, "w") as f:
            f.write("sekret-key\n")
        os.chmod(k, 0o644)
        with self.assertRaises(rw.WorkerError) as cm:
            rw.ReadOnlyClient("h", key_file=k, opener=self._opener([]), **KEYOK)
        self.assertNotIn("sekret-key", str(cm.exception))
        os.chmod(k, 0o600)
        rec = []
        rw.ReadOnlyClient("h", key_file=k, opener=self._opener(rec, b"{}"), **KEYOK).get_json("/printer/info")
        self.assertEqual(rec[0][2].get("X-api-key"), "sekret-key")

    def test_key_never_appears_in_errors(self):
        d = tempfile.mkdtemp()
        k = os.path.join(d, "key")
        with open(k, "w") as f:
            f.write("sekret-key")
        os.chmod(k, 0o600)

        def boom(req, timeout=None):
            raise urllib.error.URLError("sekret-key refused")
        with self.assertRaises(rw.WorkerError) as cm:
            rw.ReadOnlyClient("h", key_file=k, opener=boom, **KEYOK).get_json("/printer/info")
        self.assertNotIn("sekret-key", str(cm.exception))


class TestNoRedirect(unittest.TestCase):
    def test_a_redirect_is_never_followed_so_the_key_cannot_travel(self):
        seen = []

        class Target(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                seen.append(dict(self.headers))
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"{}")

            def log_message(self, *a):
                pass
        target = http.server.HTTPServer(("127.0.0.1", 0), Target)

        class Redirector(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(302)
                self.send_header("Location", "http://127.0.0.1:%d/printer/info" % target.server_port)
                self.end_headers()

            def log_message(self, *a):
                pass
        red = http.server.HTTPServer(("127.0.0.1", 0), Redirector)
        for srv in (target, red):
            threading.Thread(target=srv.handle_request, daemon=True).start()
        d = tempfile.mkdtemp()
        k = os.path.join(d, "key")
        with open(k, "w") as f:
            f.write("sekret-key")
        os.chmod(k, 0o600)
        try:
            with self.assertRaises(rw.WorkerError) as cm:
                rw.ReadOnlyClient("127.0.0.1:%d" % red.server_port, key_file=k, accept_plaintext_key=True).get_json("/printer/info")
            self.assertIn("302", str(cm.exception))
            self.assertEqual(seen, [])
        finally:
            red.server_close()
            target.server_close()


class TestPoller(unittest.TestCase):
    def setUp(self):
        self.state = os.path.join(tempfile.mkdtemp(), "state.json")

    def poll(self, status):
        c = FakeClient({MACRO_PATH: status})
        return rw.Poller(c, self.state).check()

    def test_no_request_when_seq_is_zero(self):
        self.assertIsNone(self.poll(macro_status(0, 0.0)))

    def test_new_request_is_seen_once_and_survives_a_worker_restart(self):
        tok = self.poll(macro_status(1, 100.5))
        self.assertEqual(tok, "1@100.5")
        self.assertEqual(self.poll(macro_status(1, 100.5)), tok)   # not handled yet: still pending
        rw.Poller(FakeClient(), self.state).mark_handled(tok)
        self.assertIsNone(self.poll(macro_status(1, 100.5)))        # handled, even in a fresh Poller (daily restart)

    def test_klippy_restart_resets_the_counter_and_a_new_press_is_still_seen(self):
        tok = self.poll(macro_status(1, 100.5))
        rw.Poller(FakeClient(), self.state).mark_handled(tok)
        self.assertIsNone(self.poll(macro_status(0, 0.0)))          # restart
        self.assertEqual(self.poll(macro_status(1, 7.25)), "1@7.25")  # same seq number, different stamp

    def test_a_running_print_never_yields_a_request(self):
        self.assertIsNone(self.poll(macro_status(3, 9.0, printing=True)))

    def test_any_state_but_standby_never_yields_a_request(self):
        for st in ("paused", "error", "cancelled", "complete", "printing"):
            self.assertIsNone(self.poll(macro_status(3, 9.0, state=st)), st)

    def test_corrupt_state_file_means_ask_again_not_crash(self):
        with open(self.state, "w") as f:
            f.write("{not json")
        self.assertEqual(self.poll(macro_status(2, 1.0)), "2@1.0")

    def test_klippy_down_is_not_a_request(self):
        c = FakeClient()  # route missing -> WorkerError
        self.assertIsNone(rw.Poller(c, self.state).check())


class TestLogKeeper(unittest.TestCase):
    def setUp(self):
        self.store = tempfile.mkdtemp()
        self.now = [1000.0]
        self.logs = {"logs/klippy.log": b"a\nb\nc\nd\n", "logs/moonraker.log": b"m1\nm2\n"}
        self.client = FakeClient()
        self._wire()

    def _wire(self):
        for name in LOGS:
            self.client.routes["/server/files/" + name] = (lambda n=name: self.logs[n])

    def keeper(self, **kw):
        return rw.LogKeeper(self.client, self.store, LOGS, clock=lambda: self.now[0], **kw)

    def manifest(self):
        with open(os.path.join(self.store, "manifest.jsonl")) as f:
            return [json.loads(l) for l in f if l.strip()]

    def test_first_snapshot_is_recorded_and_readable(self):
        e = self.keeper().snapshot_once()
        self.assertEqual(len(e), 2)
        m = [x for x in self.manifest() if x["name"] == "logs/klippy.log"][0]
        self.assertTrue(m["first"])
        self.assertFalse(m["gap"])
        with gzip.open(os.path.join(self.store, m["file"])) as f:
            self.assertEqual(f.read(), b"a\nb\nc\nd\n")

    def test_overlapping_growth_is_not_a_gap(self):
        k = self.keeper()
        k.snapshot_once()
        self.now[0] += 300
        self.logs["logs/klippy.log"] += b"e\nf\n"
        k.snapshot_once()
        last = [x for x in self.manifest() if x["name"] == "logs/klippy.log"][-1]
        self.assertFalse(last["gap"])

    def test_rotation_that_loses_the_previous_tail_is_flagged_as_a_gap(self):
        k = self.keeper()
        k.snapshot_once()
        self.now[0] += 7200
        self.logs["logs/klippy.log"] = b"x\ny\nz\n"   # rotated: old tail gone
        k.snapshot_once()
        last = [x for x in self.manifest() if x["name"] == "logs/klippy.log"][-1]
        self.assertTrue(last["gap"])

    def test_gap_is_detected_across_a_worker_restart(self):
        self.keeper().snapshot_once()
        self.now[0] += 86400
        self.logs["logs/klippy.log"] = b"q\nr\n"
        self.keeper().snapshot_once()   # a brand-new keeper reads the manifest from disk
        last = [x for x in self.manifest() if x["name"] == "logs/klippy.log"][-1]
        self.assertTrue(last["gap"])

    def test_two_snapshots_in_the_same_second_do_not_overwrite_each_other(self):
        k = self.keeper()
        k.snapshot_once()
        self.logs["logs/klippy.log"] = b"restarted\nfresh\n"   # rotated within the same wall-clock second
        k.snapshot_once()
        ents = [e for e in self.manifest() if e["name"] == "logs/klippy.log"]
        self.assertEqual(len(ents), 2)
        self.assertNotEqual(ents[0]["file"], ents[1]["file"])
        self.assertEqual(k.read(ents[0]), b"a\nb\nc\nd\n")
        self.assertEqual(k.read(ents[1]), b"restarted\nfresh\n")

    def test_unchanged_log_is_not_stored_twice(self):
        k = self.keeper()
        k.snapshot_once()
        self.now[0] += 300
        self.assertEqual(k.snapshot_once(), [])

    def test_size_cap_prunes_oldest_but_keeps_the_newest_of_each_log(self):
        k = self.keeper(max_bytes=1)   # absurdly small: everything but the newest must go
        for i in range(4):
            self.now[0] += 300
            self.logs["logs/klippy.log"] += b"line%d\n" % i
            k.snapshot_once()
        files = [f for f in os.listdir(self.store) if f.endswith(".gz")]
        klippy = [f for f in files if "klippy" in f]
        self.assertEqual(len(klippy), 1)
        with gzip.open(os.path.join(self.store, klippy[0])) as fh:
            self.assertTrue(fh.read().endswith(b"line3\n"))

    def test_age_cap_prunes_old_snapshots(self):
        k = self.keeper(max_age_s=1000)
        k.snapshot_once()
        self.now[0] += 5000
        self.logs["logs/klippy.log"] += b"new\n"
        k.snapshot_once()
        klippy = [f for f in os.listdir(self.store) if f.endswith(".gz") and "klippy" in f]
        self.assertEqual(len(klippy), 1)

    def test_a_failing_fetch_does_not_stop_the_other_log(self):
        del self.client.routes["/server/files/logs/klippy.log"]
        e = self.keeper().snapshot_once()
        self.assertEqual([x["name"] for x in e], ["logs/moonraker.log"])

    def test_one_corrupt_manifest_line_does_not_orphan_the_other_snapshots(self):
        k = self.keeper()
        k.snapshot_once()
        with open(os.path.join(self.store, "manifest.jsonl"), "a") as f:
            f.write("{broken\n")
        self.now[0] += 300
        self.logs["logs/klippy.log"] += b"e\n"
        self.keeper().snapshot_once()
        names = [e["file"] for e in self.manifest()]
        self.assertEqual(len(names), 3)   # 2 first snapshots + the new klippy one; the corrupt line was dropped, nothing lost

    def test_pruned_entries_leave_the_manifest(self):
        k = self.keeper(max_bytes=1)
        for i in range(4):
            self.now[0] += 300
            self.logs["logs/klippy.log"] += b"line%d\n" % i
            k.snapshot_once()
        self.assertTrue(all(not e["pruned"] for e in self.manifest()))

    def test_rotation_right_after_an_empty_snapshot_is_flagged_as_a_gap(self):
        self.logs["logs/klippy.log"] = b""
        k = self.keeper()
        k.snapshot_once()
        self.now[0] += 300
        self.logs["logs/klippy.log"] = b"later\nlines\n"
        k.snapshot_once()
        last = [x for x in self.manifest() if x["name"] == "logs/klippy.log"][-1]
        self.assertTrue(last["gap"])   # an empty snapshot proves nothing about what was written before the next one

    def test_manifest_never_holds_log_content(self):
        self.keeper().snapshot_once()
        with open(os.path.join(self.store, "manifest.jsonl")) as f:
            self.assertNotIn("m1", f.read())


class PrepareBase(unittest.TestCase):
    def setUp(self):
        self.work = tempfile.mkdtemp()
        self.store = os.path.join(self.work, "store")
        self.jobs = os.path.join(self.work, "jobs")
        self.now = [1000.0]
        self.log = b"Stats 100.0: gcodein=0\n"   # before the job started: no exit line yet
        self.client = FakeClient({
            "/server/history/list?limit=1": HIST,
            "/server/files/gcodes/part.gcode": make_gcode_bytes(),
            "/server/files/metadata?filename=part.gcode": {"result": {"modified": 1700000000.5}},
            "/server/files/logs/klippy.log": lambda: self.log,
            "/server/files/logs/moonraker.log": b"m\n",
            "/printer/objects/query?print_stats": {"result": {"status": {"print_stats": {"state": "standby"}}}},
            "/printer/info": {"result": {"state": "ready"}},
        })
        self.keeper().snapshot_once()              # the baseline the keeper took before the job started (t=1000, job starts at 3000)
        self.now[0] = 5000.0
        self.log = klippy_log().encode()

    def keeper(self):
        return rw.LogKeeper(self.client, self.store, LOGS, clock=lambda: self.now[0])

    def prepare(self, **kw):
        args = dict(client=self.client, keeper=self.keeper(), jobs_dir=self.jobs, rate_bps=40.0, token="1@5.0",
                    profile_path=PROFILE, probe_spot="134,215")
        args.update(kw)
        return rw.prepare(**args)


class TestPrepare(PrepareBase):
    def test_prepare_writes_job_resume_pending_and_evidence(self):
        d = self.prepare()
        for f in ("job.json", "PENDING.json", "worker.json", "evidence/klippy.log.gz"):
            self.assertTrue(os.path.exists(os.path.join(d, f)), f)
        job = json.load(open(os.path.join(d, "job.json")))
        self.assertTrue(os.path.exists(os.path.join(d, job["resume_file"])))
        self.assertEqual(job["stop"]["exit_pos"], EXIT_POS)
        meta = json.load(open(os.path.join(d, "worker.json")))
        self.assertIn("no kernel journal", meta["evidence"]["note"])
        self.assertEqual(meta["history_job_id"], "000042")

    def test_prepare_sends_nothing_but_gets(self):
        self.prepare()
        self.assertTrue(all(p.startswith("/") for p in self.client.calls))
        self.assertFalse(any("gcode/script" in p or "print/start" in p or "emergency" in p for p in self.client.calls))

    def test_prepare_does_not_create_a_confirmation(self):
        d = self.prepare()
        self.assertFalse(os.path.exists(os.path.join(d, "CONFIRMED.json")))

    def test_job_that_ended_normally_is_refused(self):
        self.client.routes["/server/history/list?limit=1"] = {"result": {"jobs": [dict(HIST["result"]["jobs"][0], status="completed")]}}
        with self.assertRaises(rw.WorkerError) as cm:
            self.prepare()
        self.assertIn("completed", str(cm.exception))

    def test_a_running_print_is_refused(self):
        self.client.routes["/printer/objects/query?print_stats"] = {"result": {"status": {"print_stats": {"state": "printing"}}}}
        with self.assertRaises(rw.WorkerError):
            self.prepare()

    def test_every_state_but_standby_is_refused(self):
        for st in ("paused", "error", "cancelled", "complete", "printing"):
            self.client.routes["/printer/objects/query?print_stats"] = {"result": {"status": {"print_stats": {"state": st}}}}
            with self.assertRaises(rw.WorkerError, msg=st):
                self.prepare()

    def test_job_dirs_of_tokens_that_differ_only_in_punctuation_do_not_collide(self):
        d1 = self.prepare(token="1@50.0")
        d2 = self.prepare(token="15@0.0")
        self.assertNotEqual(d1, d2)
        self.assertTrue(os.path.exists(os.path.join(d1, "PENDING.json")))

    def test_resume_file_job_is_refused_for_manual_handling(self):
        self.client.routes["/server/history/list?limit=1"] = {"result": {"jobs": [dict(HIST["result"]["jobs"][0], filename="resume_part.gcode")]}}
        with self.assertRaises(rw.WorkerError) as cm:
            self.prepare()
        self.assertIn("resume", str(cm.exception))

    def test_unsafe_history_filename_is_refused(self):
        for bad in ("../../etc/passwd", "/abs.gcode", "a/../../b.gcode"):
            self.client.routes["/server/history/list?limit=1"] = {"result": {"jobs": [dict(HIST["result"]["jobs"][0], filename=bad)]}}
            with self.assertRaises(rw.WorkerError):
                self.prepare()

    def test_log_without_an_exit_line_is_refused_not_guessed(self):
        self.log = b"Stats 100.0: gcodein=0\nStats 1.0: nothing\n"
        with self.assertRaises(rw.WorkerError) as cm:
            self.prepare()
        self.assertIn("Exiting SD card print", str(cm.exception))

    def test_kept_snapshot_with_the_exit_line_is_used_when_the_live_log_cannot_be_fetched(self):
        k = self.keeper()
        k.snapshot_once()                       # holds the exit line
        self.now[0] += 300
        self.log = self.log + b"after\n"       # grows, overlaps, no gap
        k.snapshot_once()
        del self.client.routes["/server/files/logs/klippy.log"]
        d = self.prepare()
        self.assertTrue(os.path.exists(os.path.join(d, "job.json")))

    def test_gap_after_the_snapshot_with_the_exit_line_is_refused(self):
        k = self.keeper()
        k.snapshot_once()
        self.now[0] += 86400
        self.log = b"Stats 5.0: fresh\nmore\n"   # old tail lost: gap recorded
        k.snapshot_once()
        with self.assertRaises(rw.WorkerError) as cm:
            self.prepare()
        self.assertIn("gap", str(cm.exception))

    def test_exit_line_that_was_already_in_the_log_before_the_job_started_is_refused(self):
        # job B has no exit line of its own (power cut); the log still holds job A's: building B's resume at A's offset is wrong
        self.log = klippy_log().encode()
        self.now[0] = 2000.0
        self.keeper().snapshot_once()             # a later baseline that already holds A's exit line, still before B (start 3000)
        self.now[0] = 5000.0
        self.log = self.log + b"Stats 900.0: more\n"
        with self.assertRaises(rw.WorkerError) as cm:
            self.prepare()
        self.assertIn("before the job started", str(cm.exception))

    def test_no_snapshot_from_before_the_job_means_the_exit_line_cannot_be_attributed(self):
        self.client.routes["/server/history/list?limit=1"] = hist(start_time=500.0)   # keeper started at 1000, after this job began
        with self.assertRaises(rw.WorkerError) as cm:
            self.prepare()
        self.assertIn("before the job started", str(cm.exception))

    def test_history_without_a_start_time_is_refused(self):
        self.client.routes["/server/history/list?limit=1"] = hist(start_time=None)
        with self.assertRaises(rw.WorkerError):
            self.prepare()

    def test_gcode_file_that_is_not_the_one_that_printed_is_refused(self):
        self.client.routes["/server/files/gcodes/part.gcode"] = make_gcode_bytes() + b"; re-uploaded\n"
        with self.assertRaises(rw.WorkerError) as cm:
            self.prepare()
        self.assertIn("size", str(cm.exception))

    def test_same_length_reupload_with_a_later_modification_time_is_refused(self):
        same_size = make_gcode_bytes().replace(b"G1 X20", b"G1 X21", 1)
        self.assertEqual(len(same_size), len(make_gcode_bytes()))
        self.client.routes["/server/files/gcodes/part.gcode"] = same_size
        self.client.routes["/server/files/metadata?filename=part.gcode"] = {"result": {"modified": 1700009999.0}}
        with self.assertRaises(rw.WorkerError) as cm:
            self.prepare()
        self.assertIn("modified", str(cm.exception))

    def test_unreadable_file_modification_time_is_refused(self):
        del self.client.routes["/server/files/metadata?filename=part.gcode"]
        with self.assertRaises(rw.WorkerError):
            self.prepare()

    def test_history_without_a_modification_time_is_refused(self):
        self.client.routes["/server/history/list?limit=1"] = hist(metadata={"size": len(make_gcode_bytes())})
        with self.assertRaises(rw.WorkerError):
            self.prepare()

    def test_history_without_a_file_size_is_refused(self):
        self.client.routes["/server/history/list?limit=1"] = hist(metadata={})
        with self.assertRaises(rw.WorkerError):
            self.prepare()

    def test_gcode_named_like_the_log_is_not_overwritten_by_it(self):
        gc = make_gcode_bytes()
        self.client.routes["/server/history/list?limit=1"] = hist(filename="klippy.log")
        self.client.routes["/server/files/gcodes/klippy.log"] = gc
        self.client.routes["/server/files/metadata?filename=klippy.log"] = {"result": {"modified": 1700000000.5}}
        d = self.prepare()
        with open(os.path.join(d, "klippy.log"), "rb") as f:
            self.assertEqual(f.read(), gc)

    def test_non_dict_print_stats_is_a_refusal_not_a_traceback(self):
        self.client.routes["/printer/objects/query?print_stats"] = {"result": {"status": {"print_stats": "oops"}}}
        with self.assertRaises(rw.WorkerError):
            self.prepare()

    def test_missing_rate_is_refused(self):
        with self.assertRaises(rw.WorkerError):
            self.prepare(rate_bps=None)

    def test_pending_tag_binds_job_id_and_job_file_hash(self):
        d = self.prepare()
        p = json.load(open(os.path.join(d, "PENDING.json")))
        self.assertTrue(p["tag"].startswith("000042-"))
        self.assertEqual(p["token"], "1@5.0")


class TestConfirm(PrepareBase):
    def setUp(self):
        super().setUp()
        self.dir = self.prepare()
        self.tag = json.load(open(os.path.join(self.dir, "PENDING.json")))["tag"]
        self.good = "CONFIRM %s AT PRINTER" % self.tag

    def confirm(self, typed, **kw):
        return rw.confirm(self.dir, typed, client=self.client, profile_path=PROFILE, probe_spot="134,215", **kw)

    def test_exact_phrase_confirms_and_prints_a_dry_run_command_only(self):
        out = self.confirm(self.good)
        self.assertTrue(os.path.exists(os.path.join(self.dir, "CONFIRMED.json")))
        self.assertIn("rescue_run.py", out["command"])
        self.assertIn("--dry-run", out["command"])
        self.assertNotIn("--allow-unverified", out["command"])

    def test_printed_command_is_shell_quoted(self):
        import shlex
        out = rw.confirm(self.dir, self.good, client=self.client, profile_path=PROFILE, probe_spot="1,2; touch pwned")
        parts = shlex.split(out["command"])
        self.assertIn("1,2; touch pwned", parts)
        self.assertEqual(parts.count("--dry-run"), 1)

    def test_wrong_phrases_are_refused(self):
        for bad in ("", "yes", "CONFIRM", "confirm %s at printer" % self.tag, "CONFIRM 000041-deadbeef AT PRINTER",
                    self.good + " ", "CONFIRM %s" % self.tag):
            with self.assertRaises(rw.WorkerError, msg=repr(bad)):
                self.confirm(bad)
        self.assertFalse(os.path.exists(os.path.join(self.dir, "CONFIRMED.json")))

    def test_edited_job_file_invalidates_the_confirmation(self):
        with open(os.path.join(self.dir, "job.json"), "a") as f:
            f.write(" ")
        with self.assertRaises(rw.WorkerError) as cm:
            self.confirm(self.good)
        self.assertIn("changed", str(cm.exception))

    def test_edited_resume_file_invalidates_the_confirmation(self):
        job = json.load(open(os.path.join(self.dir, "job.json")))
        with open(os.path.join(self.dir, job["resume_file"]), "a") as f:
            f.write("G1 Z0\n")
        with self.assertRaises(rw.WorkerError):
            self.confirm(self.good)

    def test_confirmation_is_refused_if_a_print_started_since(self):
        self.client.routes["/printer/objects/query?print_stats"] = {"result": {"status": {"print_stats": {"state": "printing"}}}}
        with self.assertRaises(rw.WorkerError):
            self.confirm(self.good)

    def test_confirmation_is_refused_in_every_state_but_standby(self):
        for st in ("paused", "error", "cancelled", "complete"):
            self.client.routes["/printer/objects/query?print_stats"] = {"result": {"status": {"print_stats": {"state": st}}}}
            with self.assertRaises(rw.WorkerError, msg=st):
                self.confirm(self.good)
        self.assertFalse(os.path.exists(os.path.join(self.dir, "CONFIRMED.json")))

    def test_confirmation_is_refused_if_klippy_is_not_ready(self):
        self.client.routes["/printer/info"] = {"result": {"state": "shutdown"}}
        with self.assertRaises(rw.WorkerError):
            self.confirm(self.good)

    def test_confirmation_is_single_use(self):
        self.confirm(self.good)
        with self.assertRaises(rw.WorkerError):
            self.confirm(self.good)

    def test_unverified_profile_is_reported_and_flag_is_never_added(self):
        out = self.confirm(self.good)
        self.assertTrue(out["motion_blocked_by_profile"])   # R13: the shipped profile is owner-stated
        self.assertIn("refuse", out["notes"][0])
        self.assertNotIn("allow-unverified", json.dumps(out))

    def test_worker_module_has_no_way_to_execute_the_runner(self):
        src = open(os.path.join(HERE, "rescue_worker.py")).read()
        code = "\n".join(l for l in src.splitlines() if not l.strip().startswith("#"))
        for banned in ("subprocess", "os.system", "os.exec", "Popen", "import rescue_run", "from rescue_run", "runpy", "importlib",
                       "__import__", "eval(", "exec(", "os.spawn", "os.popen"):
            self.assertNotIn(banned, code)
        import ast
        imported = set()
        for n in ast.walk(ast.parse(src)):
            if isinstance(n, ast.Import):
                imported |= {a.name.split(".")[0] for a in n.names}
            elif isinstance(n, ast.ImportFrom):
                imported.add((n.module or "").split(".")[0])
        self.assertTrue(imported <= {"argparse", "gzip", "hashlib", "ipaddress", "json", "os", "re", "shlex", "socket", "sys", "time", "urllib",
                                     "typing", "machine_profile", "rescue_job"}, imported)
        self.assertNotIn("allow-unverified", src)


class TestLoop(PrepareBase):
    def test_watch_once_prepares_then_marks_handled(self):
        st = os.path.join(self.work, "state.json")
        self.client.routes[MACRO_PATH] = macro_status(1, 5.0)
        r = rw.watch_once(self.client, st, self.keeper(), self.jobs, 40.0, PROFILE, "134,215")
        self.assertTrue(os.path.isdir(r))
        self.assertIsNone(rw.watch_once(self.client, st, self.keeper(), self.jobs, 40.0, PROFILE, "134,215"))

    def test_an_already_handled_token_is_reported_once_not_dropped_silently(self):
        import contextlib, io
        rw._SKIP_REPORTED.clear()
        st = os.path.join(self.work, "state.json")
        self.client.routes[MACRO_PATH] = macro_status(1, 5.0)
        rw.watch_once(self.client, st, self.keeper(), self.jobs, 40.0, PROFILE, "134,215")
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertIsNone(rw.watch_once(self.client, st, self.keeper(), self.jobs, 40.0, PROFILE, "134,215"))
            self.assertIsNone(rw.watch_once(self.client, st, self.keeper(), self.jobs, 40.0, PROFILE, "134,215"))
        self.assertEqual(err.getvalue().count("SKIPPED request"), 1)

    def test_printed_command_uses_the_profile_that_was_checked(self):
        d = self.prepare()
        out = rw.confirm(d, rw.PHRASE % json.load(open(os.path.join(d, "PENDING.json")))["tag"], self.client, PROFILE, "134,215")
        self.assertIn("--profile " + PROFILE, out["command"])

    def test_failed_prepare_is_remembered_so_it_does_not_loop_hot(self):
        st = os.path.join(self.work, "state.json")
        self.client.routes[MACRO_PATH] = macro_status(1, 5.0)
        self.log = b"nothing useful\n"
        with self.assertRaises(rw.WorkerError):
            rw.watch_once(self.client, st, self.keeper(), self.jobs, 40.0, PROFILE, "134,215")
        self.assertIsNone(rw.watch_once(self.client, st, self.keeper(), self.jobs, 40.0, PROFILE, "134,215"))


class TestUnexpectedFailures(PrepareBase):
    def test_an_unexpected_exception_in_prepare_is_a_refusal_and_is_remembered(self):
        st = os.path.join(self.work, "state.json")
        self.client.routes[MACRO_PATH] = macro_status(1, 5.0)
        orig = rw.rj.build_job
        rw.rj.build_job = lambda *a, **k: (_ for _ in ()).throw(KeyError("boom"))
        try:
            with self.assertRaises(rw.WorkerError):
                rw.watch_once(self.client, st, self.keeper(), self.jobs, 40.0, PROFILE, "134,215")
        finally:
            rw.rj.build_job = orig
        self.assertIsNone(rw.watch_once(self.client, st, self.keeper(), self.jobs, 40.0, PROFILE, "134,215"))


class TestMainErrors(unittest.TestCase):
    def test_run_without_a_file_rate_is_refused_at_startup_and_consumes_no_request(self):
        self.assertEqual(rw.main(["run", "--host", "h", "--work-dir", os.path.join(tempfile.mkdtemp(), "w"), "--once"]), 2)

    def test_existing_work_dir_permissions_are_not_changed(self):
        d = tempfile.mkdtemp()
        os.chmod(d, 0o755)
        rw.private_dir(d)
        self.assertEqual(stat.S_IMODE(os.stat(d).st_mode), 0o755)

    def test_confirm_on_a_directory_without_a_pending_job_is_a_refusal_not_a_traceback(self):
        self.assertEqual(rw.main(["confirm", "--host", "h", "--job-dir", tempfile.mkdtemp()]), 2)


class TestMainPermissions(unittest.TestCase):
    def test_private_dirs_are_created_0700(self):
        d = os.path.join(tempfile.mkdtemp(), "x")
        rw.private_dir(d)
        self.assertEqual(stat.S_IMODE(os.stat(d).st_mode), 0o700)


if __name__ == "__main__":
    unittest.main()
