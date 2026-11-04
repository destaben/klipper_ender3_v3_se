import os
import tempfile
import unittest

import mmu_link as m
from mmu_link import Action, Config, Obs, State, decide

CFG = Config(mode="auto")


def idle(present, klippy="ready"):
    return Obs(real_present=present, klippy_state=klippy,
               print_state="standby", heaters_off=True)


def run(cfg, st, obs, start, end, step=1.0, request_ok=True):
    """Feed one observation per step; return the non-empty actions.

    A restart request succeeds unless request_ok is False, as the I/O loop
    would report it.
    """
    out, t = [], start
    while t <= end:
        a = decide(cfg, st, obs, t)
        if a.link or a.restart:
            out.append((t, a))
        if a.restart and request_ok:
            m.restart_done(st)
        t += step
    return out


def started(present, cfg=CFG):
    st = State()
    decide(cfg, st, idle(present), 0)
    return st


class InitialLink(unittest.TestCase):
    def test_first_run_links_without_restart(self):
        st = State()
        self.assertEqual(decide(CFG, st, idle(False, klippy=None), 0),
                         Action(link=m.VIRTUAL))
        self.assertEqual(st.linked, m.VIRTUAL)

    def test_first_run_real(self):
        self.assertEqual(decide(CFG, State(), idle(True), 0),
                         Action(link=m.REAL))

    def test_waits_for_virtual_pty(self):
        st = State()
        obs = idle(False)
        obs.virtual_ready = False
        self.assertEqual(decide(CFG, st, obs, 0), Action())
        self.assertIsNone(st.linked)


class Debounce(unittest.TestCase):
    def test_short_absence_is_ignored(self):
        st = started(True)
        self.assertEqual(run(CFG, st, idle(False), 100, 105), [])
        self.assertEqual(run(CFG, st, idle(True), 106, 130), [])
        self.assertEqual(st.linked, m.REAL)

    def test_flapping_never_acts(self):
        st = started(True)
        t = 100
        for _ in range(10):
            self.assertEqual(run(CFG, st, idle(False), t, t + 5), [])
            self.assertEqual(run(CFG, st, idle(True), t + 6, t + 7), [])
            t += 8

    def test_absence_goes_virtual_after_debounce_with_one_restart(self):
        st = started(True)
        acts = run(CFG, st, idle(False), 100, 160)
        self.assertEqual(len(acts), 1)
        t, a = acts[0]
        self.assertEqual(a, Action(link=m.VIRTUAL, restart=True))
        self.assertGreaterEqual(t - 100, CFG.absent_debounce)

    def test_presence_goes_real_after_shorter_debounce(self):
        st = started(False)
        acts = run(CFG, st, idle(True), 100, 160)
        self.assertEqual(len(acts), 1)
        self.assertEqual(acts[0][1], Action(link=m.REAL, restart=True))
        self.assertLess(acts[0][0] - 100, CFG.absent_debounce)

    def test_reenumeration_after_own_restart_is_not_an_unplug(self):
        st = started(True)
        run(CFG, st, idle(False), 100, 111)      # goes virtual at ~110
        # The Pico shows up, vanishes briefly (reset), shows up again.
        self.assertEqual(run(CFG, st, idle(True), 112, 114), [])
        self.assertEqual(run(CFG, st, idle(False), 115, 125), [])
        self.assertEqual(st.linked, m.VIRTUAL)


class Safety(unittest.TestCase):
    def blocked(self, obs, present=False):
        st = started(True)
        self.assertEqual(run(CFG, st, obs, 100, 200), [])
        self.assertEqual(st.linked, m.REAL)

    def test_printing_blocks(self):
        self.blocked(Obs(False, "ready", "printing", True))

    def test_paused_blocks(self):
        self.blocked(Obs(False, "ready", "paused", True))

    def test_hot_heaters_block(self):
        self.blocked(Obs(False, "ready", "standby", False))

    def test_unknown_state_blocks(self):
        self.blocked(Obs(False, None, None, None))
        self.blocked(Obs(False, "startup", None, None))
        self.blocked(Obs(False, "ready", None, None))

    def test_applies_after_print_ends(self):
        st = started(True)
        self.assertEqual(run(CFG, st, Obs(False, "ready", "printing", True),
                             100, 130), [])
        acts = run(CFG, st, idle(False), 131, 135)
        self.assertEqual(acts[0][1], Action(link=m.VIRTUAL, restart=True))

    def test_error_state_allows_when_no_print_was_active(self):
        st = started(False)
        acts = run(CFG, st, Obs(True, "error"), 100, 110)
        self.assertEqual(acts[0][1], Action(link=m.REAL, restart=True))

    def test_mcu_lost_mid_print_does_not_fall_back_to_virtual(self):
        st = started(True)
        decide(CFG, st, Obs(True, "ready", "printing", True), 100)
        # USB drops: Klippy shuts down, print_stats is unreadable.
        self.assertEqual(run(CFG, st, Obs(False, "shutdown"), 101, 300), [])
        self.assertEqual(st.linked, m.REAL)

    def test_latch_clears_once_ready_and_idle(self):
        st = started(True)
        decide(CFG, st, Obs(True, "ready", "printing", True), 100)
        decide(CFG, st, Obs(False, "shutdown"), 101)
        decide(CFG, st, idle(True), 102)
        acts = run(CFG, st, idle(False), 103, 130)
        self.assertEqual(len(acts), 1)


class Restarts(unittest.TestCase):
    def test_failed_request_is_retried_and_kept_pending(self):
        st = started(True)
        acts = run(CFG, st, idle(False), 100, 111, request_ok=False)
        self.assertTrue(acts[0][1].restart)
        retry = run(CFG, st, idle(False), 112, 140, request_ok=False)
        self.assertTrue(retry)
        self.assertTrue(all(a == Action(restart=True) for _, a in retry))
        self.assertGreaterEqual(retry[1][0] - retry[0][0], CFG.retry_interval)

    def test_cooldown_limits_restart_rate(self):
        cfg = Config(mode="auto", quiet_after_restart=0, restart_cooldown=60)
        st = started(True, cfg)
        first = run(cfg, st, idle(False), 100, 111)
        t0 = first[0][0]
        acts = run(cfg, st, idle(True), t0 + 1, t0 + 59)
        self.assertEqual(acts, [])
        acts = run(cfg, st, idle(True), t0 + 60, t0 + 70)
        self.assertEqual(len(acts), 1)

    def test_both_devices_arriving_together_cause_one_restart(self):
        # Only the MMU is managed; a second change inside the quiet window
        # (e.g. the mainboard rebooting too) must not add a second restart.
        st = started(False)
        acts = run(CFG, st, idle(True), 100, 106)
        acts += run(CFG, st, idle(False), 107, 120)
        acts += run(CFG, st, idle(True), 121, 125)
        self.assertEqual(len([a for _, a in acts if a.restart]), 1)


class Modes(unittest.TestCase):
    def test_real_mode_never_leaves_real(self):
        cfg = Config(mode="real")
        st = State()
        self.assertEqual(decide(cfg, st, idle(False), 0), Action(link=m.REAL))
        self.assertEqual(run(cfg, st, idle(False), 1, 200), [])

    def test_sim_mode_is_virtual_and_never_restarts(self):
        cfg = Config(mode="sim")
        st = State()
        self.assertEqual(decide(cfg, st, idle(True), 0), Action(link=m.VIRTUAL))
        self.assertEqual(run(cfg, st, idle(True), 1, 200), [])


class Links(unittest.TestCase):
    def test_link_is_atomic_and_never_dangling_in_between(self):
        with tempfile.TemporaryDirectory() as d:
            by_id = os.path.join(d, "by-id")
            os.makedirs(by_id)
            m.apply_link(d, by_id, "dev", m.REAL)
            self.assertEqual(os.readlink(os.path.join(d, "mmu")),
                             os.path.join(by_id, "dev"))
            m.apply_link(d, by_id, "dev", m.VIRTUAL)
            self.assertEqual(os.readlink(os.path.join(d, "mmu")),
                             os.path.join(d, "virtual-mmu"))
            self.assertFalse(os.path.exists(os.path.join(d, "mmu.tmp")))

    def test_temperature_include_follows_mode(self):
        with tempfile.TemporaryDirectory() as d:
            m.apply_link(d, d, "dev", m.REAL)
            with open(os.path.join(d, "mmu_temp.cfg")) as f:
                self.assertIn("[temperature_sensor Pico_MMU]", f.read())
            m.apply_link(d, d, "dev", m.VIRTUAL)
            with open(os.path.join(d, "mmu_temp.cfg")) as f:
                self.assertNotIn("temperature_sensor", f.read())

    def test_real_present_requires_char_device(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertFalse(m.real_present(d, "missing"))
            open(os.path.join(d, "regular"), "w").close()
            self.assertFalse(m.real_present(d, "regular"))
            self.assertTrue(m.real_present("/dev", "null"))


if __name__ == "__main__":
    unittest.main()
