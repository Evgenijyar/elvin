"""Supported outbound call transports."""

LPTRACKER_API = "lptracker_api"
DIRECT_SIP = "direct_sip"
CALL_TRANSPORTS = frozenset({LPTRACKER_API, DIRECT_SIP})


def normalize_call_transport(value: object) -> str:
    candidate = str(value or LPTRACKER_API).strip().lower()
    return candidate if candidate in CALL_TRANSPORTS else LPTRACKER_API
