#!/usr/bin/env python3
"""
Print Watcher — AI-based print failure detection using a Tapo RTSP camera and
the Obico ML API running locally.

Captures periodic snapshots from the camera stream, sends them to the Obico ML
API for spaghetti / failure detection, and optionally pauses the print via
Moonraker when repeated failures are detected.

Environment variables
---------------------
MOONRAKER_URL           Moonraker API base URL          (default: http://moonraker:7125)
OBICO_ML_API_URL        Obico ML API base URL           (default: http://obico-ml-api:3333)
TAPO_RTSP_URL           Full RTSP URL of the Tapo camera
                        e.g. rtsp://admin:secret@192.168.1.100/stream1
TAPO_SNAPSHOT_URL       HTTP snapshot URL (used when TAPO_RTSP_URL is not set)
                        e.g. http://192.168.1.100/snapshot.jpg
DETECTION_INTERVAL      Seconds between detection runs  (default: 10)
CONFIDENCE_THRESHOLD    Failure score threshold, 0–1    (default: 0.3)
ENABLE_AUTO_PAUSE       Pause print on repeated failures (default: false)
"""

import logging
import os
import subprocess
import sys
import time

import requests

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)

MOONRAKER_URL = os.environ.get("MOONRAKER_URL", "http://moonraker:7125")
OBICO_ML_API_URL = os.environ.get("OBICO_ML_API_URL", "http://obico-ml-api:3333")
RTSP_URL = os.environ.get("TAPO_RTSP_URL", "")
SNAPSHOT_URL = os.environ.get("TAPO_SNAPSHOT_URL", "")
try:
    DETECTION_INTERVAL = int(os.environ.get("DETECTION_INTERVAL", "10"))
except ValueError:
    logger.warning("Invalid DETECTION_INTERVAL value; using default of 10 seconds")
    DETECTION_INTERVAL = 10

try:
    CONFIDENCE_THRESHOLD = float(os.environ.get("CONFIDENCE_THRESHOLD", "0.3"))
except ValueError:
    logger.warning("Invalid CONFIDENCE_THRESHOLD value; using default of 0.3")
    CONFIDENCE_THRESHOLD = 0.3
ENABLE_AUTO_PAUSE = os.environ.get("ENABLE_AUTO_PAUSE", "false").lower() == "true"

# Number of consecutive failures required before an auto-pause is triggered
CONSECUTIVE_FAILURES_BEFORE_PAUSE = 2


def capture_snapshot_rtsp(rtsp_url):
    """Return a JPEG frame captured from an RTSP stream via ffmpeg, or None on error."""
    try:
        result = subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-rtsp_transport", "tcp",
                "-i", rtsp_url,
                "-frames:v", "1",
                "-f", "image2",
                "-vcodec", "mjpeg",
                "pipe:1",
            ],
            capture_output=True,
            timeout=15,
        )
        if result.returncode == 0 and result.stdout:
            return result.stdout
        logger.error(
            "ffmpeg exited with code %d: %s",
            result.returncode,
            result.stderr.decode(errors="replace")[-500:],
        )
    except subprocess.TimeoutExpired:
        logger.error("ffmpeg timed out while capturing snapshot from RTSP stream")
    except FileNotFoundError:
        logger.error("ffmpeg not found; ensure it is installed in the container")
    return None


def capture_snapshot_http(snapshot_url):
    """Download a JPEG snapshot from an HTTP endpoint, or return None on error."""
    try:
        resp = requests.get(snapshot_url, timeout=10)
        resp.raise_for_status()
        return resp.content
    except requests.RequestException as exc:
        logger.error("Failed to fetch HTTP snapshot: %s", exc)
    return None


def analyze_frame(image_bytes):
    """Send a JPEG frame to the Obico ML API and return the parsed JSON response."""
    try:
        resp = requests.post(
            f"{OBICO_ML_API_URL}/p",
            files={"pic": ("snapshot.jpg", image_bytes, "image/jpeg")},
            timeout=30,
        )
        resp.raise_for_status()
        return resp.json()
    except requests.RequestException as exc:
        logger.error("Obico ML API request failed: %s", exc)
    return None


def get_printer_state():
    """Return the current Klipper printer state string, or None on error."""
    try:
        resp = requests.get(f"{MOONRAKER_URL}/printer/info", timeout=5)
        resp.raise_for_status()
        return resp.json().get("result", {}).get("state")
    except requests.RequestException as exc:
        logger.debug("Could not retrieve printer state: %s", exc)
    return None


def pause_print():
    """Send a pause command to Moonraker. Returns True on success."""
    try:
        resp = requests.post(f"{MOONRAKER_URL}/printer/print/pause", timeout=5)
        resp.raise_for_status()
        logger.info("Print paused via Moonraker")
        return True
    except requests.RequestException as exc:
        logger.error("Failed to pause print: %s", exc)
    return False


def extract_failure_score(result):
    """Return the highest failure probability (0–1) from an Obico ML API response."""
    if "normalized_p" in result:
        return float(result["normalized_p"])
    detections = result.get("detections", [])
    if not detections:
        return 0.0
    scores = []
    for d in detections:
        if len(d) >= 5:
            try:
                scores.append(float(d[4]))
            except (ValueError, TypeError):
                logger.warning("Skipping detection with non-numeric score: %s", d[4])
    return max(scores) if scores else 0.0


def main():
    if not RTSP_URL and not SNAPSHOT_URL:
        logger.error(
            "No camera source configured. "
            "Set TAPO_RTSP_URL or TAPO_SNAPSHOT_URL in the environment."
        )
        sys.exit(1)

    camera_label = RTSP_URL if RTSP_URL else SNAPSHOT_URL
    logger.info("Print Watcher starting")
    logger.info("  Camera:     %s", camera_label)
    logger.info("  ML API:     %s", OBICO_ML_API_URL)
    logger.info("  Interval:   %ds", DETECTION_INTERVAL)
    logger.info("  Threshold:  %.2f", CONFIDENCE_THRESHOLD)
    logger.info("  Auto-pause: %s", ENABLE_AUTO_PAUSE)

    consecutive_failures = 0

    while True:
        state = get_printer_state()
        if state != "printing":
            logger.debug("Printer not printing (state=%s); skipping analysis", state)
            consecutive_failures = 0
            time.sleep(DETECTION_INTERVAL)
            continue

        image_bytes = (
            capture_snapshot_rtsp(RTSP_URL) if RTSP_URL
            else capture_snapshot_http(SNAPSHOT_URL)
        )
        if not image_bytes:
            logger.warning("Snapshot capture failed; retrying in %ds", DETECTION_INTERVAL)
            time.sleep(DETECTION_INTERVAL)
            continue

        result = analyze_frame(image_bytes)
        if result is None:
            time.sleep(DETECTION_INTERVAL)
            continue

        score = extract_failure_score(result)
        logger.info("Failure score: %.4f (threshold: %.2f)", score, CONFIDENCE_THRESHOLD)

        if score >= CONFIDENCE_THRESHOLD:
            consecutive_failures += 1
            logger.warning(
                "Possible print failure detected — score=%.4f, consecutive=%d",
                score,
                consecutive_failures,
            )
            if ENABLE_AUTO_PAUSE and consecutive_failures >= CONSECUTIVE_FAILURES_BEFORE_PAUSE:
                logger.warning(
                    "Pausing print after %d consecutive failures", consecutive_failures
                )
                pause_print()
                consecutive_failures = 0
        else:
            if consecutive_failures > 0:
                logger.info("Score below threshold; resetting failure counter")
            consecutive_failures = 0

        time.sleep(DETECTION_INTERVAL)


if __name__ == "__main__":
    main()
