# Elvin 1.1.7

## Transparent Gemini configuration

- Removed the hardcoded Russian base prompt from `gemini_live.py`.
- Removed the hidden outcome-classification prompt block.
- `system_instruction` now contains only the visible “Системный prompt” and
  “База знаний” fields, joined verbatim; when both are empty the Live session
  is created without `system_instruction`.
- Stage tool descriptions are now exactly the text entered in the six visible
  stage-condition fields.
- Added an “Итоговая конфигурация” UI tab showing the exact system instruction
  and tool descriptions sent to Gemini.

## Robot-initiated hangup

- Added the visible `Условия вызова end_call` field.
- Added stage references such as `{{stage:stop_list}}` and
  `{{stage:callback}}`; they expand only to text entered in the corresponding
  visible stage fields.
- Added configurable playback-wait and final-delay values.
- Added the Gemini Live `end_call` function declaration and manual
  `FunctionResponse` handling.
- After `end_call`, Elvin waits for the current model turn and Asterisk media
  marker, then sends the native chan_websocket `HANGUP` control command.
- AMI `Hangup` on the exact channel and local WebSocket closure remain fallback
  paths if the native command cannot be sent.

## Compatibility

- Existing PostgreSQL databases receive additive columns only.
- Older stable builds ignore the new columns after rollback.
- Inbound PCM, outbound PCM, VAD, Smart Turn, barge-in, resampling, background
  audio and echo handling are unchanged.
