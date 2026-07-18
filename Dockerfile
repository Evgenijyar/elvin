ARG ELVIN_DEPS_IMAGE=elvin-backend-deps:local
FROM ${ELVIN_DEPS_IMAGE}

ENV PYTHONPATH=/app/src

COPY src ./src

ARG APP_UID=994
ARG APP_GID=986

RUN groupadd --gid "${APP_GID}" elvin \
    && useradd \
        --uid "${APP_UID}" \
        --gid "${APP_GID}" \
        --create-home \
        --shell /usr/sbin/nologin \
        elvin

USER elvin

EXPOSE 8000

HEALTHCHECK \
    --interval=30s \
    --timeout=3s \
    --start-period=30s \
    --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=2).read()"]

CMD ["/app/.venv/bin/python", "-m", "elvin"]
