#!/usr/bin/env python3
"""
Activity Logger — logs all bot activity to structured JSON log.
Written to: logs/activity_YYYYMMDD.log (one file per day, JSON lines)
Reviewed every 6h via cron → review_and_alert.py
"""
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

LOG_DIR = Path(__file__).parent.parent / 'logs'
LOG_DIR.mkdir(exist_ok=True)

def log_event(event_type: str, data: dict):
    """
    Write a structured event to today's activity log.
    event_type: 'SCAN' | 'SIGNAL' | 'ENTRY' | 'EXIT' | 'SL_HIT' | 'TP_HIT' | 
                'PENDING_CREATED' | 'PENDING_TRIGGERED' | 'PENDING_CANCELLED' |
                'DUPLICATE_BLOCKED' | 'ORDER_ERROR' | 'MONITOR_CLOSE' | 'BATCH_PARTIAL'
    """
    today = datetime.now(timezone.utc).strftime('%Y%m%d')
    log_file = LOG_DIR / f'activity_{today}.log'
    record = {
        'ts': datetime.now(timezone.utc).isoformat(),
        'type': event_type,
        **data
    }
    with open(log_file, 'a') as f:
        f.write(json.dumps(record) + '\n')

