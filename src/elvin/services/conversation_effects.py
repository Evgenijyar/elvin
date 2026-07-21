"""Configurable conversation effects for the Gemini Actor/Director runtime.

Every effect is opt-in.  Existing robots without an ``effects_config`` value
therefore retain the exact stable v1.1.0 media behaviour until an operator
explicitly enables one or more effects in the UI.
"""

from __future__ import annotations

import json
from copy import deepcopy
from typing import Any


# Effects in this set need semantic decisions or auxiliary native audio from
# the second Gemini Live session.  The remaining effects are deterministic
# local DSP and must not add a second network session, another API key, or an
# avoidable setup delay to a call.
DIRECTOR_EFFECT_KEYS = frozenset(
    {
        "semantic_interruption",
        "adaptive_response_delay",
        "listener_backchannels",
        "pace_matching",
        "latency_fillers",
        "interruption_resume",
        "micro_pauses",
    }
)


_EFFECTS: tuple[dict[str, Any], ...] = (
    {
        "key": "natural_interruption",
        "label": "Мягкое перебивание",
        "description": (
            "Приглушает голос Актёра, подтверждает перебивание и завершает "
            "реплику коротким замедленным хвостом с сохранением тональности."
        ),
        "fields": (
            {
                "key": "duck_db",
                "label": "Приглушение, дБ",
                "type": "number",
                "min": -30,
                "max": -1,
                "step": 1,
                "default": -9,
            },
            {
                "key": "duck_attack_ms",
                "label": "Скорость приглушения, мс",
                "type": "number",
                "min": 5,
                "max": 250,
                "step": 5,
                "default": 55,
            },
            {
                "key": "confirm_ms",
                "label": "Подтверждение перебивания, мс",
                "type": "number",
                "min": 40,
                "max": 800,
                "step": 10,
                "default": 140,
            },
            {
                "key": "release_ms",
                "label": "Длина мягкого хвоста, мс",
                "type": "number",
                "min": 60,
                "max": 1000,
                "step": 10,
                "default": 280,
            },
            {
                "key": "slowdown_percent",
                "label": "Замедление хвоста, %",
                "type": "number",
                "min": 0,
                "max": 35,
                "step": 1,
                "default": 12,
            },
            {
                "key": "fade_start_percent",
                "label": "Начало затухания, % хвоста",
                "type": "number",
                "min": 0,
                "max": 95,
                "step": 1,
                "default": 62,
            },
            {
                "key": "history_ms",
                "label": "Аудио для построения хвоста, мс",
                "type": "number",
                "min": 20,
                "max": 240,
                "step": 10,
                "default": 80,
            },
            {
                "key": "recovery_ms",
                "label": "Возврат громкости при ложном перебивании, мс",
                "type": "number",
                "min": 20,
                "max": 600,
                "step": 10,
                "default": 120,
            },
            {
                "key": "fallback_takeover_ms",
                "label": "Принудительное перебивание без ответа Режиссёра, мс",
                "type": "number",
                "min": 100,
                "max": 1500,
                "step": 10,
                "default": 360,
            },
        ),
    },
    {
        "key": "semantic_interruption",
        "label": "Смысл перебивания",
        "description": "Режиссёр различает подтверждение, шум, сомнение и реальный захват реплики.",
        "fields": (
            {
                "key": "decision_timeout_ms",
                "label": "Ожидание решения Режиссёра, мс",
                "type": "number",
                "min": 50,
                "max": 1500,
                "step": 10,
                "default": 320,
            },
            {
                "key": "takeover_confidence",
                "label": "Порог TAKEOVER",
                "type": "number",
                "min": 0.5,
                "max": 1,
                "step": 0.01,
                "default": 0.78,
            },
            {
                "key": "backchannel_confidence",
                "label": "Порог BACKCHANNEL",
                "type": "number",
                "min": 0.5,
                "max": 1,
                "step": 0.01,
                "default": 0.82,
            },
            {
                "key": "noise_confidence",
                "label": "Порог NOISE",
                "type": "number",
                "min": 0.5,
                "max": 1,
                "step": 0.01,
                "default": 0.85,
            },
            {
                "key": "uncertain_hold_ms",
                "label": "Дополнительное удержание при UNCERTAIN, мс",
                "type": "number",
                "min": 0,
                "max": 1000,
                "step": 10,
                "default": 180,
            },
            {
                "key": "max_backchannel_ms",
                "label": "Максимальная длина реплики-подтверждения, мс",
                "type": "number",
                "min": 80,
                "max": 1500,
                "step": 10,
                "default": 650,
            },
        ),
    },
    {
        "key": "natural_cut",
        "label": "Естественный обрыв",
        "description": (
            "Завершает перебиваемую реплику локальным затухающим хвостом, "
            "выбирая тихую точку и переход через ноль. Может работать отдельно."
        ),
        "fields": (
            {
                "key": "search_window_ms",
                "label": "Окно поиска, мс",
                "type": "number",
                "min": 10,
                "max": 240,
                "step": 5,
                "default": 70,
            },
            {
                "key": "energy_window_ms",
                "label": "Окно оценки энергии, мс",
                "type": "number",
                "min": 2,
                "max": 30,
                "step": 1,
                "default": 8,
            },
            {
                "key": "zero_cross_threshold",
                "label": "Порог перехода через ноль",
                "type": "number",
                "min": 16,
                "max": 3000,
                "step": 16,
                "default": 420,
            },
            {
                "key": "max_trim_ms",
                "label": "Максимальная обрезка, мс",
                "type": "number",
                "min": 0,
                "max": 160,
                "step": 5,
                "default": 55,
            },
        ),
    },
    {
        "key": "adaptive_response_delay",
        "label": "Живая задержка ответа",
        "description": "Режиссёр выбирает естественную паузу перед первой фонемой ответа.",
        "fields": (
            {
                "key": "min_ms",
                "label": "Минимальная задержка, мс",
                "type": "number",
                "min": 0,
                "max": 1200,
                "step": 10,
                "default": 140,
            },
            {
                "key": "max_ms",
                "label": "Максимальная задержка, мс",
                "type": "number",
                "min": 50,
                "max": 2500,
                "step": 10,
                "default": 650,
            },
            {
                "key": "director_wait_ms",
                "label": "Ожидание плана Режиссёра, мс",
                "type": "number",
                "min": 0,
                "max": 1200,
                "step": 10,
                "default": 240,
            },
            {
                "key": "jitter_ms",
                "label": "Допустимая вариативность, мс",
                "type": "number",
                "min": 0,
                "max": 250,
                "step": 5,
                "default": 35,
            },
            {
                "key": "thinking_pause_ms",
                "label": "Пауза после сложной реплики, мс",
                "type": "number",
                "min": 0,
                "max": 1500,
                "step": 10,
                "default": 460,
            },
            {
                "key": "direct_answer_ms",
                "label": "Пауза перед прямым ответом, мс",
                "type": "number",
                "min": 0,
                "max": 800,
                "step": 10,
                "default": 180,
            },
            {
                "key": "plan_confidence",
                "label": "Минимальная уверенность плана",
                "type": "number",
                "min": 0.5,
                "max": 1,
                "step": 0.01,
                "default": 0.65,
            },
        ),
    },
    {
        "key": "listener_backchannels",
        "label": "Реакции слушателя",
        "description": "Режиссёр может тихо вставить короткое «угу», «ага» или другую разрешённую реакцию.",
        "fields": (
            {
                "key": "phrases",
                "label": "Разрешённые реакции",
                "type": "text",
                "default": "угу\nага\nпонял",
            },
            {
                "key": "min_user_speech_ms",
                "label": "Минимальная длительность речи клиента, мс",
                "type": "number",
                "min": 500,
                "max": 20000,
                "step": 100,
                "default": 3200,
            },
            {
                "key": "opportunity_silence_ms",
                "label": "Микропауза для реакции, мс",
                "type": "number",
                "min": 80,
                "max": 700,
                "step": 10,
                "default": 220,
            },
            {
                "key": "min_interval_ms",
                "label": "Минимальный интервал, мс",
                "type": "number",
                "min": 500,
                "max": 30000,
                "step": 100,
                "default": 7000,
            },
            {
                "key": "max_per_turn",
                "label": "Максимум реакций за реплику",
                "type": "number",
                "min": 0,
                "max": 5,
                "step": 1,
                "default": 1,
            },
            {
                "key": "volume_percent",
                "label": "Громкость, %",
                "type": "number",
                "min": 1,
                "max": 100,
                "step": 1,
                "default": 48,
            },
            {
                "key": "max_audio_ms",
                "label": "Максимальная длина аудио, мс",
                "type": "number",
                "min": 100,
                "max": 1800,
                "step": 50,
                "default": 750,
            },
            {
                "key": "confidence",
                "label": "Минимальная уверенность",
                "type": "number",
                "min": 0.5,
                "max": 1,
                "step": 0.01,
                "default": 0.86,
            },
            {
                "key": "resume_confirmation_ms",
                "label": "Окно подтверждения продолжения, мс",
                "type": "number",
                "min": 500,
                "max": 2500,
                "step": 50,
                "default": 1100,
            },
        ),
    },
    {
        "key": "pace_matching",
        "label": "Подстройка темпа",
        "description": "Режиссёр оценивает темп клиента, а локальный WSOLA меняет скорость Актёра без изменения высоты голоса.",
        "fields": (
            {
                "key": "min_percent",
                "label": "Минимальный темп, %",
                "type": "number",
                "min": 75,
                "max": 100,
                "step": 1,
                "default": 94,
            },
            {
                "key": "max_percent",
                "label": "Максимальный темп, %",
                "type": "number",
                "min": 100,
                "max": 135,
                "step": 1,
                "default": 108,
            },
            {
                "key": "default_percent",
                "label": "Темп по умолчанию, %",
                "type": "number",
                "min": 75,
                "max": 135,
                "step": 1,
                "default": 100,
            },
            {
                "key": "smoothing_percent",
                "label": "Сглаживание изменений, %",
                "type": "number",
                "min": 0,
                "max": 100,
                "step": 1,
                "default": 70,
            },
            {
                "key": "block_ms",
                "label": "Блок WSOLA, мс",
                "type": "number",
                "min": 40,
                "max": 240,
                "step": 10,
                "default": 100,
            },
            {
                "key": "overlap_ms",
                "label": "Перекрытие WSOLA, мс",
                "type": "number",
                "min": 5,
                "max": 40,
                "step": 1,
                "default": 12,
            },
            {
                "key": "search_ms",
                "label": "Поиск совпадения WSOLA, мс",
                "type": "number",
                "min": 2,
                "max": 30,
                "step": 1,
                "default": 8,
            },
            {
                "key": "plan_confidence",
                "label": "Минимальная уверенность плана",
                "type": "number",
                "min": 0.5,
                "max": 1,
                "step": 0.01,
                "default": 0.65,
            },
        ),
    },
    {
        "key": "latency_fillers",
        "label": "Заполнение ожидания",
        "description": "После долгой задержки Актёра Режиссёр может произнести один короткий нейтральный филлер.",
        "fields": (
            {
                "key": "phrases",
                "label": "Разрешённые филлеры",
                "type": "text",
                "default": "угу, секунду…\nтак, понял…\nсейчас…",
            },
            {
                "key": "trigger_ms",
                "label": "Порог ожидания, мс",
                "type": "number",
                "min": 300,
                "max": 5000,
                "step": 50,
                "default": 1100,
            },
            {
                "key": "repeat_guard_ms",
                "label": "Защита от повторения, мс",
                "type": "number",
                "min": 1000,
                "max": 60000,
                "step": 500,
                "default": 15000,
            },
            {
                "key": "max_per_turn",
                "label": "Максимум за реплику",
                "type": "number",
                "min": 0,
                "max": 3,
                "step": 1,
                "default": 1,
            },
            {
                "key": "volume_percent",
                "label": "Громкость, %",
                "type": "number",
                "min": 1,
                "max": 100,
                "step": 1,
                "default": 62,
            },
            {
                "key": "max_audio_ms",
                "label": "Максимальная длина аудио, мс",
                "type": "number",
                "min": 100,
                "max": 2500,
                "step": 50,
                "default": 1200,
            },
            {
                "key": "confidence",
                "label": "Минимальная уверенность",
                "type": "number",
                "min": 0.5,
                "max": 1,
                "step": 0.01,
                "default": 0.8,
            },
        ),
    },
    {
        "key": "interruption_resume",
        "label": "Продолжение после перебивания",
        "description": "Режиссёр выбирает RESUME, REFORMULATE или DISCARD после пересечения реплик.",
        "fields": (
            {
                "key": "decision_timeout_ms",
                "label": "Ожидание решения, мс",
                "type": "number",
                "min": 50,
                "max": 1500,
                "step": 10,
                "default": 360,
            },
            {
                "key": "resume_confidence",
                "label": "Порог RESUME",
                "type": "number",
                "min": 0.5,
                "max": 1,
                "step": 0.01,
                "default": 0.82,
            },
            {
                "key": "reformulate_confidence",
                "label": "Порог REFORMULATE",
                "type": "number",
                "min": 0.5,
                "max": 1,
                "step": 0.01,
                "default": 0.76,
            },
            {
                "key": "resume_delay_ms",
                "label": "Пауза перед продолжением, мс",
                "type": "number",
                "min": 0,
                "max": 1000,
                "step": 10,
                "default": 180,
            },
            {
                "key": "max_context_chars",
                "label": "Контекст незавершённой реплики, символов",
                "type": "number",
                "min": 100,
                "max": 5000,
                "step": 100,
                "default": 1200,
            },
        ),
    },
    {
        "key": "micro_pauses",
        "label": "Микропаузы",
        "description": (
            "Аккуратно удлиняет реальные акустические паузы в речи Актёра; "
            "Режиссёр выбирает интенсивность, не привязывая звук к запаздывающей транскрипции."
        ),
        "fields": (
            {
                "key": "short_pause_ms",
                "label": "Короткая пауза, мс",
                "type": "number",
                "min": 0,
                "max": 250,
                "step": 5,
                "default": 55,
            },
            {
                "key": "medium_pause_ms",
                "label": "Средняя пауза, мс",
                "type": "number",
                "min": 0,
                "max": 400,
                "step": 5,
                "default": 115,
            },
            {
                "key": "question_pause_ms",
                "label": "Добавка после длинной паузы, мс",
                "type": "number",
                "min": 0,
                "max": 400,
                "step": 5,
                "default": 90,
            },
            {
                "key": "min_audio_before_pause_ms",
                "label": "Минимум речи до паузы, мс",
                "type": "number",
                "min": 50,
                "max": 3000,
                "step": 50,
                "default": 500,
            },
            {
                "key": "max_added_ms_per_turn",
                "label": "Лимит добавленных пауз за реплику, мс",
                "type": "number",
                "min": 0,
                "max": 2000,
                "step": 50,
                "default": 420,
            },
            {
                "key": "boundary_confidence",
                "label": "Минимальная уверенность профиля",
                "type": "number",
                "min": 0.5,
                "max": 1,
                "step": 0.01,
                "default": 0.72,
            },
            {
                "key": "natural_gap_min_ms",
                "label": "Минимальная естественная пауза, мс",
                "type": "number",
                "min": 15,
                "max": 250,
                "step": 5,
                "default": 35,
            },
            {
                "key": "silence_threshold_db",
                "label": "Порог тишины, дБFS",
                "type": "number",
                "min": -72,
                "max": -30,
                "step": 1,
                "default": -48,
            },
        ),
    },
    {
        "key": "voice_mastering",
        "label": "Мастеринг голоса",
        "description": "Мягко выравнивает телефонный голос: high-pass, компрессор, de-esser, gain и limiter.",
        "fields": (
            {
                "key": "highpass_hz",
                "label": "High-pass, Гц",
                "type": "number",
                "min": 0,
                "max": 500,
                "step": 10,
                "default": 90,
            },
            {
                "key": "compressor_threshold_db",
                "label": "Порог компрессора, дБFS",
                "type": "number",
                "min": -40,
                "max": -1,
                "step": 1,
                "default": -16,
            },
            {
                "key": "compressor_ratio",
                "label": "Коэффициент компрессии",
                "type": "number",
                "min": 1,
                "max": 8,
                "step": 0.1,
                "default": 2.2,
            },
            {
                "key": "attack_ms",
                "label": "Атака, мс",
                "type": "number",
                "min": 1,
                "max": 100,
                "step": 1,
                "default": 12,
            },
            {
                "key": "release_ms",
                "label": "Восстановление, мс",
                "type": "number",
                "min": 20,
                "max": 1000,
                "step": 10,
                "default": 180,
            },
            {
                "key": "makeup_gain_db",
                "label": "Компенсация, дБ",
                "type": "number",
                "min": -6,
                "max": 12,
                "step": 0.5,
                "default": 1.5,
            },
            {
                "key": "limiter_db",
                "label": "Лимитер, дБFS",
                "type": "number",
                "min": -12,
                "max": -0.1,
                "step": 0.1,
                "default": -1.5,
            },
            {
                "key": "deesser_percent",
                "label": "Смягчение сибилянтов, %",
                "type": "number",
                "min": 0,
                "max": 100,
                "step": 1,
                "default": 18,
            },
            {
                "key": "wet_percent",
                "label": "Доля обработки, %",
                "type": "number",
                "min": 0,
                "max": 100,
                "step": 1,
                "default": 65,
            },
        ),
    },
)

EFFECT_CATALOG: tuple[dict[str, Any], ...] = _EFFECTS
EFFECT_BY_KEY = {effect["key"]: effect for effect in EFFECT_CATALOG}


def default_effects_config(*, enabled: bool = False) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for effect in EFFECT_CATALOG:
        values: dict[str, Any] = {"enabled": enabled}
        for field in effect["fields"]:
            values[field["key"]] = deepcopy(field["default"])
        result[effect["key"]] = values
    return result


def normalize_effects_config(value: Any) -> dict[str, dict[str, Any]]:
    """Return a complete, bounded and JSON-safe effect configuration."""
    if isinstance(value, (str, bytes, bytearray)):
        try:
            value = json.loads(value)
        except (json.JSONDecodeError, UnicodeDecodeError, TypeError):
            value = {}
    source = value if isinstance(value, dict) else {}
    result = default_effects_config(enabled=False)
    for effect in EFFECT_CATALOG:
        key = effect["key"]
        raw_effect = source.get(key)
        if not isinstance(raw_effect, dict):
            continue
        normalized = result[key]
        normalized["enabled"] = bool(raw_effect.get("enabled", False))
        for field in effect["fields"]:
            field_key = field["key"]
            raw = raw_effect.get(field_key, field["default"])
            if field["type"] == "text":
                normalized[field_key] = str(raw or "")[:5000]
                continue
            try:
                number = float(raw)
            except (TypeError, ValueError):
                number = float(field["default"])
            number = max(float(field["min"]), min(float(field["max"]), number))
            step = field.get("step")
            if isinstance(field["default"], int) and not isinstance(
                field["default"], bool
            ):
                normalized[field_key] = int(round(number))
            elif step and float(step).is_integer():
                normalized[field_key] = int(round(number))
            else:
                normalized[field_key] = round(number, 4)
    return result


def any_effect_enabled(config: Any) -> bool:
    normalized = normalize_effects_config(config)
    return any(bool(effect.get("enabled")) for effect in normalized.values())


def director_required(config: Any) -> bool:
    """Return whether enabled effects require the advisory Gemini session."""
    normalized = normalize_effects_config(config)
    return any(
        bool(normalized.get(key, {}).get("enabled")) for key in DIRECTOR_EFFECT_KEYS
    )


def enabled_effect_keys(config: Any) -> list[str]:
    normalized = normalize_effects_config(config)
    return [key for key, values in normalized.items() if values.get("enabled")]


def effect_catalog_api() -> dict[str, Any]:
    return {
        "effects": deepcopy(list(EFFECT_CATALOG)),
        "defaults": default_effects_config(enabled=False),
    }


def phrases_from_value(value: Any) -> list[str]:
    text = str(value or "")
    phrases = []
    for line in text.replace(";", "\n").splitlines():
        phrase = line.strip()
        if phrase and phrase not in phrases:
            phrases.append(phrase[:120])
    return phrases[:20]
