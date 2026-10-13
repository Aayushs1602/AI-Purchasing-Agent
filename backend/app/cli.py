"""Command line for the agent.

python -m app.cli list                    # scenarios and their recommendations
python -m app.cli run accept_baseline     # run the agent on a scenario (or a REC id)
python -m app.cli run REC-1002 --reset    # restore demo data first
python -m app.cli show RUN-1A2B3C4D       # print a past run's trace again
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.agent.llm.factory import LLMConfigError, build_llm_client
from app.agent.orchestrator import AgentConfig, AgentRunner, RunConflictError
from app.config import get_settings
from app.db import make_engine, make_session_factory
from app.models import AgentRun, AgentStep, PurchaseOrder, Recommendation
from app.seed import ensure_database, reset_database


def main(argv: list[str] | None = None) -> int:
    # Model text can contain characters a Windows console cannot print; never crash on them.
    sys.stdout.reconfigure(errors="replace")

    parser = argparse.ArgumentParser(prog="python -m app.cli", description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list", help="List scenarios and recommendations")
    run_parser = sub.add_parser("run", help="Run the agent on a scenario or recommendation")
    run_parser.add_argument("target", help="Scenario id (e.g. accept_baseline) or REC id")
    run_parser.add_argument("--reset", action="store_true", help="Reset demo data first")
    show_parser = sub.add_parser("show", help="Print the trace of a past run")
    show_parser.add_argument("run_id")
    args = parser.parse_args(argv)

    settings = get_settings()
    engine = make_engine(settings.database_url)
    ensure_database(engine, settings.scenarios_dir, settings.sim_today)
    factory = make_session_factory(engine)

    if args.command == "list":
        with factory() as session:
            for rec in session.scalars(select(Recommendation).order_by(Recommendation.id)):
                print(f"{rec.id}  {rec.scenario:<26} {rec.status:<18} {rec.title}")
        return 0

    if args.command == "show":
        with factory() as session:
            run = session.get(AgentRun, args.run_id)
            if run is None:
                print(f"No run {args.run_id}")
                return 1
            print_run(session, run)
        return 0

    if args.reset:
        reset_database(engine, settings.scenarios_dir, settings.sim_today)
    with factory() as session:
        rec = session.scalar(
            select(Recommendation).where(
                or_(Recommendation.id == args.target, Recommendation.scenario == args.target)
            )
        )
    if rec is None:
        print(f"No scenario or recommendation '{args.target}'. Try: python -m app.cli list")
        return 1

    try:
        llm = build_llm_client(settings)
    except LLMConfigError as exc:
        print(exc)
        return 2
    runner = AgentRunner(
        factory,
        llm,
        today=settings.sim_today,
        config=AgentConfig(max_llm_calls=settings.agent_max_llm_calls),
    )
    print(f"Running {llm.provider}/{llm.model} on {rec.id} ({rec.scenario})...")
    try:
        run_id = runner.run(rec.id)
    except RunConflictError as exc:
        print(f"{exc}. Use --reset to start the scenario over.")
        return 1
    with factory() as session:
        print_run(session, session.get(AgentRun, run_id))
    return 0


# --- Output ------------------------------------------------------------------------------


def _args(args: dict[str, Any]) -> str:
    if not args:
        return ""
    text = ", ".join(f"{k}={json.dumps(v)}" for k, v in args.items())
    return f"({text if len(text) <= 90 else text[:87] + '...'})"


def _summarize_result(name: str, result: dict[str, Any]) -> str:
    if not result.get("ok"):
        error = result.get("error", {})
        return f"ERROR {error.get('code')}: {error.get('message', '')[:120]}"
    data = result.get("data", {})
    if name == "evaluate_order_quantity":
        codes = [f["code"] for f in data.get("hard_violations", []) + data.get("soft_warnings", [])]
        return (
            f"feasible={data.get('feasible')} need={data['projection']['net_need']} "
            f"approval={data.get('requires_approval')} {codes or ''}".rstrip()
        )
    if "purchase_order" in data:
        po = data["purchase_order"]
        outcome = data.get("outcome", "")
        validation = data.get("validation") or {}
        hard = [v["code"] for v in validation.get("hard_violations", [])]
        return f"{po['po_id']} {po['status']} {outcome} {hard or ''}".rstrip()
    if name == "escalate_to_human":
        return f"{data.get('escalation_id')} opened"
    return "ok"


def print_run(session: Session, run: AgentRun) -> None:
    rec = session.get(Recommendation, run.recommendation_id)
    print(f"\nRun {run.id} | {rec.id} {rec.title} | {run.provider}/{run.model}")
    for step in session.scalars(
        select(AgentStep).where(AgentStep.run_id == run.id).order_by(AgentStep.seq)
    ):
        payload = step.payload
        timing = f"{step.duration_ms} ms" if step.duration_ms is not None else ""
        if step.kind == "llm":
            calls = [c["name"] for c in payload["tool_calls"]]
            line = f"-> {', '.join(calls)}" if calls else "(no tool calls)"
            print(f"  #{step.seq:<3} model  {line}  {timing}")
            if payload.get("text"):
                print(f"         says: {payload['text'].strip()[:300]}")
        elif step.kind == "tool":
            summary = _summarize_result(step.name, payload["result"])
            print(f"  #{step.seq:<3} tool   {step.name}{_args(payload['args'])}  {summary}")
        else:
            print(f"  #{step.seq:<3} {step.kind:<6} {step.name}  {json.dumps(payload)[:160]}")

    print(f"\nStatus: {run.status}" + (f" | error: {run.error}" if run.error else ""))
    if run.decision:
        print(
            f"Decision: {run.decision.upper()} {run.final_quantity} units "
            f"| confidence {run.confidence}"
        )
        print(f"Rationale: {run.rationale}")
        for factor in run.key_factors or []:
            print(f"  - {factor['factor']} = {factor['value']}: {factor['impact']}")
        for risk in run.risks or []:
            print(f"  ! {risk}")
    po = session.scalar(select(PurchaseOrder).where(PurchaseOrder.run_id == run.id))
    if po is not None:
        print(
            f"PO: {po.id} {po.status} | {po.outstanding_quantity} units at "
            f"${po.effective_unit_cost:.2f} = ${po.value:,.2f}"
        )
    elapsed = ""
    if run.finished_at and run.started_at:
        elapsed = f" | {(run.finished_at - run.started_at).total_seconds():.1f} s"
    print(
        f"Model calls: {run.llm_calls} | tokens in/out: {run.prompt_tokens:,} / "
        f"{run.output_tokens:,}{elapsed}"
    )


if __name__ == "__main__":
    raise SystemExit(main())
