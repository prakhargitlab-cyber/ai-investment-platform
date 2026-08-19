FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml .
COPY app ./app
COPY db ./db
RUN pip install --no-cache-dir .
EXPOSE 8000
RUN useradd --create-home --uid 10001 appuser
USER appuser
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
