#!/usr/bin/env python3
# Copy the printer config and apply the overrides the virtual printer needs.
#
# Only hardware the simulator cannot provide is changed: serial ports, host
# sensors, MCU temperature sensors and the Klipper fork-only option that the
# virtual pty ignores. Every replacement must match, so a config change that
# makes an override stale fails the check instead of passing silently.
#
# Usage: gen_sim_config.py <config-src-dir> <config-dst-dir>

import glob
import os
import re
import shutil
import sys

SIM_CPU_TEMP_FILE = "/tmp/sim_cpu_temp"

# MCU temperature sensors: the virtual MCU has no internal temperature
DROP_SECTIONS = {
    "temperature_sensor Ender3V3SE_MCU",
    "temperature_sensor Pico_MMU",
}
# [maintain] is not provided by the Klipper fork or the Dockerfile
DROP_FILES = ["helpers/maintain.cfg"]

PRINTER_REPLACEMENTS = [
    ("serial: /dev/serial/by-id/usb-1a86_USB_Serial-if00-port0",
     "serial: /tmp/klipper_host_mcu"),
    ("restart_method: command\n", ""),
    ("[temperature_sensor Raspberry_Pi]\nsensor_type: temperature_host\n",
     "[temperature_sensor Raspberry_Pi]\nsensor_type: temperature_host\n"
     "sensor_path: " + SIM_CPU_TEMP_FILE + "\n"),
]

MMU_REPLACEMENTS = [
    ("serial: /dev/serial/by-id/usb-Klipper_stm32g0b1xx_5A00320017504D4636383420-if00",
     "serial: /tmp/klipper_pico_mmu"),
]


def replace_exact(text, old, new, path):
    if text.count(old) != 1:
        sys.exit("sim override not found exactly once in %s: %r" % (path, old))
    return text.replace(old, new, 1)


def drop_sections(text):
    out, skip = [], False
    for line in text.splitlines(True):
        match = re.match(r"^\[([^\]]+)\]", line)
        if match:
            skip = match.group(1).strip() in DROP_SECTIONS
        elif skip and line.strip() == "":
            skip = False
        if not skip:
            out.append(line)
    return "".join(out)


def edit(path, transform):
    with open(path, errors="ignore") as f:
        text = f.read()
    new = transform(text)
    if new != text:
        with open(path, "w") as f:
            f.write(new)


def main():
    src, dst = sys.argv[1], sys.argv[2]
    shutil.rmtree(dst, ignore_errors=True)
    shutil.copytree(src, dst)

    for rel in DROP_FILES:
        os.remove(os.path.join(dst, rel))

    for path in glob.glob(os.path.join(dst, "**", "*.cfg"), recursive=True):
        edit(path, drop_sections)

    def printer_overrides(text):
        for old, new in PRINTER_REPLACEMENTS:
            text = replace_exact(text, old, new, "printer.cfg")
        return text

    def mmu_overrides(text):
        for old, new in MMU_REPLACEMENTS:
            text = replace_exact(text, old, new, "macros/sp_mmu.cfg")
        return text

    edit(os.path.join(dst, "printer.cfg"), printer_overrides)
    edit(os.path.join(dst, "macros", "sp_mmu.cfg"), mmu_overrides)
    print("generated simulator config in %s" % dst)


if __name__ == "__main__":
    main()
