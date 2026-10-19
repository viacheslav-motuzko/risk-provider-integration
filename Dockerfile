# STUB: finalized in stage 5 (pinned base image and uv version, multi-stage build, probe port).
FROM python:3.12-slim

# Pin the uv version in stage 5 for reproducible builds.
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH="/opt/venv/bin:$PATH"

WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project

COPY . .

RUN useradd --system --uid 10001 --no-create-home app && chown -R app /app
USER app

# Probes and metrics port (placeholder, see runtime/health.py in stage 5).
EXPOSE 9100

CMD ["python", "manage.py", "run_risk_provider"]
