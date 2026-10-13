"""Record a run, then replay it on fresh data: the tools and validators run for real again."""

import json

import pytest
from sqlalchemy import select

from app.agent.llm.factory import build_llm_client
from app.agent.llm.recording import RecordingClient, ReplayClient
from app.agent.llm.scripted import ScriptedClient
from app.agent.orchestrator import AgentRunner
from app.config import Settings, get_settings
from app.db import create_schema, make_engine, make_session_factory
from app.models import AgentRun, Budget, PurchaseOrder
from app.scenarios import load_scenarios
from app.seed import seed_scenarios
from tests.agent.conftest import TODAY
from tests.agent.test_orchestrator import READS, create_po, finalize, submit

ACCEPT_SCRIPT = [
    READS,
    [{"name": "evaluate_order_quantity", "args": {"quantity": 800}}],
    create_po(800),
    submit(800, 2000),
    finalize("accept", 800),
]


@pytest.fixture
def fresh_factory(tmp_path):
    """A new, freshly seeded database for each call (replays must not reuse the original)."""
    count = 0

    def _make():
        nonlocal count
        count += 1
        engine = make_engine(f"sqlite:///{(tmp_path / f'db{count}.db').as_posix()}")
        create_schema(engine)
        factory = make_session_factory(engine)
        with factory() as session:
            seed_scenarios(session, load_scenarios(get_settings().scenarios_dir), TODAY)
        return factory

    return _make


def outcome(factory, run_id):
    with factory() as session:
        run = session.get(AgentRun, run_id)
        po = session.scalar(select(PurchaseOrder).where(PurchaseOrder.run_id == run_id))
        return run.status, run.decision, run.final_quantity, po.status if po else None


def record(factory, rec_id, script, path, approve=None):
    recorder = RecordingClient(ScriptedClient(script))
    runner = AgentRunner(factory, recorder, today=TODAY)
    run_id = runner.run(rec_id)
    if approve is not None:
        runner.resume(run_id, approve=approve, approver="buyer:test")
    recorder.save(path, scenario="unused")
    return run_id


def test_replay_reproduces_the_recorded_run(fresh_factory, tmp_path):
    original = fresh_factory()
    run_id = record(original, "REC-1001", ACCEPT_SCRIPT, tmp_path / "accept_baseline.json")

    replayed = fresh_factory()
    replay_id = AgentRunner(replayed, ReplayClient(tmp_path), today=TODAY).run("REC-1001")

    assert outcome(replayed, replay_id) == outcome(original, run_id)
    assert outcome(replayed, replay_id) == ("completed", "accept", 800, "confirmed")


def test_recording_holds_model_turns_only(fresh_factory, tmp_path):
    path = tmp_path / "accept_baseline.json"
    record(fresh_factory(), "REC-1001", ACCEPT_SCRIPT, path)

    saved = json.loads(path.read_text(encoding="utf-8"))

    assert saved["format"] == 1
    assert len(saved["turns"]) == 5
    first = saved["turns"][0]
    assert set(first) == {"text", "tool_calls", "usage", "finish_reason", "results"}
    assert first["results"][1] == {"name": "get_inventory", "ok": True, "error": None}
    assert "outstanding_quantity" not in json.dumps(saved)  # signatures only, never tool data


def test_replay_resumes_across_an_approval_pause(fresh_factory, tmp_path):
    script = [
        READS,
        create_po(800, 3600),
        submit(800, 3600),
        finalize("accept", 800, factor="order_value", value=3600),
    ]
    record(fresh_factory(), "REC-1012", script, tmp_path / "high_value_approval.json", True)

    replayed = fresh_factory()
    runner = AgentRunner(replayed, ReplayClient(tmp_path), today=TODAY)
    run_id = runner.run("REC-1012")
    assert outcome(replayed, run_id)[0] == "awaiting_approval"

    runner.resume(run_id, approve=True, approver="buyer:test")

    assert outcome(replayed, run_id) == ("completed", "accept", 800, "confirmed")


def test_divergence_from_the_recording_is_detected(fresh_factory, tmp_path):
    record(fresh_factory(), "REC-1001", ACCEPT_SCRIPT, tmp_path / "accept_baseline.json")
    replayed = fresh_factory()
    with replayed() as session:  # the data changed since recording: no budget line any more
        session.delete(session.scalar(select(Budget).where(Budget.category == "Water")))
        session.commit()

    run_id = AgentRunner(replayed, ReplayClient(tmp_path), today=TODAY).run("REC-1001")

    with replayed() as session:
        run = session.get(AgentRun, run_id)
        assert "Replay diverged" in run.error
        assert "get_budget: NO_BUDGET" in run.error
        assert run.status == "needs_review"  # degraded mode takes over


def test_missing_recording_explains_how_to_make_one(fresh_factory, tmp_path):
    factory = fresh_factory()
    run_id = AgentRunner(factory, ReplayClient(tmp_path), today=TODAY).run("REC-1002")

    with factory() as session:
        error = session.get(AgentRun, run_id).error
    assert "No recording for scenario 'open_po_overlap'" in error
    assert "python -m app.cli run open_po_overlap --reset --record" in error


def test_factory_builds_the_replay_client(tmp_path):
    client = build_llm_client(Settings(llm_provider="replay", recordings_dir=tmp_path))

    assert isinstance(client, ReplayClient)
    assert client.directory == tmp_path
