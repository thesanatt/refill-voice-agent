FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:0.11.12 /uv /usr/local/bin/uv

ENV PYTHONUNBUFFERED=1 UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

COPY refill ./refill
COPY docs/TESTING_GUIDE.md ./docs/TESTING_GUIDE.md
ENV REFILL_HOST=0.0.0.0 PATH="/app/.venv/bin:$PATH"
CMD ["python", "-m", "refill.server"]
