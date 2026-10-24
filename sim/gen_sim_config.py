#!/usr/bin/env python3
# Copy the printer config and apply the overrides the virtual printer needs.
#
# Only hardware the simulator cannot provide is changed: serial ports, host
# sensors, MCU temperature sensors, and restart_method on the main MCU (the
# fork ignores it on a /tmp/klipper_host_* pty and rejects it as unused). The
# MMU MCU's pty does not match that prefix, so the fork reads its
# restart_method and the option is kept there. Every replacement must match
# exactly once, so a config change that makes an override stale fails the
# check instead of passing silently.
#
# Usage: gen_sim_config.py <config-src-dir> <config-dst-dir>

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
    # A dropped section runs until the next section header or the SAVE_CONFIG
    # block ("#*#" lines). Blank lines do not end it, so a blank line inside
    # the section cannot leave its options attached to the previous section.
    out, skip, found = [], False, set()
    for line in text.splitlines(True):
        match = re.match(r"^\[([^\]]+)\]", line)
        if match:
            name = match.group(1).strip()
            skip = name in DROP_SECTIONS
            if skip:
                found.add(name)
                print("sim: not loaded, unsupported in virtual printer: [%s]"
                      % name)
        elif line.startswith("#*#"):
            skip = False
        if not skip:
            out.append(line)
    # A renamed sensor would otherwise stay loaded without any notice
    missing = DROP_SECTIONS - found
    if missing:
        sys.exit("sim: sections to drop not found in printer.cfg: %s"
                 % ", ".join(sorted(missing)))
    return "".join(out)


def edit(path, transform):
    with open(path) as f:
        text = f.read()
    new = transform(text)
    if new != text:
        with open(path, "w") as f:
            f.write(new)


def main():
    src, dst = sys.argv[1], sys.argv[2]
    shutil.rmtree(dst, ignore_errors=True)
    shutil.copytree(src, dst)

    def printer_overrides(text):
        for old, new in PRINTER_REPLACEMENTS:
            text = replace_exact(text, old, new, "printer.cfg")
        return text

    def printer_transform(text):
        # The dropped sections are only defined in printer.cfg; an unrelated
        # file never loses a section, so its errors still reach Klippy
        return printer_overrides(drop_sections(text))

    def mmu_overrides(text):
        for old, new in MMU_REPLACEMENTS:
            text = replace_exact(text, old, new, "macros/sp_mmu.cfg")
        return text

    edit(os.path.join(dst, "printer.cfg"), printer_transform)
    edit(os.path.join(dst, "macros", "sp_mmu.cfg"), mmu_overrides)
    print("generated simulator config in %s" % dst)


if __name__ == "__main__":
    main()
