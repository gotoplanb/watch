# v1 backend image. Built authoritatively in CodeBuild (ADR-004); same artifact used
# locally via docker-compose (§5). Pattern adapted from the conduct repo: uv (astral)
# base + BuildKit cache mounts + multi-stage + non-root runtime. No apt step — we need
# no system libs (psycopg[binary] bundles its own libpq).
ARG PYTHON_VERSION=3.12

FROM ghcr.io/astral-sh/uv:0.5-python${PYTHON_VERSION}-bookworm-slim AS builder

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

COPY requirements.txt .
RUN --mount=type=cache,target=/root/.cache/uv \
    uv venv /app/.venv \
    && uv pip install --python /app/.venv/bin/python -r requirements.txt


# ECR Public mirror of the Docker official image — avoids Docker Hub anonymous pull
# rate-limits (429) in CodeBuild. Same bytes as docker.io/library/python.
FROM public.ecr.aws/docker/library/python:${PYTHON_VERSION}-slim-bookworm AS runtime

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN groupadd --system --gid 1001 watch \
 && useradd --system --uid 1001 --gid watch --no-create-home watch

WORKDIR /app

COPY --from=builder --chown=watch:watch /app/.venv /app/.venv
COPY --chown=watch:watch . .

USER watch

EXPOSE 8000

# Stateless task behind the ALB (§4.3). Migrations run as a pre-traffic lifecycle
# hook in prod, not at container start (compose overrides CMD for the local loop).
CMD ["gunicorn", "config.wsgi:application", "--bind", "0.0.0.0:8000", "--workers", "3"]
