#!/bin/sh
set -e

# Generate SECRET_KEY if not already set
if [ -z "$SECRET_KEY" ]; then
  ENV_FILE="/app/config/.env"
  if [ ! -f "$ENV_FILE" ]; then
    echo "Generating SECRET_KEY for credential encryption..."
    KEY=$(python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())")
    echo "SECRET_KEY=$KEY" > "$ENV_FILE"
    chmod 600 "$ENV_FILE"
    echo "SECRET_KEY written to config/.env — back this file up separately from config.yaml"
  fi
  export SECRET_KEY=$(grep SECRET_KEY "$ENV_FILE" | cut -d= -f2)
fi

# Ensure mounted directories are writable by app user
chown -R app:app /app/config /app/logs 2>/dev/null || true

# Drop privileges and exec the main process
exec gosu app "$@"
