"""Call outcome taxonomy shared by Gemini tools, LPTracker routing and UI data."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class CallOutcomeDefinition:
    key: str
    label: str
    tool_name: str
    condition_field: str
    stage_id_field: str
    stage_name_field: str


# These six outcomes are determined from the conversation by Gemini Live.
# ``no_answer`` is deliberately not a Gemini tool: when no conversation/media
# exists, only the backend can determine that operational result reliably.
CONVERSATION_OUTCOMES: tuple[CallOutcomeDefinition, ...] = (
    CallOutcomeDefinition(
        key="lead",
        label="Лид",
        tool_name="mark_call_as_lead",
        condition_field="lead_condition",
        stage_id_field="lead_stage_id",
        stage_name_field="lead_stage_name",
    ),
    CallOutcomeDefinition(
        key="special",
        label="Спецстадия",
        tool_name="mark_call_as_special",
        condition_field="special_condition",
        stage_id_field="special_stage_id",
        stage_name_field="special_stage_name",
    ),
    CallOutcomeDefinition(
        key="refusal",
        label="Отказ",
        tool_name="mark_call_as_refusal",
        condition_field="refusal_condition",
        stage_id_field="refusal_stage_id",
        stage_name_field="refusal_stage_name",
    ),
    CallOutcomeDefinition(
        key="callback",
        label="Перезвонить",
        tool_name="mark_call_as_callback",
        condition_field="callback_condition",
        stage_id_field="callback_stage_id",
        stage_name_field="callback_stage_name",
    ),
    CallOutcomeDefinition(
        key="stop_list",
        label="Стоп-лист",
        tool_name="mark_call_as_stop_list",
        condition_field="stop_list_condition",
        stage_id_field="stop_list_stage_id",
        stage_name_field="stop_list_stage_name",
    ),
    CallOutcomeDefinition(
        key="answering_machine",
        label="Автоответчик",
        tool_name="mark_call_as_answering_machine",
        condition_field="answering_machine_condition",
        stage_id_field="answering_machine_stage_id",
        stage_name_field="answering_machine_stage_name",
    ),
)

OUTCOME_BY_KEY = {item.key: item for item in CONVERSATION_OUTCOMES}
OUTCOME_BY_TOOL = {item.tool_name: item for item in CONVERSATION_OUTCOMES}

NO_ANSWER_KEY = "no_answer"
NO_ANSWER_LABEL = "Недозвон"
NO_ANSWER_STAGE_ID_FIELD = "no_answer_stage_id"
NO_ANSWER_STAGE_NAME_FIELD = "no_answer_stage_name"


def configured_tool_declarations(robot: dict[str, Any]) -> list[dict[str, Any]]:
    """Build explicit Gemini function declarations for configured outcomes."""
    declarations: list[dict[str, Any]] = []
    for definition in CONVERSATION_OUTCOMES:
        condition = str(robot.get(definition.condition_field) or "").strip()
        if not condition:
            continue
        declarations.append(
            {
                "name": definition.tool_name,
                "description": (
                    f"Зафиксировать итог звонка «{definition.label}». "
                    "Вызывай функцию только когда разговор уверенно соответствует "
                    f"следующему условию: {condition}"
                ),
                "parameters": {
                    "type": "OBJECT",
                    "properties": {
                        "evidence": {
                            "type": "STRING",
                            "description": (
                                "Краткое основание решения без персональных данных "
                                "и без дословной длинной цитаты."
                            ),
                        }
                    },
                    "required": ["evidence"],
                },
            }
        )
    return declarations


def build_outcome_instruction(robot: dict[str, Any]) -> str:
    """Build deterministic prompt rules mapping conditions to tool names."""
    configured: list[str] = []
    for definition in CONVERSATION_OUTCOMES:
        condition = str(robot.get(definition.condition_field) or "").strip()
        if condition:
            configured.append(
                f"- {definition.label}: если выполняется условие «{condition}», "
                f"вызови {definition.tool_name}."
            )
    if not configured:
        return ""
    return "\n".join(
        [
            "КЛАССИФИКАЦИЯ РЕЗУЛЬТАТА ЗВОНКА:",
            "Постоянно анализируй разговор, но не озвучивай классификацию человеку.",
            "Вызывай только объявленные функции. Не пиши название функции словами.",
            "Вызывай функцию, когда условие стало достаточно ясным. Если итог разговора "
            "позже изменился, вызови функцию нового актуального итога ещё раз; backend "
            "сохранит последний подтверждённый итог.",
            "Не вызывай функцию только из-за предположения или двусмысленной фразы.",
            *configured,
        ]
    )


def destination_for_outcome(
    assignment: dict[str, Any], outcome: str | None
) -> tuple[int | None, str]:
    """Return configured LPTracker destination stage for an outcome."""
    if not outcome:
        return None, ""
    if outcome == NO_ANSWER_KEY:
        stage_id = assignment.get(NO_ANSWER_STAGE_ID_FIELD)
        stage_name = str(assignment.get(NO_ANSWER_STAGE_NAME_FIELD) or "")
    else:
        definition = OUTCOME_BY_KEY.get(outcome)
        if definition is None:
            return None, ""
        stage_id = assignment.get(definition.stage_id_field)
        stage_name = str(assignment.get(definition.stage_name_field) or "")
    try:
        return (int(stage_id), stage_name) if stage_id else (None, stage_name)
    except (TypeError, ValueError):
        return None, stage_name


def outcome_counts_as_lead(assignment: dict[str, Any], outcome: str | None) -> bool:
    if outcome == "lead":
        return True
    return outcome == "special" and bool(assignment.get("count_special_as_lead"))
