#!/bin/bash
# Weekly auto-cleanup for crypto-liquidity-hunter bot
# Runs every Sunday 03:00 UTC — safe to delete, keeps what matters

PROJECT="/root/.openclaw/workspace/projects/crypto-liquidity-hunter"
cd "$PROJECT" || exit 1

echo "[cleanup] Starting $(date -u)"

# 1. Truncate large log files — keep last 2000 lines each
for f in logs/monitor_daemon.log logs/admin_bot.log logs/manual_scan.log logs/review.log; do
    [ -f "$f" ] && tail -2000 "$f" > /tmp/_log_tmp && mv /tmp/_log_tmp "$f" && echo "[cleanup] Truncated $f"
done

# 2. Cron scan logs — keep newest 288 (1 day at 5-min intervals)
ls -t logs/cron_*.log 2>/dev/null | tail -n +289 | xargs rm -f
echo "[cleanup] Cron logs: $(ls logs/cron_*.log 2>/dev/null | wc -l) kept"

# 3. Activity JSONL — keep last 14 days
ls -t logs/activity_*.jsonl 2>/dev/null | tail -n +15 | xargs rm -f
echo "[cleanup] Activity logs: $(ls logs/activity_*.jsonl 2>/dev/null | wc -l) kept"

# 4. NSE OI cache & alerts — older than 14 days
find "$PROJECT/projects/trading-bot/nse_oialerts/cache/" -name "*.json" -mtime +14 -delete 2>/dev/null
find "$PROJECT/projects/trading-bot/nse_oialerts/alerts_sent/" -name "*.txt" -mtime +14 -delete 2>/dev/null
find "$PROJECT/projects/trading-bot/nse_oialerts/logs/" -name "*.log" -mtime +14 -delete 2>/dev/null
echo "[cleanup] NSE OI cache cleaned"

# 5. Python __pycache__ (auto-regenerated, never needed)
find "$PROJECT" -name "__pycache__" -not -path "*/venv/*" -type d -exec rm -rf {} + 2>/dev/null
echo "[cleanup] __pycache__ cleared"

# 6. cron_skip.log — keep last 200 lines
[ -f logs/cron_skip.log ] && tail -200 logs/cron_skip.log > /tmp/_skip && mv /tmp/_skip logs/cron_skip.log

echo "[cleanup] Done. Disk: $(df -h / | tail -1 | awk '{print $3" used, "$4" free"}')"
