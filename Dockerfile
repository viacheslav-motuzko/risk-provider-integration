# syntax=docker/dockerfile:1
# In production this is the monolith's image (one image, two Deployments); here it carries the integration only.
# Targets: `app` (the default, last) runs the integration; `fake-provider` is the demo stand-in, never deployed.
ARG PYTHON_IMAGE=python:3.12.11-slim-bookworm

FROM ${PYTHON_IMAGE} AS build
COPY --from=ghcr.io/astral-sh/uv:0.7.13 /uv /bin/uv
ENV UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/opt/venv
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project

FROM ${PYTHON_IMAGE} AS base
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/opt/venv/bin:$PATH"
RUN useradd --system --uid 10001 --no-create-home app
COPY --from=build /opt/venv /opt/venv
WORKDIR /app
COPY manage.py ./
COPY config ./config
COPY apps ./apps
USER 10001

FROM base AS fake-provider
COPY tools ./tools
EXPOSE 8765
CMD ["python", "-m", "tools.fake_provider"]

FROM base AS app
# Probes and metrics.
EXPOSE 9100
# Exec form, no shell: Python is PID 1 and receives SIGTERM itself. The command installs its handler first.
CMD ["python", "manage.py", "run_risk_provider"]
