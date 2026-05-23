FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential libpq-dev \
    && rm -rf /var/lib/apt/lists/*

COPY hackathon/requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Build context is the repo root (see hackathon/docker-compose.yml).
# `app/`        — repo-root Entivia runtime (BaseAgent, JSON validator, tools).
# `hackathon/`  — this submission; copies everything inside including
#                 agents/, core/, data/, eval/, app/, etc.
COPY app ./app
COPY hackathon ./hackathon

ENV PYTHONPATH=/app
ENV HACKATHON_DATABASE_URL=postgresql+asyncpg://hackathon:hackathon@postgres:5432/hackathon
ENV QDRANT_URL=http://qdrant:6333
ENV FASTEMBED_CACHE_PATH=/models

EXPOSE 8000

CMD ["uvicorn", "hackathon.app.main:app", "--host", "0.0.0.0", "--port", "8000"]
