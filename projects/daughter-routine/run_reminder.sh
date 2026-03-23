#!/bin/bash
# Reminder runner — called every 15 mins by cron (Mon–Fri)
# Level 1 → child nudge (15 min overdue)
# Level 2 → parent alert (30 min overdue)

cd /root/.openclaw/workspace/projects/daughter-routine
source venv/bin/activate

LOG="logs/reminders_$(date +%Y%m%d).log"
mkdir -p logs

echo "--- $(date '+%Y-%m-%d %H:%M:%S UTC') ---" >> "$LOG"
python3 reminder.py >> "$LOG" 2>&1
echo "Exit: $?" >> "$LOG"
