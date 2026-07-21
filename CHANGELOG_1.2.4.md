# Elvin 1.2.4 — восстановление outbound-аудио после XOFF

- Исправлено зависание outbound-медиашлюза после `MEDIA_XOFF`, когда
  `FLUSH_MEDIA` не сопровождался отдельным `MEDIA_XON`.
- `PAUSE_MEDIA`, `CONTINUE_MEDIA` и `FLUSH_MEDIA` теперь синхронно обновляют
  локальный gate передачи.
- PCM на Asterisk отправляется кадрами `optimal_frame_size` с интервалом
  `ptime`, поэтому большие пачки Actor/DSP/Director-аудио больше не заполняют
  очередь Asterisk и не вызывают многосекундные провалы.
- Добавлены regression-тесты на XOFF/FLUSH и кадрирование realtime-аудио.

Проверки: 59 тестов, `ruff`, `compileall`, JavaScript syntax check.
