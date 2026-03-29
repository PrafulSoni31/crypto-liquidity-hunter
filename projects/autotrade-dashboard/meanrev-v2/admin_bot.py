#!/usr/bin/env python3
"""
MeanRev AutoTrader v2 — Telegram Admin Bot
Sync all parameters between Telegram and the Dashboard.
All settings saved to server → auto-loaded by dashboard on next refresh.

Usage:
  TELEGRAM_TOKEN=xxx ADMIN_ID=123456 DASHBOARD_URL=http://localhost:3000 python3 admin_bot.py

Environment variables:
  TELEGRAM_TOKEN  — bot token from BotFather (required)
  ADMIN_ID        — your Telegram user ID (required)
  DASHBOARD_URL   — URL of the meanrev-v2 server (default: http://localhost:3000)
"""
import os, sys, json, asyncio, logging
import urllib.request, urllib.error
from datetime import datetime

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger('meanrev-bot')

try:
    from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
    from telegram.ext import (Application, CommandHandler, ContextTypes,
                               MessageHandler, filters)
except ImportError:
    print("ERROR: python-telegram-bot not installed.")
    print("Run: pip install python-telegram-bot")
    sys.exit(1)

# ── Config ──────────────────────────────────────────────────────────────────
TOKEN        = os.environ.get('TELEGRAM_TOKEN', '')
ADMIN_ID     = int(os.environ.get('ADMIN_ID', '0'))
DASHBOARD    = os.environ.get('DASHBOARD_URL', 'http://localhost:3000').rstrip('/')

if not TOKEN:
    print("ERROR: TELEGRAM_TOKEN environment variable required")
    sys.exit(1)
if not ADMIN_ID:
    print("ERROR: ADMIN_ID environment variable required")
    sys.exit(1)

# ── HTTP helpers ─────────────────────────────────────────────────────────────
def _api(method: str, path: str, body=None, timeout=10):
    url = DASHBOARD + path
    data = json.dumps(body).encode() if body else None
    headers = {'Content-Type': 'application/json', 'Accept': 'application/json'}
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        try:    return json.loads(e.read())
        except: return {'error': str(e)}
    except Exception as e:
        return {'error': str(e)}

def get_params():  return _api('GET',  '/api/params')
def get_state():   return _api('GET',  '/api/state')
def get_positions():return _api('GET', '/api/positions')
def get_trades():  return _api('GET',  '/api/trades?limit=10')

def set_param(param, value):
    return _api('POST', f'/api/params/{param}', {'value': value})

def trigger_scan():
    return _api('POST', '/api/scan_trigger')

# ── Auth decorator ────────────────────────────────────────────────────────────
def admin_only(fn):
    async def wrapper(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if update.effective_user.id != ADMIN_ID:
            await update.message.reply_text("⛔ Unauthorized.")
            return
        return await fn(update, ctx)
    wrapper.__name__ = fn.__name__
    return wrapper

def fmt_vol(v):
    if v >= 1e9:  return f'${v/1e9:.2f}B'
    if v >= 1e6:  return f'${v/1e6:.2f}M'
    if v >= 1e3:  return f'${v/1e3:.0f}K'
    return f'${v:.0f}'

def fmt_ts(ts):
    if not ts: return '—'
    try: return datetime.fromisoformat(ts.replace('Z','+00:00')).strftime('%d %b %Y %H:%M UTC')
    except: return str(ts)

# ── /start /help ─────────────────────────────────────────────────────────────
HELP_TEXT = """
🤖 *MeanRev AutoTrader v2 — Admin Bot*

*System:*
/status — Engine state (balance, positions, PnL)
/params — Show all current parameters
/positions — Open positions list
/trades — Recent closed trades
/scan — Trigger immediate market scan

*Auto-Trading:*
/auto\_on — Enable auto trading
/auto\_off — Disable auto trading
/set\_interval `<sec>` — Scan interval (60–1440s)
/set\_maxpos `<n>` — Max auto positions (1–20)
/set\_signal `<STRONG|ANY>` — Signal strength filter

*Strategy Detection:*
/set\_pump `<pct>` — Pump threshold % (5–100)
/set\_reversal `<pct>` — Reversal threshold % (1–25)
/set\_volume `<usdt>` — Min 24h volume in USDT

*Position Sizing & Risk:*
/set\_size `<usdt>` — Position size per trade (50–2000)
/set\_tp `<pct>` — Take-profit % (1–25)
/set\_sl `<pct>` — Stop-loss % (3–40)
/set\_trail `<pct>` — Trailing stop % (0–15, 0=off)
/set\_avg `<n>` — Max averaging levels (1–5)
/set\_maxpos\_cfg `<n>` — Engine max positions (1–10)

*DD Protection:*
/set\_daily\_loss `<usdt>` — Daily loss limit (0=off)
/set\_max\_dd `<pct>` — Max account drawdown % (0=off)

_Only admin can use these commands._
"""

@admin_only
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(HELP_TEXT, parse_mode='Markdown')

# ── /status ──────────────────────────────────────────────────────────────────
@admin_only
async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    s = get_state()
    if 'error' in s:
        await update.message.reply_text(f"❌ Dashboard unreachable: {s['error']}")
        return
    mode  = s.get('mode','?').upper()
    bal   = s.get('balance', 0)
    upnl  = s.get('unrealisedPnl', 0)
    rpnl  = s.get('realisedPnl', 0)
    pos   = s.get('positions', 0)
    wins  = s.get('wins', 0)
    losses= s.get('losses', 0)
    wr    = s.get('winRate', 0)
    dd    = s.get('maxDrawdownPct', 0)
    mode_emoji = '🔴' if mode == 'LIVE' else '📄'
    pnl_sign = '+' if upnl >= 0 else ''
    rpnl_sign= '+' if rpnl >= 0 else ''

    msg = (
        f"{mode_emoji} *MeanRev Engine — {mode} MODE*\n\n"
        f"💰 Balance: `${bal:,.2f}`\n"
        f"📈 Unrealised PnL: `{pnl_sign}${upnl:,.2f}`\n"
        f"✅ Realised PnL: `{rpnl_sign}${rpnl:,.2f}`\n"
        f"📊 Open Positions: `{pos}`\n"
        f"🏆 W/L: `{wins}W / {losses}L` (WR: `{wr}%`)\n"
        f"📉 Max Drawdown: `{dd:.2f}%`\n"
    )
    await update.message.reply_text(msg, parse_mode='Markdown')

# ── /params ──────────────────────────────────────────────────────────────────
@admin_only
async def cmd_params(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    p = get_params()
    if 'error' in p:
        await update.message.reply_text(f"❌ Could not fetch params: {p['error']}")
        return
    auto_icon  = '✅' if p.get('autoEnabled') else '❌'
    sig_icon   = '💪' if p.get('autoSignal') == 'STRONG' else '📶'
    trail_icon = f"{p.get('trailingStopPct',0)}%" if p.get('trailingStopPct',0) > 0 else '🚫 off'
    ddl_icon   = f"${p.get('maxDailyLoss',0):,}" if p.get('maxDailyLoss',0) > 0 else '🚫 off'
    mxdd_icon  = f"{p.get('maxAccountDD',0)}%" if p.get('maxAccountDD',0) > 0 else '🚫 off'
    saved      = fmt_ts(p.get('savedAt'))

    msg = (
        f"⚙️ *All Parameters — MeanRev v2*\n\n"
        f"*🔍 Strategy Detection:*\n"
        f"  Pump threshold:    `{p.get('pumpThreshold',20)}%`  (/set\\_pump)\n"
        f"  Reversal threshold:`{p.get('reversalThreshold',5)}%`  (/set\\_reversal)\n"
        f"  Min 24h volume:    `{fmt_vol(p.get('minVolume',5e5))}`  (/set\\_volume)\n\n"
        f"*📦 Position Management:*\n"
        f"  Size per trade:    `${p.get('positionSize',200)}`  (/set\\_size)\n"
        f"  Take-profit:       `{p.get('takeProfitPct',4)}%`  (/set\\_tp)\n"
        f"  Stop-loss:         `{p.get('stopLossPct',12)}%`  (/set\\_sl)\n"
        f"  Trailing stop:     `{trail_icon}`  (/set\\_trail)\n"
        f"  Max averaging:     `{p.get('maxAveraging',3)}×`  (/set\\_avg)\n"
        f"  Engine max pos:    `{p.get('maxPositions',3)}`  (/set\\_maxpos\\_cfg)\n\n"
        f"*🛡 DD Protection:*\n"
        f"  Daily loss limit:  `{ddl_icon}`  (/set\\_daily\\_loss)\n"
        f"  Max account DD:    `{mxdd_icon}`  (/set\\_max\\_dd)\n\n"
        f"*⚡ Auto-Trading:*\n"
        f"  Auto mode:         {auto_icon} `{'ON' if p.get('autoEnabled') else 'OFF'}`  (/auto\\_on / /auto\\_off)\n"
        f"  Scan interval:     `{p.get('autoInterval',300)}s`  (/set\\_interval)\n"
        f"  Max auto pos:      `{p.get('autoMaxPos',5)}`  (/set\\_maxpos)\n"
        f"  Signal filter:     {sig_icon} `{p.get('autoSignal','STRONG')}`  (/set\\_signal)\n\n"
        f"_Last saved: {saved}_"
    )
    await update.message.reply_text(msg, parse_mode='Markdown')

# ── /positions ────────────────────────────────────────────────────────────────
@admin_only
async def cmd_positions(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    positions = get_positions()
    if isinstance(positions, dict) and 'error' in positions:
        await update.message.reply_text(f"❌ {positions['error']}")
        return
    if not positions:
        await update.message.reply_text("📭 No open positions.")
        return
    lines = [f"📊 *Open Positions ({len(positions)}):*\n"]
    for p in positions[:10]:
        sym    = p.get('symbol','?').replace('USDT','') + '/USDT'
        side   = p.get('side','?')
        entry  = p.get('avgEntry', 0)
        mark   = p.get('markPrice', 0)
        upnl   = p.get('unrealisedPnl', 0)
        size   = p.get('totalSize', 0)
        mode   = p.get('mode','paper')
        sign   = '+' if upnl >= 0 else ''
        lines.append(
            f"{'📈' if side=='LONG' else '📉'} *{sym}* {side}\n"
            f"  Entry: `{entry:.4g}` → Mark: `{mark:.4g}`\n"
            f"  Size: `${size:.0f}` PnL: `{sign}${upnl:.2f}` [{mode}]\n"
        )
    await update.message.reply_text('\n'.join(lines), parse_mode='Markdown')

# ── /trades ────────────────────────────────────────────────────────────────────
@admin_only
async def cmd_trades(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    trades = get_trades()
    if isinstance(trades, dict) and 'error' in trades:
        await update.message.reply_text(f"❌ {trades['error']}")
        return
    if not trades:
        await update.message.reply_text("📭 No closed trades yet.")
        return
    lines = [f"📋 *Recent Trades ({len(trades)}):*\n"]
    for t in trades[:8]:
        sym  = t.get('symbol','?').replace('USDT','/USDT')
        pnl  = t.get('pnl_usd') or t.get('pnl',0)
        rsn  = t.get('reason','?')[:20]
        sign = '+' if pnl >= 0 else ''
        icon = '✅' if pnl >= 0 else '❌'
        lines.append(f"{icon} *{sym}* `{sign}${pnl:.2f}` — {rsn}")
    await update.message.reply_text('\n'.join(lines), parse_mode='Markdown')

# ── /scan ─────────────────────────────────────────────────────────────────────
@admin_only
async def cmd_scan(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    r = trigger_scan()
    if r.get('status') == 'ok':
        await update.message.reply_text("⟳ Scan trigger sent — dashboard will scan within 5s.")
    else:
        await update.message.reply_text(f"❌ Scan trigger failed: {r.get('error','unknown')}")

# ── /auto_on / /auto_off ───────────────────────────────────────────────────────
@admin_only
async def cmd_auto_on(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    r = set_param('autoEnabled', True)
    if r.get('status') == 'ok':
        await update.message.reply_text("✅ Auto-trading ENABLED — dashboard will restore on next load.")
    else:
        await update.message.reply_text(f"❌ {r.get('error','failed')}")

@admin_only
async def cmd_auto_off(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    r = set_param('autoEnabled', False)
    if r.get('status') == 'ok':
        await update.message.reply_text("🛑 Auto-trading DISABLED.")
    else:
        await update.message.reply_text(f"❌ {r.get('error','failed')}")

# ── Generic param setter helper ────────────────────────────────────────────────
async def _set(update, param, value, label, unit='', min_v=None, max_v=None):
    try:
        v = float(value)
        if min_v is not None and v < min_v:
            await update.message.reply_text(f"⚠️ Min value is {min_v}{unit}")
            return
        if max_v is not None and v > max_v:
            await update.message.reply_text(f"⚠️ Max value is {max_v}{unit}")
            return
    except ValueError:
        await update.message.reply_text(f"⚠️ `{value}` is not a valid number.")
        return
    r = set_param(param, v)
    if r.get('status') == 'ok':
        await update.message.reply_text(f"✅ *{label}* set to `{v}{unit}`\n_Saved to server — auto-loaded by dashboard._", parse_mode='Markdown')
    else:
        await update.message.reply_text(f"❌ Failed: {r.get('error','unknown')}")

def _arg(ctx, update=None):
    """Extract first argument from command."""
    args = ctx.args
    if not args:
        return None
    return args[0]

@admin_only
async def cmd_set_interval(update, ctx):
    v = _arg(ctx)
    if not v: await update.message.reply_text("Usage: /set_interval <seconds>  (60–1440)"); return
    await _set(update, 'autoInterval', v, 'Scan Interval', 's', 60, 1440)

@admin_only
async def cmd_set_maxpos(update, ctx):
    v = _arg(ctx)
    if not v: await update.message.reply_text("Usage: /set_maxpos <n>  (1–20)"); return
    await _set(update, 'autoMaxPos', v, 'Auto Max Positions', '', 1, 20)

@admin_only
async def cmd_set_signal(update, ctx):
    v = _arg(ctx)
    if not v:
        await update.message.reply_text("Usage: /set_signal <STRONG|ANY>")
        return
    r = set_param('autoSignal', v.upper())
    if r.get('status') == 'ok':
        await update.message.reply_text(f"✅ *Signal Filter* set to `{v.upper()}`", parse_mode='Markdown')
    else:
        await update.message.reply_text(f"❌ {r.get('error','must be STRONG or ANY')}")

@admin_only
async def cmd_set_pump(update, ctx):
    v = _arg(ctx)
    if not v: await update.message.reply_text("Usage: /set_pump <pct>  (5–100)"); return
    await _set(update, 'pumpThreshold', v, 'Pump Threshold', '%', 5, 100)

@admin_only
async def cmd_set_reversal(update, ctx):
    v = _arg(ctx)
    if not v: await update.message.reply_text("Usage: /set_reversal <pct>  (1–25)"); return
    await _set(update, 'reversalThreshold', v, 'Reversal Threshold', '%', 1, 25)

@admin_only
async def cmd_set_tp(update, ctx):
    v = _arg(ctx)
    if not v: await update.message.reply_text("Usage: /set_tp <pct>  (1–25)"); return
    await _set(update, 'takeProfitPct', v, 'Take-Profit', '%', 1, 25)

@admin_only
async def cmd_set_sl(update, ctx):
    v = _arg(ctx)
    if not v: await update.message.reply_text("Usage: /set_sl <pct>  (3–40)"); return
    await _set(update, 'stopLossPct', v, 'Stop-Loss', '%', 3, 40)

@admin_only
async def cmd_set_trail(update, ctx):
    v = _arg(ctx)
    if not v: await update.message.reply_text("Usage: /set_trail <pct>  (0–15, 0=disabled)"); return
    await _set(update, 'trailingStopPct', v, 'Trailing Stop', '%', 0, 15)

@admin_only
async def cmd_set_size(update, ctx):
    v = _arg(ctx)
    if not v: await update.message.reply_text("Usage: /set_size <usdt>  (50–2000)"); return
    await _set(update, 'positionSize', v, 'Position Size', ' USDT', 50, 2000)

@admin_only
async def cmd_set_avg(update, ctx):
    v = _arg(ctx)
    if not v: await update.message.reply_text("Usage: /set_avg <n>  (1–5)"); return
    await _set(update, 'maxAveraging', v, 'Max Averaging Levels', '', 1, 5)

@admin_only
async def cmd_set_maxpos_cfg(update, ctx):
    v = _arg(ctx)
    if not v: await update.message.reply_text("Usage: /set_maxpos_cfg <n>  (1–10)"); return
    await _set(update, 'maxPositions', v, 'Engine Max Positions', '', 1, 10)

@admin_only
async def cmd_set_daily_loss(update, ctx):
    v = _arg(ctx)
    if not v: await update.message.reply_text("Usage: /set_daily_loss <usdt>  (0=disabled)"); return
    await _set(update, 'maxDailyLoss', v, 'Max Daily Loss', ' USDT', 0, 1000)

@admin_only
async def cmd_set_max_dd(update, ctx):
    v = _arg(ctx)
    if not v: await update.message.reply_text("Usage: /set_max_dd <pct>  (0=disabled, max=50)"); return
    await _set(update, 'maxAccountDD', v, 'Max Account Drawdown', '%', 0, 50)

@admin_only
async def cmd_set_volume(update, ctx):
    v = _arg(ctx)
    if not v:
        await update.message.reply_text(
            "Usage: /set_volume <usdt>\n"
            "Examples: 500000 (=$500K), 1000000 (=$1M)\n"
            "Range: 100000 – 50000000000"
        ); return
    await _set(update, 'minVolume', v, 'Min 24h Volume', ' USDT', 1e5, 5e10)

# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    log.info(f"Starting MeanRev v2 Admin Bot")
    log.info(f"Dashboard URL: {DASHBOARD}")
    log.info(f"Admin ID:      {ADMIN_ID}")

    # Quick connectivity check
    health = _api('GET', '/health', timeout=5)
    if 'error' in health:
        log.warning(f"Dashboard not reachable: {health['error']} — bot will start anyway")
    else:
        log.info(f"Dashboard: {health.get('status','?')} mode={health.get('engine',{}).get('mode','?')}")

    app = Application.builder().token(TOKEN).build()

    app.add_handler(CommandHandler('start',          cmd_start))
    app.add_handler(CommandHandler('help',           cmd_start))
    app.add_handler(CommandHandler('status',         cmd_status))
    app.add_handler(CommandHandler('params',         cmd_params))
    app.add_handler(CommandHandler('positions',      cmd_positions))
    app.add_handler(CommandHandler('trades',         cmd_trades))
    app.add_handler(CommandHandler('scan',           cmd_scan))
    app.add_handler(CommandHandler('auto_on',        cmd_auto_on))
    app.add_handler(CommandHandler('auto_off',       cmd_auto_off))
    app.add_handler(CommandHandler('set_interval',   cmd_set_interval))
    app.add_handler(CommandHandler('set_maxpos',     cmd_set_maxpos))
    app.add_handler(CommandHandler('set_signal',     cmd_set_signal))
    app.add_handler(CommandHandler('set_pump',       cmd_set_pump))
    app.add_handler(CommandHandler('set_reversal',   cmd_set_reversal))
    app.add_handler(CommandHandler('set_tp',         cmd_set_tp))
    app.add_handler(CommandHandler('set_sl',         cmd_set_sl))
    app.add_handler(CommandHandler('set_trail',      cmd_set_trail))
    app.add_handler(CommandHandler('set_size',       cmd_set_size))
    app.add_handler(CommandHandler('set_avg',        cmd_set_avg))
    app.add_handler(CommandHandler('set_maxpos_cfg', cmd_set_maxpos_cfg))
    app.add_handler(CommandHandler('set_daily_loss', cmd_set_daily_loss))
    app.add_handler(CommandHandler('set_max_dd',     cmd_set_max_dd))
    app.add_handler(CommandHandler('set_volume',     cmd_set_volume))

    log.info("Bot polling started. Send /help to your bot.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == '__main__':
    main()
