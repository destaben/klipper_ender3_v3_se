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
made.** Never auto-fix non-severe findings. (`/auto` is the owner-invoked exception: nobody
can be asked mid-run, so it lists them in the PR description and applies none; see the `auto` skill.)

**Delegated exception.** When the owner has explicitly handed over a PR ("handle the PRs while I am
away", "merge them after review"), a non-severe finding that is cheap, clearly right, or in code the
change already touches is fixed in the same PR, once: no extra review round for it, and a test when
the fix is testable. Every other non-severe finding is listed with one sentence on why it was left.
A finding that needs a design choice is never decided here: it is listed for the owner. Severe
findings keep the rule above. The exception does not apply under `/auto`, which lists non-severe
findings and applies none. The review-iteration loop in `github-ops` keeps its own precondition (the owner
asked, in this session, to iterate on the automated review); this exception does not widen it.

## None
No findings. `/release` proceeds straight to the documenter step.

## Why this split
Auto-fixing is only safe when the bar for "broken" is unambiguous (fails a
test, leaks a secret, loses data). Anything judgment-based goes to the user,
because an agent's judgment on style or scope is a worse proxy for the user's
intent than just asking. The delegated exception exists because, with the owner away, asking
means waiting for days to fix a typo; it stays limited to cheap, evident fixes for that reason.
