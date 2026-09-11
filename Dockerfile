# syntax=docker/dockerfile:1.7
FROM ghcr.io/astral-sh/uv:0.12.9 AS uv
# Verified AgentServer 0.14.0 persistent runtime, Python 3.13.
FROM langchain/langgraph-api:3.13@sha256:ea41dd0c850faea620be0e3383cdca2031b1362040f92d3748846818e0047551

COPY --from=uv /uv /usr/local/bin/uv
WORKDIR /app/financeclaw
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 UV_LINK_MODE=copy \
    TIKTOKEN_CACHE_DIR=/app/financeclaw/.cache/tiktoken
COPY pyproject.toml uv.lock README.md ./
RUN uv export --frozen --no-dev --extra ziwei --no-emit-project -o /tmp/requirements.txt \
    && uv pip install --system --no-cache -r /tmp/requirements.txt
COPY financeclaw ./financeclaw
COPY langgraph.json alembic.ini ./
COPY deploy ./deploy
RUN uv pip install --system --no-deps --no-cache . \
    && python deploy/configure_image.py \
    && python -c 'import importlib.metadata as m; assert m.version("langgraph-api") == "0.14.0"; assert m.version("langgraph-sdk") == "0.4.4"; assert m.version("langgraph") == "1.2.11"' \
    && python -c 'import tiktoken; tiktoken.get_encoding("cl100k_base")'
EXPOSE 8000
ENTRYPOINT ["/bin/sh", "/app/financeclaw/deploy/entrypoint.sh"]
