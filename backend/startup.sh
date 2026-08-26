#!/bin/sh
set -e
cd "$(dirname "$0")"
python -c "import asyncio; from database import init_db; asyncio.run(init_db())"
exec uvicorn main:app --host 0.0.0.0 --port "${PORT:-8000}"
