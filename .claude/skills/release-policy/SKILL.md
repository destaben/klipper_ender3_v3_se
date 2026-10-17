---
name: release-policy
description: Severity definitions and gates used by /release and bug-hunter — what counts as severe, what gets auto-fixed, and when the user is asked instead.
---

## Severe
- Security issues (injection, secret leakage, unsafe shell/file handling).
- Data loss (destructive ops without confirmation, silently dropped state).
- Failing tests.
- Broken public contracts (a changed signature, return shape, or exported
  behavior that an existing caller relies on).

**Severe findings are auto-fixed and re-tested, capped at 2 loops.** If still
severe after 2 passes, `/release` stops and asks the user — it does not keep
retrying.

## Non-severe
Everything else: style inconsistencies, missed opportunities for reuse,
non-breaking API additions, documentation gaps that don't affect behavior.

**Non-severe findings are listed, and the user is asked before any change is
made.** Never auto-fix non-severe findings.

## None
No findings. `/release` proceeds straight to the documenter step.

## Why this split
Auto-fixing is only safe when the bar for "broken" is unambiguous (fails a
test, leaks a secret, loses data). Anything judgment-based goes to the user,
because an agent's judgment on style or scope is a worse proxy for the user's
intent than just asking.
