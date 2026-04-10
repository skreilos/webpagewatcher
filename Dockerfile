FROM python:3.12-alpine

WORKDIR /app

RUN apk add --no-cache ca-certificates iputils tzdata

COPY requirements.txt monitor.py pages.yaml ./
RUN pip install --no-cache-dir -r requirements.txt

ENV WEBPAGE_WATCHER_STATE_FILE=/data/state.json
# Sekunden zwischen Läufen (Compose kann WEBPAGE_WATCHER_INTERVAL_SECONDS setzen)
ENV WEBPAGE_WATCHER_INTERVAL_SECONDS=900

# Einmal Pushover-Test beim Container-Start, danach wiederholte Checks
CMD ["sh", "-c", "python /app/monitor.py -c /app/config.yaml --startup-ping || true; while true; do python /app/monitor.py -c /app/config.yaml || true; sleep ${WEBPAGE_WATCHER_INTERVAL_SECONDS:-900}; done"]
