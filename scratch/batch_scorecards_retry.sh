#!/bin/bash
# A-4 retry: 8 symbols that hit Gemini 429. 45s spacing + up to 3 attempts each.
SYMBOLS="SPCX LPK IREN POET MSFT RKLB COHR AVGO"
for s in $SYMBOLS; do
  for attempt in 1 2 3; do
    echo "=== $s attempt $attempt $(date +%H:%M:%S) ==="
    body=$(curl -s -X POST "http://127.0.0.1:8787/api/scorecard/generate/$s" -H "Content-Type: application/json" -d '{}')
    echo "$body" | head -c 200; echo
    if echo "$body" | grep -q '"success": true'; then break; fi
    sleep 60
  done
  sleep 45
done
echo "=== RETRY DONE ==="
