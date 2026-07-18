"""Gemini voice catalogue used by the robot editor.

Google publishes voice names and style descriptions, but does not publish a
formal gender taxonomy.  The two UI groups below are a practical, subjective
product classification and can be adjusted without changing stored data.
"""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class VoiceOption:
    name: str
    style: str
    group: str


VOICE_OPTIONS: tuple[VoiceOption, ...] = (
    VoiceOption("Puck", "бодрый", "male"),
    VoiceOption("Charon", "информативный", "male"),
    VoiceOption("Fenrir", "эмоциональный", "male"),
    VoiceOption("Orus", "уверенный", "male"),
    VoiceOption("Enceladus", "с придыханием", "male"),
    VoiceOption("Iapetus", "чёткий", "male"),
    VoiceOption("Umbriel", "спокойный", "male"),
    VoiceOption("Algieba", "плавный", "male"),
    VoiceOption("Algenib", "хрипловатый", "male"),
    VoiceOption("Rasalgethi", "информативный", "male"),
    VoiceOption("Alnilam", "твёрдый", "male"),
    VoiceOption("Schedar", "ровный", "male"),
    VoiceOption("Achird", "дружелюбный", "male"),
    VoiceOption("Zubenelgenubi", "разговорный", "male"),
    VoiceOption("Sadachbia", "живой", "male"),
    VoiceOption("Sadaltager", "знающий", "male"),
    VoiceOption("Zephyr", "яркий", "female"),
    VoiceOption("Kore", "уверенный", "female"),
    VoiceOption("Leda", "молодой", "female"),
    VoiceOption("Aoede", "лёгкий", "female"),
    VoiceOption("Callirrhoe", "непринуждённый", "female"),
    VoiceOption("Autonoe", "яркий", "female"),
    VoiceOption("Despina", "плавный", "female"),
    VoiceOption("Erinome", "чёткий", "female"),
    VoiceOption("Laomedeia", "бодрый", "female"),
    VoiceOption("Achernar", "мягкий", "female"),
    VoiceOption("Gacrux", "зрелый", "female"),
    VoiceOption("Pulcherrima", "прямой", "female"),
    VoiceOption("Vindemiatrix", "нежный", "female"),
    VoiceOption("Sulafat", "тёплый", "female"),
)


def as_api_items() -> list[dict[str, str]]:
    return [
        {"name": item.name, "style": item.style, "group": item.group}
        for item in VOICE_OPTIONS
    ]
