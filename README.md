# Elvin Voice Platform

Новый Python-проект исходящих AI-звонков для LPTracker. Интерфейс, PostgreSQL-схема и бизнес-функции совместимы с предыдущей Python-версией, а голосовой медиаконтур построен заново.

## Главное архитектурное правило

1. Backend создаёт локальные Silero VAD и Smart Turn.
2. Backend открывает **реальную Gemini Live-сессию** и ждёт успешного setup complete.
3. Только после этого backend вызывает LPTracker `POST /lead/{lead_id}/call`.
4. LPTracker первым звонит в Asterisk, затем клиенту и соединяет два плеча.
5. Asterisk ничего не набирает: он принимает звонок LPTracker и передаёт двусторонний PCM через `chan_websocket`.
6. Входящий `slin16` анализируется по каждому 20-миллисекундному фрейму. Сводка `Asterisk PCM level` выводится примерно раз в секунду.
7. Silero открывает turn, pre-roll возвращает начало слова, Smart Turn закрывает реплику.
8. Auto VAD Gemini отключён. Backend отправляет explicit `activityStart` / `activityEnd`.
9. PCM 24 кГц от Gemini преобразуется в 16 кГц и отправляется Asterisk.

## Функции интерфейса

- вход по логину и паролю LPTracker;
- проекты LPTracker слева, роботы выбранного проекта справа;
- добавление сохранённого робота в проект;
- выбор стадии-источника и стадий результатов звонка;
- лимиты звонков и лидов, счётчики и остановка по первому достигнутому лимиту;
- отдельные Gemini Live tools для классификации исхода разговора;
- циклическое фоновое аудио только на исходящем плече Asterisk;
- сбор и просмотр очереди;
- старт, стоп и удаление назначения;
- создание и редактирование роботов: роль, база знаний, условия стадий, название, описание, голос и температура;
- модель зафиксирована как `gemini-3.1-flash-live-preview`;
- в системных настройках редактируется только Gemini API key.

## Совместимость с прежней базой

Проект использует прежнюю PostgreSQL-схему `app` и таблицы:

- `app.elvin_settings`;
- `app.robot_profiles`;
- `app.project_robot_assignments`;
- `app.call_batches`;
- `app.call_queue_items`;
- `app.lptracker_webhook_events`.

На сервере достаточно оставить прежние `ELVIN_DB_*` переменные. Локально без БД автоматически используется `data/elvin-state.json`.

## Открытие в PyCharm

1. Распакуйте архив.
2. В PyCharm выберите **File → Open** и укажите папку распакованного проекта.
3. Откройте встроенный Terminal.
4. Выполните:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install uv
uv sync
uv pip install -r media-requirements.txt
Copy-Item .env.example .env
```

5. Для запуска откройте `run_local.py`, нажмите правой кнопкой и выберите **Run 'run_local'**.
6. Откройте `http://127.0.0.1:8000`.

Локально реальные звонки отключены. LPTracker-интерфейс и файловое хранилище работают после ввода настоящих LPTracker-учётных данных.


### Локальный запуск через Docker Compose

После создания `.env` можно выполнить:

```powershell
docker compose up --build
```

Для серверного деплоя используется более быстрый двухслойный вариант `Dockerfile.deps` + `Dockerfile`; Compose применяет самостоятельный `Dockerfile.standalone`.

## Push в существующий репозиторий

Из корня распакованного проекта:

```powershell
git init
git remote add origin https://github.com/Evgenijyar/elvin.git
git fetch origin
git checkout -B main
git add .
git commit -m "feat: rebuild Elvin with prepared Gemini and local turn control"
git push --force-with-lease origin main
```

`--force-with-lease` нужен, потому что архив представляет полную новую кодовую базу для той же ветки. Перед push рекомендуется сохранить старую ветку/тег на GitHub.

## Серверный деплой

В архиве есть `deploy/server/elvin-deploy.sh`. Действующая команда `elvin-deploy` на сервере может продолжать использоваться, если она уже обновляет `/opt/lead-voice/app` из `origin/main` и собирает `Dockerfile.deps` + `Dockerfile`.

Production env-файлы остаются прежними:

- `/opt/lead-voice/config/database.env`;
- `/opt/lead-voice/config/application.env`;
- `/opt/lead-voice/config/asterisk-secrets.env`.

Для реальных звонков должны быть включены:

```env
ELVIN_CALLS_ENABLED=true
ELVIN_MEDIA_READY=true
```

## Диагностика звонка

Для каждого звонка создаётся каталог `recordings/<batch>-<lead>/`:

- `caller-in.wav` — что пришло из Asterisk;
- `bot-to-asterisk.wav` — что отправлено обратно;
- `frames.ndjson.gz` — характеристики каждого PCM-фрейма;
- `timeline.json` — ключевые события и задержки.

Обычный лог выводит секундные агрегаты и события start/end, чтобы сам журнал не тормозил медиапоток.

Подробная последовательность медиаканала описана в `ARCHITECTURE.md`.

## Проверки

```powershell
uv run pytest
uv run ruff check src tests
python -m compileall src
```

Реальный end-to-end тест требует production-секретов, работающего LPTracker SIP-плеча, `chan_websocket` в Asterisk и действующего Gemini API key.

## Server operations

After the one-time command link is installed, normal server operations are:

```bash
elvin-deploy
elvin-deploy status
elvin-deploy health
elvin-deploy logs 300
elvin-deploy asterisk-logs 300
```

`elvin-deploy` first starts an isolated candidate. The previous production
container is kept as a rollback copy until the new container passes readiness.


## Стабильная точка и изменения 1.1.0

Неприкосновенная точка отката описана в `STABLE_BASELINE.md`. Полный список
запрошенных изменений версии 1.1.0 находится в `CHANGELOG_1.1.0.md`.
