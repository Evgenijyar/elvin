"""Local, deterministic interruption policy for caller barge-in.

The policy deliberately uses only accumulated VAD speech time.  It does not
send tentative listener feedback to Gemini and does not require a second
model/session.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any


MIN_INTERJECTION_SPEECH_MS = 100
MAX_INTERJECTION_SPEECH_MS = 2_000
DEFAULT_INTERJECTION_SPEECH_MS = 650
MAX_INTERRUPTION_TAIL_MS = 2_000
DEFAULT_INTERRUPTION_TAIL_MS = 250


def _bounded_int(value: object, *, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(parsed, maximum))


@dataclass(frozen=True, slots=True)
class InterruptionPolicy:
    """Robot-level settings for local interruption handling."""

    ignore_short_interjections: bool = False
    interjection_max_speech_ms: int = DEFAULT_INTERJECTION_SPEECH_MS
    delayed_interruption: bool = False
    interruption_tail_ms: int = DEFAULT_INTERRUPTION_TAIL_MS

    @classmethod
    def from_robot(cls, robot: dict[str, Any]) -> "InterruptionPolicy":
        return cls(
            ignore_short_interjections=bool(
                robot.get("ignore_short_interjections", False)
            ),
            interjection_max_speech_ms=_bounded_int(
                robot.get("interjection_max_speech_ms"),
                default=DEFAULT_INTERJECTION_SPEECH_MS,
                minimum=MIN_INTERJECTION_SPEECH_MS,
                maximum=MAX_INTERJECTION_SPEECH_MS,
            ),
            delayed_interruption=bool(robot.get("delayed_interruption", False)),
            interruption_tail_ms=_bounded_int(
                robot.get("interruption_tail_ms"),
                default=DEFAULT_INTERRUPTION_TAIL_MS,
                minimum=0,
                maximum=MAX_INTERRUPTION_TAIL_MS,
            ),
        )

    @property
    def enabled(self) -> bool:
        return self.ignore_short_interjections or self.delayed_interruption

    @property
    def effective_tail_ms(self) -> int:
        return self.interruption_tail_ms if self.delayed_interruption else 0


class InterruptionAction(StrEnum):
    WAIT = "wait"
    IGNORE = "ignore"
    COMMIT = "commit"


@dataclass(frozen=True, slots=True)
class InterruptionDecision:
    action: InterruptionAction
    confirmed_now: bool = False
    audio: bytes = b""
    audio_bytes: int = 0
    speech_ms: float = 0.0
    speech_ended: bool = False


class LocalInterruptionGate:
    """Buffers a possible interruption until local timing confirms it."""

    def __init__(self, policy: InterruptionPolicy) -> None:
        self.policy = policy
        self._active = False
        self._audio = bytearray()
        self._speech_ms = 0.0
        self._speech_ended = False
        self._confirmed_at: float | None = None
        self._commit_at: float | None = None

    @property
    def active(self) -> bool:
        return self._active

    def begin(self) -> None:
        self.reset()
        self._active = True

    def observe(
        self,
        *,
        audio: bytes,
        speech_ms: float,
        speech_ended: bool,
        now: float,
    ) -> InterruptionDecision:
        if not self._active:
            raise RuntimeError("interruption candidate is not active")

        if audio:
            self._audio.extend(audio)
        self._speech_ms = max(self._speech_ms, float(speech_ms))
        self._speech_ended = self._speech_ended or speech_ended

        confirmed_now = False
        if self._confirmed_at is None and (
            not self.policy.ignore_short_interjections
            or self._speech_ms >= self.policy.interjection_max_speech_ms
        ):
            confirmed_now = True
            self._confirmed_at = now
            self._commit_at = now + self.policy.effective_tail_ms / 1000.0

        if self._speech_ended and self._confirmed_at is None:
            decision = InterruptionDecision(
                action=InterruptionAction.IGNORE,
                audio_bytes=len(self._audio),
                speech_ms=self._speech_ms,
                speech_ended=True,
            )
            self.reset()
            return decision

        if self._commit_at is not None and now >= self._commit_at:
            audio_bytes = bytes(self._audio)
            decision = InterruptionDecision(
                action=InterruptionAction.COMMIT,
                confirmed_now=confirmed_now,
                audio=audio_bytes,
                audio_bytes=len(audio_bytes),
                speech_ms=self._speech_ms,
                speech_ended=self._speech_ended,
            )
            self.reset()
            return decision

        return InterruptionDecision(
            action=InterruptionAction.WAIT,
            confirmed_now=confirmed_now,
            audio_bytes=len(self._audio),
            speech_ms=self._speech_ms,
            speech_ended=self._speech_ended,
        )

    def reset(self) -> None:
        self._active = False
        self._audio.clear()
        self._speech_ms = 0.0
        self._speech_ended = False
        self._confirmed_at = None
        self._commit_at = None
