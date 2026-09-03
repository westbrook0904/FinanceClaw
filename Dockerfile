# syntax=docker/dockerfile:1.7
FROM ghcr.io/astral-sh/uv:0.12.9 AS uv
FROM python:3.13-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/financeclaw/.venv

RUN groupadd --system financeclaw && useradd --system --gid financeclaw financeclaw
WORKDIR /opt/financeclaw
COPY --from=uv /uv /usr/local/bin/uv
COPY pyproject.toml uv.lock README.md ./
COPY financeclaw ./financeclaw
COPY main.py alembic.ini ./
RUN uv sync --frozen --no-dev --no-editable

USER financeclaw
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=3s --start-period=20s --retries=3 \
  CMD ["/opt/financeclaw/.venv/bin/python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=2)"]
CMD ["/opt/financeclaw/.venv/bin/uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]
