"""Call outcome taxonomy shared by Gemini tools, LPTracker routing and UI data."""

from __future__ import annotations

import re
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

END_CALL_TOOL_NAME = "end_call"
_STAGE_REFERENCE_PATTERN = re.compile(r"\{\{stage:([a-z_]+)\}\}")

NO_ANSWER_KEY = "no_answer"
NO_ANSWER_LABEL = "Недозвон"
NO_ANSWER_STAGE_ID_FIELD = "no_answer_stage_id"
NO_ANSWER_STAGE_NAME_FIELD = "no_answer_stage_name"


def resolve_stage_references(text: str, robot: dict[str, Any]) -> str:
    """Replace visible ``{{stage:key}}`` references with visible stage text.

    The replacement contains no backend-authored behavioral instruction: the
    model receives only text that the user entered in the robot editor.
    Unknown or empty references are left untouched so a configuration mistake
    remains visible in the prompt preview instead of silently changing meaning.
    """

    source = str(text or "")

    def replace(match: re.Match[str]) -> str:
        definition = OUTCOME_BY_KEY.get(match.group(1))
        if definition is None:
            return match.group(0)
        condition = str(robot.get(definition.condition_field) or "").strip()
        return condition if condition else match.group(0)

    return _STAGE_REFERENCE_PATTERN.sub(replace, source)


def configured_tool_declarations(robot: dict[str, Any]) -> list[dict[str, Any]]:
    """Build tools using only conditions visible in the robot editor."""

    declarations: list[dict[str, Any]] = []
    for definition in CONVERSATION_OUTCOMES:
        condition = str(robot.get(definition.condition_field) or "").strip()
        if not condition:
            continue
        declarations.append(
            {
                "name": definition.tool_name,
                # Deliberately verbatim: no hidden instruction or wrapper is
                # added around the user-authored stage condition.
                "description": condition,
                "parameters": {
                    "type": "OBJECT",
                    "properties": {"evidence": {"type": "STRING"}},
                    "required": ["evidence"],
                },
            }
        )

    end_condition = resolve_stage_references(
        str(robot.get("call_end_condition") or "").strip(), robot
    ).strip()
    if end_condition:
        declarations.append(
            {
                "name": END_CALL_TOOL_NAME,
                # The complete hangup condition is authored in the UI. Stage
                # placeholders resolve only to other UI-authored conditions.
                "description": end_condition,
                "parameters": {
                    "type": "OBJECT",
                    "properties": {"reason": {"type": "STRING"}},
                    "required": ["reason"],
                },
            }
        )
    return declarations


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
