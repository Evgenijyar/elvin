# Неприкосновенные стабильные точки Elvin

## Основная точка отката перед Actor/Director

Текущая проверенная production-сборка со стадиями, outcome tools, лимитами и
фоновым аудио:

- тег Git: `v1.1.0-stable`;
- commit: `3eaef60099bfef96f4feb0a666bc3698935e92f1`.

Этот тег является основной точкой отката для разработки Gemini Director и
разговорных эффектов. Он не перемещается и не изменяется.

## Предыдущая стабильная точка

Проверенная сборка до стадий, лимитов и фонового аудио:

- тег Git: `v1.0.0-stable`;
- commit: `55c1b469b47cfc38e07684e6715804872270c4ef`;
- Docker-образ: `elvin-backend:55c1b469b47c`.

Тег `v1.0.0-stable` также не перемещается и не изменяется.

## Проверка тегов

```bash
git rev-parse v1.1.0-stable^{commit}
git rev-parse v1.0.0-stable^{commit}
```

Ожидаемые commits:

```text
3eaef60099bfef96f4feb0a666bc3698935e92f1
55c1b469b47cfc38e07684e6715804872270c4ef
```

## Откат к версии 1.1.0

```bash
git fetch --tags origin
git checkout main
git reset --hard v1.1.0-stable
git push --force-with-lease origin main
elvin-deploy
```

Новая колонка `effects_config` и сохранённый ключ Режиссёра могут остаться в
PostgreSQL: версия 1.1.0 их не использует.

## Откат к версии 1.0.0

```bash
git fetch --tags origin
git checkout main
git reset --hard v1.0.0-stable
git push --force-with-lease origin main
elvin-deploy
```
