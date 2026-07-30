# Elvin 1.2.0

## Selectable production call transport

- Added a per-assignment switch between the established LPTracker
  `/lead/{id}/call` flow and direct Asterisk SIP origination.
- Existing assignments default to the LPTracker API transport, preserving the
  previous production behavior until the switch is changed explicitly.
- The direct transport prepares Gemini first, originates the first documented
  phone from the queued lead through `PJSIP/lptracker-endpoint`, and bridges
  the answered channel into the same `chan_websocket` voice runtime.
- Added a dedicated Asterisk dialplan context and a detailed AMI lifecycle log
  for every production direct call.
- Phone extraction now follows LPTracker's documented `contact.details` order,
  validates the first phone entry, and stores only a dial-safe normalized
  number in the private queue item.
- The isolated **Тест телефонии** page remains available and unchanged.
