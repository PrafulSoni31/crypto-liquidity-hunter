#!/usr/bin/env python3
"""
Send OI Momentum Alerts to Charlie via Telegram.
Reads scanner output (buy_signals / sell_signals keys) and sends directly via Bot API.

Fixed: key mismatch (scanner uses buy_signals/sell_signals, not 'signals')
Fixed: IST time display
Added: direct Telegram delivery with Bot API
"""

import json
import os
import sys
import requests
from datetime import datetime, timezone, timedelta
from typing import List, Dict

# ─── Config ────────────────────────────────────────────────────────────────────
BOT_TOKEN  = "8734017273:AAG5CQL90qARQPsB9Qr0JOcf4w-0tSBGGXY"  # ⚡ SHIVA OI MOMENTUM ALERT bot
CHARLIE_ID = "686482312"
IST        = timezone(timedelta(hours=5, minutes=30))
CACHE_DIR  = "projects/trading-bot/nse_oialerts/cache"
ALERT_DIR  = "projects/trading-bot/nse_oialerts/alerts_sent"
WORKSPACE  = "/root/.openclaw/workspace"
# ──────────────────────────────────────────────────────────────────────────────


def now_ist() -> datetime:
    return datetime.now(IST)


def load_signals(trade_type: str) -> Dict:
    """Load today's scanner output. Returns dict with buy_signals and sell_signals lists."""
    date_str = now_ist().strftime('%Y%m%d')
    path = os.path.join(WORKSPACE, CACHE_DIR, f"{trade_type.upper()}_{date_str}.json")

    if not os.path.exists(path):
        print(f"⚠️  Cache file not found: {path}")
        return {"buy_signals": [], "sell_signals": []}

    with open(path) as f:
        data = json.load(f)

    # Handle both old schema ('signals') and new schema ('buy_signals'/'sell_signals')
    if "signals" in data and "buy_signals" not in data:
        signals = data["signals"]
        buys  = [s for s in signals if s.get("signal_type") == "LONG_BUILDUP"]
        sells = [s for s in signals if s.get("signal_type") == "SHORT_BUILDUP"]
        data["buy_signals"]  = buys
        data["sell_signals"] = sells

    data.setdefault("buy_signals",  [])
    data.setdefault("sell_signals", [])
    return data


def format_ce_pe_block(s: Dict) -> str:
    """
    Render CE/PE OI analysis block from cached signal dict.
    CE/PE interpretation:
      CE OI ↑ = Call writing = Resistance (bearish pressure)
      PE OI ↑ = Put writing  = Support    (bullish pressure)
      CE OI ↓ = Calls exiting = Bullish
      PE OI ↓ = Puts exiting  = Bearish
    """
    ce_pe_bias = s.get("ce_pe_bias", "N/A")
    pcr        = s.get("pcr", 0.0)

    if ce_pe_bias == "N/A" or not pcr:
        return "  🔲 Options: N/A\n"

    ce_chg = s.get("ce_oi_change", 0.0)
    pe_chg = s.get("pe_oi_change", 0.0)

    ce_arrow   = "↑ Building" if ce_chg >= 5 else ("↓ Unwinding" if ce_chg <= -5 else "→ Stable")
    pe_arrow   = "↑ Building" if pe_chg >= 5 else ("↓ Unwinding" if pe_chg <= -5 else "→ Stable")
    ce_meaning = "⚠️ Resistance" if ce_chg >= 5 else ("✅ Easing" if ce_chg <= -5 else "")
    pe_meaning = "✅ Support"    if pe_chg >= 5 else ("⚠️ Weakening" if pe_chg <= -5 else "")

    bias_emoji = {"bullish": "🟢", "bearish": "🔴", "neutral": "⚪"}.get(ce_pe_bias, "⚪")
    pcr_label  = "Bullish" if pcr >= 1.2 else ("Bearish" if pcr <= 0.8 else "Neutral")

    max_pain = s.get("max_pain")
    mp_txt   = f"  💥 Max Pain: ₹{max_pain:.0f}\n" if max_pain else ""

    return (
        f"  📋 *Options Chain:*\n"
        f"  • CE OI: {ce_arrow} ({ce_chg:+.1f}%) {ce_meaning}\n"
        f"  • PE OI: {pe_arrow} ({pe_chg:+.1f}%) {pe_meaning}\n"
        f"  • PCR: {pcr:.2f} → {pcr_label}\n"
        f"  • Bias: {bias_emoji} {ce_pe_bias.upper()}\n"
        f"{mp_txt}"
    )


def format_signal_block(s: Dict, rank: int) -> str:
    is_buy     = s.get("signal_type", "") == "LONG_BUILDUP"
    emoji      = "🟢" if is_buy else "🔴"
    action     = "BUY (buy CE / sell PE)" if is_buy else "SELL (buy PE / sell CE)"
    conviction = s.get("conviction_score", 0)
    aligned    = s.get("options_aligned", False)
    align_tag  = " ✅ *Options Aligned*" if aligned else ""
    deliv      = s.get("delivery_pct")
    deliv_txt  = f"\n  📦 Delivery: {deliv:.0f}%" if deliv else ""

    ce_pe_block = format_ce_pe_block(s)

    return (
        f"\n{emoji} *{rank}. {s['symbol']}*  ⭐ {conviction:.0f}/100{align_tag}\n"
        f"`━━━━━━━━━━━━━━━━━━━━━`\n"
        f"📌 *Action:*    {action}\n"
        f"💰 *Entry:*     ₹{s['current_price']}\n"
        f"🎯 *Target:*    ₹{s['target']}\n"
        f"🛑 *Stop Loss:* ₹{s['stop_loss']}\n"
        f"⚖️ *R:R:*       1:{s['risk_reward']:.1f}\n"
        f"📊 Price: {s['price_change_pct']:+.2f}%  |  OI: {s['oi_change_pct']:+.2f}%  |  Vol: {s['volume_spike']:.1f}×{deliv_txt}\n"
        f"{ce_pe_block}"
        f"💬 _{s.get('reasoning','')}_\n"
    )


def build_message(data: Dict, trade_type: str) -> str:
    now        = now_ist()
    buys       = data["buy_signals"]
    sells      = data["sell_signals"]
    total      = len(buys) + len(sells)
    type_upper = trade_type.upper()

    trade_emoji = {"INTRADAY": "⚡", "BTST": "🌙", "STBT": "🌙"}.get(type_upper, "📊")

    header = (
        f"{trade_emoji} *SHIVA OI MOMENTUM — {type_upper}*\n"
        f"📅 {now.strftime('%A, %d %b %Y')}  🕐 {now.strftime('%I:%M %p IST')}\n"
        f"{'─'*30}\n"
    )

    if total == 0:
        body = (
            "⚠️ *No high-conviction setups today*\n\n"
            "Market showing mixed OI signals.\n"
            "Better to stay flat than force a trade.\n\n"
            "_💡 No trade is better than a bad trade_ 🎃"
        )
        return header + body

    body = f"✅ *{total} high-conviction setup(s) found*\n"
    body += f"🟢 BUY: {len(buys)}   🔴 SELL: {len(sells)}\n"

    rank = 1
    for s in buys:
        body += format_signal_block(s, rank)
        rank += 1
    for s in sells:
        body += format_signal_block(s, rank)
        rank += 1

    footer = (
        "\n`━━━━━━━━━━━━━━━━━━━━━`\n"
        "📖 *CE/PE OI Guide:*\n"
        "• CE OI ↑ = Call writing = *Resistance* (bearish)\n"
        "• PE OI ↑ = Put writing  = *Support* (bullish)\n"
        "• CE OI ↓ = Calls exiting = *Bullish*\n"
        "• PE OI ↓ = Puts exiting  = *Bearish*\n"
        "• PCR > 1.2 = Bullish  |  PCR < 0.8 = Bearish\n\n"
        "📚 *OI Signal Rules:*\n"
        "```\n"
        "Price↑ + OI↑ = Long Buildup  ✅ BUY\n"
        "Price↓ + OI↑ = Short Buildup ✅ SELL\n"
        "Price↑ + OI↓ = Short Covering ❌ Avoid\n"
        "Price↓ + OI↓ = Long Unwinding ❌ Avoid\n"
        "```\n"
        "⚠️ _Risk max 2% per trade. Always check Nifty trend._\n"
        "🎃 _Shiva — OI + Options confirmed, no noise._"
    )

    return header + body + footer


def send_telegram(message: str) -> bool:
    """Send message directly via Telegram Bot API."""
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id":    CHARLIE_ID,
        "text":       message,
        "parse_mode": "Markdown"
    }
    try:
        r = requests.post(url, json=payload, timeout=15)
        if r.status_code == 200:
            print(f"✅ Telegram alert delivered to Charlie")
            return True
        else:
            print(f"❌ Telegram error {r.status_code}: {r.text[:200]}")
            # Try plain text fallback (strip markdown)
            payload["parse_mode"] = ""
            r2 = requests.post(url, json=payload, timeout=15)
            if r2.status_code == 200:
                print(f"✅ Delivered (plain text fallback)")
                return True
            return False
    except Exception as e:
        print(f"❌ Telegram send failed: {e}")
        return False


def save_alert(message: str, trade_type: str):
    """Save alert text to file for records."""
    os.makedirs(os.path.join(WORKSPACE, ALERT_DIR), exist_ok=True)
    ts   = now_ist().strftime('%Y%m%d_%H%M%S')
    path = os.path.join(WORKSPACE, ALERT_DIR, f"{trade_type}_{ts}.txt")
    with open(path, "w") as f:
        f.write(message)
    print(f"💾 Alert saved: {path}")


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("trade_type", choices=["intraday", "btst", "stbt"])
    parser.add_argument("--dry-run", action="store_true", help="Print but don't send")
    args = parser.parse_args()

    # Change to workspace so relative paths work
    os.chdir(WORKSPACE)

    print(f"\n{'='*50}")
    print(f"🎃 SHIVA OI ALERT SENDER — {args.trade_type.upper()}")
    print(f"{'='*50}")

    data    = load_signals(args.trade_type)
    message = build_message(data, args.trade_type)

    print("\n📨 Message preview:\n")
    print(message)
    print()

    save_alert(message, args.trade_type)

    if args.dry_run:
        print("🔍 Dry-run mode — not sending to Telegram")
    else:
        send_telegram(message)


if __name__ == "__main__":
    main()
