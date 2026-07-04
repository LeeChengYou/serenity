#!/bin/bash
# A-4: batch-generate AI scorecards for top-mentioned symbols lacking one.
# EWY excluded (ETF, not a supply-chain bottleneck candidate).
SYMBOLS="AAOI LITE AXTI NBIS SOI MRVL META MU JBL XFAB SNDK GOOGL AMZN IQE AMD INTC GFS SPCX TSEM LPK IREN POET MSFT RDDT RKLB COHR AVGO"
for s in $SYMBOLS; do
  echo "=== $s $(date +%H:%M:%S) ==="
  code=$(curl -s -o "/tmp/sc_$s.json" -w "%{http_code}" -X POST "http://127.0.0.1:8787/api/scorecard/generate/$s" -H "Content-Type: application/json" -d '{}')
  echo "HTTP $code"
  head -c 300 "/tmp/sc_$s.json"; echo
  sleep 3
done
echo "=== BATCH DONE ==="
