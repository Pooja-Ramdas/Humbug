#!/bin/sh
set -e

DB_PATH="${DB_PATH:-/app/data/data.db}"
DATA_DIR="$(dirname "$DB_PATH")"

# Ensure data directory exists
mkdir -p "$DATA_DIR"

# Generate the seed database if it doesn't already exist.
# We keep any existing DB so that injected faults and tickets survive
# a container restart during a review session.
if [ ! -f "$DB_PATH" ]; then
  echo "[entrypoint] Generating synthetic network data..."
  python /app/generate_data.py \
    --seed 42 \
    --out-dir "$DATA_DIR" \
    --db "$DB_PATH"
  echo "[entrypoint] Seed database created at $DB_PATH"
else
  echo "[entrypoint] Using existing database at $DB_PATH"
fi

PORT="${PORT:-8000}"
echo "[entrypoint] Starting Humbug backend on :$PORT"
exec uvicorn backend.app:app \
  --host 0.0.0.0 \
  --port "$PORT" \
  --workers 1 \
  --log-level info
