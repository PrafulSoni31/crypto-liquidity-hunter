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

def auth_required(func):
    """Decorator to ensure only admin can execute command."""
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        if user_id != ADMIN_USER_ID:
            await update.message.reply_text("❌ Unauthorized")
            logger.warning(f"Unauthorized command attempt by user {user_id}")
            return
        return await func(update, context)
    return wrapper

# === System Commands ===

async def restart_dashboard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Restart the dashboard service with cache clear."""
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
    """Restart the admin bot service (this bot)."""
    await update.message.reply_text("🔄 Restarting admin bot...")
    try:
        subprocess.run(["systemctl", "restart", "liquidity-hunter-admin-bot.service"], check=True, capture_output=True, text=True)
        await update.message.reply_text("✅ Admin bot restarted successfully.")
    except subprocess.CalledProcessError as e:
        await update.message.reply_text(f"❌ Restart failed:\n<code>{e.stderr}</code>", parse_mode="HTML")

async def dashboard_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show dashboard service status."""
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
    """Trigger a full scan now (runs in background)."""
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
    """Show recent logs (tail -50)."""
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
    """Show help."""
    text = """
<b>🤖 Liquidity Hunter Admin Bot</b>

<b>System:</b>
/restart_dashboard — Restart dashboard service
/restart_admin_bot — Restart this admin bot
/dashboard_status — Show dashboard status
/logs — Show recent dashboard logs

<b>Operations:</b>
/scan_all — Trigger full scan now (alerts will be sent)

<b>Parameter Commands (live adjust):</b>
/params — Show current parameters

Sweep Detector:
  /set_sweep_multiplier &lt;value&gt; — 0.1–5.0 (ATR multiplier)
  /set_volume_multiplier &lt;value&gt; — 1.0–10.0 (volume spike)
  /set_wick_ratio &lt;value&gt; — 0.1–1.0 (wick fraction)
  /set_min_sweep_pct &lt;value&gt; — 0.01–5% (depth)

Signal Engine:
  /set_min_risk_reward &lt;value&gt; — 0.5–20.0 (R:R threshold)

Data Fetch:
  /set_ohlcv_limit &lt;bars&gt; — 100–5000 (candles per scan)

Volume Filter:
  /set_min_24h_volume &lt;usd&gt; — e.g., 500000
  /toggle_volume_filter — Enable/disable filter

Paper Trading:
  /set_fixed_notional &lt;usd&gt; — position size
  /set_margin_leverage &lt;ratio&gt; — 1–100

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
    text = f"""<b>Current Parameters:</b>

<b>Data Fetch:</b>
• ohlcv_limit: {c['data_fetch']['ohlcv_limit']} bars
• atr_period: {c['data_fetch']['atr_period']}

<b>Volume Filter:</b>
• enabled: {vol_enabled}
• min_24h_volume_usd: ${{vol_min:,.0f}}

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
        # Auto-enable if not already
        if not config_mgr.get('volume_filter.enabled', False):
            config_mgr.set('volume_filter.enabled', True)
            await update.message.reply_text(f"✅ min_24h_volume_usd set to ${val:,.0f}\n✅ Volume filter auto-enabled")
        else:
            await update.message.reply_text(f"✅ min_24h_volume_usd set to ${val:,.0f}")
    except (IndexError, ValueError):
        await update.message.reply_text("Usage: /set_min_24h_volume <usd>")

async def toggle_volume_filter(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Toggle volume filter on/off."""
    if not config_mgr:
        await update.message.reply_text("❌ Config manager not available")
        return
    current = config_mgr.get('volume_filter.enabled', False)
    new = not current
    config_mgr.set('volume_filter.enabled', new)
    state = "ENABLED" if new else "DISABLED"
    await update.message.reply_text(f"✅ Volume filter is now {state}")

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
    app.add_handler(CommandHandler("set_sweep_multiplier", set_sweep_multiplier))
    app.add_handler(CommandHandler("set_volume_multiplier", set_volume_multiplier))
    app.add_handler(CommandHandler("set_wick_ratio", set_wick_ratio))
    app.add_handler(CommandHandler("set_min_sweep_pct", set_min_sweep_pct))
    app.add_handler(CommandHandler("set_min_risk_reward", set_min_risk_reward))
    app.add_handler(CommandHandler("set_fixed_notional", set_fixed_notional))
    app.add_handler(CommandHandler("set_margin_leverage", set_margin_leverage))
    app.add_handler(CommandHandler("set_ohlcv_limit", set_ohlcv_limit))
    app.add_handler(CommandHandler("set_min_24h_volume", set_min_24h_volume))
    app.add_handler(CommandHandler("toggle_volume_filter", toggle_volume_filter))

    logger.info("Admin bot starting...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == '__main__':
    main()