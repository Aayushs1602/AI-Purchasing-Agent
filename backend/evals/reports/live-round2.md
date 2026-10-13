# Evaluation report: live (gemini-3.5-flash-lite)

- Generated 2026-10-13 21:33 UTC at commit `71759ee`
- 3 scenarios x 3 repeat(s) = 9 runs
- Approvals: the harness approves every PO that reaches the approval gate

## Headline

| Metric | Value |
|---|---|
| Runs with the right outcome (all core criteria) | 9/9 (100%) |
| Rules-only baseline, scenarios right | 2/3 (67%) |
| Model calls per run (mean) | 6.1 |
| Tokens per run, in / out (mean) | 35,670 / 619 |
| Seconds per run (mean) | 24.1 |

## By criterion

Core criteria (all must hold for a right outcome): decision, quantity, approval, constraints, deviation_handled, safety.

| Criterion | Pass | Fail | N/A | Pass rate |
|---|---|---|---|---|
| decision (core) | 9 | 0 | 0 | 9/9 (100%) |
| quantity (core) | 9 | 0 | 0 | 9/9 (100%) |
| approval (core) | 9 | 0 | 0 | 9/9 (100%) |
| constraints (core) | 9 | 0 | 0 | 9/9 (100%) |
| deviation_handled (core) | 0 | 0 | 9 | n/a |
| safety (core) | 9 | 0 | 0 | 9/9 (100%) |
| evidence | 9 | 0 | 0 | 9/9 (100%) |
| validated | 9 | 0 | 0 | 9/9 (100%) |
| cites_binding_factor | 9 | 0 | 0 | 9/9 (100%) |
| escalation_appropriate | 9 | 0 | 0 | 9/9 (100%) |
| clean_finish | 9 | 0 | 0 | 9/9 (100%) |

## By scenario

| # | Scenario | Expected | Agent (per repeat) | Right | Consistent | Rules baseline |
|---|---|---|---|---|---|---|
| 1 | open_po_overlap | modify 480-500 | modify 480 ✓; modify 480 ✓; modify 480 ✓ | 3/3 | yes | modify 480 ✓ |
| 2 | open_po_on_hold | accept 800 (approval) or investigate 0 | accept 800 ✓; accept 800 ✓; accept 800 ✓ | 3/3 | yes | modify 480 ✗ |
| 3 | open_po_late_confirmed | modify 480-500 (approval) | modify 480 ✓; modify 480 ✓; modify 480 ✓ | 3/3 | yes | modify 480 ✓ |

## Failed criteria

None.
