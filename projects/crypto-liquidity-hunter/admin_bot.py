#!/usr/bin/env python3
"""
Telegram Admin Bot for Liquidity Hunter.
ALL parameters sync with dashboard via /api/config (GET + POST).
Single master parameter list — same as dashboard Settings tab.
"""
import os, sys, subprocess, logging, json, urllib.request, urllib.error
from pathlib import Path
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

# ── Config ────────────────────────────────────────────────────────────────────
ADMIN_USER_ID  = 686482312
BOT_TOKEN      = "8663125030:AAHO1AIHTTObsj4exqoEc82935zhrYxO7Ys"
PROJECT_ROOT   = Path("/root/.openclaw/workspace/projects/crypto-liquidity-hunter")
DASHBOARD_URL  = os.environ.get("DASHBOARD_URL", "http://localhost:5000")

logging.basicConfig(level=logging.INFO,
    format='%(asctime)s %(name)s %(levelname)s: %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)])
logger = logging.getLogger(__name__)

# Import config manager (direct file access — faster than HTTP for bot)
try:
    sys.path.insert(0, str(PROJECT_ROOT))
    from core.config_manager import config_mgr
except ImportError:
    config_mgr = None
    logger.error("ConfigManager not available")

# ── Auth ──────────────────────────────────────────────────────────────────────
def is_admin(update: Update) -> bool:
    uid = update.effective_user.id if update.effective_user else None
    return uid == ADMIN_USER_ID

async def deny(update):
    await update.message.reply_text("⛔ Unauthorised.")

# ── HTTP helper (for dashboard API calls) ────────────────────────────────────
def _http(method, path, body=None, timeout=8):
    url  = DASHBOARD_URL.rstrip('/') + path
    data = json.dumps(body).encode() if body else None
    hdrs = {'Content-Type': 'application/json', 'Accept': 'application/json'}
    req  = urllib.request.Request(url, data=data, headers=hdrs, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        try:    return json.loads(e.read())
        except: return {'error': str(e)}
    except Exception as e:
        return {'error': str(e)}

# ── Config helpers ────────────────────────────────────────────────────────────
def cfg_get(key, default=None):
    """Get value — try config_mgr first (fast), fall back to HTTP."""
    if config_mgr:
        return config_mgr.get(KEY_DOT_PATHS.get(key, key), default)
    d = _http('GET', '/api/config')
    return d.get(key, default)

def cfg_set(key, value):
    """Set value via config_mgr (writes YAML directly) and also via HTTP to sync gunicorn."""
    dot = KEY_DOT_PATHS.get(key, key)
    if config_mgr:
        config_mgr.set(dot, value)
    # Also POST to dashboard so live workers pick it up
    _http('POST', '/api/config', {key: value})

# dot-paths for config_mgr
KEY_DOT_PATHS = {
    'sweep_multiplier':       'sweep_detector.sweep_multiplier',
    'volume_multiplier':      'sweep_detector.volume_multiplier',
    'wick_ratio':             'sweep_detector.wick_ratio',
    'min_sweep_pct':          'sweep_detector.min_sweep_pct',
    'confirmation_bars':      'sweep_detector.confirmation_bars',
    'min_body_ratio':         'sweep_detector.min_body_ratio',
    'lookback_bars':          'sweep_detector.lookback_bars',
    'min_risk_reward':        'signal_engine.min_risk_reward',
    'require_confluence':     'signal_engine.require_confluence',
    'risk_per_trade':         'signal_engine.risk_per_trade',
    'stop_buffer_pct':        'signal_engine.stop_buffer_pct',
    'target_buffer_pct':      'signal_engine.target_buffer_pct',
    'min_confidence':         'alerts.telegram.min_confidence',
    'alerts_enabled':         'alerts.telegram.enabled',
    'ohlcv_limit':            'data_fetch.ohlcv_limit',
    'atr_period':             'data_fetch.atr_period',
    'timeframes':             'data_fetch.timeframes',
    'volume_filter_enabled':  'volume_filter.enabled',
    'min_24h_volume_usd':     'volume_filter.min_24h_volume_usd',
    'fixed_notional_usd':     'paper_trading.fixed_notional_usd',
    'margin_leverage':        'paper_trading.margin_leverage',
    'commission_per_trade':   'paper_trading.commission_per_trade',
    'position_sizing':        'paper_trading.position_sizing',
    'scan_interval_minutes':  'cron.scan_interval_minutes',
    'signal_execution_mode':  'signal_execution.mode',
    'auto_execute':           'signal_execution.auto_execute',
    'entry_tolerance_pct':    'signal_execution.entry_tolerance_pct',
    'equal_touch_tolerance':  'liquidity_mapper.equal_touch_tolerance',
    'swing_lookback':         'liquidity_mapper.swing_lookback',
    'round_tolerance':        'liquidity_mapper.round_tolerance',
    'min_swing_strength':     'liquidity_mapper.min_swing_strength',
}

# ── /help ─────────────────────────────────────────────────────────────────────
HELP_TEXT = """<b>🎯 Liquidity Hunter Admin Bot</b>

<b>System:</b>
/restart_dashboard — Restart dashboard service
/restart_admin_bot — Restart this admin bot
/reload_config — Reload config from file
/dashboard_status — Show service status
/logs — Recent dashboard logs

<b>Operations:</b>
/scan_all — Trigger full scan now
/update_pairs — Fetch all Binance USDT pairs

<b>Show/Reload Config:</b>
/params — All current parameters (formatted)
/debug_config — Raw YAML dump

<b>🔍 Sweep Detector:</b>
/set_sweep_multiplier &lt;0.1–5.0&gt;
/set_volume_multiplier &lt;1.0–10.0&gt;
/set_wick_ratio &lt;0.1–1.0&gt;
/set_min_sweep_pct &lt;0.01–5.0&gt;
/set_confirmation_bars &lt;1–20&gt;
/set_min_body_ratio &lt;0.1–1.0&gt;
/set_lookback_bars &lt;5–100&gt;

<b>⚡ Signal Engine:</b>
/set_min_risk_reward &lt;0.5–20.0&gt;
/toggle_confluence — OB/FVG gate on/off
/set_risk_per_trade &lt;0.001–0.1&gt;
/set_stop_buffer &lt;0.001–0.05&gt;
/set_target_buffer &lt;0.001–0.05&gt;

<b>🔔 Alerts:</b>
/set_min_confidence &lt;0.0–1.0&gt;
/toggle_alerts — enable/disable Telegram alerts

<b>📊 Volume Filter:</b>
/set_min_24h_volume &lt;usd&gt;
/toggle_volume_filter

<b>📡 Data Fetch:</b>
/set_ohlcv_limit &lt;100–5000&gt;
/set_atr_period &lt;5–50&gt;
/set_timeframes &lt;4h,1h,15m&gt;

<b>⏱️ Scan Schedule:</b>
/set_scan_interval &lt;minutes&gt; (+ /apply_cron)
/cron_status — show current crontab
/test_cron — preview crontab
/apply_cron — install crontab

<b>📝 Paper Trading:</b>
/set_fixed_notional &lt;usd&gt;
/set_margin_leverage &lt;1–125&gt;
/set_commission &lt;0.0001–0.01&gt;
/set_position_sizing &lt;fixed_notional|risk_percent&gt;

<b>🎯 Signal Execution:</b>
/set_execution_mode &lt;pending|auto&gt;
/toggle_auto_execute
/set_entry_tolerance &lt;0.1–5.0&gt;

<b>🗺️ Liquidity Mapper:</b>
/set_touch_tolerance &lt;0.001–0.05&gt;
/set_swing_lookback &lt;3–20&gt;
/set_round_tolerance &lt;0.001–0.05&gt;
/set_swing_strength &lt;1–10&gt;

<i>All changes sync to dashboard immediately.</i>
"""

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update): await deny(update); return
    await update.message.reply_text(HELP_TEXT, parse_mode="HTML")

# ── /params ───────────────────────────────────────────────────────────────────
async def show_params(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update): await deny(update); return
    d = _http('GET', '/api/config')
    if 'error' in d:
        await update.message.reply_text(f"❌ Could not fetch config: {d['error']}")
        return
    on  = lambda v: '✅' if v else '❌'
    pct = lambda v: f"{round(v*100,1)}%" if v is not None else '—'
    vol = lambda v: (f"${v/1e9:.2f}B" if v>=1e9 else f"${v/1e6:.2f}M" if v>=1e6 else f"${v/1e3:.0f}K") if v else '—'

    text = f"""<b>⚙️ All Parameters — Liquidity Hunter</b>

<b>🔍 Sweep Detector:</b>
  sweep_multiplier: <b>{d.get('sweep_multiplier')}</b>
  volume_multiplier: <b>{d.get('volume_multiplier')}</b>
  wick_ratio: <b>{d.get('wick_ratio')}</b>
  min_sweep_pct: <b>{d.get('min_sweep_pct')}%</b>
  confirmation_bars: <b>{d.get('confirmation_bars')}</b>
  min_body_ratio: <b>{d.get('min_body_ratio')}</b>
  lookback_bars: <b>{d.get('lookback_bars')}</b>

<b>⚡ Signal Engine:</b>
  min_risk_reward: <b>{d.get('min_risk_reward')}</b>
  require_confluence: {on(d.get('require_confluence'))}
  risk_per_trade: <b>{pct(d.get('risk_per_trade'))}</b>
  stop_buffer_pct: <b>{d.get('stop_buffer_pct')}</b>
  target_buffer_pct: <b>{d.get('target_buffer_pct')}</b>

<b>🔔 Alerts:</b>
  min_confidence: <b>{pct(d.get('min_confidence'))}</b>
  alerts_enabled: {on(d.get('alerts_enabled'))}

<b>📊 Volume Filter:</b>
  enabled: {on(d.get('volume_filter_enabled'))}
  min_24h_volume: <b>{vol(d.get('min_24h_volume_usd'))}</b>

<b>📡 Data Fetch:</b>
  ohlcv_limit: <b>{d.get('ohlcv_limit')} bars</b>
  atr_period: <b>{d.get('atr_period')}</b>
  timeframes: <b>{', '.join(d.get('timeframes',[]))}</b>

<b>⏱️ Scan Schedule:</b>
  scan_interval_minutes: <b>{d.get('scan_interval_minutes')} min</b>

<b>📝 Paper Trading:</b>
  fixed_notional: <b>${d.get('fixed_notional_usd')}</b>
  leverage: <b>{d.get('margin_leverage')}×</b>
  commission: <b>{pct(d.get('commission_per_trade'))}</b>
  sizing: <b>{d.get('position_sizing')}</b>

<b>🎯 Signal Execution:</b>
  mode: <b>{d.get('signal_execution_mode')}</b>
  auto_execute: {on(d.get('auto_execute'))}
  entry_tolerance: <b>{d.get('entry_tolerance_pct')}%</b>

<b>🗺️ Liquidity Mapper:</b>
  equal_touch_tolerance: <b>{d.get('equal_touch_tolerance')}</b>
  swing_lookback: <b>{d.get('swing_lookback')}</b>
  round_tolerance: <b>{d.get('round_tolerance')}</b>
  min_swing_strength: <b>{d.get('min_swing_strength')}</b>
"""
    await update.message.reply_text(text, parse_mode="HTML")

async def debug_config(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update): await deny(update); return
    if not config_mgr:
        await update.message.reply_text("❌ Config manager not available"); return
    dump = json.dumps(config_mgr._config, indent=2, default=str)
    if len(dump) > 4000: dump = dump[:4000] + "\n... (truncated)"
    await update.message.reply_text(f"<pre>{dump}</pre>", parse_mode="HTML")

async def reload_config(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update): await deny(update); return
    if not config_mgr:
        await update.message.reply_text("❌ Config manager not available"); return
    try:
        config_mgr._config = config_mgr.load()
        await update.message.reply_text("✅ Config reloaded from file.")
    except Exception as e:
        await update.message.reply_text(f"❌ Reload failed: {e}")

# ── Generic setter ─────────────────────────────────────────────────────────────
async def _set(update, key, raw, label, typ=float, mn=None, mx=None, unit=''):
    try:
        val = typ(raw)
        if mn is not None and val < mn:
            await update.message.reply_text(f"❌ Min is {mn}{unit}"); return False
        if mx is not None and val > mx:
            await update.message.reply_text(f"❌ Max is {mx}{unit}"); return False
        cfg_set(key, val)
        await update.message.reply_text(
            f"✅ <b>{label}</b> = <code>{val}{unit}</code>\n"
            f"<i>Saved to config — synced to dashboard.</i>", parse_mode="HTML")
        return True
    except (ValueError, TypeError):
        await update.message.reply_text(f"❌ Invalid value: {raw}")
        return False

def _arg(ctx): return ctx.args[0] if ctx.args else None

# ── System commands ────────────────────────────────────────────────────────────
async def restart_dashboard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update): await deny(update); return
    await update.message.reply_text("🔄 Restarting dashboard...")
    try:
        subprocess.run(["systemctl","restart","liquidity-hunter-dashboard.service"],
                       check=True, capture_output=True, text=True)
        await update.message.reply_text("✅ Dashboard restarted.")
    except subprocess.CalledProcessError as e:
        await update.message.reply_text(f"❌ Failed:\n<code>{e.stderr}</code>", parse_mode="HTML")

async def restart_admin_bot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update): await deny(update); return
    await update.message.reply_text("🔄 Restarting admin bot...")
    try:
        subprocess.run(["systemctl","restart","liquidity-hunter-admin-bot.service"],
                       check=True, capture_output=True, text=True)
    except subprocess.CalledProcessError as e:
        await update.message.reply_text(f"❌ Failed:\n<code>{e.stderr}</code>", parse_mode="HTML")

async def dashboard_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update): await deny(update); return
    try:
        res = subprocess.run(["systemctl","status","liquidity-hunter-dashboard.service","--no-pager"],
                             capture_output=True, text=True, timeout=5)
        out = (res.stdout or res.stderr)[:4000]
        await update.message.reply_text(f"<pre>{out}</pre>", parse_mode="HTML")
    except Exception as e:
        await update.message.reply_text(f"❌ {e}")

async def view_logs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update): await deny(update); return
    for lf in [PROJECT_ROOT/"logs"/"dashboard.log", PROJECT_ROOT/"dashboard.log"]:
        if lf.exists():
            lines = lf.read_text().splitlines()[-50:]
            out = "\n".join(lines)[-4000:]
            await update.message.reply_text(f"<pre>{out}</pre>", parse_mode="HTML")
            return
    await update.message.reply_text("❌ Log file not found")

async def run_scan_all(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update): await deny(update); return
    await update.message.reply_text("🔍 Starting full scan...")
    log = open(PROJECT_ROOT/"logs"/"manual_scan.log", "a")
    subprocess.Popen([sys.executable, str(PROJECT_ROOT/"main.py"), "scan-all","--alert"],
                     cwd=PROJECT_ROOT, stdout=log, stderr=subprocess.STDOUT)
    await update.message.reply_text("✅ Scan running in background — check Telegram for alerts.")

async def update_pairs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update): await deny(update); return
    await update.message.reply_text("🔄 Fetching Binance USDT pairs...")
    try:
        from core.data_fetcher import MarketDataFetcher
        fetcher = MarketDataFetcher('binance')
        markets = fetcher.load_markets()
        pairs = sorted([f"binance:{m['symbol']}" for m in markets.values()
                        if m.get('active') and m.get('quote')=='USDT' and m.get('spot')])
        config_mgr.set('pairs', pairs)
        await update.message.reply_text(f"✅ Updated: {len(pairs)} pairs saved.")
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {e}")

# ── Cron ──────────────────────────────────────────────────────────────────────
async def set_scan_interval(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update): await deny(update); return
    v = _arg(context)
    if not v: await update.message.reply_text("Usage: /set_scan_interval <minutes>"); return
    await _set(update, 'scan_interval_minutes', v, 'Scan Interval', int, 1, 1440, ' min')

async def cron_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update): await deny(update); return
    try:
        r = subprocess.run(["/usr/bin/crontab","-l"], capture_output=True, text=True, timeout=5)
        lines = [l for l in r.stdout.splitlines() if "run_scan_all" in l]
        if lines: await update.message.reply_text(f"<code>{lines[0]}</code>", parse_mode="HTML")
        else:     await update.message.reply_text("⚠️ No cron entry for run_scan_all.sh")
    except Exception as e: await update.message.reply_text(f"❌ {e}")

async def test_cron(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update): await deny(update); return
    interval = cfg_get('scan_interval_minutes', 5)
    line = f"*/{interval} * * * * cd {PROJECT_ROOT} && ./scheduler/run_scan_all.sh >> /dev/null 2>&1"
    await update.message.reply_text(f"Would set:\n<code>{line}</code>\nConfirm with /apply_cron", parse_mode="HTML")

async def apply_cron(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update): await deny(update); return
    interval = cfg_get('scan_interval_minutes', 5)
    line = f"*/{interval} * * * * cd {PROJECT_ROOT} && ./scheduler/run_scan_all.sh >> /dev/null 2>&1"
    try:
        r = subprocess.run(["/usr/bin/crontab","-l"], capture_output=True, text=True, timeout=5)
        lines = [l for l in (r.stdout.splitlines() if r.returncode==0 else []) if "run_scan_all" not in l]
        lines.append(line)
        subprocess.run(["/usr/bin/crontab","-"], input="\n".join(lines).encode(), check=True, timeout=5)
        await update.message.reply_text(f"✅ Cron set: every {interval} min")
    except Exception as e: await update.message.reply_text(f"❌ {e}")

# ── Sweep Detector params ──────────────────────────────────────────────────────
async def set_sweep_multiplier(update, context):
    if not is_admin(update): await deny(update); return
    v = _arg(context)
    if not v: await update.message.reply_text("Usage: /set_sweep_multiplier <0.1–5.0>"); return
    await _set(update, 'sweep_multiplier', v, 'Sweep Multiplier', float, 0.1, 5.0)

async def set_volume_multiplier(update, context):
    if not is_admin(update): await deny(update); return
    v = _arg(context)
    if not v: await update.message.reply_text("Usage: /set_volume_multiplier <1.0–10.0>"); return
    await _set(update, 'volume_multiplier', v, 'Volume Multiplier', float, 1.0, 10.0)

async def set_wick_ratio(update, context):
    if not is_admin(update): await deny(update); return
    v = _arg(context)
    if not v: await update.message.reply_text("Usage: /set_wick_ratio <0.1–1.0>"); return
    await _set(update, 'wick_ratio', v, 'Wick Ratio', float, 0.1, 1.0)

async def set_min_sweep_pct(update, context):
    if not is_admin(update): await deny(update); return
    v = _arg(context)
    if not v: await update.message.reply_text("Usage: /set_min_sweep_pct <0.01–5.0>"); return
    await _set(update, 'min_sweep_pct', v, 'Min Sweep %', float, 0.01, 5.0)

async def set_confirmation_bars(update, context):
    if not is_admin(update): await deny(update); return
    v = _arg(context)
    if not v: await update.message.reply_text("Usage: /set_confirmation_bars <1–20>"); return
    await _set(update, 'confirmation_bars', v, 'Confirmation Bars', int, 1, 20)

async def set_min_body_ratio(update, context):
    if not is_admin(update): await deny(update); return
    v = _arg(context)
    if not v: await update.message.reply_text("Usage: /set_min_body_ratio <0.1–1.0>"); return
    await _set(update, 'min_body_ratio', v, 'Min Body Ratio', float, 0.1, 1.0)

async def set_lookback_bars(update, context):
    if not is_admin(update): await deny(update); return
    v = _arg(context)
    if not v: await update.message.reply_text("Usage: /set_lookback_bars <5–100>"); return
    await _set(update, 'lookback_bars', v, 'Lookback Bars', int, 5, 100)

# ── Signal Engine params ───────────────────────────────────────────────────────
async def set_min_risk_reward(update, context):
    if not is_admin(update): await deny(update); return
    v = _arg(context)
    if not v: await update.message.reply_text("Usage: /set_min_risk_reward <0.5–20.0>"); return
    await _set(update, 'min_risk_reward', v, 'Min Risk:Reward', float, 0.5, 20.0)

async def toggle_confluence(update, context):
    if not is_admin(update): await deny(update); return
    cur = cfg_get('require_confluence', True)
    new = not cur
    cfg_set('require_confluence', new)
    state = "ON ✅ (OB/FVG required)" if new else "OFF ⚠️ (all sweeps)"
    await update.message.reply_text(f"Confluence Gate: <b>{state}</b>", parse_mode="HTML")

async def set_risk_per_trade(update, context):
    if not is_admin(update): await deny(update); return
    v = _arg(context)
    if not v: await update.message.reply_text("Usage: /set_risk_per_trade <0.001–0.1>"); return
    await _set(update, 'risk_per_trade', v, 'Risk Per Trade', float, 0.001, 0.1)

async def set_stop_buffer(update, context):
    if not is_admin(update): await deny(update); return
    v = _arg(context)
    if not v: await update.message.reply_text("Usage: /set_stop_buffer <0.001–0.05>"); return
    await _set(update, 'stop_buffer_pct', v, 'Stop Buffer %', float, 0.001, 0.05)

async def set_target_buffer(update, context):
    if not is_admin(update): await deny(update); return
    v = _arg(context)
    if not v: await update.message.reply_text("Usage: /set_target_buffer <0.001–0.05>"); return
    await _set(update, 'target_buffer_pct', v, 'Target Buffer %', float, 0.001, 0.05)

# ── Alert params ───────────────────────────────────────────────────────────────
async def set_min_confidence(update, context):
    if not is_admin(update): await deny(update); return
    v = _arg(context)
    if not v: await update.message.reply_text("Usage: /set_min_confidence <0.0–1.0>"); return
    await _set(update, 'min_confidence', v, 'Min Confidence', float, 0.0, 1.0)

async def toggle_alerts(update, context):
    if not is_admin(update): await deny(update); return
    cur = cfg_get('alerts_enabled', True)
    new = not cur
    cfg_set('alerts_enabled', new)
    state = "ENABLED ✅" if new else "DISABLED ❌"
    await update.message.reply_text(f"Telegram Alerts: <b>{state}</b>", parse_mode="HTML")

# ── Volume Filter params ───────────────────────────────────────────────────────
async def set_min_24h_volume(update, context):
    if not is_admin(update): await deny(update); return
    v = _arg(context)
    if not v: await update.message.reply_text("Usage: /set_min_24h_volume <usd>  e.g. 1000000"); return
    await _set(update, 'min_24h_volume_usd', v, 'Min 24h Volume', float, 0, None, ' USD')

async def toggle_volume_filter(update, context):
    if not is_admin(update): await deny(update); return
    cur = cfg_get('volume_filter_enabled', True)
    new = not cur
    cfg_set('volume_filter_enabled', new)
    await update.message.reply_text(f"Volume Filter: <b>{'ENABLED ✅' if new else 'DISABLED ❌'}</b>", parse_mode="HTML")

# ── Data Fetch params ──────────────────────────────────────────────────────────
async def set_ohlcv_limit(update, context):
    if not is_admin(update): await deny(update); return
    v = _arg(context)
    if not v: await update.message.reply_text("Usage: /set_ohlcv_limit <100–5000>"); return
    await _set(update, 'ohlcv_limit', v, 'OHLCV Limit', int, 100, 5000, ' bars')

async def set_atr_period(update, context):
    if not is_admin(update): await deny(update); return
    v = _arg(context)
    if not v: await update.message.reply_text("Usage: /set_atr_period <5–50>"); return
    await _set(update, 'atr_period', v, 'ATR Period', int, 5, 50)

async def set_timeframes(update, context):
    if not is_admin(update): await deny(update); return
    if not context.args:
        await update.message.reply_text("Usage: /set_timeframes <4h,1h,15m>"); return
    tfs = [t.strip() for t in context.args[0].split(',') if t.strip()]
    if not tfs: await update.message.reply_text("❌ Provide at least one timeframe"); return
    cfg_set('timeframes', tfs)
    await update.message.reply_text(f"✅ <b>Timeframes</b> = <code>{', '.join(tfs)}</code>", parse_mode="HTML")

# ── Paper Trading params ───────────────────────────────────────────────────────
async def set_fixed_notional(update, context):
    if not is_admin(update): await deny(update); return
    v = _arg(context)
    if not v: await update.message.reply_text("Usage: /set_fixed_notional <usd>"); return
    await _set(update, 'fixed_notional_usd', v, 'Fixed Notional', float, 1, None, ' USD')

async def set_margin_leverage(update, context):
    if not is_admin(update): await deny(update); return
    v = _arg(context)
    if not v: await update.message.reply_text("Usage: /set_margin_leverage <1–125>"); return
    await _set(update, 'margin_leverage', v, 'Margin Leverage', float, 1, 125, '×')

async def set_commission(update, context):
    if not is_admin(update): await deny(update); return
    v = _arg(context)
    if not v: await update.message.reply_text("Usage: /set_commission <0.0001–0.01>  e.g. 0.001 = 0.1%"); return
    await _set(update, 'commission_per_trade', v, 'Commission', float, 0.0, 0.01)

async def set_position_sizing(update, context):
    if not is_admin(update): await deny(update); return
    v = _arg(context)
    if v not in ('fixed_notional','risk_percent'):
        await update.message.reply_text("Usage: /set_position_sizing <fixed_notional|risk_percent>"); return
    cfg_set('position_sizing', v)
    await update.message.reply_text(f"✅ <b>Position Sizing</b> = <code>{v}</code>", parse_mode="HTML")

# ── Signal Execution params ────────────────────────────────────────────────────
async def set_execution_mode(update, context):
    if not is_admin(update): await deny(update); return
    v = _arg(context)
    if v not in ('pending','auto'):
        await update.message.reply_text("Usage: /set_execution_mode <pending|auto>"); return
    cfg_set('signal_execution_mode', v)
    await update.message.reply_text(f"✅ <b>Execution Mode</b> = <code>{v}</code>", parse_mode="HTML")

async def toggle_auto_execute(update, context):
    if not is_admin(update): await deny(update); return
    cur = cfg_get('auto_execute', False)
    new = not cur
    cfg_set('auto_execute', new)
    await update.message.reply_text(f"Auto Execute: <b>{'ON ✅' if new else 'OFF ❌'}</b>", parse_mode="HTML")

async def set_entry_tolerance(update, context):
    if not is_admin(update): await deny(update); return
    v = _arg(context)
    if not v: await update.message.reply_text("Usage: /set_entry_tolerance <0.1–5.0>"); return
    await _set(update, 'entry_tolerance_pct', v, 'Entry Tolerance', float, 0.1, 5.0, '%')

# ── Liquidity Mapper params ────────────────────────────────────────────────────
async def set_touch_tolerance(update, context):
    if not is_admin(update): await deny(update); return
    v = _arg(context)
    if not v: await update.message.reply_text("Usage: /set_touch_tolerance <0.001–0.05>"); return
    await _set(update, 'equal_touch_tolerance', v, 'Equal Touch Tolerance', float, 0.001, 0.05)

async def set_swing_lookback(update, context):
    if not is_admin(update): await deny(update); return
    v = _arg(context)
    if not v: await update.message.reply_text("Usage: /set_swing_lookback <3–20>"); return
    await _set(update, 'swing_lookback', v, 'Swing Lookback', int, 3, 20)

async def set_round_tolerance(update, context):
    if not is_admin(update): await deny(update); return
    v = _arg(context)
    if not v: await update.message.reply_text("Usage: /set_round_tolerance <0.001–0.05>"); return
    await _set(update, 'round_tolerance', v, 'Round Tolerance', float, 0.001, 0.05)

async def set_swing_strength(update, context):
    if not is_admin(update): await deny(update); return
    v = _arg(context)
    if not v: await update.message.reply_text("Usage: /set_swing_strength <1–10>"); return
    await _set(update, 'min_swing_strength', v, 'Min Swing Strength', int, 1, 10)

# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    logger.info(f"Starting Liquidity Hunter Admin Bot")
    logger.info(f"Dashboard: {DASHBOARD_URL}")

    # Quick health check
    h = _http('GET', '/health', timeout=3)
    if 'error' in h: logger.warning(f"Dashboard not reachable: {h['error']}")
    else: logger.info(f"Dashboard: {h.get('status','?')}")

    app = Application.builder().token(BOT_TOKEN).build()

    # System
    app.add_handler(CommandHandler("help",               help_command))
    app.add_handler(CommandHandler("start",              help_command))
    app.add_handler(CommandHandler("restart_dashboard",  restart_dashboard))
    app.add_handler(CommandHandler("restart_admin_bot",  restart_admin_bot))
    app.add_handler(CommandHandler("dashboard_status",   dashboard_status))
    app.add_handler(CommandHandler("logs",               view_logs))
    app.add_handler(CommandHandler("scan_all",           run_scan_all))
    app.add_handler(CommandHandler("update_pairs",       update_pairs))

    # Config show
    app.add_handler(CommandHandler("params",             show_params))
    app.add_handler(CommandHandler("debug_config",       debug_config))
    app.add_handler(CommandHandler("reload_config",      reload_config))

    # Cron
    app.add_handler(CommandHandler("set_scan_interval",  set_scan_interval))
    app.add_handler(CommandHandler("cron_status",        cron_status))
    app.add_handler(CommandHandler("test_cron",          test_cron))
    app.add_handler(CommandHandler("apply_cron",         apply_cron))

    # Sweep Detector
    app.add_handler(CommandHandler("set_sweep_multiplier",  set_sweep_multiplier))
    app.add_handler(CommandHandler("set_volume_multiplier", set_volume_multiplier))
    app.add_handler(CommandHandler("set_wick_ratio",        set_wick_ratio))
    app.add_handler(CommandHandler("set_min_sweep_pct",     set_min_sweep_pct))
    app.add_handler(CommandHandler("set_confirmation_bars", set_confirmation_bars))
    app.add_handler(CommandHandler("set_min_body_ratio",    set_min_body_ratio))
    app.add_handler(CommandHandler("set_lookback_bars",     set_lookback_bars))

    # Signal Engine
    app.add_handler(CommandHandler("set_min_risk_reward", set_min_risk_reward))
    app.add_handler(CommandHandler("toggle_confluence",   toggle_confluence))
    app.add_handler(CommandHandler("set_risk_per_trade",  set_risk_per_trade))
    app.add_handler(CommandHandler("set_stop_buffer",     set_stop_buffer))
    app.add_handler(CommandHandler("set_target_buffer",   set_target_buffer))

    # Alerts
    app.add_handler(CommandHandler("set_min_confidence",  set_min_confidence))
    app.add_handler(CommandHandler("toggle_alerts",       toggle_alerts))

    # Volume Filter
    app.add_handler(CommandHandler("set_min_24h_volume",  set_min_24h_volume))
    app.add_handler(CommandHandler("toggle_volume_filter",toggle_volume_filter))

    # Data Fetch
    app.add_handler(CommandHandler("set_ohlcv_limit",     set_ohlcv_limit))
    app.add_handler(CommandHandler("set_atr_period",      set_atr_period))
    app.add_handler(CommandHandler("set_timeframes",      set_timeframes))

    # Paper Trading
    app.add_handler(CommandHandler("set_fixed_notional",  set_fixed_notional))
    app.add_handler(CommandHandler("set_margin_leverage", set_margin_leverage))
    app.add_handler(CommandHandler("set_commission",      set_commission))
    app.add_handler(CommandHandler("set_position_sizing", set_position_sizing))

    # Signal Execution
    app.add_handler(CommandHandler("set_execution_mode",  set_execution_mode))
    app.add_handler(CommandHandler("toggle_auto_execute", toggle_auto_execute))
    app.add_handler(CommandHandler("set_entry_tolerance", set_entry_tolerance))

    # Liquidity Mapper
    app.add_handler(CommandHandler("set_touch_tolerance", set_touch_tolerance))
    app.add_handler(CommandHandler("set_swing_lookback",  set_swing_lookback))
    app.add_handler(CommandHandler("set_round_tolerance", set_round_tolerance))
    app.add_handler(CommandHandler("set_swing_strength",  set_swing_strength))

    logger.info("Bot polling started. Send /help to get started.")
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == '__main__':
    import time
    for attempt in range(5):
        try:
            main(); break
        except Exception as e:
            if 'Conflict' in str(e) and attempt < 4:
                wait = 15 * (attempt + 1)
                logger.warning(f"Conflict on attempt {attempt+1}, retrying in {wait}s...")
                time.sleep(wait)
            else:
                raise
