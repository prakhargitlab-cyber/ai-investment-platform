FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOME=/tmp

WORKDIR /app
# Only the installable research-engine PACKAGE is copied -- never the
# whole ai/research-engine directory. pyproject.toml's own
# [tool.setuptools.packages.find] include=["app*"] means app/ is the
# entire installable package; nothing outside it (including loose
# root-level scripts such as JWT-signing helpers or SQL files) is ever
# required, imported, or installed. Copying only these two paths makes
# that boundary structural rather than a .dockerignore pattern that a
# future broadened COPY could silently reintroduce.
COPY ai/research-engine/pyproject.toml /opt/research-engine/pyproject.toml
COPY ai/research-engine/app /opt/research-engine/app
COPY ai/yahoo-finance-mcp /opt/yahoo-finance-mcp
RUN pip install --no-cache-dir /opt/research-engine /opt/yahoo-finance-mcp \
    && groupadd --gid 10002 app \
    && useradd --uid 10002 --gid 10002 --no-create-home --shell /usr/sbin/nologin app

USER 10002:10002
EXPOSE 8002

CMD ["python", "-m", "yahoo_mcp_server.main", "--transport", "streamable-http"]
