import os
import tempfile
import unittest

import mmu_link as m
from mmu_link import Action, Config, Obs, State, decide

CFG = Config(mode="auto")


def idle(present, klippy="ready", mmu=None):
    """An idle printer. mmu is the mode Klippy is actually running on."""
    return Obs(real_present=present, klippy_state=klippy,
               print_state="standby", heaters_off=True, klippy_mmu=mmu)


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


class FirstRunReconcile(unittest.TestCase):
    """A sidecar restart must not lose a mode change Klippy has not seen."""

    def test_stale_klippy_is_restarted_once_when_idle(self):
        # Klippy runs on the virtual MCU, the Pico was replugged, and the
        # sidecar was recreated: the new process links real and must restart.
        st = State()
        obs = idle(True, mmu=m.VIRTUAL)
        self.assertEqual(decide(CFG, st, obs, 0), Action(link=m.REAL))
        acts = run(CFG, st, obs, 1, 15)
        self.assertEqual([a for _, a in acts], [Action(restart=True)])

    def test_matching_klippy_is_left_alone(self):
        st = State()
        obs = idle(True, mmu=m.REAL)
        self.assertEqual(decide(CFG, st, obs, 0), Action(link=m.REAL))
        self.assertEqual(run(CFG, st, obs, 1, 100), [])

    def test_unknown_klippy_mode_is_left_alone(self):
        st = State()
        self.assertEqual(decide(CFG, st, idle(True, klippy="startup"), 0),
                         Action(link=m.REAL))
        self.assertEqual(run(CFG, st, idle(True, klippy="startup"), 1, 100), [])

    def test_stale_klippy_is_not_restarted_while_printing(self):
        st = State()
        decide(CFG, st, idle(True, mmu=m.VIRTUAL), 0)
        busy = Obs(True, "ready", "printing", True, klippy_mmu=m.VIRTUAL)
        self.assertEqual(run(CFG, st, busy, 1, 100), [])
        acts = run(CFG, st, idle(True, mmu=m.VIRTUAL), 101, 110)
        self.assertEqual([a for _, a in acts], [Action(restart=True)])


class FirstRunLatch(unittest.TestCase):
    """The latch written before a sidecar restart must be honoured by the very
    first decision of the new process."""

    def test_latched_fresh_start_does_not_link_virtual(self):
        # The Pico dropped mid-print (latch stored), then the sidecar was
        # recreated with the Pico still unplugged: keep the old link.
        st = State(print_latched=True)
        down = Obs(False, "shutdown")
        self.assertEqual(run(CFG, st, down, 0, 300), [])
        self.assertIsNone(st.linked)

    def test_replug_after_latched_fresh_start_links_real(self):
        st = State(print_latched=True)
        run(CFG, st, Obs(False, "shutdown"), 0, 30)
        self.assertEqual(decide(CFG, st, Obs(True, "shutdown"), 31),
                         Action(link=m.REAL))

    def test_latch_does_not_block_sim_mode(self):
        cfg = Config(mode="sim")
        st = State(print_latched=True)
        self.assertEqual(decide(cfg, st, Obs(False, "shutdown"), 0),
                         Action(link=m.VIRTUAL))

    def test_stale_latch_clears_once_klippy_is_ready_and_idle(self):
        st = State(print_latched=True)
        self.assertEqual(decide(CFG, st, idle(False), 0),
                         Action(link=m.VIRTUAL))


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
        retry = run(CFG, st, idle(False), 112, 200, request_ok=False)
        self.assertTrue(retry)
        self.assertTrue(all(a == Action(restart=True) for _, a in retry))
        self.assertGreaterEqual(retry[1][0] - retry[0][0], CFG.retry_interval)

    def test_timed_out_request_that_took_effect_is_not_repeated(self):
        # Moonraker restarted Klippy but the POST timed out: once Klippy is
        # back on the new mode the pending restart must be dropped.
        st = started(True)
        acts = run(CFG, st, idle(False, mmu=m.REAL), 100, 111, request_ok=False)
        self.assertEqual(len(acts), 1)
        self.assertEqual(run(CFG, st, idle(False, klippy="startup"), 112, 125,
                             request_ok=False), [])
        self.assertEqual(run(CFG, st, idle(False, mmu=m.VIRTUAL), 126, 200,
                             request_ok=False), [])
        self.assertFalse(st.restart_pending)

    def test_request_that_did_not_take_effect_is_retried(self):
        st = started(True)
        acts = run(CFG, st, idle(False, mmu=m.REAL), 100, 111)
        self.assertEqual(len(acts), 1)
        # Still on the old mode well after the restart was accepted
        later = run(CFG, st, idle(False, mmu=m.REAL), 112, 200)
        self.assertEqual([a for _, a in later], [Action(restart=True)])

    def test_stale_mode_right_after_accepted_request_is_not_a_second_restart(self):
        st = started(True)
        run(CFG, st, idle(False, mmu=m.REAL), 100, 111)
        self.assertEqual(run(CFG, st, idle(False, mmu=m.REAL), 112, 125), [])

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


class Latch(unittest.TestCase):
    def test_latch_survives_a_sidecar_restart(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertFalse(m.load_latch(d))
            m.store_latch(d, True)
            self.assertTrue(m.load_latch(d))
            m.store_latch(d, False)
            self.assertFalse(m.load_latch(d))
            m.store_latch(d, False)  # clearing twice is fine

    def test_restored_latch_blocks_the_fallback(self):
        st = started(True)
        st.print_latched = True    # as loaded from the file
        self.assertEqual(run(CFG, st, Obs(False, "shutdown"), 100, 300), [])
        self.assertEqual(st.linked, m.REAL)


class StartupGrace(unittest.TestCase):
    """A Pico that enumerates just after the sidecar starts must not cause a
    needless virtual link followed by an extra restart."""

    def test_auto_waits_before_first_virtual_link(self):
        cfg = Config(mode="auto", startup_grace=5)
        st = State()
        absent = idle(False, klippy=None)
        self.assertEqual(run(cfg, st, absent, 0, 4), [])
        self.assertIsNone(st.linked)
        self.assertEqual(decide(cfg, st, absent, 5), Action(link=m.VIRTUAL))

    def test_pico_appearing_within_grace_links_real_at_once(self):
        cfg = Config(mode="auto", startup_grace=5)
        st = State()
        decide(cfg, st, idle(False, klippy=None), 0)
        self.assertEqual(decide(cfg, st, idle(True, klippy=None), 2),
                         Action(link=m.REAL))

    def test_grace_does_not_delay_a_present_pico(self):
        cfg = Config(mode="auto", startup_grace=5)
        self.assertEqual(decide(cfg, State(), idle(True, klippy=None), 0),
                         Action(link=m.REAL))

    def test_grace_only_applies_in_auto_mode(self):
        cfg = Config(mode="sim", startup_grace=5)
        self.assertEqual(decide(cfg, State(), idle(False, klippy=None), 0),
                         Action(link=m.VIRTUAL))


def virtual_idle(klippy="ready", gen="a", mmu=m.VIRTUAL):
    obs = idle(False, klippy=klippy, mmu=mmu if klippy == "ready" else None)
    obs.virtual_gen = gen
    return obs


class VirtualRespawn(unittest.TestCase):
    """The virtual MCU runs in its own container; docker respawns it. A
    respawn is part of every normal restart, so only one that leaves Klippy
    down needs a restart of its own."""

    def linked_virtual(self):
        st = State()
        decide(CFG, st, virtual_idle(), 0)
        self.assertEqual(st.linked, m.VIRTUAL)
        return st

    def test_first_sight_of_the_pty_is_not_a_respawn(self):
        st = self.linked_virtual()
        self.assertEqual(run(CFG, st, virtual_idle(), 1, 100), [])

    def test_respawn_that_leaves_klippy_down_is_restarted_once(self):
        st = self.linked_virtual()
        run(CFG, st, virtual_idle(), 1, 9)
        down = virtual_idle(klippy="shutdown", gen="b")
        acts = run(CFG, st, down, 10, 60)
        self.assertEqual([a for _, a in acts], [Action(restart=True)])
        self.assertGreaterEqual(acts[0][0], 10 + CFG.respawn_grace)

    def test_plug_after_a_respawn_restart_obeys_the_shared_cooldown(self):
        # A respawn restart and a plug/unplug restart share one cooldown:
        # replugging the Pico seconds after Klippy was restarted must wait.
        st = self.linked_virtual()
        run(CFG, st, virtual_idle(), 1, 9)
        acts = run(CFG, st, virtual_idle(klippy="shutdown", gen="b"), 10, 60)
        t0 = acts[0][0]
        early = run(CFG, st, idle(True, mmu=m.VIRTUAL), t0 + 1, t0 + 59)
        self.assertEqual(early, [])
        late = run(CFG, st, idle(True, mmu=m.VIRTUAL), t0 + 60, t0 + 70)
        self.assertEqual(len([a for _, a in late if a.restart]), 1)

    def test_respawn_klippy_recovers_from_is_left_alone(self):
        st = self.linked_virtual()
        self.assertEqual(run(CFG, st, virtual_idle(klippy="shutdown", gen="b"),
                             10, 14), [])
        self.assertEqual(run(CFG, st, virtual_idle(gen="b"), 15, 100), [])

    def test_respawn_during_a_print_is_not_restarted(self):
        st = self.linked_virtual()
        busy = Obs(False, "ready", "printing", True, klippy_mmu=m.VIRTUAL,
                   virtual_gen="a")
        run(CFG, st, busy, 1, 9)
        self.assertTrue(st.print_latched)
        self.assertEqual(run(CFG, st, virtual_idle(klippy="shutdown", gen="b"),
                             10, 100), [])

    def test_respawn_is_ignored_while_linked_real(self):
        st = started(True)
        for t, gen in ((1, "a"), (2, "b")):
            obs = idle(True, mmu=m.REAL)
            obs.virtual_gen = gen
            decide(CFG, st, obs, t)
        down = Obs(True, "shutdown", virtual_gen="c")
        self.assertEqual(run(CFG, st, down, 3, 100), [])


class CooldownEverywhere(unittest.TestCase):
    """No path may restart Klippy more often than the cooldown; only a failed
    request is retried sooner."""

    def test_crash_looping_virtual_mcu_is_rate_limited(self):
        st = State()
        decide(CFG, st, virtual_idle(), 0)
        times = []
        for t in range(1, 400):
            # The pty is recreated every 20 s and Klippy stays in shutdown
            obs = virtual_idle(klippy="shutdown", gen=str(t // 20))
            a = decide(CFG, st, obs, float(t))
            if a.restart:
                times.append(t)
                m.restart_done(st)
        self.assertGreaterEqual(len(times), 2)
        gaps = [b - a for a, b in zip(times, times[1:])]
        self.assertTrue(all(g >= CFG.restart_cooldown for g in gaps), gaps)

    def test_failed_request_is_still_retried_at_retry_interval(self):
        st = started(True)
        run(CFG, st, idle(False), 100, 111, request_ok=False)
        retry = run(CFG, st, idle(False), 112, 200, request_ok=False)
        self.assertGreaterEqual(len(retry), 2)
        gaps = [b[0] - a[0] for a, b in zip(retry, retry[1:])]
        self.assertTrue(all(CFG.retry_interval <= g < CFG.restart_cooldown
                            for g in gaps), gaps)


class VirtualGen(unittest.TestCase):
    def test_changes_when_the_link_is_recreated(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "virtual-mmu")
            self.assertIsNone(m.virtual_gen(path))
            os.symlink("/dev/pts/3", path)
            first = m.virtual_gen(path)
            self.assertIsNotNone(first)
            self.assertEqual(m.virtual_gen(path), first)
            os.unlink(path)
            os.symlink("/dev/pts/3", path)   # same target, new link
            self.assertNotEqual(m.virtual_gen(path), first)


class Runner(unittest.TestCase):
    """The I/O loop must survive errors and retry, never exit."""

    def make(self, d, mode="auto"):
        cfg = Config(mode=mode)
        r = m.Runner(cfg, link_dir=d, by_id_dir=d, real_name="none",
                     moonraker="http://invalid", poll=0)
        r.observe = lambda present: idle(present)
        r.request_restart = lambda url: True
        return r

    def test_failed_link_write_is_retried(self):
        real = m.apply_link
        calls = []

        def flaky(*a):
            calls.append(1)
            if len(calls) == 1:
                raise OSError("disk full")
            return real(*a)

        with tempfile.TemporaryDirectory() as d:
            open(os.path.join(d, "virtual-mmu"), "w").close()
            r = self.make(d)
            m.apply_link, saved = flaky, real
            try:
                r.step_safe(0)
                self.assertFalse(os.path.islink(os.path.join(d, "mmu")))
                r.step_safe(1)
            finally:
                m.apply_link = saved
            self.assertTrue(os.path.islink(os.path.join(d, "mmu")))

    def test_failed_latch_write_is_retried(self):
        with tempfile.TemporaryDirectory() as d:
            open(os.path.join(d, "virtual-mmu"), "w").close()
            r = self.make(d)
            r.observe = lambda present: Obs(present, "ready", "printing", True)
            real = m.store_latch
            calls = []

            def flaky(*a):
                calls.append(1)
                if len(calls) == 1:
                    raise OSError("read-only")
                return real(*a)

            m.store_latch = flaky
            try:
                r.step_safe(0)
                r.step_safe(1)
            finally:
                m.store_latch = real
            self.assertTrue(m.load_latch(d))

    def test_print_started_before_the_post_cancels_the_restart(self):
        # Klippy reported idle when decide() ran, and a print started before
        # the request was sent: the restart must be postponed.
        with tempfile.TemporaryDirectory() as d:
            r = self.make(d)
            sent = []
            r.request_restart = lambda url: sent.append(url) or True
            stale = idle(True, mmu=m.VIRTUAL)
            busy = Obs(True, "ready", "printing", True, klippy_mmu=m.VIRTUAL)
            r.observe = lambda present: stale
            r.step_safe(0)                    # first run: links real
            seq = iter([stale, busy])
            r.observe = lambda present: next(seq)
            r.step_safe(1)
            self.assertEqual(sent, [])
            self.assertTrue(r.st.print_latched)
            self.assertTrue(r.st.restart_pending)

    def test_restart_is_sent_when_still_idle_at_the_post(self):
        with tempfile.TemporaryDirectory() as d:
            r = self.make(d)
            sent = []
            r.request_restart = lambda url: sent.append(url) or True
            stale = idle(True, mmu=m.VIRTUAL)
            r.observe = lambda present: stale
            r.step_safe(0)
            r.step_safe(1)
            self.assertEqual(len(sent), 1)

    def test_unexpected_exception_does_not_escape(self):
        with tempfile.TemporaryDirectory() as d:
            r = self.make(d)

            def boom(present):
                raise RuntimeError("bug")

            r.observe = boom
            r.step_safe(0)   # must not raise


class LinkDirCheck(unittest.TestCase):
    def test_refuses_the_tracked_default(self):
        with tempfile.TemporaryDirectory() as d:
            open(os.path.join(d, m.TRACKED_MARKER), "w").close()
            self.assertIn("tracked", m.check_link_dir(d))

    def test_refuses_a_missing_directory(self):
        self.assertIsNotNone(m.check_link_dir("/nonexistent-mmu-dir"))

    def test_accepts_a_writable_directory(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertIsNone(m.check_link_dir(d))


if __name__ == "__main__":
    unittest.main()
