#!/usr/bin/env python3
"""
6-Hour Review Bot — reads activity logs, computes stats, sends Telegram report.
Run via cron every 6 hours.

Usage: python scheduler/review_and_alert.py
"""

import sys
import json
import os
import sqlite3
import requests
from datetime import datetime, timezone, timedelta
from pathlib import Path
from collections import defaultdict

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from scheduler.activity_logger import read_events_last_n_hours

# ── Config ──────────────────────────────────────────────────────────────────
import yaml
with open(PROJECT_ROOT / 'config' / 'pairs.yaml') as f:
    config = yaml.safe_load(f)

BOT_TOKEN = config['alerts']['telegram']['bot_token']
CHAT_ID   = config['alerts']['telegram']['chat_id']
DB_PATH   = PROJECT_ROOT / 'data' / 'store.db'

def send_telegram(text: str):
    try:
        requests.post(
            f'https://api.telegram.org/bot{BOT_TOKEN}/sendMessage',
            json={'chat_id': CHAT_ID, 'text': text, 'parse_mode': 'HTML'},
            timeout=10
        )
    except Exception as e:
        print(f'Telegram send error: {e}')


def get_db_stats():
    """Pull live trade stats from DB."""
    stats = {}
    try:
        with sqlite3.connect(DB_PATH) as conn:
            conn.row_factory = sqlite3.Row
            # Open trades
            open_trades = conn.execute(
                "SELECT * FROM trades WHERE status='open' AND mode='live'"
            ).fetchall()
            stats['open_trades'] = [dict(r) for r in open_trades]

            # Trades closed in last 6h
            cutoff = (datetime.now(timezone.utc) - timedelta(hours=6)).isoformat()
            closed = conn.execute("""
                SELECT id, pair, direction, entry_price, exit_price, sl, tp,
                       status, pnl_usd, entry_time, exit_time
                FROM trades
                WHERE status != 'open' AND mode='live'
                  AND exit_time >= ?
                ORDER BY exit_time DESC
            """, (cutoff,)).fetchall()
            stats['closed_trades'] = [dict(r) for r in closed]

            # All-time live stats
            totals = conn.execute("""
                SELECT
                    COUNT(*) as total,
                    SUM(CASE WHEN status='target_hit' THEN 1 ELSE 0 END) as wins,
                    SUM(CASE WHEN status='stop_loss' THEN 1 ELSE 0 END) as losses,
                    SUM(CASE WHEN status='closed_on_exchange' THEN 1 ELSE 0 END) as exchange_closed,
                    SUM(CASE WHEN status='entry_failed' THEN 1 ELSE 0 END) as entry_failed,
                    ROUND(SUM(COALESCE(pnl_usd, 0)), 2) as total_pnl
                FROM trades
                WHERE mode='live' AND status != 'open'
            """).fetchone()
            stats['totals'] = dict(totals) if totals else {}

            # Pending signals
            pending = conn.execute(
                "SELECT COUNT(*) FROM pending_signals WHERE status='pending'"
            ).fetchone()
            stats['pending_count'] = pending[0] if pending else 0

    except Exception as e:
        stats['db_error'] = str(e)
    return stats


def build_report(events: list, db: dict) -> str:
    """Build HTML Telegram report."""
    now_str = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')

    # Count events by type
    counts = defaultdict(int)
    errors = []
    duplicates_blocked = 0
    batch_partials = 0
    entries = []
    closes = []

    for e in events:
        t = e.get('type', '')
        counts[t] += 1
        if t == 'ORDER_ERROR':
            errors.append(e)
        if t == 'DUPLICATE_BLOCKED':
            duplicates_blocked += 1
        if t == 'BATCH_PARTIAL':
            batch_partials += 1
        if t == 'ENTRY_FILLED':
            entries.append(e)
        if t in ('SL_HIT', 'TP_HIT', 'CLOSED_ON_EXCHANGE'):
            closes.append(e)

    # DB stats
    totals      = db.get('totals', {})
    open_trades = db.get('open_trades', [])
    closed_6h   = db.get('closed_trades', [])
    pending_cnt = db.get('pending_count', 0)

    total_closed = totals.get('total', 0)
    wins         = totals.get('wins', 0)
    losses       = totals.get('losses', 0)
    total_pnl    = totals.get('total_pnl', 0.0)
    win_rate     = (wins / total_closed * 100) if total_closed > 0 else 0

    # Build message
    lines = [
        f'🤖 <b>6-Hour Bot Review</b>  [{now_str}]',
        '',
        '📊 <b>ALL-TIME LIVE STATS</b>',
        f'  Trades: {total_closed} | Wins: {wins} | Losses: {losses}',
        f'  Win Rate: {win_rate:.1f}%',
        f'  Total P&amp;L: <b>${(total_pnl or 0):+.2f}</b>',
        '',
        '🕐 <b>LAST 6 HOURS</b>',
        f'  Scans run: {counts.get("SCAN_END", 0)}',
        f'  Signals found: {counts.get("SIGNAL_FOUND", 0)}',
        f'  Entries placed: {counts.get("ENTRY_PLACED", 0)}',
        f'  Entries filled: {counts.get("ENTRY_FILLED", 0)}',
        f'  Closes: {len(closes)}',
        f'  Duplicates blocked: {duplicates_blocked}',
        f'  Batch partial (⚠️): {batch_partials}',
        f'  Errors: {len(errors)}',
    ]

    # Open positions
    if open_trades:
        lines += ['', '📂 <b>OPEN POSITIONS</b>']
        for t in open_trades:
            pair = t['pair'].replace('binance:', '')
            ep   = t.get('entry_price', 0)
            sl   = t.get('sl', 0)
            tp   = t.get('tp', 0)
            dir_ = '🔴 SHORT' if t['direction'] == 'short' else '🟢 LONG'
            lines.append(f'  {dir_} {pair}  ep={ep:.5g}  sl={sl:.5g}  tp={tp:.5g}')
    else:
        lines.append('')
        lines.append('📂 <b>OPEN POSITIONS:</b> None')

    # Recent closes
    if closed_6h:
        lines += ['', '✅ <b>CLOSED (last 6h)</b>']
        for t in closed_6h[:5]:
            pair   = t['pair'].replace('binance:', '')
            pnl    = t.get('pnl_usd') or 0
            status = t.get('status', '?')
            icon   = '✅' if pnl > 0 else '❌'
            lines.append(f'  {icon} {pair} {status} P&amp;L: ${pnl:+.2f}')

    # Errors
    if errors:
        lines += ['', '⚠️ <b>ERRORS (last 6h)</b>']
        for e in errors[:3]:
            msg = str(e.get('error', e.get('msg', '?')))[:80]
            lines.append(f'  ❗ {e.get("pair","?")} — {msg}')

    # Pending
    lines += ['', f'⏳ Pending signals waiting: {pending_cnt}']

    # Issues/improvements
    issues = []
    if batch_partials > 0:
        issues.append(f'⚠️ {batch_partials} batch partial(s) — entry filled but SL failed separately')
    if len(errors) > 3:
        issues.append(f'⚠️ High error rate: {len(errors)} errors in 6h')
    if duplicates_blocked > 5:
        issues.append(f'ℹ️ {duplicates_blocked} duplicate entries blocked (working correctly)')
    win_rate_threshold = 40
    if total_closed >= 5 and win_rate < win_rate_threshold:
        issues.append(f'📉 Win rate {win_rate:.0f}% below {win_rate_threshold}% — consider tightening signal filters')

    if issues:
        lines += ['', '🔍 <b>ATTENTION</b>']
        for iss in issues:
            lines.append(f'  {iss}')

    lines.append('')
    lines.append('—')

    return '\n'.join(lines)


def main():
    print(f'[{datetime.now(timezone.utc).isoformat()}] Running 6h review...')
    events = read_events_last_n_hours(hours=6)
    db     = get_db_stats()
    report = build_report(events, db)
    send_telegram(report)
    print(f'Report sent. Events processed: {len(events)}')
    print(report)

    # Also log the review event itself
    try:
        from scheduler.activity_logger import log_event
        log_event('REVIEW_SUMMARY',
                  events_processed=len(events),
                  open_trades=len(db.get('open_trades', [])),
                  closed_6h=len(db.get('closed_trades', [])),
                  total_pnl=db.get('totals', {}).get('total_pnl', 0))
    except Exception:
        pass


if __name__ == '__main__':
    main()
