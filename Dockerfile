FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    curl \
    && rm -rf /var/lib/apt/lists/*

RUN pip install uv

COPY pyproject.toml ./
RUN uv pip install --system -r pyproject.toml

# Modelo spaCy que usa Presidio (app/services/anonymizer.py -> es_core_news_md).
# Sin esto el anonymizer crashea al primer LLM call y se rompe la anonimización PII.
RUN python -m spacy download es_core_news_md

COPY . .

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
