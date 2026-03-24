#!/usr/bin/env python3
"""
Telegram Admin Bot for Liquidity Hunter.
Provides remote control: restart dashboard, trigger scans, view status, adjust parameters.
"""
import os
import sys
import subprocess
import logging
from pathlib import Path
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

# === CONFIG ===
ADMIN_USER_ID = 686482312  # Your Telegram user ID
BOT_TOKEN = "8663125030:AAHO1AIHTTObsj4exqoEc82935zhrYxO7Ys"
PROJECT_ROOT = Path("/root/.openclaw/workspace/projects/crypto-liquidity-hunter")

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(name)s %(levelname)s: %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger(__name__)

# Import config manager
try:
    from core.config_manager import config_mgr
except ImportError:
    config_mgr = None
    logger.error("ConfigManager not available - parameter commands disabled")

# === Auth helper ===
def auth_required(update: Update) -> bool:
    """Return True if sender is the admin; send error and return False otherwise."""
    uid = update.effective_user.id if update.effective_user else None
    if uid != ADMIN_USER_ID:
        import asyncio
        logger.warning(f"Unauthorised access attempt from user_id={uid}")
        return False
    return True

# === System Commands ===

async def restart_dashboard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🔄 Restarting dashboard and clearing caches...")
    try:
        subprocess.run(
            ["find", str(PROJECT_ROOT), "-type", "d", "-name", "__pycache__", "-exec", "rm", "-rf", "{}", "+"],
            capture_output=True, text=True, timeout=10
        )
        subprocess.run(
            ["find", str(PROJECT_ROOT), "-name", "*.pyc", "-delete"],
            capture_output=True, text=True, timeout=10
        )
        subprocess.run(["systemctl", "restart", "liquidity-hunter-dashboard.service"], check=True, capture_output=True, text=True)
        await update.message.reply_text(
            "✅ Dashboard restarted.\n"
            "📌 Hard refresh browser: Ctrl+Shift+R (Windows) or Cmd+Shift+R (Mac).",
            parse_mode="HTML"
        )
    except subprocess.CalledProcessError as e:
        await update.message.reply_text(f"❌ Restart failed:\n<code>{e.stderr}</code>", parse_mode="HTML")
    except subprocess.TimeoutExpired:
        await update.message.reply_text("⚠️ Cache clear timed out.")

async def restart_admin_bot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🔄 Restarting admin bot...")
    try:
        subprocess.run(["systemctl", "restart", "liquidity-hunter-admin-bot.service"], check=True, capture_output=True, text=True)
        await update.message.reply_text("✅ Admin bot restarted successfully.")
    except subprocess.CalledProcessError as e:
        await update.message.reply_text(f"❌ Restart failed:\n<code>{e.stderr}</code>", parse_mode="HTML")

async def dashboard_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        result = subprocess.run(
            ["systemctl", "status", "liquidity-hunter-dashboard.service", "--no-pager"],
            capture_output=True, text=True, timeout=5
        )
        output = result.stdout[:4000] if result.stdout else result.stderr
        await update.message.reply_text(f"<pre>{output}</pre>", parse_mode="HTML")
    except subprocess.TimeoutExpired:
        await update.message.reply_text("❌ Status check timed out")

async def run_scan_all(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🔍 Starting full scan across all pairs...")
    script_path = PROJECT_ROOT / "main.py"
    try:
        subprocess.Popen(
            [sys.executable, str(script_path), "scan-all", "--alert"],
            cwd=PROJECT_ROOT,
            stdout=open(PROJECT_ROOT / "logs" / "manual_scan.log", "a"),
            stderr=subprocess.STDOUT
        )
        await update.message.reply_text("✅ Scan started in background. Check Telegram for alerts.")
    except Exception as e:
        await update.message.reply_text(f"❌ Failed to start scan: {e}")

async def view_logs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    log_file = PROJECT_ROOT / "dashboard.log"
    try:
        with open(log_file, "r") as f:
            lines = f.readlines()[-50:]
        output = "".join(lines)
        if len(output) > 4000:
            output = output[-4000:]
        await update.message.reply_text(f"<pre>{output}</pre>", parse_mode="HTML")
    except FileNotFoundError:
        await update.message.reply_text("❌ Log file not found")
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {e}")

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = """
<b>🤖 Liquidity Hunter Admin Bot</b>

<b>System:</b>
/restart_dashboard — Restart dashboard service
/restart_admin_bot — Restart this admin bot
/reload_config — Reload config from file
/dashboard_status — Show dashboard status
/logs — Show recent dashboard logs

<b>Operations:</b>
/scan_all — Trigger full scan now
/update_pairs — Fetch all Binance USDT spot pairs

<b>Cron:</b>
/set_scan_interval &lt;minutes&gt; — Set scan interval (1–1440)
/cron_status — Show current cron entry
/test_cron — Preview what cron would be set to
/apply_cron — Install cron entry now

<b>Parameter Commands (live adjust):</b>
/params — Show current parameters
/debug_config — Dump raw config (troubleshooting)

Sweep Detector:
  /set_sweep_multiplier &lt;value&gt; — 0.1–5.0
  /set_volume_multiplier &lt;value&gt; — 1.0–10.0
  /set_wick_ratio &lt;value&gt; — 0.1–1.0
  /set_min_sweep_pct &lt;value&gt; — 0.01–5%

Signal Engine:
  /set_min_risk_reward &lt;value&gt; — 0.5–20.0
  /set_min_confidence &lt;0.0–1.0&gt; — alert confidence threshold

Data Fetch:
  /set_ohlcv_limit &lt;bars&gt; — 100–5000
  /set_timeframes &lt;4h,1h,15m&gt; — set timeframes

Volume Filter:
  /set_min_24h_volume &lt;usd&gt;
  /toggle_volume_filter

Paper Trading:
  /set_fixed_notional &lt;usd&gt;
  /set_margin_leverage &lt;ratio&gt;

Only admin can use these commands.
"""
    await update.message.reply_text(text, parse_mode="HTML")

# === Parameter Commands ===

async def show_params(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not config_mgr:
        await update.message.reply_text("❌ Config manager not available")
        return
    c = config_mgr._config
    vol_cfg = c.get('volume_filter', {})
    vol_enabled = vol_cfg.get('enabled', False)
    vol_min = vol_cfg.get('min_24h_volume_usd', 0)
    cron_cfg = c.get('cron', {})
    scan_interval = cron_cfg.get('scan_interval_minutes', 60)
    logger.info(f"[show_params] volume_filter: {vol_cfg}")
    text = f"""<b>Current Parameters:</b>

<b>Data Fetch:</b>
• ohlcv_limit: {c['data_fetch']['ohlcv_limit']} bars
• atr_period: {c['data_fetch']['atr_period']}
• timeframes: {', '.join(c['data_fetch']['timeframes'])}

<b>Cron:</b>
• scan_interval_minutes: {scan_interval}

<b>Volume Filter:</b>
• enabled: {vol_enabled}
• min_24h_volume_usd: ${vol_min:,.0f}

<b>Sweep Detector:</b>
• sweep_multiplier: {c['sweep_detector']['sweep_multiplier']}
• volume_multiplier: {c['sweep_detector']['volume_multiplier']}
• wick_ratio: {c['sweep_detector']['wick_ratio']}
• min_sweep_pct: {c['sweep_detector']['min_sweep_pct']}
• confirmation_bars: {c['sweep_detector']['confirmation_bars']}

<b>Signal Engine:</b>
• min_risk_reward: {c['signal_engine']['min_risk_reward']}

<b>Paper Trading:</b>
• fixed_notional_usd: {c.get('paper_trading', {}).get('fixed_notional_usd', 50)}
• margin_leverage: {c.get('paper_trading', {}).get('margin_leverage', 1)}
• commission_per_trade: {c.get('paper_trading', {}).get('commission_per_trade', 0.001)}
"""
    await update.message.reply_text(text, parse_mode="HTML")

async def debug_config(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not config_mgr:
        await update.message.reply_text("❌ Config manager not available")
        return
    import json
    dump = json.dumps(config_mgr._config, indent=2, default=str)
    if len(dump) > 4000:
        dump = dump[:4000] + "\n... (truncated)"
    await update.message.reply_text(f"<pre>{dump}</pre>", parse_mode="HTML")

async def reload_config(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not config_mgr:
        await update.message.reply_text("❌ Config manager not available")
        return
    try:
        config_mgr._config = config_mgr.load()
        await update.message.reply_text("✅ Config reloaded from file.")
    except Exception as e:
        await update.message.reply_text(f"❌ Reload failed: {e}")

async def set_sweep_multiplier(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not config_mgr:
        await update.message.reply_text("❌ Config manager not available")
        return
    try:
        val = float(context.args[0])
        if not (0.1 <= val <= 5.0):
            await update.message.reply_text("❌ Value must be between 0.1 and 5.0")
            return
        config_mgr.set('sweep_detector.sweep_multiplier', val)
        await update.message.reply_text(f"✅ sweep_multiplier set to {val}")
    except (IndexError, ValueError):
        await update.message.reply_text("Usage: /set_sweep_multiplier <value>")

async def set_volume_multiplier(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not config_mgr:
        await update.message.reply_text("❌ Config manager not available")
        return
    try:
        val = float(context.args[0])
        if not (1.0 <= val <= 10.0):
            await update.message.reply_text("❌ Value must be between 1.0 and 10.0")
            return
        config_mgr.set('sweep_detector.volume_multiplier', val)
        await update.message.reply_text(f"✅ volume_multiplier set to {val}")
    except (IndexError, ValueError):
        await update.message.reply_text("Usage: /set_volume_multiplier <value>")

async def set_wick_ratio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not config_mgr:
        await update.message.reply_text("❌ Config manager not available")
        return
    try:
        val = float(context.args[0])
        if not (0.1 <= val <= 1.0):
            await update.message.reply_text("❌ Value must be between 0.1 and 1.0")
            return
        config_mgr.set('sweep_detector.wick_ratio', val)
        await update.message.reply_text(f"✅ wick_ratio set to {val}")
    except (IndexError, ValueError):
        await update.message.reply_text("Usage: /set_wick_ratio <value>")

async def set_min_sweep_pct(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not config_mgr:
        await update.message.reply_text("❌ Config manager not available")
        return
    try:
        val = float(context.args[0])
        if not (0.01 <= val <= 5.0):
            await update.message.reply_text("❌ Value must be between 0.01% and 5%")
            return
        config_mgr.set('sweep_detector.min_sweep_pct', val)
        await update.message.reply_text(f"✅ min_sweep_pct set to {val}%")
    except (IndexError, ValueError):
        await update.message.reply_text("Usage: /set_min_sweep_pct <value>")

async def set_min_risk_reward(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not config_mgr:
        await update.message.reply_text("❌ Config manager not available")
        return
    try:
        val = float(context.args[0])
        if not (0.5 <= val <= 20.0):
            await update.message.reply_text("❌ Value must be between 0.5 and 20.0")
            return
        config_mgr.set('signal_engine.min_risk_reward', val)
        await update.message.reply_text(f"✅ min_risk_reward set to {val}")
    except (IndexError, ValueError):
        await update.message.reply_text("Usage: /set_min_risk_reward <value>")

async def set_min_confidence(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Set minimum confidence threshold for Telegram alerts (0.0 – 1.0)."""
    if not auth_required(update):
        await update.message.reply_text("❌ Unauthorised.")
        return
    try:
        val = float(context.args[0])
        if not (0.0 <= val <= 1.0):
            await update.message.reply_text("❌ Value must be between 0.0 and 1.0 (e.g. 0.6 = 60%)")
            return
        config_mgr.set('alerts.telegram.min_confidence', val)
        pct = int(val * 100)
        await update.message.reply_text(
            f"✅ Min confidence set to {pct}%\n"
            f"Signals below {pct}% confidence will NOT trigger Telegram alerts."
        )
    except (IndexError, ValueError):
        await update.message.reply_text(
            "Usage: /set_min_confidence <0.0–1.0>\n"
            "Examples:\n"
            "  /set_min_confidence 0.6  → only 60%+ conf alerts\n"
            "  /set_min_confidence 0.8  → only 80%+ conf alerts\n"
            "  /set_min_confidence 0.0  → all alerts (no filter)"
        )

async def set_fixed_notional(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not config_mgr:
        await update.message.reply_text("❌ Config manager not available")
        return
    try:
        val = float(context.args[0])
        if val <= 0:
            await update.message.reply_text("❌ Value must be positive")
            return
        config_mgr.set('paper_trading.fixed_notional_usd', val)
        await update.message.reply_text(f"✅ fixed_notional_usd set to ${val}")
    except (IndexError, ValueError):
        await update.message.reply_text("Usage: /set_fixed_notional <usd>")

async def set_margin_leverage(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not config_mgr:
        await update.message.reply_text("❌ Config manager not available")
        return
    try:
        val = float(context.args[0])
        if not (1 <= val <= 100):
            await update.message.reply_text("❌ Leverage must be between 1 and 100")
            return
        config_mgr.set('paper_trading.margin_leverage', val)
        await update.message.reply_text(f"✅ margin_leverage set to 1:{int(val)}")
    except (IndexError, ValueError):
        await update.message.reply_text("Usage: /set_margin_leverage <ratio>")

async def set_ohlcv_limit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Set OHLCV fetch limit (bars per scan)."""
    if not config_mgr:
        await update.message.reply_text("❌ Config manager not available")
        return
    try:
        val = int(context.args[0])
        if not (100 <= val <= 5000):
            await update.message.reply_text("❌ Value must be between 100 and 5000 bars")
            return
        config_mgr.set('data_fetch.ohlcv_limit', val)
        await update.message.reply_text(f"✅ ohlcv_limit set to {val} bars")
    except (IndexError, ValueError):
        await update.message.reply_text("Usage: /set_ohlcv_limit <100-5000>")

async def set_timeframes(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Set timeframes to scan (comma-separated, e.g., 4h,1h,15m)."""
    if not config_mgr:
        await update.message.reply_text("❌ Config manager not available")
        return
    try:
        tfs = [tf.strip() for tf in context.args[0].split(',') if tf.strip()]
        if not tfs:
            await update.message.reply_text("❌ Provide at least one timeframe")
            return
        config_mgr.set('data_fetch.timeframes', tfs)
        await update.message.reply_text(f"✅ timeframes set to: {', '.join(tfs)}")
    except (IndexError, ValueError):
        await update.message.reply_text("Usage: /set_timeframes <4h,1h,15m>")

async def set_min_24h_volume(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Set minimum 24h volume filter threshold (USD) and enable filter."""
    if not config_mgr:
        await update.message.reply_text("❌ Config manager not available")
        return
    try:
        val = float(context.args[0])
        if val <= 0:
            await update.message.reply_text("❌ Value must be positive")
            return
        config_mgr.set('volume_filter.min_24h_volume_usd', val)
        if not config_mgr.get('volume_filter.enabled', False):
            config_mgr.set('volume_filter.enabled', True)
        read_val = config_mgr.get('volume_filter.min_24h_volume_usd')
        enabled = config_mgr.get('volume_filter.enabled')
        await update.message.reply_text(f"✅ min_24h_volume_usd set to ${val:,.0f}\nRead-back: ${read_val:,.0f}\nVolume filter enabled: {enabled}")
        logger.info(f"Set volume_filter: min_24h_volume_usd={val}, enabled={enabled}")
    except (IndexError, ValueError):
        await update.message.reply_text("Usage: /set_min_24h_volume <usd>")

async def toggle_volume_filter(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not config_mgr:
        await update.message.reply_text("❌ Config manager not available")
        return
    current = config_mgr.get('volume_filter.enabled', False)
    new = not current
    config_mgr.set('volume_filter.enabled', new)
    state = "ENABLED" if new else "DISABLED"
    await update.message.reply_text(f"✅ Volume filter is now {state}")

async def set_scan_interval(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Set scan interval in minutes (cron)."""
    if not config_mgr:
        await update.message.reply_text("❌ Config manager not available")
        return
    try:
        val = int(context.args[0])
        if not (1 <= val <= 1440):
            await update.message.reply_text("❌ Value must be between 1 and 1440 minutes")
            return
        # Update config
        config_mgr.set('cron.scan_interval_minutes', val)
        await update.message.reply_text(f"✅ Config updated: scan_interval_minutes = {val}. Use /test_cron to apply to crontab.")
    except (IndexError, ValueError):
        await update.message.reply_text("Usage: /set_scan_interval <minutes>")

async def cron_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show current crontab entry for run_scan_all.sh."""
    try:
        result = subprocess.run(["/usr/bin/crontab", "-l"], capture_output=True, text=True, timeout=5)
        lines = result.stdout.splitlines() if result.returncode == 0 else []
        matching = [l for l in lines if "run_scan_all.sh" in l]
        if matching:
            await update.message.reply_text(f"<b>Current cron entry:</b>\n<code>{matching[0]}</code>", parse_mode="HTML")
        else:
            await update.message.reply_text("⚠️ No cron entry found for run_scan_all.sh")
    except FileNotFoundError:
        await update.message.reply_text("❌ crontab command not found on this system")
    except subprocess.TimeoutExpired:
        await update.message.reply_text("❌ Crontab read timed out")
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {e}")

async def test_cron(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Test writing crontab (dry-run). Shows what would be set."""
    try:
        # Read current config interval
        interval = config_mgr.get('cron.scan_interval_minutes', 60)
        cron_line = f"*/{interval} * * * * cd /root/.openclaw/workspace/projects/crypto-liquidity-hunter && ./run_scan_all.sh >> /dev/null 2>&1"
        # Try reading crontab
        result = subprocess.run(["/usr/bin/crontab", "-l"], capture_output=True, text=True, timeout=5)
        lines = result.stdout.splitlines() if result.returncode == 0 else []
        new_lines = [l for l in lines if "run_scan_all.sh" not in l]
        new_lines.append(cron_line)
        # Don't actually install; just show what would be set
        preview = "\n".join(new_lines)
        await update.message.reply_text(f"<b>Would set crontab to:</b>\n<code>{preview}</code>", parse_mode="HTML")
        # Also offer to apply
        await update.message.reply_text("Confirm with /apply_cron to install this crontab.")
    except Exception as e:
        await update.message.reply_text(f"❌ Test failed: {e}")

async def apply_cron(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Apply the cron entry based on current config."""
    try:
        interval = config_mgr.get('cron.scan_interval_minutes', 60)
        cron_line = f"*/{interval} * * * * cd /root/.openclaw/workspace/projects/crypto-liquidity-hunter && ./run_scan_all.sh >> /dev/null 2>&1"
        result = subprocess.run(["/usr/bin/crontab", "-l"], capture_output=True, text=True, timeout=5)
        lines = result.stdout.splitlines() if result.returncode == 0 else []
        new_lines = [l for l in lines if "run_scan_all.sh" not in l]
        new_lines.append(cron_line)
        subprocess.run(["/usr/bin/crontab", "-"], input="\n".join(new_lines).encode(), check=True, capture_output=True, timeout=5)
        await update.message.reply_text(f"✅ Cron applied: run every {interval} minutes")
    except subprocess.CalledProcessError as e:
        await update.message.reply_text(f"❌ Failed to install crontab:\n<code>{e.stderr.decode() if e.stderr else 'unknown'}</code>", parse_mode="HTML")
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {e}")

async def update_pairs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Fetch all active Binance USDT spot pairs and update config."""
    await update.message.reply_text("🔄 Fetching all Binance USDT spot pairs...")
    try:
        from core.data_fetcher import MarketDataFetcher
        fetcher = MarketDataFetcher('binance')
        markets = fetcher.load_markets()
        usdt_pairs = []
        for symbol_key, market in markets.items():
            if market.get('active') and market.get('quote') == 'USDT' and market.get('spot'):
                usdt_pairs.append(f"binance:{market['symbol']}")
        usdt_pairs.sort()
        config_mgr.set('pairs', usdt_pairs)
        await update.message.reply_text(f"✅ Updated pairs: {len(usdt_pairs)} Binance USDT spot pairs added. Use /params to verify.")
    except Exception as e:
        logger.exception("Failed to update pairs")
        await update.message.reply_text(f"❌ Error: {e}")

def main():
    if not BOT_TOKEN:
        logger.error("BOT_TOKEN not set")
        sys.exit(1)

    app = Application.builder().token(BOT_TOKEN).build()
    
    # System commands
    app.add_handler(CommandHandler("restart_dashboard", restart_dashboard))
    app.add_handler(CommandHandler("restart_admin_bot", restart_admin_bot))
    app.add_handler(CommandHandler("dashboard_status", dashboard_status))
    app.add_handler(CommandHandler("scan_all", run_scan_all))
    app.add_handler(CommandHandler("logs", view_logs))
    app.add_handler(CommandHandler("help", help_command))
    
    # Parameter commands
    app.add_handler(CommandHandler("params", show_params))
    app.add_handler(CommandHandler("debug_config", debug_config))
    app.add_handler(CommandHandler("reload_config", reload_config))
    app.add_handler(CommandHandler("set_sweep_multiplier", set_sweep_multiplier))
    app.add_handler(CommandHandler("set_volume_multiplier", set_volume_multiplier))
    app.add_handler(CommandHandler("set_wick_ratio", set_wick_ratio))
    app.add_handler(CommandHandler("set_min_sweep_pct", set_min_sweep_pct))
    app.add_handler(CommandHandler("set_min_risk_reward", set_min_risk_reward))
    app.add_handler(CommandHandler("set_min_confidence", set_min_confidence))
    app.add_handler(CommandHandler("set_fixed_notional", set_fixed_notional))
    app.add_handler(CommandHandler("set_margin_leverage", set_margin_leverage))
    app.add_handler(CommandHandler("set_ohlcv_limit", set_ohlcv_limit))
    app.add_handler(CommandHandler("set_timeframes", set_timeframes))
    app.add_handler(CommandHandler("set_min_24h_volume", set_min_24h_volume))
    app.add_handler(CommandHandler("toggle_volume_filter", toggle_volume_filter))
    app.add_handler(CommandHandler("set_scan_interval", set_scan_interval))
    app.add_handler(CommandHandler("cron_status", cron_status))
    app.add_handler(CommandHandler("test_cron", test_cron))
    app.add_handler(CommandHandler("apply_cron", apply_cron))
    app.add_handler(CommandHandler("update_pairs", update_pairs))

    logger.info("Admin bot starting...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == '__main__':
    main()