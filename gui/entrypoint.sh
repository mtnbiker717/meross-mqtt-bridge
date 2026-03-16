#!/bin/sh
set -e

# Ensure mounted directories are writable by app user
chown -R app:app /app/config /app/logs 2>/dev/null || true

# Drop privileges and exec the main process
exec gosu app "$@"
