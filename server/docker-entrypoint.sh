#!/bin/sh
set -eu

if [ "${RID_SKIP_MIGRATIONS:-0}" != "1" ]; then
  echo "running database migrations"
  alembic upgrade head
fi

exec "$@"
