#!/bin/sh
set -e

# The container starts as root purely to fix ownership of the data directory,
# then immediately drops to an unprivileged user to run the bot.
#
# This exists because docker-compose bind-mounts the host's ./data over
# /app/data. A bind mount carries the *host* directory's ownership, which is
# whatever the person who ran `git clone` happens to be — so the ownership the
# Dockerfile set is discarded, and the bot cannot create its database. Fixing it
# here means the mount works no matter who owns the host directory.

APP_UID=10001
APP_GID=10001
DATA_DIR="$(dirname "${DATABASE_PATH:-/app/data/stockbot.db}")"

mkdir -p "$DATA_DIR"
if [ "$(id -u)" = "0" ]; then
    chown -R "$APP_UID:$APP_GID" "$DATA_DIR" 2>/dev/null || true
    # setpriv ships with util-linux, which is part of the Debian base image, so
    # no extra package is needed to drop privileges. --clear-groups is used
    # rather than --init-groups because the latter needs the uid to resolve in
    # /etc/passwd and fails hard when it does not; the bot needs no
    # supplementary groups either way.
    if command -v setpriv >/dev/null 2>&1; then
        exec setpriv --reuid="$APP_UID" --regid="$APP_GID" --clear-groups "$@"
    fi
    echo "setpriv unavailable; continuing as root" >&2
fi

exec "$@"
