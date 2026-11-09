#!/usr/bin/env python3
"""Pick the review out of a claude-code-action execution file and validate it.

Usage: check_review.py EXECUTION_FILE PR_NUMBER OUTPUT_FILE

Exit 0 and write the review to OUTPUT_FILE only when the run's final `result`
message succeeded and is a finished review (an errored final result is never
replaced by an earlier success):

* its first non-blank line is `## Review of PR #<n>` (optionally followed by
  a space or punctuation and more text, e.g. a commit sha), and
* its last non-blank line is
  `Result: <total> findings (<severe> severe, <non-severe> non-severe)` with
  total == severe + non-severe ("1 finding" when the total is exactly 1;
  markdown bold/backticks and a trailing period are tolerated), and
* it has a markdown heading for each agent (`bug-hunter` and `documenter`), so
  a review written before the documenter reported is not accepted. This is a
  heuristic, not proof that both ran: the prompt asks for one section per
  agent.

Anything else (a progress message, an empty result, a missing or unreadable
execution file) exits 3 and writes nothing, so the caller can retry or fail
the job instead of posting an unfinished review as if it were one. Exit 3 is
reserved for "incomplete review"; every other non-zero code (2 for misuse, an
unwritable output, a review that contains a secret's value or an uncaught
error, 1 only if the script cannot even start, e.g. a syntax error) is a
setup problem, so the workflow does not retry on it.
"""
import json
import os
import re
import sys
from pathlib import Path

# Environment variables whose values must never appear in a posted review.
SECRET_ENV_VARS = ("CLAUDE_CODE_OAUTH_TOKEN", "GH_TOKEN", "GITHUB_TOKEN")

RESULT_RE = re.compile(
    r"^Result: (\d+) (findings?) \((\d+) severe, (\d+) non-severe\)$"
)


def last_result(execution_file: Path):
    try:
        raw = execution_file.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        # Also accept JSON Lines, one message per line.
        try:
            data = [json.loads(ln) for ln in raw.splitlines() if ln.strip()]
        except ValueError:
            return None
    if isinstance(data, dict):
        data = [data]
    if not isinstance(data, list):
        return None
    # Only the run's final result counts: if it errored, an earlier success
    # must not be posted in its place.
    results = [m for m in data if isinstance(m, dict) and m.get("type") == "result"]
    if not results:
        return None
    final = results[-1]
    if final.get("subtype") != "success" or final.get("is_error"):
        return None
    text = final.get("result")
    return text if isinstance(text, str) else None


def unfence(text):
    """Drop a code fence wrapped around the whole message, if any."""
    lines = (text or "").strip().splitlines()
    if len(lines) >= 2 and lines[0].startswith("```") and lines[-1].strip() == "```":
        return "\n".join(lines[1:-1])
    return text or ""


def _norm(line):
    return line.lower().replace("bug hunter", "bug-hunter")


def _is_heading(line):
    return re.match(r"#{1,6}\s", line) is not None or (
        # A leading bold label, alone or followed by text: "**bug-hunter:** ok"
        re.match(r"\*\*[^*]+\*\*", line) is not None
    )


def validate(text, pr_number):
    """Return None if `text` is a finished review, else the reason it is not."""
    lines = [ln.strip() for ln in unfence(text).splitlines() if ln.strip()]
    if not lines:
        return "empty result"
    heading = f"## Review of PR #{pr_number}"
    # An agent that was blocked may still format a "0 findings" review around
    # an admission that it checked nothing; that is not a review.
    # Only lines that START with one of these phrases (markdown marks ignored)
    # in the opening lines count, so a finding that merely quotes one does not
    # block a good review.
    for ln in lines[:4]:
        opening = ln.lstrip("#*_> `").lower()
        if opening.startswith(
            ("this review is incomplete", "review incomplete", "could not complete")
        ):
            return "the review says it is incomplete"
    # One markdown heading per agent, so a sentence that merely names an agent
    # ("still waiting for the documenter") does not count as its section.
    # A heading is a `#` line or a line that is only a bold label; "Bug Hunter"
    # spelled with a space counts as the bug-hunter.
    # Accepted limit (ADR 0002, "Known limits"): this proves each agent has a
    # section, not that the section is finished, so a line like
    # "**documenter: still running**" can pass. Judging bodies would need a
    # second model pass; the review is advisory and read by a human.
    # Each agent needs its own heading: one line naming both does not count.
    used = set()
    for agent in ("bug-hunter", "documenter"):
        found = next(
            (
                i
                for i, ln in enumerate(lines[1:], 1)
                if i not in used and _is_heading(ln) and agent in _norm(ln)
            ),
            None,
        )
        if found is None:
            return f"review has no heading for {agent}"
        used.add(found)
    first = lines[0].replace("*", "").replace("`", "").strip()
    if not re.match(re.escape(heading) + r"(?![0-9A-Za-z])", first):
        return f"first line is not '{heading}'"
    last = lines[-1].replace("*", "").replace("`", "").rstrip(".").strip()
    match = RESULT_RE.match(last)
    if not match:
        return "last line is not 'Result: <n> findings (<n> severe, <n> non-severe)'"
    total, severe, non_severe = (int(g) for i, g in enumerate(match.groups()) if i != 1)
    if match.group(2) != ("finding" if total == 1 else "findings"):
        return "Result noun does not match the count"
    if total != severe + non_severe:
        return "Result counts do not add up"
    return None


def leaked_secret(text, environ=None):
    """Name of the first secret env var whose value appears in `text`, if any."""
    environ = os.environ if environ is None else environ
    for name in SECRET_ENV_VARS:
        value = environ.get(name, "")
        if len(value) < 8:
            continue
        if value in text:
            return name
        # A long secret quoted in part (or split) still leaks it: any
        # 16-character window of the value counts.
        if len(value) > 16 and any(
            value[i : i + 16] in text for i in range(len(value) - 15)
        ):
            return name
    return None


def main(argv):
    if len(argv) != 4:
        print(__doc__, file=sys.stderr)
        return 2
    execution_file, pr_number, output_file = argv[1], argv[2], Path(argv[3])
    text = last_result(Path(execution_file))
    # The leak check comes first, so a message that quotes a token is a
    # security stop (exit 2, no retry) whether or not it is a finished review.
    leaked = leaked_secret(text or "")
    if leaked:
        # A prompt-injected diff may have made the agent read and echo a
        # secret. Never print the value; fail as a setup/security error (no
        # retry, nothing posted).
        print(f"review contains the value of {leaked}; not posting", file=sys.stderr)
        return 2
    reason = validate(text, pr_number)
    if reason:
        print(f"incomplete review, not posting: {reason}", file=sys.stderr)
        # Diagnostic: a short excerpt of what the agent wrote, so a red run can
        # be understood from the log (GitHub also masks registered secrets).
        if text:
            print(f"result excerpt: {' '.join(text.split())[:200]}", file=sys.stderr)
        return 3
    output_file.write_text(unfence(text).strip() + "\n", encoding="utf-8")
    return 0


def run(argv):
    # Any uncaught error (unwritable output, ...) is a setup problem, not a bad
    # review: exit 2 so the workflow fails instead of spending a retry.
    try:
        return main(argv)
    except Exception as exc:  # noqa: BLE001
        print(f"check_review.py error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(run(sys.argv))
