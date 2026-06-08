#!/bin/bash
set -e
FRONTEND=/Users/richard/my_stock_ai/zplan-web/frontend
WEB=/Users/richard/my_stock_ai/zplan-web
cd $FRONTEND && npm run build
cd $WEB
kill $(lsof -t -i :8000) 2>/dev/null || true
sleep 1
.venv/bin/uvicorn main:app --host 0.0.0.0 --port 8000 &
sleep 4
cd $FRONTEND && node test-features.mjs
