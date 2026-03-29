#!/bin/bash
# Scan script — runs on cron schedule (default every 5 min)
cd /root/.openclaw/workspace/projects/crypto-liquidity-hunter
source venv/bin/activate

# Rotate logs: keep last 96 log files (8 hours worth at 5-min intervals)
find logs/ -name 'cron_*.log' -type f | sort | head -n -96 | xargs rm -f 2>/dev/null

# Log filename uses YYYYMMDDHHMM for 5-min granularity
python main.py scan-all --capital 10000 --alert 2>&1 | tee -a logs/cron_$(date +\%Y\%m\%d\%H\%M).log
