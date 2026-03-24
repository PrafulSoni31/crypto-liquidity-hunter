"""
Trade Alerts: Dispatch notifications (Telegram).

Dedup rules (NEW):
  1. Skip if an OPEN trade already exists for same pair+direction+entry (within 0.5%)
  2. Skip if last closed trade for same pair+direction already hit TP or SL
     (don't re-alert until a fresh sweep entry is found)
Configurable confidence threshold via config['alerts']['telegram']['min_confidence'].
"""
import os
import requests
import logging
import sqlite3
from pathlib import Path
from typing import Dict, Optional
from dataclasses import asdict

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).parent.parent
DB_PATH      = PROJECT_ROOT / 'data' / 'store.db'


# ─── Dedup helpers ─────────────────────────────────────────────────────────────
def _has_open_trade(pair: str, direction: str, entry_price: float, tolerance: float = 0.005) -> bool:
    """Return True if an open trade for same pair+direction+~entry already exists."""
    try:
        with sqlite3.connect(DB_PATH) as conn:
            rows = conn.execute(
                "SELECT entry_price FROM trades WHERE pair=? AND direction=? AND status='open'",
                (pair, direction)
            ).fetchall()
        for (ep,) in rows:
            if abs(ep - entry_price) / max(entry_price, 1e-9) < tolerance:
                return True
    except Exception as e:
        logger.debug(f"Dedup check error: {e}")
    return False


def _last_trade_resolved(pair: str, direction: str) -> bool:
    """Return True if the most recent CLOSED trade for pair+direction already hit TP or SL.
    This means we already fired an alert for this setup — skip until a fresh sweep comes in.
    """
    try:
        with sqlite3.connect(DB_PATH) as conn:
            row = conn.execute(
                """SELECT status FROM trades
                   WHERE pair=? AND direction=? AND status!='open'
                   ORDER BY exit_time DESC LIMIT 1""",
                (pair, direction)
            ).fetchone()
        if row and row[0] in ('target_hit', 'stop_loss'):
            # Check whether a NEW open trade has been created since then
            with sqlite3.connect(DB_PATH) as conn:
                new = conn.execute(
                    "SELECT id FROM trades WHERE pair=? AND direction=? AND status='open' LIMIT 1",
                    (pair, direction)
                ).fetchone()
            return new is None   # True = no new open trade → skip alert
    except Exception as e:
        logger.debug(f"Resolved check error: {e}")
    return False


# ─── Formatter helpers ──────────────────────────────────────────────────────────
def _fmt(price: float) -> str:
    if price < 0.001:  return f"{price:.8f}"
    if price < 1:      return f"{price:.6f}"
    if price < 10:     return f"{price:.4f}"
    return f"{price:.2f}"


class TelegramAlerter:
    def __init__(self, bot_token: str, chat_id: str):
        self.bot_token = bot_token
        self.chat_id   = chat_id
        self.base_url  = f"https://api.telegram.org/bot{bot_token}"

    def send_message(self, text: str, parse_mode: str = 'HTML') -> bool:
        url = f"{self.base_url}/sendMessage"
        payload = {
            'chat_id': self.chat_id,
            'text': text,
            'parse_mode': parse_mode,
            'disable_web_page_preview': True
        }
        try:
            resp = requests.post(url, json=payload, timeout=10)
            if resp.status_code == 200:
                return True
            # Fallback plain
            payload['parse_mode'] = ''
            resp2 = requests.post(url, json=payload, timeout=10)
            if resp2.status_code != 200:
                logger.error(f"Telegram send failed: {resp.text}")
            return resp2.status_code == 200
        except Exception as e:
            logger.error(f"Telegram exception: {e}")
            return False

    def format_signal_alert(self, signal) -> str:
        direction_emoji = '🟢 LONG' if signal.direction == 'long' else '🔴 SHORT'
        pair_clean = signal.pair.replace('binance:', '')
        conf_pct   = int(signal.confidence * 100)
        conf_bar   = '█' * (conf_pct // 10) + '░' * (10 - conf_pct // 10)

        return (
            f"<b>🎯 LIQUIDITY SIGNAL  {direction_emoji}</b>\n"
            f"{'─' * 28}\n"
            f"<b>Pair:</b>       {pair_clean}\n"
            f"<b>Entry:</b>      ${_fmt(signal.entry_price)}\n"
            f"<b>Stop Loss:</b>  ${_fmt(signal.stop_loss)}\n"
            f"<b>Target:</b>     ${_fmt(signal.target)}\n"
            f"<b>R:R:</b>        {signal.risk_reward:.2f}\n"
            f"<b>Notional:</b>   ${signal.notional_usd:.0f} (${signal.margin_required_usd:.0f} margin)\n"
            f"<b>Confidence:</b> {conf_bar} {conf_pct}%\n"
            f"{'─' * 28}\n"
            f"<i>⚠️ Paper trading only. Manage risk.</i>"
        )

    def format_sweep_alert(self, sweep: Dict) -> str:
        d_emoji = '🔽' if sweep['direction'] == 'long' else '🔼'
        return (
            f"<b>🌊 SWEEP DETECTED {d_emoji}</b>\n"
            f"Direction: {sweep['direction'].upper()}\n"
            f"Price:     ${_fmt(sweep['sweep_price'])}\n"
            f"Vol ratio: {sweep.get('volume_ratio',0):.1f}×\n"
            f"Depth:     {abs(sweep.get('sweep_depth_pct',0)):.2f}%"
        )


class AlertDispatcher:
    def __init__(self, config: Dict):
        self.telegram       = None
        self.min_confidence = config.get('telegram', {}).get('min_confidence', 0.0)
        if config.get('telegram', {}).get('enabled'):
            self.telegram = TelegramAlerter(
                bot_token=config['telegram']['bot_token'],
                chat_id=config['telegram']['chat_id']
            )

    def should_send(self, signal) -> tuple:
        """Return (send: bool, reason: str)."""
        # 1. Confidence filter
        if signal.confidence < self.min_confidence:
            return False, f"conf {signal.confidence:.2f} < threshold {self.min_confidence:.2f}"

        # 2. Duplicate open trade check
        if _has_open_trade(signal.pair, signal.direction, signal.entry_price):
            return False, "open trade already exists for this signal"

        # 3. Skip if last trade for this setup was already resolved (TP or SL)
        if _last_trade_resolved(signal.pair, signal.direction):
            return False, "last trade for pair+direction already resolved — waiting for fresh sweep"

        return True, "ok"

    def send_signal(self, signal) -> bool:
        if not self.telegram:
            return False
        send, reason = self.should_send(signal)
        if not send:
            logger.info(f"Alert suppressed ({signal.pair} {signal.direction}): {reason}")
            return False
        text = self.telegram.format_signal_alert(signal)
        sent = self.telegram.send_message(text)
        if sent:
            logger.info(f"Alert sent: {signal.pair} {signal.direction} RR={signal.risk_reward:.2f}")
        return sent

    def send_sweep(self, sweep: Dict):
        if self.telegram:
            self.telegram.send_message(self.telegram.format_sweep_alert(sweep))

    def send_backtest(self, metrics: Dict):
        if self.telegram:
            txt = f"<b>📊 BACKTEST</b>\nWin Rate: {metrics.get('win_rate')}%\nSharpe: {metrics.get('sharpe')}\nMDD: {metrics.get('max_drawdown_pct')}%"
            self.telegram.send_message(txt)
