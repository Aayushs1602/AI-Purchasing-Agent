"""Run the evaluation.

    python -m evals.run --mode scripted --strict          # golden trajectories (CI)
    python -m evals.run --mode replay                     # recorded Gemini runs, no key
    python -m evals.run --mode live --repeats 3 --record  # real Gemini; save recordings
    python -m evals.run --mode live --scenarios budget_cap,storage_cap

Reports go to evals/reports/<name>.md and .json (name defaults to the mode).
"""

from __future__ import annotations

import argparse
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from app.agent.llm.base import LLMClient
from app.agent.llm.factory import LLMConfigError, build_llm_client
from app.agent.llm.recording import RecordingClient, ReplayClient
from app.agent.llm.scripted import ScriptedClient
from app.config import get_settings
from app.scenarios import load_scenarios
from evals import report
from evals.golden import GOLDEN
from evals.harness import AUTO_APPROVER, CaseResult, run_case

REPORTS_DIR = Path(__file__).parent / "reports"


def main(argv: list[str] | None = None) -> int:
    sys.stdout.reconfigure(errors="replace")
    parser = argparse.ArgumentParser(prog="python -m evals.run", description=__doc__.split("\n")[0])
    parser.add_argument("--mode", choices=["scripted", "replay", "live"], required=True)
    parser.add_argument("--repeats", type=int, default=1)
    parser.add_argument("--scenarios", help="Comma-separated scenario ids (default: all)")
    parser.add_argument("--record", action="store_true", help="Live mode: save recordings")
    parser.add_argument("--name", help="Report file name (default: the mode)")
    parser.add_argument(
        "--strict", action="store_true", help="Exit 1 unless every run has the right outcome"
    )
    args = parser.parse_args(argv)

    settings = get_settings()
    scenarios = load_scenarios(settings.scenarios_dir)
    selected = scenarios
    if args.scenarios:
        wanted = set(args.scenarios.split(","))
        selected = [s for s in scenarios if s.id in wanted]
        unknown = wanted - {s.id for s in selected}
        if unknown:
            print(f"Unknown scenarios: {', '.join(sorted(unknown))}")
            return 2

    live: LLMClient | None = None
    if args.mode == "live":
        try:
            live = build_llm_client(settings.model_copy(update={"llm_provider": "gemini"}))
        except LLMConfigError as exc:
            print(exc)
            return 2
    model = {"scripted": "golden trajectories", "replay": "recordings", "live": None}[args.mode]
    model = model or live.model

    results: list[CaseResult] = []
    total = len(selected) * args.repeats
    with tempfile.TemporaryDirectory(prefix="evals-") as workdir:
        for scenario in selected:
            for repeat in range(1, args.repeats + 1):
                llm = _client(args.mode, scenario.id, live, settings.recordings_dir)
                recording = args.record and args.mode == "live" and repeat == 1
                if recording:
                    llm = RecordingClient(llm)
                result = run_case(
                    scenario,
                    scenarios,
                    llm,
                    repeat=repeat,
                    workdir=Path(workdir),
                    today=settings.sim_today,
                    max_llm_calls=settings.agent_max_llm_calls,
                )
                results.append(result)
                if recording:
                    _save_recording(llm, scenario, result, settings.recordings_dir)
                quantity = result.po_quantity if result.po_status else 0
                print(
                    f"[{len(results)}/{total}] {scenario.id} #{repeat}: "
                    f"{result.decision or result.status} {quantity} -> "
                    f"{'RIGHT' if result.grade.outcome else 'WRONG'} "
                    f"({result.llm_calls} calls, {result.seconds}s)",
                    flush=True,
                )

    path = REPORTS_DIR / (args.name or args.mode)
    report.write(results, selected, mode=args.mode, model=model, repeats=args.repeats, path=path)
    right = sum(r.grade.outcome for r in results)
    print(f"\n{right}/{len(results)} runs right. Report: {path.with_suffix('.md')}")
    return 1 if args.strict and right < len(results) else 0


def _client(mode: str, scenario_id: str, live: LLMClient | None, recordings: Path) -> LLMClient:
    if mode == "scripted":
        return ScriptedClient(GOLDEN[scenario_id])
    if mode == "replay":
        return ReplayClient(recordings)
    return live


def _save_recording(client: RecordingClient, scenario, result: CaseResult, directory: Path):
    client.save(
        directory / f"{scenario.id}.json",
        scenario=scenario.id,
        recommendation_id=scenario.world.recommendation.id,
        recorded_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        approval=({"approve": True, "approver": AUTO_APPROVER} if result.approvals_given else None),
        outcome={
            "status": result.status,
            "decision": result.decision,
            "po_quantity": result.po_quantity,
            "right": result.grade.outcome,
        },
    )


if __name__ == "__main__":
    raise SystemExit(main())
