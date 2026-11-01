FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY bridge ./bridge
COPY docker-entrypoint.sh .

ENV PYTHONUNBUFFERED=1 DATA_DIR=/data
EXPOSE 7913
HEALTHCHECK --interval=60s --timeout=10s --start-period=20s \
  CMD python -c "import os, urllib.request; urllib.request.urlopen(f\"http://127.0.0.1:{os.environ.get('WEB_PORT') or 7913}/healthz\", timeout=5)"
ENTRYPOINT ["./docker-entrypoint.sh"]
CMD ["run"]
