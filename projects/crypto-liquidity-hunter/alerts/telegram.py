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


def _last_trade_resolved(pair: str, direction: str, entry_price: float, tolerance: float = 0.005) -> bool:
    """Return True if the most recent CLOSED trade for same pair+direction+~entry already
    hit TP or SL AND no new open trade with a different entry has been created since.
    This prevents re-alerting on the same price level that was already resolved.
    """
    try:
        with sqlite3.connect(DB_PATH) as conn:
            # Most recent closed trade at approximately this entry price
            rows = conn.execute(
                """SELECT entry_price, status FROM trades
                   WHERE pair=? AND direction=? AND status!='open'
                   ORDER BY exit_time DESC LIMIT 10""",
                (pair, direction)
            ).fetchall()
        for (ep, status) in rows:
            if abs(ep - entry_price) / max(entry_price, 1e-9) < tolerance:
                if status in ('target_hit', 'stop_loss'):
                    # This exact price level was already resolved — skip
                    return True
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

        # Phase 2: confluence badges
        htf_bias    = getattr(signal, 'htf_bias', 'neutral')
        ob_conf     = getattr(signal, 'ob_confluence', False)
        fvg_conf    = getattr(signal, 'fvg_confluence', False)

        htf_emoji = {'bullish': '📈', 'bearish': '📉', 'neutral': '➖'}.get(htf_bias, '➖')
        ob_badge  = '✅ OB' if ob_conf  else '—'
        fvg_badge = '✅ FVG' if fvg_conf else '—'

        # Confluence line (only show if at least one active)
        confluence_line = ''
        if ob_conf or fvg_conf:
            badges = ' + '.join(filter(lambda x: x != '—', [ob_badge, fvg_badge]))
            confluence_line = f"<b>Confluence:</b>  {badges}\n"

        return (
            f"<b>🎯 LIQUIDITY SIGNAL  {direction_emoji}</b>\n"
            f"{'─' * 28}\n"
            f"<b>Pair:</b>       {pair_clean}\n"
            f"<b>Timeframe:</b>  {getattr(signal, 'timeframe', '—')}\n"
            f"<b>Entry:</b>      ${_fmt(signal.entry_price)}\n"
            f"<b>Stop Loss:</b>  ${_fmt(signal.stop_loss)}\n"
            f"<b>Target:</b>     ${_fmt(signal.target)}\n"
            f"<b>R:R:</b>        {signal.risk_reward:.2f}\n"
            f"<b>Notional:</b>   ${signal.notional_usd:.0f} (${signal.margin_required_usd:.0f} margin)\n"
            f"<b>Confidence:</b> {conf_bar} {conf_pct}%\n"
            f"<b>HTF Bias:</b>   {htf_emoji} {htf_bias.upper()}\n"
            f"{confluence_line}"
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

        # 3. Skip if this exact price level was already resolved (TP or SL)
        if _last_trade_resolved(signal.pair, signal.direction, signal.entry_price):
            return False, "this entry level already resolved (TP/SL hit) — waiting for fresh sweep"

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

    def send_scan_summary(self, summary: Dict) -> bool:
        """Send a scan summary message after every scan-all run."""
        if not self.telegram:
            return False
        try:
            from datetime import datetime, timezone, timedelta
            IST = timezone(timedelta(hours=5, minutes=30))
            now_ist = datetime.now(IST).strftime('%d %b %Y  %H:%M IST')

            pairs_scanned  = summary.get('pairs_scanned', 0)
            tfs_scanned    = summary.get('timeframes', [])
            signals_count  = summary.get('signals_count', 0)
            pending_count  = summary.get('pending_count', 0)
            open_trades    = summary.get('open_trades', 0)
            sweeps_found   = summary.get('sweeps_found', 0)
            top_signals    = summary.get('top_signals', [])   # list of dicts
            open_pnl       = summary.get('open_pnl_usd', 0.0)
            active_account = summary.get('active_account', '—')
            scan_duration  = summary.get('duration_sec', 0)

            # Signal section
            if signals_count > 0:
                sig_header = f"🎯 <b>{signals_count} NEW SIGNAL{'S' if signals_count>1 else ''} — check dashboard!</b>\n"
                sig_lines  = ''
                for s in top_signals[:3]:
                    direction_emoji = '🟢' if s.get('direction') == 'long' else '🔴'
                    sig_lines += (f"  {direction_emoji} <b>{s.get('pair','').replace('binance:','')}</b> "
                                  f"{s.get('timeframe','')} | Entry ${_fmt(float(s.get('entry_price',0)))} "
                                  f"| R:R {s.get('risk_reward','?')} | Conf {int(float(s.get('confidence',0))*100)}%\n")
            else:
                sig_header = '😴 <b>No new signals this scan</b> — market waiting for sweep\n'
                sig_lines  = ''

            # Pending orders line
            pending_line = f"⏳ Pending orders: <b>{pending_count}</b>\n" if pending_count > 0 else ''

            # Open trades PnL
            if open_trades > 0:
                pnl_sign  = '+' if open_pnl >= 0 else ''
                pnl_color = '📈' if open_pnl >= 0 else '📉'
                trade_line = f"{pnl_color} Open trades: <b>{open_trades}</b>  Unrealized PnL: <b>{pnl_sign}${open_pnl:.2f}</b>\n"
            else:
                trade_line = '📊 No open trades\n'

            # Sweep activity
            sweep_line = f"🌊 Sweeps confirmed: <b>{sweeps_found}</b>\n" if sweeps_found > 0 else '🌊 No sweeps confirmed this scan\n'

            msg = (
                f"<b>📡 LIQUIDITY HUNTER — SCAN REPORT</b>\n"
                f"{'─' * 30}\n"
                f"🕐 {now_ist}\n"
                f"🔍 Pairs: <b>{pairs_scanned}</b>  |  TF: <b>{', '.join(tfs_scanned)}</b>  |  ⏱ {scan_duration}s\n"
                f"👤 Account: <b>{active_account}</b>\n"
                f"{'─' * 30}\n"
                f"{sweep_line}"
                f"{sig_header}"
                f"{sig_lines}"
                f"{pending_line}"
                f"{'─' * 30}\n"
                f"{trade_line}"
                f"<i>Dashboard → http://76.13.247.112:5000</i>"
            )
            return self.telegram.send_message(msg)
        except Exception as e:
            logger.error(f"send_scan_summary error: {e}")
            return False
