from elvin.integrations.gemini_live import build_system_instruction
from elvin.services.call_outcomes import (
    END_CALL_TOOL_NAME,
    configured_tool_declarations,
    destination_for_outcome,
    outcome_counts_as_lead,
    resolve_stage_references,
)


def _robot() -> dict[str, object]:
    return {
        "role_prompt": "МОЙ СИСТЕМНЫЙ PROMPT",
        "knowledge_base": "МОЯ БАЗА ЗНАНИЙ",
        "lead_condition": "клиент согласился на повторный контакт",
        "special_condition": "клиент согласился на видеовстречу",
        "refusal_condition": "клиент явно отказался",
        "callback_condition": "клиент попросил перезвонить",
        "stop_list_condition": "клиент попросил больше не звонить",
        "answering_machine_condition": "ответил автоответчик",
        "call_end_condition": (
            "Заверши звонок после прощания либо при {{stage:stop_list}} "
            "или {{stage:callback}}."
        ),
    }


def test_each_configured_outcome_and_end_call_gets_its_own_tool() -> None:
    declarations = configured_tool_declarations(_robot())
    assert len(declarations) == 7
    names = {item["name"] for item in declarations}
    assert names == {
        "mark_call_as_lead",
        "mark_call_as_special",
        "mark_call_as_refusal",
        "mark_call_as_callback",
        "mark_call_as_stop_list",
        "mark_call_as_answering_machine",
        END_CALL_TOOL_NAME,
    }
    assert all(item["parameters"]["required"] for item in declarations)


def test_no_hidden_behavior_is_added_to_system_instruction_or_stage_tools() -> None:
    robot = _robot()
    instruction = build_system_instruction(robot)
    assert instruction == "МОЙ СИСТЕМНЫЙ PROMPT\n\nМОЯ БАЗА ЗНАНИЙ"
    assert "Ты голосовой ИИ-робот" not in instruction
    assert "КЛАССИФИКАЦИЯ РЕЗУЛЬТАТА" not in instruction

    declarations = configured_tool_declarations(robot)
    lead = next(item for item in declarations if item["name"] == "mark_call_as_lead")
    assert lead["description"] == robot["lead_condition"]


def test_empty_visible_fields_produce_no_system_instruction_or_tools() -> None:
    assert build_system_instruction({}) == ""
    assert configured_tool_declarations({}) == []


def test_end_call_stage_references_expand_only_from_visible_fields() -> None:
    robot = _robot()
    resolved = resolve_stage_references(str(robot["call_end_condition"]), robot)
    assert "клиент попросил больше не звонить" in resolved
    assert "клиент попросил перезвонить" in resolved
    assert "{{stage:" not in resolved
    end_call = next(
        item for item in configured_tool_declarations(robot)
        if item["name"] == END_CALL_TOOL_NAME
    )
    assert end_call["description"] == resolved


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
