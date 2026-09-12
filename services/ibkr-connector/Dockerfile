FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends default-jre-headless \
    && rm -rf /var/lib/apt/lists/*

COPY services/ibkr-connector/pyproject.toml /app/pyproject.toml
COPY services/ibkr-connector/app /app/app

RUN pip install --no-cache-dir .

RUN addgroup --gid 1000 ibkr \
    && adduser --uid 1000 --gid 1000 --disabled-password --gecos "" ibkr \
    && chown -R 1000:1000 /app

USER 1000:1000

EXPOSE 8080

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080", "--no-access-log"]
