"""LLM clients, tested offline: Gemini against a fake SDK client, plus the scripted client."""

import pytest
from google.genai import errors, types

from app.agent.llm.base import LLMError, ToolResult, ToolSpec
from app.agent.llm.factory import LLMConfigError, build_llm_client
from app.agent.llm.gemini import GeminiClient, RateLimiter
from app.agent.llm.scripted import ScriptedClient
from app.config import Settings

TOOLS = [ToolSpec("get_budget", "Remaining budget.", {"type": "object", "properties": {}})]


def model_turn(*parts, finish_reason="STOP"):
    return types.GenerateContentResponse(
        candidates=[
            types.Candidate(
                content=types.Content(role="model", parts=list(parts)),
                finish_reason=finish_reason,
            )
        ],
        usage_metadata=types.GenerateContentResponseUsageMetadata(
            prompt_token_count=100, candidates_token_count=20, thoughts_token_count=5
        ),
    )


def call_part(name, call_id="c1", signature=b"\x00sig\xff"):
    return types.Part(
        function_call=types.FunctionCall(id=call_id, name=name, args={}),
        thought_signature=signature,
    )


class FakeModels:
    """Replays queued responses (or raises queued errors) and records each request."""

    def __init__(self, *outcomes):
        self.outcomes = list(outcomes)
        self.requests = []

    def generate_content(self, *, model, contents, config):
        self.requests.append([c.model_copy(deep=True) for c in contents])
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class FakeGenai:
    def __init__(self, *outcomes):
        self.models = FakeModels(*outcomes)


def make_client(*outcomes, sleeps=None):
    fake = FakeGenai(*outcomes)
    sleeps = sleeps if sleeps is not None else []
    client = GeminiClient(
        api_key="unused",
        model="gemini-test",
        min_interval_seconds=0,
        client=fake,
        sleep=sleeps.append,
    )
    return client, fake


def api_error(code, message="boom", details=None):
    body = {"error": {"code": code, "message": message, "status": "X", "details": details or []}}
    return errors.APIError(code, body)


class TestGeminiSession:
    def test_tool_calls_and_results_round_trip(self):
        client, fake = make_client(
            model_turn(call_part("get_budget")),
            model_turn(types.Part(text="Budget is fine.")),
        )
        session = client.new_session("system", TOOLS)

        first = session.send_message("check the budget")
        assert [(c.id, c.name) for c in first.tool_calls] == [("c1", "get_budget")]
        assert (first.usage.prompt_tokens, first.usage.output_tokens) == (100, 20)

        second = session.send_tool_results([ToolResult("c1", "get_budget", {"ok": True})])
        assert second.text == "Budget is fine."
        assert second.tool_calls == []

        sent = fake.models.requests[1]
        response_part = sent[-1].parts[0].function_response
        assert (response_part.id, response_part.name) == ("c1", "get_budget")
        # The model turn went back with its thought signature untouched.
        assert sent[1].parts[0].thought_signature == b"\x00sig\xff"

    def test_history_export_and_resume_preserve_signatures(self):
        client, fake = make_client(
            model_turn(call_part("get_budget")),
            model_turn(types.Part(text="done")),
        )
        session = client.new_session("system", TOOLS)
        session.send_message("go")

        resumed = client.new_session("system", TOOLS, history=session.export_history())
        resumed.send_tool_results([ToolResult("c1", "get_budget", {"ok": True})])

        replayed_model_turn = fake.models.requests[1][1]
        assert replayed_model_turn.parts[0].thought_signature == b"\x00sig\xff"

    def test_calls_without_ids_get_synthetic_ones(self):
        client, fake = make_client(
            model_turn(call_part("get_budget", call_id=None)),
            model_turn(types.Part(text="ok")),
        )
        session = client.new_session("system", TOOLS)

        call = session.send_message("go").tool_calls[0]
        session.send_tool_results([ToolResult(call.id, call.name, {"ok": True})])

        assert call.id.startswith("call-")
        assert fake.models.requests[1][-1].parts[0].function_response.id is None

    def test_thought_parts_are_not_returned_as_text(self):
        client, _ = make_client(
            model_turn(types.Part(text="private reasoning", thought=True), types.Part(text="Hi"))
        )

        assert client.new_session("system", TOOLS).send_message("go").text == "Hi"

    def test_empty_response_is_an_error(self):
        client, _ = make_client(types.GenerateContentResponse(candidates=[]))

        with pytest.raises(LLMError, match="no content"):
            client.new_session("system", TOOLS).send_message("go")


class TestGeminiRetries:
    def test_rate_limit_is_retried_after_the_hinted_delay(self):
        sleeps = []
        client, _ = make_client(
            api_error(429, details=[{"retryDelay": "7s"}]),
            model_turn(types.Part(text="ok")),
            sleeps=sleeps,
        )

        assert client.new_session("system", TOOLS).send_message("go").text == "ok"
        assert 8.0 in sleeps  # hinted 7s + 1s margin

    def test_server_errors_back_off_exponentially_then_give_up(self):
        sleeps = []
        client, _ = make_client(*[api_error(503)] * 5, sleeps=sleeps)

        with pytest.raises(LLMError) as exc:
            client.new_session("system", TOOLS).send_message("go")

        assert exc.value.retryable
        assert [s for s in sleeps if s] == [2.0, 4.0, 8.0, 16.0]

    def test_daily_quota_is_not_retried(self):
        sleeps = []
        quota = api_error(
            429,
            details=[{"violations": [{"quotaId": "GenerateRequestsPerDayPerProjectPerModel"}]}],
        )
        client, _ = make_client(quota, sleeps=sleeps)

        with pytest.raises(LLMError, match="daily quota"):
            client.new_session("system", TOOLS).send_message("go")
        assert not [s for s in sleeps if s]

    def test_bad_requests_are_not_retried(self):
        client, fake = make_client(api_error(400, "invalid schema"))

        with pytest.raises(LLMError, match="invalid schema"):
            client.new_session("system", TOOLS).send_message("go")
        assert len(fake.models.requests) == 1


def test_rate_limiter_spaces_calls():
    now = [100.0]
    sleeps = []

    def sleep(seconds):
        sleeps.append(seconds)
        now[0] += seconds

    limiter = RateLimiter(4.0, clock=lambda: now[0], sleep=sleep)
    limiter.wait()
    now[0] += 1.0
    limiter.wait()

    assert sleeps == [3.0]


class TestScriptedClient:
    def test_plays_back_turns_and_passes_results_to_callables(self):
        client = ScriptedClient(
            [
                [{"name": "get_budget"}],
                lambda results: f"budget ok: {results[0].payload['ok']}",
            ]
        )
        session = client.new_session("system", TOOLS)

        call = session.send_message("go").tool_calls[0]
        reply = session.send_tool_results([ToolResult(call.id, call.name, {"ok": True})])

        assert reply.text == "budget ok: True"

    def test_resumed_session_continues_the_script(self):
        client = ScriptedClient([[{"name": "get_budget"}], "done"])
        first = client.new_session("system", TOOLS)
        first.send_message("go")

        resumed = client.new_session("system", TOOLS, history=first.export_history())

        assert resumed.send_tool_results([]).text == "done"

    def test_running_out_of_script_is_an_error(self):
        client = ScriptedClient([])

        with pytest.raises(LLMError, match="ran out"):
            client.new_session("system", TOOLS).send_message("go")


def test_factory_requires_a_key():
    with pytest.raises(LLMConfigError, match="GEMINI_API_KEY"):
        build_llm_client(Settings(gemini_api_key=""))


def test_factory_builds_gemini_client():
    client = build_llm_client(Settings(gemini_api_key="test-key", gemini_model="gemini-x"))

    assert (client.provider, client.model) == ("gemini", "gemini-x")
