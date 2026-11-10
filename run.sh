#!/usr/bin/env sh
# Local start: ./run.sh   then open http://localhost:8000
set -e
[ -f .env ] && export $(grep -v '^#' .env | xargs)
python -m uvicorn app.main:app --host 0.0.0.0 --port "${PORT:-8000}" --reload
