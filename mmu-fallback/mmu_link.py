#!/usr/bin/env python3
"""Keep /run/klipper/mmu pointing at the real Pico MMU, or at a virtual MCU.

Klipper cannot start without every configured MCU, and it cannot hot-swap one.
This watcher keeps a stable serial path for [mcu PICO_MMU] and, when the choice
between the real and the virtual MCU changes, asks Moonraker for exactly one
firmware restart, but only when that cannot disturb a print.

The decision logic (decide) is pure and has no I/O, so it is unit tested; the
rest of the file is a thin loop around it. Standard library only.
"""

import json
import os
import stat
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Optional

REAL = "real"
VIRTUAL = "virtual"
ACTIVE_PRINT_STATES = ("printing", "paused")
# Klippy states in which a restart is acceptable. "startup" and anything
# unknown (Moonraker down, query failed) mean: wait and look again.
RESTARTABLE_ERROR_STATES = ("error", "shutdown")

TEMP_SENSOR_REAL = (
    "[temperature_sensor Pico_MMU]\n"
    "sensor_type: temperature_mcu\n"
    "sensor_mcu: PICO_MMU\n"
    "min_temp: 0\n"
    "max_temp: 100\n"
)
TEMP_SENSOR_VIRTUAL = (
    "# Virtual MMU: the simulated MCU has no temperature sensor.\n"
)


@dataclass
class Config:
    mode: str = "real"            # real | auto | sim
    present_debounce: float = 3.0   # real device must be present this long
    absent_debounce: float = 10.0   # ...and absent this long, to go virtual
    restart_cooldown: float = 60.0  # minimum time between restarts
    quiet_after_restart: float = 20.0  # ignore flips while the Pico re-enumerates
    retry_interval: float = 5.0     # retry a failed restart request


@dataclass
class State:
    linked: Optional[str] = None
    candidate: Optional[str] = None
    candidate_since: float = 0.0
    print_latched: bool = False
    last_restart: Optional[float] = None
    last_attempt: Optional[float] = None
    restart_pending: bool = False


@dataclass
class Obs:
    real_present: bool
    klippy_state: Optional[str] = None
    print_state: Optional[str] = None
    heaters_off: Optional[bool] = None
    virtual_ready: bool = True  # the virtual MCU's pty exists


@dataclass
class Action:
    link: Optional[str] = None
    restart: bool = False


def desired_mode(cfg, obs):
    if cfg.mode == "real":
        return REAL
    if cfg.mode == "sim":
        return VIRTUAL
    return REAL if obs.real_present else VIRTUAL


def safe_to_restart(st, obs):
    """True when a restart cannot hurt a print or a hot printer."""
    if st.print_latched:
        return False
    if obs.klippy_state in RESTARTABLE_ERROR_STATES:
        return True
    if obs.klippy_state != "ready":
        return False
    if obs.print_state is None or obs.print_state in ACTIVE_PRINT_STATES:
        return False
    return obs.heaters_off is True


def update_latch(st, obs):
    # A print that was running when the MCU vanished leaves Klippy in an error
    # state, where print_stats is unreadable. Remember it so that a dropped
    # real MCU is never replaced by a virtual one mid-print; the latch clears
    # only once Klippy is ready again and reports no active print.
    if obs.print_state in ACTIVE_PRINT_STATES:
        st.print_latched = True
    elif obs.klippy_state == "ready" and obs.print_state is not None:
        st.print_latched = False


def decide(cfg, st, obs, now):
    """Advance the state for one observation and return what to do."""
    update_latch(st, obs)
    want = desired_mode(cfg, obs)
    if want == VIRTUAL and not obs.virtual_ready:
        return Action()

    if st.linked is None:
        # First run: nothing is linked yet, so Klippy cannot be running
        # against a stale link. Link without a restart.
        st.linked = want
        return Action(link=want)

    if want == st.linked:
        st.candidate = None
        if (st.restart_pending and safe_to_restart(st, obs)
                and (st.last_attempt is None
                     or now - st.last_attempt >= cfg.retry_interval)):
            st.last_attempt = now
            return Action(restart=True)
        return Action()

    if (st.last_restart is not None
            and now - st.last_restart < cfg.quiet_after_restart):
        st.candidate = None
        return Action()

    if st.candidate != want:
        st.candidate = want
        st.candidate_since = now
    wait = cfg.present_debounce if want == REAL else cfg.absent_debounce
    if now - st.candidate_since < wait:
        return Action()
    if not safe_to_restart(st, obs):
        return Action()
    if st.last_restart is not None and now - st.last_restart < cfg.restart_cooldown:
        return Action()

    st.linked = want
    st.candidate = None
    st.last_restart = now
    st.last_attempt = now
    st.restart_pending = True
    return Action(link=want, restart=True)


def restart_done(st):
    st.restart_pending = False


# --- I/O -------------------------------------------------------------------


def log(msg):
    print(time.strftime("%H:%M:%S ") + msg, flush=True)


def real_present(by_id_dir, name):
    try:
        return stat.S_ISCHR(os.stat(os.path.join(by_id_dir, name)).st_mode)
    except OSError:
        return False


def atomic_symlink(target, path):
    tmp = path + ".tmp"
    try:
        os.unlink(tmp)
    except FileNotFoundError:
        pass
    os.symlink(target, tmp)
    os.replace(tmp, path)


def atomic_write(text, path):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        f.write(text)
    os.replace(tmp, path)


def apply_link(link_dir, by_id_dir, real_name, mode):
    # The temperature include goes first: the serial link is what Klippy
    # reads to connect, so by then the matching config must already be there.
    if mode == REAL:
        atomic_write(TEMP_SENSOR_REAL, os.path.join(link_dir, "mmu_temp.cfg"))
        atomic_symlink(os.path.join(by_id_dir, real_name),
                       os.path.join(link_dir, "mmu"))
    else:
        atomic_write(TEMP_SENSOR_VIRTUAL, os.path.join(link_dir, "mmu_temp.cfg"))
        atomic_symlink(os.path.join(link_dir, "virtual-mmu"),
                       os.path.join(link_dir, "mmu"))


def http(url, method="GET", timeout=5):
    req = urllib.request.Request(url, method=method,
                                 data=b"" if method == "POST" else None)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.load(resp)


def observe(moonraker, present):
    obs = Obs(real_present=present)
    try:
        info = http(moonraker + "/server/info")
        obs.klippy_state = info["result"]["klippy_state"]
        if obs.klippy_state != "ready":
            return obs
        q = http(moonraker + "/printer/objects/query"
                 "?print_stats&extruder&heater_bed")
        status = q["result"]["status"]
        obs.print_state = status["print_stats"]["state"]
        obs.heaters_off = (status["extruder"]["target"] == 0
                           and status["heater_bed"]["target"] == 0)
    except (OSError, ValueError, KeyError):
        # Unreachable or malformed: leave the fields unknown, which blocks.
        obs.print_state = None
        obs.heaters_off = None
    return obs


def request_restart(moonraker):
    try:
        http(moonraker + "/printer/firmware_restart", method="POST", timeout=10)
        return True
    except (OSError, ValueError):
        return False


class VirtualMcu:
    def __init__(self, elf, path):
        self.elf, self.path, self.proc = elf, path, None

    def ensure_running(self):
        if self.proc is not None and self.proc.poll() is None:
            return
        if self.proc is not None:
            log("virtual MCU exited (%s), respawning" % self.proc.returncode)
        try:
            os.unlink(self.path)
        except FileNotFoundError:
            pass
        self.proc = subprocess.Popen([self.elf, "-I", self.path])

    def ready(self):
        return os.path.exists(self.path)


def env_float(name, default):
    return float(os.environ.get(name, default))


def main():
    mode = os.environ.get("PRINTER_MODE", "real")
    if mode not in ("real", "auto", "sim"):
        sys.exit("PRINTER_MODE must be real, auto or sim, got %r" % mode)
    cfg = Config(
        mode=mode,
        present_debounce=env_float("PRESENT_DEBOUNCE", 3),
        absent_debounce=env_float("ABSENT_DEBOUNCE", 10),
        restart_cooldown=env_float("RESTART_COOLDOWN", 60),
        quiet_after_restart=env_float("QUIET_AFTER_RESTART", 20),
    )
    by_id_dir = os.environ.get("BY_ID_DIR", "/dev/serial/by-id")
    real_name = os.environ.get(
        "MMU_REAL_BY_ID",
        "usb-Klipper_stm32g0b1xx_5A00320017504D4636383420-if00")
    link_dir = os.environ.get("LINK_DIR", "/run/klipper")
    moonraker = os.environ.get("MOONRAKER_URL", "http://moonraker:7125")
    poll = env_float("POLL_INTERVAL", 1)
    elf = os.environ.get("VIRTUAL_MCU_ELF", "/opt/mmu-fallback/klipper.elf")

    virtual = VirtualMcu(elf, os.path.join(link_dir, "virtual-mmu"))
    st = State()
    log("mode=%s link_dir=%s" % (mode, link_dir))
    while True:
        if mode != "real":
            virtual.ensure_running()
        obs = observe(moonraker, real_present(by_id_dir, real_name))
        obs.virtual_ready = virtual.ready()
        action = decide(cfg, st, obs, time.monotonic())
        if action.link:
            apply_link(link_dir, by_id_dir, real_name, action.link)
            log("link -> %s (klippy=%s print=%s)"
                % (action.link, obs.klippy_state, obs.print_state))
        if action.restart:
            if request_restart(moonraker):
                restart_done(st)
                log("firmware_restart requested")
            else:
                log("firmware_restart failed, will retry")
        time.sleep(poll)


if __name__ == "__main__":
    main()
