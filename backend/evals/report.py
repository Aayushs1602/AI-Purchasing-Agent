"""Write evaluation results as a markdown report (for people) and JSON (for diffing)."""

from __future__ import annotations

import json
import subprocess
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from statistics import fmean

from app.scenarios import Scenario
from evals.graders import CORE, CRITERIA
from evals.harness import CaseResult


def _commit() -> str:
    try:
        return subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True, check=True
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def _pct(passed: int, total: int) -> str:
    return f"{passed}/{total} ({passed / total:.0%})" if total else "n/a"


def _result_label(r: CaseResult) -> str:
    quantity = r.po_quantity if r.po_status not in (None, "cancelled", "declined") else 0
    label = f"{r.decision or r.status} {quantity}"
    return f"{label} {'✓' if r.grade.outcome else '✗'}"


def _expected_label(scenario: Scenario) -> str:
    parts = []
    for o in scenario.expect.acceptable:
        low, high = o.po_quantity
        qty = str(low) if low == high else f"{low}-{high}"
        approval = {"required": " (approval)", "not_required": "", "any": ""}[o.approval]
        parts.append(f"{o.decision} {qty}{approval}")
    return " or ".join(parts)


def render(
    results: list[CaseResult],
    scenarios: list[Scenario],
    *,
    mode: str,
    model: str,
    repeats: int,
) -> str:
    by_scenario: dict[str, list[CaseResult]] = defaultdict(list)
    for r in results:
        by_scenario[r.scenario_id].append(r)
    order = [s for s in scenarios if s.id in by_scenario]

    outcome_passes = sum(r.grade.outcome for r in results)
    baseline_passes = sum(by_scenario[s.id][0].baseline_ok for s in order)
    lines = [
        f"# Evaluation report: {mode} ({model})",
        "",
        f"- Generated {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC at commit `{_commit()}`",
        f"- {len(order)} scenarios x {repeats} repeat(s) = {len(results)} runs",
        "- Approvals: the harness approves every PO that reaches the approval gate",
        "",
        "## Headline",
        "",
        "| Metric | Value |",
        "|---|---|",
        f"| Runs with the right outcome (all core criteria) | "
        f"{_pct(outcome_passes, len(results))} |",
        f"| Rules-only baseline, scenarios right | {_pct(baseline_passes, len(order))} |",
        f"| Model calls per run (mean) | {fmean(r.llm_calls for r in results):.1f} |",
        f"| Tokens per run, in / out (mean) | "
        f"{fmean(r.prompt_tokens for r in results):,.0f} / "
        f"{fmean(r.output_tokens for r in results):,.0f} |",
        f"| Seconds per run (mean) | {fmean(r.seconds for r in results):.1f} |",
        "",
        "## By criterion",
        "",
        f"Core criteria (all must hold for a right outcome): {', '.join(CORE)}.",
        "",
        "| Criterion | Pass | Fail | N/A | Pass rate |",
        "|---|---|---|---|---|",
    ]
    for name in CRITERIA:
        values = [r.grade.criteria[name].passed for r in results]
        passed, failed = values.count(True), values.count(False)
        lines.append(
            f"| {name}{' (core)' if name in CORE else ''} | {passed} | {failed} | "
            f"{values.count(None)} | {_pct(passed, passed + failed)} |"
        )

    lines += [
        "",
        "## By scenario",
        "",
        "| # | Scenario | Expected | Agent (per repeat) | Right | Consistent | Rules baseline |",
        "|---|---|---|---|---|---|---|",
    ]
    for index, scenario in enumerate(order, 1):
        runs = by_scenario[scenario.id]
        signatures = {(r.decision, r.po_quantity, r.po_status) for r in runs}
        baseline = runs[0].baseline
        lines.append(
            f"| {index} | {scenario.id} | {_expected_label(scenario)} | "
            f"{'; '.join(_result_label(r) for r in runs)} | "
            f"{sum(r.grade.outcome for r in runs)}/{len(runs)} | "
            f"{'yes' if len(signatures) == 1 else 'no'} | "
            f"{baseline.decision} {baseline.quantity} {'✓' if runs[0].baseline_ok else '✗'} |"
        )

    failures = [r for r in results if r.grade.failures() or r.error]
    lines += ["", "## Failed criteria", ""]
    if not failures:
        lines.append("None.")
    for r in failures:
        lines.append(f"### {r.scenario_id} (repeat {r.repeat}) - status {r.status}")
        lines.append("")
        for criterion in r.grade.failures():
            core = " (core)" if criterion.name in CORE else ""
            lines.append(f"- **{criterion.name}**{core}: {criterion.detail}")
        if r.error:
            lines.append(f"- run error: {r.error}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def to_json(results: list[CaseResult]) -> list[dict]:
    return [
        {
            "scenario": r.scenario_id,
            "repeat": r.repeat,
            "status": r.status,
            "decision": r.decision,
            "po_quantity": r.po_quantity,
            "po_status": r.po_status,
            "approvals_given": r.approvals_given,
            "llm_calls": r.llm_calls,
            "prompt_tokens": r.prompt_tokens,
            "output_tokens": r.output_tokens,
            "seconds": r.seconds,
            "error": r.error,
            "outcome": r.grade.outcome,
            "criteria": {
                c.name: {"passed": c.passed, "detail": c.detail} for c in r.grade.criteria.values()
            },
            "baseline": {
                "decision": r.baseline.decision,
                "quantity": r.baseline.quantity,
                "right": r.baseline_ok,
            },
        }
        for r in results
    ]


def write(results: list[CaseResult], scenarios, *, mode, model, repeats, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    markdown = render(results, scenarios, mode=mode, model=model, repeats=repeats)
    path.with_suffix(".md").write_text(markdown, encoding="utf-8")
    path.with_suffix(".json").write_text(
        json.dumps(to_json(results), indent=2) + "\n", encoding="utf-8"
    )
