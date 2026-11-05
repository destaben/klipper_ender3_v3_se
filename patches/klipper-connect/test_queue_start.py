"""Tests for 0001-queue-start-when-printer-off.patch.

Run in the klipper-connect image, with the patched package on PYTHONPATH:
    python -m unittest test_queue_start -v
"""
import threading
import types
import unittest

from prusa.connect.printer.command import CommandFailed
from prusa.connect.printer.const import State

from klipper_connect.bridge import Bridge
from klipper_connect.moonraker import Moonraker, MoonrakerError


class FakeMoonraker(Moonraker):
    """Moonraker client whose HTTP layer is a scripted dict."""

    def __init__(self, klippy_state, power):
        super().__init__("http://moonraker.invalid:7125")
        self.klippy_state = klippy_state
        self.power = power  # "on", "off" or None (power API unavailable)
        self.calls = []

    def _request(self, method, path, timeout=None, **kwargs):
        self.calls.append((method, path))
        if path == "/server/info":
            return {"klippy_state": self.klippy_state}
        if path == "/machine/device_power/device":
            if self.power is None:
                raise MoonrakerError("GET ... -> 404: Device not found")
            return {kwargs["params"]["device"]: self.power}
        if path in ("/server/job_queue/job", "/printer/print/start"):
            return {}
        raise AssertionError("unexpected request: %s %s" % (method, path))

    def queued(self):
        return ("POST", "/server/job_queue/job") in self.calls


class PrinterIsOffTest(unittest.TestCase):
    def test_klippy_down_and_plug_off(self):
        self.assertTrue(FakeMoonraker("error", "off").printer_is_off())

    def test_klippy_ready_is_never_off(self):
        self.assertFalse(FakeMoonraker("ready", "off").printer_is_off())

    def test_plug_on_but_klippy_down_is_a_fault_not_off(self):
        # A printer that is on but faulty must not get a queued print that
        # would start unattended after the next restart.
        self.assertFalse(FakeMoonraker("shutdown", "on").printer_is_off())

    def test_unknown_power_state_is_not_off(self):
        self.assertFalse(FakeMoonraker("error", None).printer_is_off())

    def test_unreachable_moonraker_is_not_off(self):
        mr = FakeMoonraker("error", "off")

        def boom(*args, **kwargs):
            raise MoonrakerError("Moonraker unreachable")
        mr._request = boom
        self.assertFalse(mr.printer_is_off())


class OffStateTest(unittest.TestCase):
    """The state reported to Connect while the printer is powered off."""

    def make(self, klippy_state, power):
        br = Bridge.__new__(Bridge)
        br.mr = FakeMoonraker(klippy_state, power)
        return br

    def test_error_becomes_idle_when_printer_is_off(self):
        self.assertEqual(self.make("error", "off")._off_state(State.ERROR),
                         State.IDLE)

    def test_error_stays_error_when_plug_is_on(self):
        # A real fault with the printer on must stay visible in Connect.
        self.assertEqual(self.make("shutdown", "on")._off_state(State.ERROR),
                         State.ERROR)

    def test_other_states_are_untouched(self):
        br = self.make("error", "off")
        for state in (State.BUSY, State.READY, State.PRINTING, State.IDLE):
            self.assertEqual(br._off_state(state), state)
        # and no HTTP request was needed for them
        self.assertEqual(br.mr.calls, [])

    def test_polling_does_not_query_the_plug_every_second(self):
        br = self.make("error", "off")
        for _ in range(10):
            br._off_state(State.ERROR)
        self.assertEqual(br.mr.calls.count(("GET", "/server/info")), 1)
        self.assertEqual(
            br.mr.calls.count(("GET", "/machine/device_power/device")), 1)

    def test_start_always_asks_fresh(self):
        br = self.make("error", "off")
        br._off_state(State.ERROR)           # fills the cache
        br.mr.power = "on"                    # plug switched on meanwhile
        self.assertFalse(br.mr.printer_is_off())


class StartTest(unittest.TestCase):
    """Bridge._start, built without __init__ so no network or SDK is needed."""

    def make(self, bridge_state, klippy_state, power, file_exists=True):
        br = Bridge.__new__(Bridge)
        br.mr = FakeMoonraker(klippy_state, power)
        br.printer = types.SimpleNamespace(state=bridge_state)
        br.lock = threading.Lock()
        br.pending_start_cmd_id = None
        br.os_path = lambda name: __file__ if file_exists else "/nonexistent"
        return br

    def test_printer_off_queues_instead_of_rejecting(self):
        br = self.make(State.ERROR, "error", "off")
        br._start("a.gcode", 7)
        self.assertTrue(br.mr.queued())
        self.assertNotIn(("POST", "/printer/print/start"), br.mr.calls)
        self.assertEqual(br.pending_start_cmd_id, 7)

    def test_failed_queue_does_not_leave_a_pending_command(self):
        br = self.make(State.ERROR, "error", "off")

        def boom(filename):
            raise MoonrakerError("POST /server/job_queue/job -> 500")
        br.mr.queue_print = boom
        with self.assertRaises(MoonrakerError):
            br._start("a.gcode", 7)
        self.assertIsNone(br.pending_start_cmd_id)

    def test_error_with_plug_on_is_still_rejected(self):
        br = self.make(State.ERROR, "shutdown", "on")
        with self.assertRaises(CommandFailed):
            br._start("a.gcode", 7)
        self.assertFalse(br.mr.queued())

    def test_normal_start_is_untouched(self):
        br = self.make(State.IDLE, "ready", "on")
        br._start("a.gcode", 7)
        self.assertIn(("POST", "/printer/print/start"), br.mr.calls)
        self.assertFalse(br.mr.queued())

    def test_missing_file_is_rejected_even_when_off(self):
        br = self.make(State.ERROR, "error", "off", file_exists=False)
        with self.assertRaises(CommandFailed):
            br._start("a.gcode", 7)
        self.assertFalse(br.mr.queued())


if __name__ == "__main__":
    unittest.main()
