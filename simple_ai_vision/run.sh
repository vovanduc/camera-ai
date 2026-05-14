#!/usr/bin/env sh
set -eu

exec uvicorn app:app --host 0.0.0.0 --port 8000
