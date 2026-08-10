FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py .
COPY stockbot ./stockbot

# The watchlist, price history and credit counter live here. Mount a volume or
# a redeploy wipes them — and the UZSE weekly figures are built up locally over
# days, so losing this directory costs real data, not just convenience.
RUN mkdir -p /app/data && useradd --create-home --uid 10001 bot \
    && chown -R bot:bot /app
VOLUME ["/app/data"]

COPY docker-entrypoint.sh /usr/local/bin/
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

# Deliberately no USER here: the entrypoint starts as root only long enough to
# take ownership of the mounted data directory, then drops to uid 10001 before
# exec-ing the bot. A bind mount overrides whatever ownership this image sets,
# so it has to be fixed at runtime rather than at build time.
ENTRYPOINT ["docker-entrypoint.sh"]

# No port is exposed on purpose: the bot uses long polling, so it needs no
# inbound traffic, no domain and no certificate.

CMD ["python", "main.py"]
