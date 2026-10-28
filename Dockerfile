# Stage 1: install the locked dependencies and the project into /app/.venv
FROM python:3.12-slim AS builder

COPY --from=ghcr.io/astral-sh/uv:0.12.23 /uv /uvx /bin/

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

WORKDIR /app

# Dependencies first: this layer is rebuilt only when pyproject.toml or uv.lock change
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --locked --no-dev --no-install-project

# Project code last. The install stays editable on purpose: the config loaders look for
# config/ three folders above the package, so src/ and config/ must stay side by side in /app.
COPY src ./src
COPY config ./config
RUN uv sync --locked --no-dev

# Stage 2: the same base image without uv and the build leftovers
FROM python:3.12-slim

RUN groupadd --gid 1001 app && useradd --uid 1001 --gid 1001 --no-create-home app

WORKDIR /app
COPY --from=builder --chown=1001:1001 /app /app

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1

USER 1001

# The image is a batch job for now, so there is no port to ask. This checks that the
# package, its dependencies and the config files load. Python instead of curl: no extra tool.
HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
    CMD ["python", "-c", "from flowguard.common.pipeline_config import load_pipeline_config; load_pipeline_config()"]

# Default command: the hourly ingest. The backfill uses the same image:
#   docker run --rm --entrypoint python flowguard -m flowguard.features.backfill
ENTRYPOINT ["python", "-m", "flowguard.features.ingest"]
