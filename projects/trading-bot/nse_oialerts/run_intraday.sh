#!/bin/bash
# NSE OI Intraday Scanner + Alert
# Runs at 9:30 AM IST (04:00 UTC) Monday–Friday
# Scans NSE for intraday OI signals, then sends Telegram alert to Charlie

set -e
WORKSPACE="/root/.openclaw/workspace"
LOG="$WORKSPACE/projects/trading-bot/nse_oialerts/logs/intraday_$(date +%Y%m%d).log"
mkdir -p "$(dirname "$LOG")"

echo "======================================" >> "$LOG"
echo "INTRADAY SCAN — $(date '+%Y-%m-%d %H:%M:%S UTC')" >> "$LOG"
echo "======================================" >> "$LOG"

cd "$WORKSPACE"

# Activate venv if available
if [ -f "projects/trading-bot/venv/bin/activate" ]; then
    source projects/trading-bot/venv/bin/activate
fi

# Step 1: Run scanner
echo "[1/2] Running OI scanner..." >> "$LOG"
python3 projects/trading-bot/nse_oialerts/oi_momentum_scanner.py intraday --top 5 --save >> "$LOG" 2>&1
SCAN_EXIT=$?

if [ $SCAN_EXIT -ne 0 ]; then
    echo "ERROR: Scanner failed with exit code $SCAN_EXIT" >> "$LOG"
    exit $SCAN_EXIT
fi

# Step 2: Send alert
echo "[2/2] Sending Telegram alert..." >> "$LOG"
python3 projects/trading-bot/nse_oialerts/send_alert.py intraday >> "$LOG" 2>&1

echo "DONE — $(date '+%Y-%m-%d %H:%M:%S UTC')" >> "$LOG"
