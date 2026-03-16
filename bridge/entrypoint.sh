#!/bin/sh
set -e

# Load SECRET_KEY from config/.env if not already in environment
if [ -z "$SECRET_KEY" ]; then
  ENV_FILE="/app/config/.env"
  if [ -f "$ENV_FILE" ]; then
    export SECRET_KEY=$(grep SECRET_KEY "$ENV_FILE" | cut -d= -f2)
  fi
  # If still unset: bridge runs in plaintext mode (graceful degradation)
fi

# Ensure mounted directories are writable by app user
chown -R app:app /app/config /app/logs 2>/dev/null || true

# Drop privileges and exec the main process
exec gosu app "$@"
