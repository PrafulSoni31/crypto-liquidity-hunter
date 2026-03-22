#!/usr/bin/env python3
"""
Telegram Admin Bot for Liquidity Hunter.
Provides remote control: restart dashboard, trigger scans, view status.
"""
import os
import sys
import subprocess
import logging
from pathlib import Path
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

# === CONFIG ===
ADMIN_USER_ID = 686482312  # Your Telegram user ID (change if needed)
BOT_TOKEN = "8663125030:AAHO1AIHTTObsj4exqoEc82935zhrYxO7Ys"
PROJECT_ROOT = Path("/root/.openclaw/workspace/projects/crypto-liquidity-hunter")

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(name)s %(levelname)s: %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger(__name__)

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

@auth_required
async def restart_dashboard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Restart the dashboard service with cache clear."""
    await update.message.reply_text("🔄 Restarting dashboard and clearing caches...")
    try:
        # Clear Python cache
        subprocess.run(
            ["find", str(PROJECT_ROOT), "-type", "d", "-name", "__pycache__", "-exec", "rm", "-rf", "{}", "+"],
            capture_output=True, text=True, timeout=10
        )
        subprocess.run(
            ["find", str(PROJECT_ROOT), "-name", "*.pyc", "-delete"],
            capture_output=True, text=True, timeout=10
        )
        # Restart service
        subprocess.run(["systemctl", "restart", "liquidity-hunter-dashboard.service"], check=True, capture_output=True, text=True)
        await update.message.reply_text(
            "✅ Dashboard restarted with clean cache.\n\n"
            "📌 <b>Next step:</b> Hard refresh your browser to see updates:\n"
            "• Chrome/Windows: <code>Ctrl + Shift + R</code>\n"
            "• Mac: <code>Cmd + Shift + R</code>\n"
            "Or open in incognito window.",
            parse_mode="HTML"
        )
    except subprocess.CalledProcessError as e:
        await update.message.reply_text(f"❌ Restart failed:\n<code>{e.stderr}</code>", parse_mode="HTML")
    except subprocess.TimeoutExpired:
        await update.message.reply_text("⚠️ Cache clear timed out, but dashboard restart may have succeeded.")

@auth_required
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

@auth_required
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

@auth_required
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

@auth_required
async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show help."""
    text = """
<b>🤖 Liquidity Hunter Admin Bot</b>

Commands:
/restart_dashboard — Restart dashboard service
/dashboard_status — Show dashboard status
/scan_all — Trigger full scan now (alerts will be sent)
/logs — Show recent dashboard logs
/help — This message

Only admin can use these commands.
"""
    await update.message.reply_text(text, parse_mode="HTML")

def main():
    if not BOT_TOKEN:
        logger.error("BOT_TOKEN not set")
        sys.exit(1)

    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("restart_dashboard", restart_dashboard))
    app.add_handler(CommandHandler("dashboard_status", dashboard_status))
    app.add_handler(CommandHandler("scan_all", run_scan_all))
    app.add_handler(CommandHandler("logs", view_logs))
    app.add_handler(CommandHandler("help", help_command))

    logger.info("Admin bot starting...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == '__main__':
    main()
