FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOME=/tmp

WORKDIR /app
COPY ai/research-engine /opt/research-engine
COPY ai/yahoo-finance-mcp /opt/yahoo-finance-mcp
RUN pip install --no-cache-dir /opt/research-engine /opt/yahoo-finance-mcp \
    && groupadd --gid 10002 app \
    && useradd --uid 10002 --gid 10002 --no-create-home --shell /usr/sbin/nologin app

USER 10002:10002
EXPOSE 8002

CMD ["python", "-m", "yahoo_mcp_server.main", "--transport", "streamable-http"]
