from elvin.integrations.gemini_live import build_system_instruction
from elvin.services.call_outcomes import (
    configured_tool_declarations,
    destination_for_outcome,
    outcome_counts_as_lead,
)


def _robot() -> dict[str, object]:
    return {
        "lead_condition": "клиент согласился на повторный контакт",
        "special_condition": "клиент согласился на видеовстречу",
        "refusal_condition": "клиент явно отказался",
        "callback_condition": "клиент попросил перезвонить",
        "stop_list_condition": "клиент попросил больше не звонить",
        "answering_machine_condition": "ответил автоответчик",
    }


def test_each_configured_outcome_gets_its_own_tool() -> None:
    declarations = configured_tool_declarations(_robot())
    assert len(declarations) == 6
    names = {item["name"] for item in declarations}
    assert names == {
        "mark_call_as_lead",
        "mark_call_as_special",
        "mark_call_as_refusal",
        "mark_call_as_callback",
        "mark_call_as_stop_list",
        "mark_call_as_answering_machine",
    }
    assert all(item["parameters"]["required"] == ["evidence"] for item in declarations)


def test_system_instruction_contains_explicit_tool_mapping() -> None:
    instruction = build_system_instruction(_robot())
    assert "КЛАССИФИКАЦИЯ РЕЗУЛЬТАТА ЗВОНКА" in instruction
    assert "mark_call_as_lead" in instruction
    assert "mark_call_as_stop_list" in instruction
    assert "не озвучивай классификацию человеку" in instruction


def test_destination_and_lead_counter_rules() -> None:
    assignment = {
        "lead_stage_id": 10,
        "lead_stage_name": "Лид",
        "special_stage_id": 20,
        "special_stage_name": "Видеовстреча",
        "no_answer_stage_id": 30,
        "no_answer_stage_name": "Недозвон",
        "count_special_as_lead": True,
    }
    assert destination_for_outcome(assignment, "lead") == (10, "Лид")
    assert destination_for_outcome(assignment, "special") == (20, "Видеовстреча")
    assert destination_for_outcome(assignment, "no_answer") == (30, "Недозвон")
    assert outcome_counts_as_lead(assignment, "lead") is True
    assert outcome_counts_as_lead(assignment, "special") is True
    assignment["count_special_as_lead"] = False
    assert outcome_counts_as_lead(assignment, "special") is False
