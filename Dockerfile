# Stage 1: Build dependencies using wheels
FROM python:3.11-slim AS builder

WORKDIR /build

RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    python3-dev \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml .
# Gera wheels das dependências na pasta /wheels (preferindo binários pré-compilados)
RUN pip wheel --prefer-binary --wheel-dir=/wheels -e ".[all]" && \
    pip wheel --prefer-binary --wheel-dir=/wheels uvicorn "mcp[fastapi]"

# Stage 2: Runtime image without compilation tools
FROM python:3.11-slim

WORKDIR /app

# Copia os wheels compilados do stage anterior
COPY --from=builder /wheels /wheels
RUN pip install --no-index --find-links=/wheels /wheels/*.whl && \
    rm -rf /wheels && \
    apt-get update && apt-get install -y --no-install-recommends curl && \
    rm -rf /var/lib/apt/lists/*

COPY src/ ./src/
COPY prompts/ ./prompts/
COPY config/ ./config/
COPY static/ ./static/
COPY pyproject.toml .

ENV PYTHONPATH=/app
ENV PYTHONUNBUFFERED=1

EXPOSE 3458

ENTRYPOINT ["uvicorn", "src.mcp_server:app", "--host", "0.0.0.0", "--port", "3458"]
