# Elvin 1.1.9

## Isolated direct-SIP telephony test

- Added a separate **Тест телефонии** page to the authenticated Elvin UI.
- The page accepts a phone number and an audio file without using the
  production LPTracker callback, Gemini, call queue, or chan_websocket path.
- Uploaded audio is bounded, validated, converted by ffmpeg to an
  Asterisk-compatible 8 kHz mono PCM WAV, and removed after the call.
- Added a dedicated AMI event session that originates
  `PJSIP/<number>@lptracker-endpoint` and records the complete per-test
  timeline in both the Docker console and the UI.
- Added an isolated Asterisk dialplan context that starts playback only after
  the outbound subscriber channel has answered.
- Test audio is exposed to Asterisk through a dedicated read-only media path;
  the wider Elvin data directory keeps its restrictive permissions.
- Deployment installs and validates only the new context; the production
  `from-lptracker -> elvin-ai -> chan_websocket` route is unchanged.
