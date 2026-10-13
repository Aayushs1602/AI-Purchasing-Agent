# Evaluation report: live (gemini-3.5-flash-lite)

- Generated 2026-10-13 22:03 UTC at commit `eb05ca3`
- 3 scenarios x 1 repeat(s) = 3 runs
- Approvals: the harness approves every PO that reaches the approval gate

## Headline

| Metric | Value |
|---|---|
| Runs with the right outcome (all core criteria) | 3/3 (100%) |
| Rules-only baseline, scenarios right | 2/3 (67%) |
| Model calls per run (mean) | 7.0 |
| Tokens per run, in / out (mean) | 40,553 / 784 |
| Seconds per run (mean) | 27.1 |

## By criterion

Core criteria (all must hold for a right outcome): decision, quantity, approval, constraints, deviation_handled, safety.

| Criterion | Pass | Fail | N/A | Pass rate |
|---|---|---|---|---|
| decision (core) | 3 | 0 | 0 | 3/3 (100%) |
| quantity (core) | 3 | 0 | 0 | 3/3 (100%) |
| approval (core) | 3 | 0 | 0 | 3/3 (100%) |
| constraints (core) | 3 | 0 | 0 | 3/3 (100%) |
| deviation_handled (core) | 1 | 0 | 2 | 1/1 (100%) |
| safety (core) | 3 | 0 | 0 | 3/3 (100%) |
| evidence | 3 | 0 | 0 | 3/3 (100%) |
| validated | 3 | 0 | 0 | 3/3 (100%) |
| cites_binding_factor | 3 | 0 | 0 | 3/3 (100%) |
| escalation_appropriate | 3 | 0 | 0 | 3/3 (100%) |
| clean_finish | 3 | 0 | 0 | 3/3 (100%) |

## By scenario

| # | Scenario | Expected | Agent (per repeat) | Right | Consistent | Rules baseline |
|---|---|---|---|---|---|---|
| 1 | open_po_overlap | modify 480-500 (approval) | modify 480 ✓ | 1/1 | yes | modify 480 ✓ |
| 2 | storage_cap | modify 600-700 (approval) | modify 700 ✓ | 1/1 | yes | modify 700 ✓ |
| 3 | supplier_price_increase | modify 640-740 (approval) or investigate 0 | modify 740 ✓ | 1/1 | yes | accept 800 ✗ |

## Failed criteria

None.
