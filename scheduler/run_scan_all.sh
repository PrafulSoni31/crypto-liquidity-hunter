#!/bin/bash
# Scan script — runs on cron schedule (default every 5 min)
cd /root/.openclaw/workspace/projects/crypto-liquidity-hunter
source venv/bin/activate

# ── LOCK: Prevent multiple simultaneous scans ─────────────────────────────
# If a previous scan is still running, skip this one entirely.
LOCKFILE=/tmp/clh_scan.lock
exec 9>"$LOCKFILE"
if ! flock -n 9; then
    echo "[$(date -u +%H:%M:%S)] Another scan already running — skipping this cycle" >> logs/cron_skip.log
    exit 0
fi

# Rotate logs: keep last 96 log files (8 hours worth at 5-min intervals)
find logs/ -name 'cron_*.log' -type f | sort | head -n -96 | xargs rm -f 2>/dev/null

# Log filename uses YYYYMMDDHHMM for 5-min granularity
python main.py scan-all --capital 10000 --alert 2>&1 | tee -a logs/cron_$(date +%Y%m%d%H%M).log
