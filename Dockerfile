FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    LAUNCHSIGNAL_DB_PATH=/data/launchsignal.sqlite3

WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install --no-cache-dir . && useradd -r -u 10001 launchsignal

# Durable state lives on a volume so restarts keep the baseline and the
# deduplication history.
RUN mkdir -p /data && chown launchsignal:launchsignal /data
VOLUME ["/data"]
USER launchsignal

HEALTHCHECK --interval=60s --timeout=10s --start-period=20s --retries=3 \
  CMD launchsignal health > /dev/null || exit 1

ENTRYPOINT ["launchsignal"]
CMD ["serve"]
