#!/usr/bin/env python3
# Poll a Klippy API socket until the printer reports ready or the timeout ends.
#
# Usage: check_ready.py <api-socket> <timeout-seconds>
# Exit status 0 when klippy_state is "ready", 1 otherwise.

import json
import socket
import sys
import time


def query_state(path):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(5)
        sock.connect(path)
        request = {"id": 1, "method": "info"}
        sock.sendall(json.dumps(request).encode() + b"\x03")
        buf = b""
        while not buf.endswith(b"\x03"):
            chunk = sock.recv(4096)
            if not chunk:
                raise OSError("API socket closed")
            buf += chunk
    return json.loads(buf[:-1])["result"]


def main():
    path, timeout = sys.argv[1], float(sys.argv[2])
    deadline = time.monotonic() + timeout
    state = None
    while time.monotonic() < deadline:
        try:
            info = query_state(path)
            state = info.get("state")
            message = info.get("state_message", "").replace("\n", " ")
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
