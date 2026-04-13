"""
Trade Validator — Post-entry verification + Telegram alerts.

Runs AFTER every trade entry. Checks Binance state against DB and sends
instant Telegram notification with full details or error alert.

Also validates every position close from the monitor.
"""
import logging
import time
import hmac
import hashlib
import requests
import yaml
from pathlib import Path
from typing import Dict, Optional

logger = logging.getLogger(__name__)

CONFIG_PATH = Path("/root/.openclaw/workspace/projects/crypto-liquidity-hunter/config/pairs.yaml")


def _load_telegram_config() -> Dict:
    try:
        with open(CONFIG_PATH) as f:
            cfg = yaml.safe_load(f)
        tg = cfg.get('alerts', {}).get('telegram', {})
        if tg.get('enabled') and tg.get('bot_token') and tg.get('chat_id'):
            return tg
    except Exception:
        pass
    return {}


def _send_telegram(text: str, tg_cfg: Dict = None) -> bool:
    """Send a message via Telegram bot."""
    if not tg_cfg:
        tg_cfg = _load_telegram_config()
    if not tg_cfg:
        logger.warning('[Validator] Telegram not configured — skipping alert')
        return False
    try:
        url = f"https://api.telegram.org/bot{tg_cfg['bot_token']}/sendMessage"
        r = requests.post(url, json={
            'chat_id': tg_cfg['chat_id'],
            'text': text,
            'parse_mode': 'HTML',
            'disable_web_page_preview': True,
        }, timeout=10)
        return r.status_code == 200
    except Exception as e:
        logger.error(f'[Validator] Telegram send error: {e}')
        return False


def _sign(secret: str, params: str) -> str:
    ts = int(time.time() * 1000)
    full = params + f'&timestamp={ts}&recvWindow=15000'
    sig = hmac.new(secret.encode(), full.encode(), hashlib.sha256).hexdigest()
    return full + '&signature=' + sig


def _get_binance_position(api_key: str, api_secret: str, raw_sym: str) -> Optional[Dict]:
    try:
        par = _sign(api_secret, f'symbol={raw_sym}')
        r = requests.get(f'https://fapi.binance.com/fapi/v2/positionRisk?{par}',
                         headers={'X-MBX-APIKEY': api_key}, timeout=8)
        for p in r.json():
            if float(p.get('positionAmt', 0)) != 0:
                return p
    except Exception:
        pass
    return None


def _get_binance_orders(api_key: str, api_secret: str, raw_sym: str) -> list:
    try:
        par = _sign(api_secret, f'symbol={raw_sym}')
        r = requests.get(f'https://fapi.binance.com/fapi/v1/openOrders?{par}',
                         headers={'X-MBX-APIKEY': api_key}, timeout=8)
        return r.json() if isinstance(r.json(), list) else []
    except Exception:
        return []


# ─── Entry Validator ──────────────────────────────────────────────────────────

def validate_entry(symbol: str, direction: str, entry_price: float,
                   sl: float, tp: float, qty: float, notional: float,
                   trade_id: int, sl_tp_mode: str,
                   api_key: str, api_secret: str) -> bool:
    """
    Validate a trade AFTER entry. Checks Binance position matches DB.
    Sends Telegram alert with result.
    Returns True if everything checks out, False if there's a problem.
    """
    tg_cfg = _load_telegram_config()
    raw_sym = symbol.replace('/', '')
    problems = []

    # 1. Check position exists
    pos = _get_binance_position(api_key, api_secret, raw_sym)
    if not pos:
        msg = (f"🚨 <b>PROBLEM: {symbol}</b>\n"
               f"DB says {direction.upper()} entered @ {entry_price}\n"
               f"<b>But NO position found on Binance!</b>\n"
               f"Trade #{trade_id} — manual review needed")
        _send_telegram(msg, tg_cfg)
        return False

    actual_qty = abs(float(pos['positionAmt']))
    actual_dir = 'short' if float(pos['positionAmt']) < 0 else 'long'
    actual_entry = float(pos['entryPrice'])

    # 2. Check direction matches
    if actual_dir != direction:
        msg = (f"🚨 <b>DIRECTION MISMATCH: {symbol}</b>\n"
               f"DB: <b>{direction.upper()}</b>\n"
               f"Binance: <b>{actual_dir.upper()}</b>\n"
               f"Qty: {actual_qty} | Entry: {actual_entry}\n"
               f"Trade #{trade_id} — <b>BOT PAUSED, close manually!</b>")
        _send_telegram(msg, tg_cfg)
        return False

    # 3. Check SL/TP orders (only for binance_bracket mode)
    orders = _get_binance_orders(api_key, api_secret, raw_sym)
    bracket_status = ""
    if sl_tp_mode == 'binance_bracket':
        if len(orders) >= 2:
            bracket_status = f"✅ {len(orders)} bracket orders on Binance"
        elif len(orders) == 1:
            bracket_status = f"⚠️ Only 1 order on Binance (expected 2)"
            problems.append("missing_bracket_order")
        else:
            bracket_status = f"⚠️ No bracket orders on Binance"
            problems.append("no_bracket_orders")
    else:
        bracket_status = "👁️ Monitor mode — bot watching SL/TP"

    # 4. Check qty is reasonable (not 10x oversized)
    expected_qty_approx = notional / entry_price if entry_price > 0 else 0
    if actual_qty > expected_qty_approx * 3:
        problems.append(f"qty_oversized: actual={actual_qty} expected≈{expected_qty_approx:.1f}")

    # 5. Build success or warning message
    dir_emoji = "🟢" if direction == 'long' else "🔴"
    sl_pct = abs(sl - entry_price) / entry_price * 100 if entry_price > 0 and sl > 0 else 0
    tp_pct = abs(tp - entry_price) / entry_price * 100 if entry_price > 0 and tp > 0 else 0
    rr = tp_pct / sl_pct if sl_pct > 0 else 0

    if problems:
        status_line = "⚠️ <b>WARNINGS:</b> " + ", ".join(problems)
    else:
        status_line = "✅ <b>All checks passed</b>"

    msg = (
        f"{dir_emoji} <b>TRADE ENTERED: {symbol}</b>\n"
        f"Direction: <b>{direction.upper()}</b>\n"
        f"Entry: <b>{entry_price:.6g}</b>\n"
        f"SL: <b>{sl:.6g}</b> ({sl_pct:.1f}%)\n"
        f"TP: <b>{tp:.6g}</b> ({tp_pct:.1f}%) | R:R {rr:.1f}x\n"
        f"Qty: <b>{actual_qty}</b> | Notional: ${notional:.0f}\n"
        f"Brackets: {bracket_status}\n"
        f"Trade #{trade_id}\n"
        f"\n{status_line}"
    )

    _send_telegram(msg, tg_cfg)
    return len(problems) == 0


# ─── Close Validator ──────────────────────────────────────────────────────────

def validate_close(symbol: str, direction: str, entry_price: float,
                   exit_price: float, pnl: float, status: str,
                   trade_id: int, api_key: str = None, api_secret: str = None):
    """
    Validate a trade AFTER close. Sends Telegram notification.
    """
    tg_cfg = _load_telegram_config()

    pnl_emoji = "💰" if pnl > 0 else "💸"
    status_emoji = {
        'target_hit': '🎯',
        'stop_loss': '🛑',
        'closed_on_exchange': '📤',
        'entry_failed': '❌',
        'manual': '✋',
    }.get(status, '📋')

    pnl_pct = (pnl / (50.0)) * 100 if pnl else 0  # approx % of typical notional

    msg = (
        f"{status_emoji} <b>TRADE CLOSED: {symbol}</b>\n"
        f"Direction: <b>{direction.upper()}</b>\n"
        f"Entry: {entry_price:.6g} → Exit: {exit_price:.6g}\n"
        f"P&L: {pnl_emoji} <b>${pnl:+.2f}</b> ({pnl_pct:+.1f}%)\n"
        f"Reason: <b>{status.replace('_', ' ').title()}</b>\n"
        f"Trade #{trade_id}"
    )

    # If close was unexpected, verify position is actually gone
    if api_key and api_secret:
        raw_sym = symbol.replace('/', '')
        pos = _get_binance_position(api_key, api_secret, raw_sym)
        if pos:
            remaining = abs(float(pos['positionAmt']))
            if remaining > 0:
                msg += f"\n\n⚠️ <b>DUST REMAINING:</b> {remaining} contracts still on Binance!"

    _send_telegram(msg, tg_cfg)


# ─── Problem Alert ────────────────────────────────────────────────────────────

def alert_problem(symbol: str, problem: str, details: str = ""):
    """Send an immediate problem alert via Telegram."""
    tg_cfg = _load_telegram_config()
    msg = f"🚨 <b>BOT ALERT: {symbol}</b>\n{problem}"
    if details:
        msg += f"\n<pre>{details[:500]}</pre>"
    _send_telegram(msg, tg_cfg)
