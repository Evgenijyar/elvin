"""Optional outbound-only DSP used by the configurable conversation effects.

The stable voice path remains byte-for-byte untouched while every effect is
disabled.  This module never receives caller audio and is only invoked after
Gemini Actor audio has already been resampled to telephony PCM16/16 kHz.
"""

from __future__ import annotations

import math
import random
import sys
from array import array
from collections import deque
from typing import Any

SAMPLE_RATE = 16_000
SAMPLE_WIDTH = 2
PCM_MIN = -32768
PCM_MAX = 32767


def _samples(pcm: bytes) -> array:
    values = array("h")
    values.frombytes(pcm[: len(pcm) - (len(pcm) % 2)])
    if sys.byteorder != "little":
        values.byteswap()
    return values


def _pcm(values: array | list[int]) -> bytes:
    if not isinstance(values, array):
        values = array("h", (_clip(value) for value in values))
    if sys.byteorder != "little":
        values = array("h", values)
        values.byteswap()
    return values.tobytes()


def _clip(value: float | int) -> int:
    return max(PCM_MIN, min(PCM_MAX, int(round(value))))


def duration_ms(pcm: bytes, sample_rate: int = SAMPLE_RATE) -> float:
    return len(pcm) * 1000.0 / (sample_rate * SAMPLE_WIDTH)


def silence(duration: float, sample_rate: int = SAMPLE_RATE) -> bytes:
    samples = max(0, int(round(sample_rate * max(0.0, duration) / 1000.0)))
    return b"\x00\x00" * samples


def apply_gain_db(pcm: bytes, gain_db: float) -> bytes:
    if not pcm or abs(gain_db) < 0.001:
        return pcm
    gain = 10.0 ** (gain_db / 20.0)
    values = _samples(pcm)
    for index, value in enumerate(values):
        values[index] = _clip(value * gain)
    return _pcm(values)


def apply_gain_ramp_db(pcm: bytes, start_db: float, end_db: float) -> bytes:
    if not pcm:
        return pcm
    if abs(start_db - end_db) < 0.001:
        return apply_gain_db(pcm, end_db)
    values = _samples(pcm)
    span = max(1, len(values) - 1)
    for index, value in enumerate(values):
        db = start_db + (end_db - start_db) * (index / span)
        gain = 10.0 ** (db / 20.0)
        values[index] = _clip(value * gain)
    return _pcm(values)


def apply_gain_percent(pcm: bytes, percent: float) -> bytes:
    if not pcm:
        return pcm
    gain = max(0.0, float(percent)) / 100.0
    if abs(gain - 1.0) < 0.0001:
        return pcm
    values = _samples(pcm)
    for index, value in enumerate(values):
        values[index] = _clip(value * gain)
    return _pcm(values)


def linear_fade(
    pcm: bytes, *, start_percent: float = 0.0, end_gain: float = 0.0
) -> bytes:
    values = _samples(pcm)
    if not values:
        return pcm
    start = max(0, min(len(values) - 1, int(len(values) * start_percent / 100.0)))
    span = max(1, len(values) - start - 1)
    for index in range(start, len(values)):
        progress = (index - start) / span
        gain = 1.0 + (float(end_gain) - 1.0) * progress
        values[index] = _clip(values[index] * gain)
    return _pcm(values)


def find_natural_cut(
    pcm: bytes,
    *,
    search_window_ms: int,
    energy_window_ms: int,
    zero_cross_threshold: int,
    max_trim_ms: int,
    sample_rate: int = SAMPLE_RATE,
) -> bytes:
    """Trim to a low-energy zero crossing near the end of a PCM buffer."""
    values = _samples(pcm)
    if len(values) < 4:
        return pcm
    search_samples = min(
        len(values) - 2,
        max(2, int(sample_rate * max(1, search_window_ms) / 1000)),
    )
    energy_samples = max(1, int(sample_rate * max(1, energy_window_ms) / 1000))
    max_trim_samples = min(
        search_samples,
        max(0, int(sample_rate * max(0, max_trim_ms) / 1000)),
    )
    lower = max(1, len(values) - search_samples)
    preferred_lower = (
        max(lower, len(values) - max_trim_samples) if max_trim_samples else lower
    )
    best_index = len(values)
    best_score = float("inf")
    for index in range(len(values) - 1, preferred_lower - 1, -1):
        previous = values[index - 1]
        current = values[index]
        crossing = (previous <= 0 < current) or (previous >= 0 > current)
        near_zero = min(abs(previous), abs(current)) <= max(1, zero_cross_threshold)
        if not crossing and not near_zero:
            continue
        begin = max(0, index - energy_samples)
        window = values[begin:index]
        if not window:
            continue
        energy = sum(abs(value) for value in window) / len(window)
        distance_penalty = (len(values) - index) * 0.15
        score = energy + distance_penalty
        if score < best_score:
            best_score = score
            best_index = index
    if best_index >= len(values):
        return pcm
    return _pcm(values[:best_index])


def _correlation(
    left: list[float], right: array, right_offset: int, length: int
) -> float:
    dot = 0.0
    left_energy = 1.0
    right_energy = 1.0
    # Evaluating every second sample halves CPU while remaining stable for a
    # tiny 8–12% telephone tempo correction.
    for index in range(0, length, 2):
        a = left[index]
        b = right[right_offset + index]
        dot += a * b
        left_energy += a * a
        right_energy += b * b
    return dot / math.sqrt(left_energy * right_energy)


def wsola_tempo(
    pcm: bytes,
    tempo_percent: float,
    *,
    sample_rate: int = SAMPLE_RATE,
    window_ms: int = 24,
    overlap_ms: int = 12,
    search_ms: int = 8,
) -> bytes:
    """Small WSOLA tempo change that approximately preserves pitch.

    This is deliberately bounded to the subtle range used by the UI.  It is
    not a general purpose offline time-stretcher, but avoids the pitch drop of
    ordinary sample-rate resampling in the live telephone path.
    """
    tempo = max(0.65, min(1.45, float(tempo_percent) / 100.0))
    if not pcm or abs(tempo - 1.0) < 0.005:
        return pcm
    source = _samples(pcm)
    window = max(64, int(sample_rate * window_ms / 1000))
    overlap = max(16, min(window - 16, int(sample_rate * overlap_ms / 1000)))
    search = max(4, int(sample_rate * search_ms / 1000))
    analysis_hop = window - overlap
    synthesis_hop = max(16, int(round(analysis_hop / tempo)))
    if len(source) < window * 2:
        # A short packet does not contain enough periods for robust matching;
        # keep it unchanged instead of introducing a click.
        return pcm

    target_samples = max(1, int(round(len(source) / tempo)))
    estimated = max(target_samples + window, int(len(source) / tempo) + window * 2)
    output = [0.0] * estimated
    weight = [0.0] * estimated

    def add_frame(src_start: int, dst_start: int) -> None:
        for offset in range(window):
            dst = dst_start + offset
            if dst >= len(output) or src_start + offset >= len(source):
                break
            if offset < overlap:
                fade = offset / overlap
            elif offset >= window - overlap:
                fade = max(0.0, (window - offset) / overlap)
            else:
                fade = 1.0
            fade = max(0.05, fade)
            output[dst] += source[src_start + offset] * fade
            weight[dst] += fade

    source_pos = 0
    output_pos = 0
    add_frame(source_pos, output_pos)
    source_pos += analysis_hop
    output_pos += synthesis_hop

    while output_pos < target_samples and source_pos < len(source):
        overlap_reference: list[float] = []
        for index in range(overlap):
            dst = output_pos + index
            overlap_reference.append(output[dst] / weight[dst] if weight[dst] else 0.0)
        # The old implementation stopped as soon as less than one complete
        # source window remained, then padded the requested duration with
        # zeros.  With 100 ms live blocks that produced a 6–21 ms dropout at
        # every block boundary.  Reuse the final complete analysis window for
        # the bounded tail instead; overlap-add keeps it continuous.
        max_source_start = max(0, len(source) - window)
        expected = min(source_pos, max_source_start)
        best = expected
        best_score = -2.0
        start = max(0, expected - search)
        end = min(max_source_start, expected + search)
        for candidate in range(start, end + 1, 2):
            score = _correlation(overlap_reference, source, candidate, overlap)
            if score > best_score:
                best_score = score
                best = candidate
        add_frame(best, output_pos)
        source_pos = best + analysis_hop
        output_pos += synthesis_hop

    last = 0
    result = array("h")
    for index, value in enumerate(output):
        if weight[index] > 0:
            result.append(_clip(value / weight[index]))
            last = index
        elif index <= last + synthesis_hop:
            result.append(0)
        else:
            break
    if len(result) < target_samples:
        # This is only reachable for unusually tiny/bounded inputs.  Extend
        # the last real sample instead of creating an audible zero hole.
        fill = result[-1] if result else 0
        result.extend([fill] * (target_samples - len(result)))
    return _pcm(result[:target_samples])


def _repeat_tail_with_crossfade(
    pcm: bytes, target_samples: int, sample_rate: int
) -> bytes:
    values = list(_samples(pcm))
    if not values or len(values) >= target_samples:
        return _pcm(values[:target_samples])
    period = max(32, min(len(values), int(sample_rate * 0.022)))
    overlap = max(8, period // 3)
    seed = values[-period:]
    while len(values) < target_samples:
        remaining = target_samples - len(values)
        segment = seed[: min(period, remaining + overlap)]
        if len(values) >= overlap and len(segment) >= overlap:
            for index in range(overlap):
                progress = (index + 1) / (overlap + 1)
                values[-overlap + index] = _clip(
                    values[-overlap + index] * (1.0 - progress)
                    + segment[index] * progress
                )
            values.extend(segment[overlap:])
        else:
            values.extend(segment)
    return _pcm(values[:target_samples])


def build_release_tail(
    history_pcm: bytes,
    *,
    release_ms: int,
    slowdown_percent: float,
    fade_start_percent: float,
    natural_cut: dict[str, Any] | None = None,
    sample_rate: int = SAMPLE_RATE,
) -> bytes:
    if not history_pcm or release_ms <= 0:
        return b""
    source = history_pcm
    if natural_cut:
        source = find_natural_cut(
            source,
            search_window_ms=int(natural_cut.get("search_window_ms", 70)),
            energy_window_ms=int(natural_cut.get("energy_window_ms", 8)),
            zero_cross_threshold=int(natural_cut.get("zero_cross_threshold", 420)),
            max_trim_ms=int(natural_cut.get("max_trim_ms", 55)),
            sample_rate=sample_rate,
        )
    tempo = max(65.0, 100.0 - max(0.0, float(slowdown_percent)))
    slowed = wsola_tempo(source, tempo, sample_rate=sample_rate)
    target_samples = max(1, int(sample_rate * release_ms / 1000))
    extended = _repeat_tail_with_crossfade(slowed, target_samples, sample_rate)
    return linear_fade(
        extended,
        start_percent=max(0.0, min(99.0, float(fade_start_percent))),
        end_gain=0.0,
    )


class PlaybackHistory:
    def __init__(self, max_ms: int = 300, sample_rate: int = SAMPLE_RATE) -> None:
        self.sample_rate = sample_rate
        self.max_bytes = max(2, int(sample_rate * max_ms / 1000) * 2)
        self._chunks: deque[bytes] = deque()
        self._bytes = 0

    def append(self, pcm: bytes) -> None:
        if not pcm:
            return
        self._chunks.append(bytes(pcm))
        self._bytes += len(pcm)
        while self._chunks and self._bytes > self.max_bytes:
            removed = self._chunks.popleft()
            self._bytes -= len(removed)

    def tail(self, milliseconds: int) -> bytes:
        wanted = max(2, int(self.sample_rate * max(1, milliseconds) / 1000) * 2)
        return b"".join(self._chunks)[-wanted:]

    def clear(self) -> None:
        self._chunks.clear()
        self._bytes = 0


class VoiceMasteringProcessor:
    """Stateful, deliberately gentle telephone-band mastering."""

    def __init__(self, config: dict[str, Any], sample_rate: int = SAMPLE_RATE) -> None:
        self.config = config
        self.sample_rate = sample_rate
        self.previous_input = 0.0
        self.previous_highpass = 0.0
        self.envelope = 0.0
        self.previous_output = 0.0

    def process(self, pcm: bytes) -> bytes:
        if not pcm or not self.config.get("enabled"):
            return pcm
        values = _samples(pcm)
        highpass_hz = max(0.0, float(self.config.get("highpass_hz", 90)))
        rc = 1.0 / (2.0 * math.pi * highpass_hz) if highpass_hz > 0 else 0.0
        dt = 1.0 / self.sample_rate
        hp_alpha = rc / (rc + dt) if highpass_hz > 0 else 0.0
        threshold_db = float(self.config.get("compressor_threshold_db", -16))
        threshold = 32767.0 * (10.0 ** (threshold_db / 20.0))
        ratio = max(1.0, float(self.config.get("compressor_ratio", 2.2)))
        attack = max(0.001, float(self.config.get("attack_ms", 12)) / 1000.0)
        release = max(0.001, float(self.config.get("release_ms", 180)) / 1000.0)
        attack_coeff = math.exp(-1.0 / (attack * self.sample_rate))
        release_coeff = math.exp(-1.0 / (release * self.sample_rate))
        makeup = 10.0 ** (float(self.config.get("makeup_gain_db", 1.5)) / 20.0)
        limiter = 32767.0 * (
            10.0 ** (float(self.config.get("limiter_db", -1.5)) / 20.0)
        )
        deesser = max(
            0.0, min(1.0, float(self.config.get("deesser_percent", 18)) / 100.0)
        )
        wet = max(0.0, min(1.0, float(self.config.get("wet_percent", 65)) / 100.0))

        result = array("h")
        for raw in values:
            sample = float(raw)
            if highpass_hz > 0:
                highpassed = hp_alpha * (
                    self.previous_highpass + sample - self.previous_input
                )
                self.previous_input = sample
                self.previous_highpass = highpassed
            else:
                highpassed = sample
            magnitude = abs(highpassed)
            coefficient = attack_coeff if magnitude > self.envelope else release_coeff
            self.envelope = (
                coefficient * self.envelope + (1.0 - coefficient) * magnitude
            )
            gain = 1.0
            if self.envelope > threshold > 0:
                compressed = threshold + (self.envelope - threshold) / ratio
                gain = compressed / self.envelope
            processed = highpassed * gain * makeup
            # A tiny first-order high-frequency reduction acts as a bounded
            # telephone de-esser without an FFT dependency.
            smoothed = self.previous_output + 0.35 * (processed - self.previous_output)
            processed = processed * (1.0 - deesser) + smoothed * deesser
            self.previous_output = smoothed
            processed = max(-limiter, min(limiter, processed))
            mixed = raw * (1.0 - wet) + processed * wet
            result.append(_clip(mixed))
        return _pcm(result)


class ConversationAudioEffects:
    """Per-call outbound effect state."""

    def __init__(self, config: dict[str, dict[str, Any]]) -> None:
        self.config = config
        natural = config.get("natural_interruption", {})
        history_ms = max(300, int(natural.get("history_ms", 80)) + 100)
        self.history = PlaybackHistory(max_ms=history_ms)
        self.mastering = VoiceMasteringProcessor(config.get("voice_mastering", {}))
        self.current_pace_percent = float(
            config.get("pace_matching", {}).get("default_percent", 100)
        )
        self.pending_pause_ms = 0
        self.added_pause_ms = 0
        self.processed_audio_ms = 0.0
        self.generation = -1
        self._pace_buffer = bytearray()
        self._pace_last_sample: int | None = None
        self._micro_frame_buffer = bytearray()
        self._micro_quiet_ms = 0.0
        self._micro_voiced_ms = 0.0
        self._micro_pause_style = "NONE"
        self._micro_pause_confidence = 0.0
        self._inserted_pause_ms = 0
        self._rng = random.Random()

    def start_generation(self, generation: int) -> None:
        if generation == self.generation:
            return
        self.generation = generation
        self.pending_pause_ms = 0
        self.added_pause_ms = 0
        self.processed_audio_ms = 0.0
        self._pace_buffer.clear()
        self._pace_last_sample = None
        self._micro_frame_buffer.clear()
        self._micro_quiet_ms = 0.0
        self._micro_voiced_ms = 0.0
        self._micro_pause_style = "NONE"
        self._micro_pause_confidence = 0.0
        self._inserted_pause_ms = 0

    def set_pace(self, requested_percent: float) -> None:
        pace = self.config.get("pace_matching", {})
        if not pace.get("enabled"):
            self.current_pace_percent = 100.0
            return
        minimum = float(pace.get("min_percent", 94))
        maximum = float(pace.get("max_percent", 108))
        requested = max(minimum, min(maximum, float(requested_percent)))
        smoothing = max(0.0, min(1.0, float(pace.get("smoothing_percent", 70)) / 100.0))
        self.current_pace_percent = (
            self.current_pace_percent * smoothing + requested * (1.0 - smoothing)
        )

    def set_micro_pause_profile(self, style: str, confidence: float) -> None:
        self._micro_pause_style = str(style or "NONE").upper()
        self._micro_pause_confidence = max(0.0, min(1.0, float(confidence)))

    def take_inserted_pause_ms(self) -> int:
        inserted = self._inserted_pause_ms
        self._inserted_pause_ms = 0
        return inserted

    def schedule_pause(self, kind: str) -> None:
        config = self.config.get("micro_pauses", {})
        if not config.get("enabled"):
            return
        if kind == "medium":
            requested = int(config.get("medium_pause_ms", 115))
        elif kind == "question":
            requested = int(config.get("question_pause_ms", 90))
        else:
            requested = int(config.get("short_pause_ms", 55))
        maximum = int(config.get("max_added_ms_per_turn", 420))
        allowed = max(0, maximum - self.added_pause_ms)
        requested = min(requested, allowed)
        if requested > self.pending_pause_ms:
            self.pending_pause_ms = requested

    def take_pause(self) -> bytes:
        milliseconds = self.pending_pause_ms
        config = self.config.get("micro_pauses", {})
        minimum_audio = int(config.get("min_audio_before_pause_ms", 500))
        if milliseconds <= 0 or self.processed_audio_ms < minimum_audio:
            return b""
        self.pending_pause_ms = 0
        self.added_pause_ms += milliseconds
        return silence(milliseconds)

    def _acoustic_micro_pauses(self, pcm: bytes, *, final: bool = False) -> bytes:
        config = self.config.get("micro_pauses", {})
        if not config.get("enabled"):
            return pcm
        if pcm:
            self._micro_frame_buffer.extend(pcm)
        # Five milliseconds is short enough to locate a real gap precisely,
        # but long enough for a stable RMS estimate on telephone PCM.
        frame_bytes = int(SAMPLE_RATE * 0.005) * SAMPLE_WIDTH
        available = len(self._micro_frame_buffer)
        complete = available - (available % frame_bytes)
        if final:
            complete = available
        if complete <= 0:
            return b""
        source = bytes(self._micro_frame_buffer[:complete])
        del self._micro_frame_buffer[:complete]
        threshold_db = float(config.get("silence_threshold_db", -48))
        threshold = 10.0 ** (threshold_db / 20.0)
        minimum_gap = float(config.get("natural_gap_min_ms", 35))
        minimum_voice = float(config.get("min_audio_before_pause_ms", 500))
        confidence_threshold = float(config.get("boundary_confidence", 0.72))
        result = bytearray()
        for offset in range(0, len(source), frame_bytes):
            frame = source[offset : offset + frame_bytes]
            values = _samples(frame)
            if not values:
                continue
            rms = (
                math.sqrt(
                    sum(float(value) * float(value) for value in values) / len(values)
                )
                / 32768.0
            )
            frame_ms = duration_ms(frame)
            if rms <= threshold:
                self._micro_quiet_ms += frame_ms
                result.extend(frame)
                continue
            if (
                self._micro_quiet_ms >= minimum_gap
                and self._micro_voiced_ms >= minimum_voice
                and self._micro_pause_style in {"LIGHT", "MEDIUM"}
                and self._micro_pause_confidence >= confidence_threshold
            ):
                if (
                    self._micro_pause_style == "MEDIUM"
                    and self._micro_quiet_ms >= max(80.0, minimum_gap * 2.0)
                ):
                    requested = int(config.get("question_pause_ms", 90))
                elif self._micro_pause_style == "MEDIUM":
                    requested = int(config.get("medium_pause_ms", 115))
                else:
                    requested = int(config.get("short_pause_ms", 55))
                maximum = int(config.get("max_added_ms_per_turn", 420))
                requested = max(
                    0,
                    min(requested, maximum - self.added_pause_ms),
                )
                if requested:
                    result.extend(silence(requested))
                    self.added_pause_ms += requested
                    self._inserted_pause_ms += requested
            self._micro_quiet_ms = 0.0
            self._micro_voiced_ms += frame_ms
            result.extend(frame)
        return bytes(result)

    def _finish_actor_audio(
        self,
        pcm: bytes,
        *,
        duck_db: float = 0.0,
        final: bool = False,
    ) -> bytes:
        if not pcm:
            result = self._acoustic_micro_pauses(b"", final=final)
            self.history.append(result)
            return result
        result = self.mastering.process(pcm)
        if duck_db < -0.01:
            result = apply_gain_db(result, duck_db)
        self.processed_audio_ms += duration_ms(result)
        result = self._acoustic_micro_pauses(result, final=final)
        self.history.append(result)
        return result

    def _smooth_pace_boundary(self, pcm: bytes) -> bytes:
        if not pcm:
            return pcm
        values = _samples(pcm)
        if not values:
            return pcm
        previous = self._pace_last_sample
        if previous is not None:
            transition = min(len(values), int(SAMPLE_RATE * 0.002))
            for index in range(transition):
                progress = (index + 1) / transition
                values[index] = _clip(
                    previous * (1.0 - progress) + values[index] * progress
                )
        self._pace_last_sample = int(values[-1])
        return _pcm(values)

    def _pace_chunk(self, pcm: bytes) -> bytes:
        pace = self.config.get("pace_matching", {})
        if not pcm or not pace.get("enabled"):
            return pcm
        if abs(self.current_pace_percent - 100.0) < 0.5:
            return pcm
        block_ms = int(pace.get("block_ms", 100))
        overlap_ms = int(pace.get("overlap_ms", 12))
        # ``block_ms`` controls live buffering; the internal WSOLA analysis
        # window stays smaller so a single configured block contains several
        # candidate periods and can actually be stretched.
        window_ms = max(overlap_ms * 2, min(block_ms // 3, 40))
        return self._smooth_pace_boundary(
            wsola_tempo(
                pcm,
                self.current_pace_percent,
                window_ms=window_ms,
                overlap_ms=overlap_ms,
                search_ms=int(pace.get("search_ms", 8)),
            )
        )

    def process_actor_audio(self, pcm: bytes, *, duck_db: float = 0.0) -> bytes:
        if not pcm:
            return pcm
        pace = self.config.get("pace_matching", {})
        if pace.get("enabled") and abs(self.current_pace_percent - 100.0) >= 0.5:
            self._pace_buffer.extend(pcm)
            block_bytes = max(
                2, int(SAMPLE_RATE * int(pace.get("block_ms", 100)) / 1000) * 2
            )
            block_bytes -= block_bytes % 2
            complete = len(self._pace_buffer) - (len(self._pace_buffer) % block_bytes)
            if complete <= 0:
                return b""
            source = bytes(self._pace_buffer[:complete])
            del self._pace_buffer[:complete]
            return self._finish_actor_audio(self._pace_chunk(source), duck_db=duck_db)
        if self._pace_buffer:
            pcm = bytes(self._pace_buffer) + pcm
            self._pace_buffer.clear()
        return self._finish_actor_audio(pcm, duck_db=duck_db)

    def flush_actor_audio(self, *, duck_db: float = 0.0) -> bytes:
        source = bytes(self._pace_buffer)
        self._pace_buffer.clear()
        return self._finish_actor_audio(
            self._pace_chunk(source) if source else b"",
            duck_db=duck_db,
            final=True,
        )

    def discard_pending_audio(self) -> None:
        self._pace_buffer.clear()
        self._pace_last_sample = None
        self._micro_frame_buffer.clear()
        self._micro_quiet_ms = 0.0
        self.pending_pause_ms = 0

    def release_tail(self) -> bytes:
        config = self.config.get("natural_interruption", {})
        natural_cut = self.config.get("natural_cut", {})
        if not config.get("enabled") and not natural_cut.get("enabled"):
            return b""
        return build_release_tail(
            self.history.tail(int(config.get("history_ms", 80))),
            release_ms=int(config.get("release_ms", 280)),
            slowdown_percent=float(config.get("slowdown_percent", 12)),
            fade_start_percent=float(config.get("fade_start_percent", 62)),
            natural_cut=natural_cut if natural_cut.get("enabled") else None,
        )

    def response_delay(self, requested_ms: int | float | None) -> float:
        config = self.config.get("adaptive_response_delay", {})
        if not config.get("enabled"):
            return 0.0
        minimum = int(config.get("min_ms", 140))
        maximum = max(minimum, int(config.get("max_ms", 650)))
        base = minimum if requested_ms is None else int(requested_ms)
        base = max(minimum, min(maximum, base))
        jitter = max(0, int(config.get("jitter_ms", 35)))
        if jitter:
            base += self._rng.randint(-jitter, jitter)
        return max(0.0, base / 1000.0)
