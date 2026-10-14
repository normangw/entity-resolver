# Where each component lives (HW2 Problem 4)
# -----------------------------------------
# Model: the entity resolver (RapidFuzz scorer over an alias index) runs as a
#   library inside this application container. It is CPU-only, scores a name in
#   milliseconds and needs no GPU, so a second model container or a hosted API
#   would only add a network hop and another thing to keep running.
# Weights / artifact: the "model artifact" is the alias index
#   src/entity_resolver/data/companies.json. It is a few KB and versioned with the
#   code, so it is baked into the image. compose.yml copies it into the named
#   volume `alias-index` on first start. compose.yml sets ALIAS_INDEX_PATH under
#   `environment`, which overrides .env, and the volume keeps the first-start
#   file, so changing companies.json needs `docker compose down -v` and a rebuild.
# Dependencies: none yet (no database, vector store or cache), so compose.yml
#   runs this one service.

FROM python:3.12-slim

# uv, pinned, copied from its official image.
COPY --from=ghcr.io/astral-sh/uv:0.9.5 /uv /uvx /bin/

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH"

# Non-root user that owns the app directory.
RUN useradd --create-home --uid 1000 app
WORKDIR /app
RUN chown app:app /app
USER app

# 1) Dependencies only, from the lock file. This layer stays cached until
#    pyproject.toml or uv.lock change, so code edits rebuild in seconds.
COPY --chown=app:app pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

# 2) The project itself.
COPY --chown=app:app README.md ./
COPY --chown=app:app src ./src
RUN uv sync --frozen --no-dev

EXPOSE 8000

# python:3.12-slim has no curl, so the health check uses the standard library.
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import urllib.request, sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=2).status == 200 else 1)"]

CMD ["uvicorn", "entity_resolver.main:app", "--host", "0.0.0.0", "--port", "8000"]
