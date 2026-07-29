"""Official Gemini prebuilt voice catalogue exposed by the robot editor."""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class VoiceOption:
    name: str
    style: str
    group: str


VOICE_OPTIONS: tuple[VoiceOption, ...] = (
    VoiceOption("Zephyr", "яркий", "all"),
    VoiceOption("Puck", "бодрый", "all"),
    VoiceOption("Charon", "информативный", "all"),
    VoiceOption("Kore", "уверенный", "all"),
    VoiceOption("Fenrir", "эмоциональный", "all"),
    VoiceOption("Leda", "молодой", "all"),
    VoiceOption("Orus", "уверенный", "all"),
    VoiceOption("Aoede", "лёгкий", "all"),
    VoiceOption("Callirrhoe", "непринуждённый", "all"),
    VoiceOption("Autonoe", "яркий", "all"),
    VoiceOption("Enceladus", "с придыханием", "all"),
    VoiceOption("Iapetus", "чёткий", "all"),
    VoiceOption("Umbriel", "непринуждённый", "all"),
    VoiceOption("Algieba", "плавный", "all"),
    VoiceOption("Despina", "плавный", "all"),
    VoiceOption("Erinome", "чёткий", "all"),
    VoiceOption("Algenib", "хрипловатый", "all"),
    VoiceOption("Rasalgethi", "информативный", "all"),
    VoiceOption("Laomedeia", "бодрый", "all"),
    VoiceOption("Achernar", "мягкий", "all"),
    VoiceOption("Alnilam", "уверенный", "all"),
    VoiceOption("Schedar", "ровный", "all"),
    VoiceOption("Gacrux", "зрелый", "all"),
    VoiceOption("Pulcherrima", "прямой", "all"),
    VoiceOption("Achird", "дружелюбный", "all"),
    VoiceOption("Zubenelgenubi", "разговорный", "all"),
    VoiceOption("Vindemiatrix", "нежный", "all"),
    VoiceOption("Sadachbia", "живой", "all"),
    VoiceOption("Sadaltager", "знающий", "all"),
    VoiceOption("Sulafat", "тёплый", "all"),
)


def as_api_items() -> list[dict[str, str]]:
    return [
        {"name": item.name, "style": item.style, "group": item.group}
        for item in VOICE_OPTIONS
    ]
