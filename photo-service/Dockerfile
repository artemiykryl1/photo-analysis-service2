FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_LINK_MODE=copy

# Install uv (single static binary, no extra build deps required).
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app

# Dependency layer first, cached separately from the application code.
# --no-install-project: only resolve/install third-party deps here, since
# the `app/` package (required by hatchling's packages=["app"]) does not
# exist on disk yet - installing the project itself happens below, after
# the code is copied in.
COPY pyproject.toml ./
RUN uv sync --no-dev --no-install-project

# Application code.
COPY app/ ./app/
COPY alembic.ini ./
COPY migrations/ ./migrations/

# Now that app/ exists, install the project itself (fast: deps already cached).
RUN uv sync --no-dev

EXPOSE 8000

CMD ["uv", "run", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
