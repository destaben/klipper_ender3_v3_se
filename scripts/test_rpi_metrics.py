"""Tests for scripts/rpi-metrics.sh using a PATH-stubbed vcgencmd.

Standard library only. If promtool is on PATH (or RPI_PROMTOOL names a
command), the output is also checked with `promtool check metrics`.
"""
import os
import shlex
import shutil
import stat
import subprocess
import tempfile
import unittest

SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "rpi-metrics.sh")
SH = shutil.which("dash") or "sh"

WIRELESS = (
    "Inter-| sta-|   Quality        |   Discarded packets               | Missed | WE\n"
    " face | tus | link level noise |  nwid  crypt   frag  retry   misc | beacon | 22\n"
    " wlan0: 0000   60.  -50.  -256        0      0      0      0      0        0\n"
)


class RpiMetricsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.out = os.path.join(self.tmp, "out")
        self.bin = os.path.join(self.tmp, "bin")
        os.mkdir(self.out)
        os.mkdir(self.bin)
        self.calls = os.path.join(self.tmp, "calls.log")

    def write(self, name, text):
        path = os.path.join(self.tmp, name)
        with open(path, "w") as f:
            f.write(text)
        return path

    def stub_vcgencmd(self, throttled="throttled=0x50005", volts="volt=1.2000V",
                      temp="temp=51.0'C", clock="frequency(48)=1500398464"):
        body = (
            "#!/bin/sh\n"
            'echo "$*" >> "%s"\n'
            'case "$1 $2" in\n'
            '  "get_throttled ") echo "%s";;\n'
            '  "measure_volts core") echo "%s";;\n'
            '  "measure_temp ") echo "%s";;\n'
            '  "measure_clock arm") echo "%s";;\n'
            "  *) exit 1;;\n"
            "esac\n"
        ) % (self.calls, throttled, volts, temp, clock)
        path = os.path.join(self.bin, "vcgencmd")
        with open(path, "w") as f:
            f.write(body)
        os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR)

    def run_script(self, thermal="51234\n", cpufreq="1500000\n", wireless=WIRELESS, args=None):
        env = {
            "PATH": self.bin + os.pathsep + "/usr/bin:/bin",
            "RPI_THERMAL_FILE": self.write("thermal", thermal) if thermal is not None
            else os.path.join(self.tmp, "no-thermal"),
            "RPI_CPUFREQ_FILE": self.write("cpufreq", cpufreq) if cpufreq is not None
            else os.path.join(self.tmp, "no-cpufreq"),
            "RPI_WIRELESS_FILE": self.write("wireless", wireless) if wireless is not None
            else os.path.join(self.tmp, "no-wireless"),
        }
        return subprocess.run([SH, SCRIPT] + ([self.out] if args is None else args), env=env,
                              capture_output=True, text=True)

    def prom(self):
        with open(os.path.join(self.out, "rpi.prom")) as f:
            return f.read()

    def values(self):
        result = {}
        for line in self.prom().splitlines():
            if line and not line.startswith("#"):
                key, _, val = line.rpartition(" ")
                result[key] = val
        return result

    def test_full_output(self):
        self.stub_vcgencmd()
        r = self.run_script()
        self.assertEqual(r.returncode, 0, r.stderr)
        v = self.values()
        self.assertEqual(v["rpi_throttled_bitmask"], str(0x50005))
        self.assertEqual(v['rpi_throttled_flag{flag="under_voltage"}'], "1")
        self.assertEqual(v['rpi_throttled_flag{flag="arm_freq_capped"}'], "0")
        self.assertEqual(v['rpi_throttled_flag{flag="throttled"}'], "1")
        self.assertEqual(v['rpi_throttled_flag{flag="soft_temp_limit"}'], "0")
        self.assertEqual(v['rpi_throttled_flag{flag="under_voltage_occurred"}'], "1")
        self.assertEqual(v['rpi_throttled_flag{flag="throttled_occurred"}'], "1")
        self.assertEqual(v['rpi_throttled_flag{flag="soft_temp_limit_occurred"}'], "0")
        self.assertEqual(v["rpi_core_volts"], "1.2000")
        self.assertEqual(v["rpi_cpu_temp_celsius"], "51.2")
        self.assertEqual(v["rpi_arm_clock_hz"], "1500000000")
        self.assertEqual(v['rpi_wifi_signal_dbm{interface="wlan0"}'], "-50")
        self.assertEqual(v['rpi_wifi_link_quality{interface="wlan0"}'], "60")
        self.assertIn("rpi_metrics_last_run_timestamp_seconds", v)

    def test_only_two_vcgencmd_calls_when_sysfs_present(self):
        self.stub_vcgencmd()
        self.run_script()
        with open(self.calls) as f:
            calls = f.read().splitlines()
        self.assertEqual(sorted(calls), ["get_throttled", "measure_volts core"])

    def test_vcgencmd_fallback_for_temp_and_clock(self):
        self.stub_vcgencmd()
        r = self.run_script(thermal=None, cpufreq=None)
        self.assertEqual(r.returncode, 0, r.stderr)
        v = self.values()
        self.assertEqual(v["rpi_cpu_temp_celsius"], "51.0")
        self.assertEqual(v["rpi_arm_clock_hz"], "1500398464")

    def test_no_vcgencmd_omits_its_metrics(self):
        r = self.run_script()
        self.assertEqual(r.returncode, 0, r.stderr)
        v = self.values()
        self.assertNotIn("rpi_throttled_bitmask", v)
        self.assertNotIn("rpi_core_volts", v)
        self.assertIn("rpi_cpu_temp_celsius", v)

    def test_garbage_vcgencmd_output_is_omitted(self):
        self.stub_vcgencmd(throttled="throttled=oops", volts="error=1")
        r = self.run_script()
        self.assertEqual(r.returncode, 0, r.stderr)
        v = self.values()
        self.assertNotIn("rpi_throttled_bitmask", v)
        self.assertNotIn("rpi_core_volts", v)

    def test_missing_wifi_interface_omits_wifi(self):
        self.stub_vcgencmd()
        self.run_script(wireless=WIRELESS.replace("wlan0", "wlan9"))
        self.assertNotIn("rpi_wifi_signal_dbm", self.prom())

    def test_atomic_no_leftover_temp_files(self):
        self.stub_vcgencmd()
        self.run_script()
        self.assertEqual(os.listdir(self.out), ["rpi.prom"])
        mode = os.stat(os.path.join(self.out, "rpi.prom")).st_mode & 0o777
        self.assertEqual(mode, 0o644)

    def test_bad_args(self):
        self.assertEqual(self.run_script(args=[]).returncode, 2)
        self.assertEqual(self.run_script(args=[os.path.join(self.tmp, "nope")]).returncode, 2)

    def test_promtool_check_metrics(self):
        cmd = shlex.split(os.environ.get("RPI_PROMTOOL", "")) or (
            ["promtool"] if shutil.which("promtool") else None)
        if not cmd:
            self.skipTest("promtool not available")
        self.stub_vcgencmd()
        self.run_script()
        with open(os.path.join(self.out, "rpi.prom")) as f:
            r = subprocess.run(cmd + ["check", "metrics"], stdin=f,
                               capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)


if __name__ == "__main__":
    unittest.main()
