import unittest

import refusals as rf


class ConsoleLine(unittest.TestCase):
    def test_refused_format(self):
        line = rf.console_line(rf.Finding("R02", "no spots"))
        self.assertEqual(line, "RESCUE: REFUSED [R02] no valid measured probe spot: no spots")

    def test_warning_format(self):
        line = rf.console_line(rf.Finding("R11", "3.0 h since the shutdown"))
        self.assertTrue(line.startswith("RESCUE: WARNING [R11] "))

    def test_escaping_for_respond(self):
        f = rf.Finding("R17", 'line 5: "G28"; then\nmore °C')
        line = rf.console_line(f)
        self.assertNotIn('"', line)
        self.assertNotIn(";", line)
        self.assertNotIn("\n", line)
        self.assertTrue(all(32 <= ord(c) < 127 for c in line))
        cmd = rf.respond_command(f)
        self.assertTrue(cmd.startswith('RESPOND TYPE=error MSG="RESCUE: REFUSED [R17]'))
        self.assertTrue(cmd.endswith('"'))
        self.assertEqual(cmd.count('"'), 2)

    def test_long_line_truncated(self):
        line = rf.console_line(rf.Finding("R13", "x" * 1000))
        self.assertLessEqual(len(line), rf.MAX_LINE)
        self.assertTrue(line.endswith("..."))

    def test_unknown_code(self):
        with self.assertRaises(ValueError):
            rf.console_line(rf.Finding("R99", ""))

    def test_catalogue_is_complete(self):
        self.assertEqual(sorted(rf.HARD), ["R01", "R02", "R07", "R09", "R13", "R14",
                                           "R15", "R17", "R18", "R21", "R22"])
        self.assertEqual(sorted(rf.WARN), ["PIN_AT_BOOT", "R03", "R04", "R05", "R06",
                                           "R08", "R10", "R11", "R12", "R16", "R19",
                                           "R20", "R23"])


class Refused(unittest.TestCase):
    def test_r13_blocks_motion_only(self):
        self.assertFalse(rf.refused([rf.Finding("R13", ""), rf.Finding("R11", "")]))
        self.assertTrue(rf.refused([rf.Finding("R13", "")], include_motion_only=True))

    def test_hard(self):
        self.assertTrue(rf.refused([rf.Finding("R02", "")]))
        self.assertFalse(rf.refused([rf.Finding("PIN_AT_BOOT", "")]))

    def test_no_usable_stop_evidence_is_a_hard_refusal(self):
        # S2: without a stop window no resume line can be chosen.
        line = rf.console_line(rf.Finding("R18", "no stop window"))
        self.assertTrue(line.startswith("RESCUE: REFUSED [R18] "))
        self.assertTrue(rf.refused([rf.Finding("R18", "")]))


if __name__ == "__main__":
    unittest.main()
