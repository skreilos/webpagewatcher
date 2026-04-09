FROM python:3.12-alpine

WORKDIR /app

RUN apk add --no-cache iputils

COPY requirements.txt monitor.py pages.yaml ./
RUN pip install --no-cache-dir -r requirements.txt

ENV WEBPAGE_WATCHER_STATE_FILE=/data/state.json

# Stündlicher Loop (Alternative: einmaliger Lauf per externem Cron: docker compose run --rm …)
CMD ["sh", "-c", "while true; do python /app/monitor.py -c /app/config.yaml || true; sleep 3600; done"]
