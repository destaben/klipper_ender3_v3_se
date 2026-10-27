#!/usr/bin/env python3
# Poll a Klippy API socket until the printer reports ready or the timeout ends.
#
# Usage: check_ready.py <api-socket> <timeout-seconds> [klippy-pid]
# Exit status 0 when klippy_state is "ready", 1 otherwise. With a pid, a
# Klippy process that exits first fails the check at once.

import json
import socket
import sys
import time


def call(path, method, params=None):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(5)
        sock.connect(path)
        request = {"id": 1, "method": method}
        if params is not None:
            request["params"] = params
        sock.sendall(json.dumps(request).encode() + b"\x03")
        buf = b""
        while not buf.endswith(b"\x03"):
            chunk = sock.recv(4096)
            if not chunk:
                raise OSError("API socket closed")
            buf += chunk
    reply = json.loads(buf[:-1])
    if "error" in reply:
        error = reply["error"]
        message = error.get("message") if isinstance(error, dict) else error
        raise ValueError("API error: %s" % str(message or "unknown").strip())
    if "result" not in reply:
        raise ValueError("API reply has no result: %r" % reply)
    return reply["result"]


def query_state(path):
    return call(path, "info")


def process_running(pid):
    # A dead child stays a zombie until its parent reaps it, and kill(pid, 0)
    # still succeeds for a zombie, so read the state from /proc instead
    try:
        with open("/proc/%d/stat" % pid) as f:
            stat = f.read()
    except OSError:
        return False
    state = stat.rsplit(")", 1)[1].split()[0]
    return state not in ("Z", "X")


def main():
    path, timeout = sys.argv[1], float(sys.argv[2])
    pid = int(sys.argv[3]) if len(sys.argv) > 3 else None
    deadline = time.monotonic() + timeout
    state = None
    while time.monotonic() < deadline:
        if pid is not None and not process_running(pid):
            print("klippy process %d exited before reporting ready" % pid,
                  flush=True)
            return 1
        try:
            info = query_state(path)
            state = info.get("state")
            message = (info.get("state_message") or "").replace("\n", " ")
            print("klippy_state: %s | %s" % (state, message[:200]), flush=True)
            if state == "ready":
                return 0
            if state in ("error", "shutdown"):
                return 1
        except (OSError, ValueError) as e:
            print("waiting for API: %s" % e, flush=True)
        time.sleep(2)
    print("timed out; last klippy_state: %s" % state, flush=True)
    return 1


if __name__ == "__main__":
    sys.exit(main())
