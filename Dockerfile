# Tapo manager service image. Uses uv for fast, reproducible installs.
FROM python:3.13-slim

# uv from the official distroless image (pinned, no network install step)
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /app

# Install deps first (cached layer) using the lockfile only.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

# Then the source.
COPY . .
RUN uv sync --frozen --no-dev

ENV PATH="/app/.venv/bin:${PATH}"

ENTRYPOINT ["/app/scripts/entrypoint.sh"]
