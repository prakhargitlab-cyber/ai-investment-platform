FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml .
COPY app ./app
RUN pip install --no-cache-dir .
RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        tesseract-ocr \
        tesseract-ocr-eng \
        poppler-utils \
    && rm -rf /var/lib/apt/lists/*
EXPOSE 8000
RUN groupadd --gid 10001 appuser && useradd --create-home --uid 10001 --gid 10001 appuser
USER 10001:10001
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
