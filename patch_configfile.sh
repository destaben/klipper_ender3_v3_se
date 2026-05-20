#!/bin/sh
# patch_configfile.sh — Patch idempotente per Klipper configfile.py (Nebula Smart Kit)
#
# Filtra le sezioni [gcode_macro ...] e l'opzione `gcode` dall'oggetto configfile
# servito da Moonraker. Risolve il popup "config too large" sul Nebula Pad.
#
# USO (direttamente sul Nebula via SSH):
#   sh patch_configfile.sh status
#   sh patch_configfile.sh apply              # applica la patch
#   sh patch_configfile.sh apply --dry-run    # anteprima senza scrivere
#   sh patch_configfile.sh restore            # ripristina ultimo backup
#   sh patch_configfile.sh restore --list     # elenca backup esistenti
#
# Variabile ambiente:
#   TARGET   percorso a configfile.py (default: /usr/share/klipper/klippy/configfile.py)
#
# Esempi:
#   sh patch_configfile.sh apply
#   TARGET=/opt/klipper/klippy/configfile.py sh patch_configfile.sh status

set -eu

TARGET="${TARGET:-/usr/share/klipper/klippy/configfile.py}"
CMD="${1:-}"
FLAG="${2:-}"

usage() {
    sed -n '2,18p' "$0" | sed 's/^# \{0,1\}//'
    exit "${1:-0}"
}

case "$CMD" in
    status|apply|restore) ;;
    -h|--help|help|"")    usage 0 ;;
    *) echo "[x] Comando sconosciuto: $CMD" >&2; usage 1 ;;
esac

command -v python3 >/dev/null 2>&1 || {
    echo "[x] python3 non trovato. Necessario per la patch." >&2
    exit 1
}

python3 - "$TARGET" "$CMD" "$FLAG" <<'PYEOF'
import sys, os, shutil, time, glob

path = sys.argv[1]
cmd  = sys.argv[2]
flag = sys.argv[3] if len(sys.argv) > 3 else ""

OLD = """    def _build_status(self, config):
        self.status_raw_config.clear()
        for section in config.get_prefix_sections(''):
            self.status_raw_config[section.get_name()] = section_status = {}
            for option in section.get_prefix_options(''):
                section_status[option] = section.get(option, note_valid=False)"""

NEW = """    def _build_status(self, config):
        self.status_raw_config.clear()
        for section in config.get_prefix_sections(''):
            sname = section.get_name()
            # PATCH: skip gcode_macro sections (too large for master-server buffer)
            if sname.startswith('gcode_macro '):
                continue
            self.status_raw_config[sname] = section_status = {}
            for option in section.get_prefix_options(''):
                # PATCH: skip 'gcode' option in other sections (delayed_gcode, etc.)
                if option == 'gcode':
                    continue
                section_status[option] = section.get(option, note_valid=False)"""

MARKER = "# PATCH: skip gcode_macro sections"

def log(msg, lvl="INFO"):
    p = {"INFO": "[*]", "OK": "[+]", "WARN": "[!]", "ERR": "[x]"}.get(lvl, "[*]")
    print(p, msg)

def backups():
    return sorted(glob.glob(path + ".bak.*"))

def need_file():
    if not os.path.exists(path):
        log("File non trovato: " + path, "ERR")
        sys.exit(1)

def do_status():
    need_file()
    with open(path, encoding="utf-8") as f:
        c = f.read()
    log("Target:    " + path)
    log("Size:      %d bytes" % os.path.getsize(path))
    if MARKER in c:
        log("Stato:     PATCH APPLICATA", "OK")
    elif OLD in c:
        log("Stato:     ORIGINALE (patchabile)")
    else:
        log("Stato:     SCONOSCIUTO (modificato a mano o versione diversa)", "WARN")
    bks = backups()
    if bks:
        log("Backup (%d):" % len(bks))
        for b in bks:
            log("    %s  (%d bytes)" % (b, os.path.getsize(b)))
    else:
        log("Backup:    nessuno")

def do_apply():
    need_file()
    dry = (flag == "--dry-run")
    with open(path, encoding="utf-8") as f:
        c = f.read()
    if MARKER in c:
        log("Patch gia' applicata - nulla da fare.", "WARN")
        return
    if OLD not in c:
        log("Pattern originale NON trovato.", "ERR")
        log("Cause possibili: file modificato a mano, versione Klipper diversa, indentazione cambiata.", "ERR")
        sys.exit(2)
    new_c = c.replace(OLD, NEW)
    delta = len(new_c) - len(c)
    if dry:
        log("DRY-RUN: pattern OK, applicabile (delta %+d bytes)." % delta, "OK")
        return
    ts = time.strftime("%Y%m%d-%H%M%S")
    bk = "%s.bak.%s" % (path, ts)
    shutil.copy2(path, bk)
    log("Backup creato: " + bk, "OK")
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(new_c)
    shutil.copystat(path, tmp)
    os.replace(tmp, path)
    log("Patch applicata (%+d bytes)." % delta, "OK")
    print()
    log("PROSSIMI PASSI:")
    log("  /etc/init.d/S55klipper_service restart")
    log("  curl -s 'http://127.0.0.1:7125/printer/objects/query?configfile' | wc -c")

def do_restore():
    bks = backups()
    if flag == "--list":
        if not bks:
            log("Nessun backup.", "WARN"); return
        log("Backup disponibili:")
        for b in bks:
            log("    %s  (%d bytes)" % (b, os.path.getsize(b)))
        return
    if not bks:
        log("Nessun backup disponibile per il restore.", "ERR")
        sys.exit(1)
    src = bks[-1]
    log("Ripristino da: " + src)
    if os.path.exists(path):
        safety = "%s.pre-restore.%s" % (path, time.strftime("%Y%m%d-%H%M%S"))
        shutil.copy2(path, safety)
        log("Safety backup: " + safety, "OK")
    shutil.copy2(src, path)
    log("File ripristinato.", "OK")
    log("Riavvia Klipper: /etc/init.d/S55klipper_service restart")

actions = {"status": do_status, "apply": do_apply, "restore": do_restore}
actions[cmd]()
PYEOF
