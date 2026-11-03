FROM python:3.12-slim

# Set by the release workflow so the running container can say which build it is.
ARG APP_VERSION=dev
LABEL org.opencontainers.image.source="https://github.com/mathiasbk/openprinttag-spoolman"

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY bridge ./bridge
COPY docker-entrypoint.sh .

ENV PYTHONUNBUFFERED=1 DATA_DIR=/data APP_VERSION=$APP_VERSION
EXPOSE 7913
HEALTHCHECK --interval=60s --timeout=10s --start-period=20s \
  CMD python -c "import os, urllib.request; urllib.request.urlopen(f\"http://127.0.0.1:{os.environ.get('WEB_PORT') or 7913}/healthz\", timeout=5)"
ENTRYPOINT ["./docker-entrypoint.sh"]
CMD ["run"]
