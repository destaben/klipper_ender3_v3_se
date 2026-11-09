"""Refusal and warning catalogue (council ruling) and console rendering.

HARD refusals stop the rescue. R13 is special: it blocks MOTION only, so plan
mode still runs and reports everything else. WARNINGS need an explicit human
confirmation in the interactive phases (P4); in P1 they are only printed.

Every finding renders as one ASCII line that can be passed verbatim to
RESPOND TYPE=error MSG="..." (see console_line / respond_command).

Pure logic, standard library only.
"""

from dataclasses import dataclass
from typing import List

HARD = {
    "R01": "part too tall to lift clear",
    "R02": "no valid measured probe spot",
    "R07": "G-code file missing or changed since the print",
    "R09": "shutdown was not an MCU communication loss",
    "R21": "overlap is clearly absurd for this stop window",
    "R22": "no resume line without a gap in the part",
    "R13": "unmeasured safety-critical profile field (blocks motion)",
    "R14": "profile does not match printer.cfg",
    "R15": "job queue not empty",
    "R17": "forbidden command in the resumed tail",
    "R18": "no usable stop evidence: no resume line can be chosen",
}
WARN = {
    "R03": "resume point or approach near a no-go zone",
    "R04": "multi-tool print or lane mismatch",
    "R05": "absolute extrusion (M82)",
    "R06": "vase mode or non-monotonic Z",
    "R08": "cut before min_resume_layer",
    "R10": "insufficient log evidence, manual line needed",
    "R11": "long time since the shutdown",
    "R12": "overlap above the budget (extra material, never a gap)",
    "R16": "unknown previously excluded objects",
    "R19": "G-code modified time differs from the history (content check decides)",
    "R20": "probe point outside the bed mesh (mesh extrapolated)",
    "R23": "relative XYZ mode (G91) in the resumed tail",
    "PIN_AT_BOOT": "probe pin self-test at boot lands on the part",
}
MAX_LINE = 240


@dataclass
class Finding:
    code: str
    detail: str

    @property
    def hard(self) -> bool:
        return self.code in HARD

    @property
    def motion_only(self) -> bool:
        return self.code == "R13"


def _ascii(text: str) -> str:
    out = []
    for ch in text:
        if ch in "\r\n\t":
            out.append(" ")
        elif 32 <= ord(ch) < 127:
            out.append(ch)
        else:
            out.append("?")
    return "".join(out)


def console_line(f: Finding) -> str:
    """'RESCUE: REFUSED [R02] ...' or 'RESCUE: WARNING [R11] ...', ASCII,
    one line, quotes and semicolons replaced so it is safe in MSG="..."."""
    if f.code not in HARD and f.code not in WARN:
        raise ValueError("unknown finding code %r" % f.code)
    kind = "REFUSED" if f.hard else "WARNING"
    title = HARD.get(f.code) or WARN.get(f.code)
    text = "%s: %s" % (title, f.detail) if f.detail else title
    line = "RESCUE: %s [%s] %s" % (kind, f.code, _ascii(text))
    # Klipper ends a quoted value at '"' and drops everything after ';'.
    line = line.replace('"', "'").replace(";", ",")
    if len(line) > MAX_LINE:
        line = line[:MAX_LINE - 3] + "..."
    return line


def respond_command(f: Finding) -> str:
    return 'RESPOND TYPE=error MSG="%s"' % console_line(f)


def refused(findings: List[Finding], include_motion_only: bool = False) -> bool:
    return any(f.hard and (include_motion_only or not f.motion_only)
               for f in findings)
