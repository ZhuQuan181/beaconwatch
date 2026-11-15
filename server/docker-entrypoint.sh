#!/bin/sh
set -eu

if [ "${RID_SKIP_MIGRATIONS:-0}" != "1" ]; then
  max_attempts="${RID_MIGRATION_MAX_ATTEMPTS:-20}"
  delay="${RID_MIGRATION_RETRY_BASE_S:-1}"
  max_delay="${RID_MIGRATION_RETRY_MAX_S:-10}"
  attempt=1
  while :; do
    echo "running database migrations (attempt ${attempt}/${max_attempts})"
    if alembic upgrade head; then
      break
    fi
    if [ "${attempt}" -ge "${max_attempts}" ]; then
      echo "database migrations failed after ${attempt} attempts" >&2
      exit 1
    fi
    echo "migrations failed; retrying in ${delay}s" >&2
    sleep "${delay}"
    attempt=$((attempt + 1))
    delay=$((delay * 2))
    if [ "${delay}" -gt "${max_delay}" ]; then
      delay="${max_delay}"
    fi
  done
fi

exec "$@"
