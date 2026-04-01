#!/usr/bin/env python3
"""
Activity Logger — Structured JSON logging for all bot events.
Append-only JSON-lines file per day: logs/activity_YYYYMMDD.jsonl

Event types:
  SCAN_START / SCAN_END      — cron scan run boundaries
  SIGNAL_FOUND               — new signal detected
  PENDING_CREATED            — pending signal saved (waiting for price)
  PENDING_TRIGGERED          — pending signal price hit, entry placed
  PENDING_CANCELLED          — duplicate/open-trade block
  PENDING_EXPIRED            — signal expired without trigger
  ENTRY_PLACED               — order sent to Binance
  ENTRY_FILLED               — fill confirmed
  ENTRY_FAILED               — order rejected / partial batch
  SL_PLACED                  — stop-loss order live on exchange
  TP_PLACED                  — take-profit order live on exchange
  POSITION_OPEN              — position confirmed on exchange
  SL_HIT                     — stop-loss triggered
  TP_HIT                     — take-profit triggered
  CLOSED_ON_EXCHANGE         — position gone, reason unknown
  ENTRY_FAILED_NO_POSITION   — DB had open trade but no position ever found
  BATCH_PARTIAL              — batchOrders: entry filled but SL failed
  DUPLICATE_BLOCKED          — duplicate entry prevented
  ORDER_ERROR                — order placement error
  MONITOR_EVENT              — position monitor status/event
  REVIEW_SUMMARY             — 6h review report
"""

import json
import os
from datetime import datetime, timezone
from pathlib import Path

LOG_DIR = Path(__file__).parent.parent / 'logs'


def _today_file() -> Path:
    LOG_DIR.mkdir(exist_ok=True)
    day = datetime.now(timezone.utc).strftime('%Y%m%d')
    return LOG_DIR / f'activity_{day}.jsonl'


def log_event(event_type: str, **kwargs):
    """Write one structured event line."""
    record = {
        'ts':   datetime.now(timezone.utc).isoformat(),
        'type': event_type,
        **kwargs,
    }
    with open(_today_file(), 'a') as f:
        f.write(json.dumps(record, default=str) + '\n')


def read_today_events() -> list:
    p = _today_file()
    if not p.exists():
        return []
    events = []
    with open(p) as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    events.append(json.loads(line))
                except Exception:
                    pass
    return events


def read_events_last_n_hours(hours: int = 6) -> list:
    from datetime import timedelta
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    cutoff_iso = cutoff.isoformat()
    events = read_today_events()
    # Also pull yesterday if hours spans midnight
    yesterday = LOG_DIR / f'activity_{(datetime.now(timezone.utc).date() - __import__("datetime").timedelta(days=1)).strftime("%Y%m%d")}.jsonl'
    if yesterday.exists():
        with open(yesterday) as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        events.append(json.loads(line))
                    except Exception:
                        pass
    return [e for e in events if e.get('ts', '') >= cutoff_iso]
