FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml .
COPY app ./app
RUN pip install --no-cache-dir .
EXPOSE 8000
RUN groupadd --gid 10001 appuser && useradd --create-home --uid 10001 --gid 10001 appuser
USER 10001:10001
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
