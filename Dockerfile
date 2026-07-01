FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml .
RUN pip install --no-cache-dir -e "." && \
    pip install --no-cache-dir uvicorn "mcp[fastapi]"

COPY src/ ./src/
COPY prompts/ ./prompts/
COPY config/ ./config/
COPY static/ ./static/

ENV PYTHONPATH=/app
ENV PYTHONUNBUFFERED=1

EXPOSE 3458

ENTRYPOINT ["uvicorn", "src.mcp_server:app", "--host", "0.0.0.0", "--port", "3458"]
