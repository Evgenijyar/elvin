# Неприкосновенная стабильная точка Elvin

Проверенная production-сборка до разработки стадий, лимитов и фонового аудио:

- тег Git: `v1.0.0-stable`;
- commit: `55c1b469b47cfc38e07684e6715804872270c4ef`;
- Docker-образ: `elvin-backend:55c1b469b47c`;
- на момент фиксации ветки `main` и `production` указывали на этот commit.

Тег `v1.0.0-stable` в этой разработке не перемещается и не изменяется.

## Проверка точки отката

```bash
git show --no-patch --decorate v1.0.0-stable
git rev-parse v1.0.0-stable^{commit}
```

Ожидаемый commit:

```text
55c1b469b47cfc38e07684e6715804872270c4ef
```

## Откат Git при необходимости

```bash
git fetch --tags origin
git checkout main
git reset --hard v1.0.0-stable
git push --force-with-lease origin main
```

После этого выполняется обычный серверный деплой:

```bash
elvin-deploy
```

При необходимости можно повторно запустить сохранённый Docker-образ
`elvin-backend:55c1b469b47c` согласно действующей серверной схеме Elvin.
