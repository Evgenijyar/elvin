"""Phone extraction, validation and masking shared by outbound call paths."""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Any

_PHONE_DIGITS = re.compile(r"^[1-9]\d{6,14}$")
_PHONE_CANDIDATE = re.compile(r"\+?\d(?:[\d\s().-]*\d)?")


class PhoneNumberError(ValueError):
    """A value cannot be converted to a dial-safe international number."""


def normalize_outbound_phone(value: Any) -> str:
    """Return a dial-safe number containing 7–15 digits and no AMI syntax."""
    raw = str(value or "").strip()
    if not raw:
        raise PhoneNumberError("Введите номер телефона.")
    if not re.fullmatch(r"[\d\s()+-]+", raw):
        raise PhoneNumberError(
            "Номер может содержать только цифры, пробелы, скобки, плюс и дефисы."
        )
    digits = re.sub(r"\D", "", raw)
    if len(digits) == 10:
        digits = f"7{digits}"
    elif len(digits) == 11 and digits.startswith("8"):
        digits = f"7{digits[1:]}"
    if not _PHONE_DIGITS.fullmatch(digits):
        raise PhoneNumberError(
            "Введите международный номер: от 7 до 15 цифр, например 79991234567."
        )
    return digits


def first_phone_from_details(details: Iterable[Mapping[str, Any]] | None) -> str | None:
    """Return the first valid phone detail in LPTracker's documented order."""
    for detail in details or ():
        if not isinstance(detail, Mapping):
            continue
        detail_type = str(detail.get("type") or "").strip().lower()
        if "phone" not in detail_type:
            continue
        raw = str(detail.get("data") or "").strip()
        if not raw:
            continue
        candidates = _PHONE_CANDIDATE.findall(raw)
        # In a combined example such as ``email/+7999...`` prefer an explicit
        # international phone candidate over unrelated digits in the email.
        candidates.sort(key=lambda candidate: not candidate.lstrip().startswith("+"))
        for candidate in candidates:
            try:
                return normalize_outbound_phone(candidate)
            except PhoneNumberError:
                continue
    return None


def mask_phone_number(phone: str) -> str:
    digits = re.sub(r"\D", "", str(phone or ""))
    if len(digits) <= 4:
        return "***"
    visible_prefix = min(2, max(1, len(digits) - 4))
    return f"{digits[:visible_prefix]}{'*' * (len(digits) - visible_prefix - 4)}{digits[-4:]}"
