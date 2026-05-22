"""Prometheus exporter for Klipper extruder TMC2209 motor load.

Exposes the StallGuard result (SG_RESULT) from the extruder TMC2209 driver
as a Prometheus gauge metric. This enables long-term recording of motor load
data to determine reliable thresholds for automatic jam detection.

Usage:
    python extruder_load_exporter.py [--moonraker-url URL] [--port PORT] [--interval SECONDS]

Environment variables (override defaults):
    MOONRAKER_URL   - Moonraker API base URL (default: http://localhost:7125)
    EXPORTER_PORT   - Port to expose metrics on (default: 9101)
    POLL_INTERVAL   - Seconds between polls (default: 2)
"""

import logging
import os
import signal
import sys
import time
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.request import urlopen, Request
from urllib.error import URLError
import json

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)

MOONRAKER_URL = os.environ.get("MOONRAKER_URL", "http://localhost:7125")
EXPORTER_PORT = int(os.environ.get("EXPORTER_PORT", "9101"))
POLL_INTERVAL = float(os.environ.get("POLL_INTERVAL", "2"))

# Metric state
_sg_result: float = -1.0
_extruder_target_temp: float = 0.0
_extruder_actual_temp: float = 0.0
_poll_errors: int = 0
_poll_successes: int = 0


def query_moonraker(endpoint: str) -> dict:
    """Query the Moonraker API and return JSON response."""
    url = f"{MOONRAKER_URL}{endpoint}"
    req = Request(url, headers={"Accept": "application/json"})
    with urlopen(req, timeout=5) as resp:
        return json.loads(resp.read().decode())


def poll_tmc_status() -> None:
    """Poll TMC2209 extruder driver status from Moonraker."""
    global _sg_result, _extruder_target_temp, _extruder_actual_temp
    global _poll_errors, _poll_successes

    try:
        # Query TMC driver status for the extruder
        data = query_moonraker(
            "/printer/objects/query?tmc2209+extruder&extruder"
        )
        result = data.get("result", {}).get("status", {})

        tmc_status = result.get("tmc2209 extruder", {})
        sg = tmc_status.get("drv_status", {}).get("sg_result")
        if sg is not None:
            _sg_result = float(sg)

        extruder_status = result.get("extruder", {})
        _extruder_target_temp = float(extruder_status.get("target", 0))
        _extruder_actual_temp = float(extruder_status.get("temperature", 0))

        _poll_successes += 1
    except (URLError, OSError, ValueError, KeyError) as exc:
        _poll_errors += 1
        logger.warning("Poll failed: %s", exc)


def generate_metrics() -> str:
    """Generate Prometheus exposition format metrics."""
    lines = []

    lines.append(
        "# HELP klipper_extruder_sg_result "
        "TMC2209 StallGuard result for extruder (0=high load, 510=no load)"
    )
    lines.append("# TYPE klipper_extruder_sg_result gauge")
    lines.append(f"klipper_extruder_sg_result {_sg_result}")

    lines.append(
        "# HELP klipper_extruder_temperature_celsius "
        "Current extruder temperature in Celsius"
    )
    lines.append("# TYPE klipper_extruder_temperature_celsius gauge")
    lines.append(
        f"klipper_extruder_temperature_celsius {_extruder_actual_temp}"
    )

    lines.append(
        "# HELP klipper_extruder_target_temperature_celsius "
        "Target extruder temperature in Celsius"
    )
    lines.append("# TYPE klipper_extruder_target_temperature_celsius gauge")
    lines.append(
        f"klipper_extruder_target_temperature_celsius {_extruder_target_temp}"
    )

    lines.append(
        "# HELP klipper_extruder_exporter_poll_errors_total "
        "Total number of failed polls to Moonraker"
    )
    lines.append("# TYPE klipper_extruder_exporter_poll_errors_total counter")
    lines.append(f"klipper_extruder_exporter_poll_errors_total {_poll_errors}")

    lines.append(
        "# HELP klipper_extruder_exporter_poll_successes_total "
        "Total number of successful polls to Moonraker"
    )
    lines.append(
        "# TYPE klipper_extruder_exporter_poll_successes_total counter"
    )
    lines.append(
        f"klipper_extruder_exporter_poll_successes_total {_poll_successes}"
    )

    lines.append("")
    return "\n".join(lines)


class MetricsHandler(BaseHTTPRequestHandler):
    """HTTP handler that serves Prometheus metrics."""

    def do_GET(self):  # noqa: N802
        if self.path == "/metrics":
            body = generate_metrics().encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        """Suppress default HTTP log noise."""
        pass


def main() -> None:
    """Main entry point: start HTTP server and polling loop."""
    logger.info(
        "Starting extruder load exporter — "
        "Moonraker: %s, Port: %d, Interval: %.1fs",
        MOONRAKER_URL,
        EXPORTER_PORT,
        POLL_INTERVAL,
    )

    server = HTTPServer(("0.0.0.0", EXPORTER_PORT), MetricsHandler)

    # Handle graceful shutdown
    def shutdown_handler(signum, frame):
        logger.info("Shutting down...")
        server.shutdown()
        sys.exit(0)

    signal.signal(signal.SIGTERM, shutdown_handler)
    signal.signal(signal.SIGINT, shutdown_handler)

    # Run HTTP server in a background thread
    import threading

    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()

    logger.info("Metrics available at http://0.0.0.0:%d/metrics", EXPORTER_PORT)

    # Polling loop
    while True:
        poll_tmc_status()
        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
