"""Describe a situation in words: one model call fills the scenario form."""

import json

import pytest
from pydantic import ValidationError

from app.agent.llm.scripted import ScriptedClient
from app.agent.scenario_draft import (
    DRAFT_PROMPT,
    TOOL_NAME,
    DraftError,
    draft_from_description,
    merge_draft,
    tool_spec,
)
from app.config import get_settings
from app.custom_scenarios import CustomScenarioInput, from_world
from app.scenarios import load_scenarios

BASE = from_world(load_scenarios(get_settings().scenarios_dir)[0].world).model_dump(mode="json")
SUPPLIERS = [{"id": "SUP-ANDES", "name": "Distribuidora Andes"}]
DESCRIPTION = "Only 120 left. A 320-unit PO was due 2 days ago and the supplier says it is on hold."


def fill(**args):
    return [{"name": TOOL_NAME, "args": args}]


def test_the_model_gets_one_tool_with_every_field_optional():
    spec = tool_spec()

    assert spec.name == TOOL_NAME
    assert "required" not in spec.parameters
    assert "pattern" not in json.dumps(spec.parameters)
    assert set(spec.parameters["properties"]) == set(CustomScenarioInput.model_fields)


class TestMerge:
    def test_fills_only_what_was_given(self):
        draft = merge_draft(BASE, {"on_hand": 120, "budget_committed": 18500})

        assert draft.filled == ["budget_committed", "on_hand"]
        assert draft.input["on_hand"] == 120
        assert draft.input["moq"] == BASE["moq"]
        assert draft.problems == []
        CustomScenarioInput.model_validate(draft.input)

    def test_nulls_mean_not_mentioned_and_unknown_fields_are_reported(self):
        draft = merge_draft(BASE, {"on_hand": None, "mood": "urgent", "moq": 600})

        assert draft.filled == ["moq"]
        assert draft.input["on_hand"] == BASE["on_hand"]
        assert [(p.field, p.message) for p in draft.problems] == [
            ("mood", "Not a form field; ignored.")
        ]

    def test_an_invalid_value_keeps_the_form_value(self):
        draft = merge_draft(BASE, {"case_pack": 0, "on_hand": 150})

        assert draft.filled == ["on_hand"]
        assert draft.input["case_pack"] == BASE["case_pack"]
        assert draft.problems[0].field == "case_pack"
        assert draft.problems[0].message.endswith("kept the form's value.")

    def test_a_supplier_named_without_an_id_is_new(self):
        draft = merge_draft(BASE, {"supplier_name": "Fresh Farms"})

        assert draft.input["supplier_id"] is None
        # Without stated rates the form still needs them: the preview shows that problem.
        with pytest.raises(ValidationError, match="new supplier needs"):
            CustomScenarioInput.model_validate(draft.input)


class TestModelCall:
    def test_one_call_fills_the_form(self):
        llm = ScriptedClient(
            [
                fill(
                    on_hand=120,
                    open_purchase_orders=[
                        {"quantity": 320, "eta_in_days": -2, "note": "Supplier says on hold."}
                    ],
                )
            ]
        )

        draft = draft_from_description(llm, DESCRIPTION, BASE, SUPPLIERS)

        assert draft.filled == ["on_hand", "open_purchase_orders"]
        assert draft.input["open_purchase_orders"][0]["eta_in_days"] == -2
        session = llm.sessions[0]
        assert session.system_prompt == DRAFT_PROMPT
        assert [t.name for t in session.tools] == [TOOL_NAME]
        sent = json.dumps(session.export_history())
        assert DESCRIPTION in sent and "SUP-ANDES" in sent

    def test_a_text_reply_is_nudged_once(self):
        llm = ScriptedClient(["Sure, let me think.", fill(on_hand=90)])

        assert draft_from_description(llm, DESCRIPTION, BASE, SUPPLIERS).input["on_hand"] == 90

    def test_no_form_after_the_nudge_is_an_error(self):
        llm = ScriptedClient(["Hmm.", "Still thinking."])

        with pytest.raises(DraftError, match="did not fill in the form"):
            draft_from_description(llm, DESCRIPTION, BASE, SUPPLIERS)

    def test_model_failure_is_an_error(self):
        with pytest.raises(DraftError, match="ran out"):
            draft_from_description(ScriptedClient([]), DESCRIPTION, BASE, SUPPLIERS)
