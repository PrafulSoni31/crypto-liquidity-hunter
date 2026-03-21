#!/usr/bin/env python3
"""
Send OI Momentum Alerts to Charlie via Telegram
This script is designed to be called by cron jobs.
"""

import json
import os
import sys
from datetime import datetime
from typing import List, Dict

# Charlie's Telegram ID
CHARLIE_ID = "686482312"


def load_latest_signals(trade_type: str) -> List[Dict]:
    """Load the most recent signals from cache."""
    cache_dir = "projects/trading-bot/nse_oialerts/cache"
    
    # Find the most recent file for this trade type
    date_str = datetime.now().strftime('%Y%m%d')
    filename = f"{cache_dir}/{trade_type.upper()}_{date_str}.json"
    
    if not os.path.exists(filename):
        print(f"No signals file found: {filename}")
        return []
    
    with open(filename, 'r') as f:
        data = json.load(f)
        return data.get('signals', [])


def format_alert_message(signals: List[Dict], trade_type: str) -> str:
    """Format the Telegram message."""
    
    emoji_map = {
        'LONG_BUILDUP': '🟢',
        'SHORT_BUILDUP': '🔴',
        'INTRADAY': '⚡',
        'BTST': '🌙',
        'STBT': '🌙'
    }
    
    trade_emoji = emoji_map.get(trade_type.upper(), '📊')
    
    # Determine session
    hour = datetime.now().hour
    if 9 <= hour < 12:
        session = "MORNING SESSION"
    elif 12 <= hour < 15:
        session = "AFTERNOON SESSION"
    else:
        session = "EVENING SESSION"
    
    header = f"""{trade_emoji} **SHIVA OI MOMENTUM - {trade_type.upper()}**

🔔 **{session}**
📅 {datetime.now().strftime('%A, %d %B %Y')}
⏰ {datetime.now().strftime('%I:%M %p IST')}
📊 **Deep Research OI-Based Picks**

"""
    
    if not signals:
        return header + """⚠️ **No High-Conviction Setups Today**

Market is showing mixed OI signals. Best to stay on sidelines or reduce position sizes.

💡 *Remember: No trade is better than a bad trade*

🎃 Shiva out."""
    
    body = f"✅ **Found {len(signals)} High-Conviction Setup(s)**\n\n"
    
    for i, signal in enumerate(signals, 1):
        direction_emoji = emoji_map.get(signal['signal_type'], '⚪')
        action = "BUY" if signal['signal_type'] == 'LONG_BUILDUP' else "SELL (Short)"
        
        body += f"""
**{i}. {direction_emoji} {signal['symbol']}** 
⭐ Conviction: {signal['conviction_score']:.0f}/100
━━━━━━━━━━━━━━━━━━━━━━
📈 **Action:** {action}
💰 **Entry:** ₹{signal['current_price']}
🎯 **Target:** ₹{signal['target']}
🛑 **Stop Loss:** ₹{signal['stop_loss']}
⚖️ **R:R Ratio:** 1:{signal['risk_reward']:.1f}

📉 **OI Analysis:**
• Price Move: {signal['price_change_pct']:+.2f}%
• OI Change: {signal['oi_change_pct']:+.2f}%
• Volume: {signal['volume_spike']:.1f}x average

📝 {signal['reasoning']}

"""
    
    footer = f"""
━━━━━━━━━━━━━━━━━━━━━━
🎃 **Trade Management Rules:**

1️⃣ **Risk Per Trade:** Max 2% of capital
2️⃣ **Position Size:** Calculate based on SL distance
3️⃣ **Market Context:** Check Nifty trend before entry
4️⃣ **Partial Profits:** Book 50% at 1:1 R:R
5️⃣ **Move SL:** To breakeven after 1:1 R:R

📚 **OI Signal Cheat Sheet:**
```
Price ↑ + OI ↑ = Long Buildup (STRONG BUY) ✅
Price ↓ + OI ↑ = Short Buildup (STRONG SELL) ✅
Price ↑ + OI ↓ = Short Covering (WEAK - Avoid) ❌
Price ↓ + OI ↓ = Long Unwinding (WEAK - Avoid) ❌
```

⚠️ *Disclaimer: These are research-based signals for educational purposes. Always do your own analysis and risk management.*

💪 Trade smart, Charlie! 
🎃 Shiva watching the markets for you.
"""
    
    return header + body + footer


def main():
    """Main function to send alert."""
    import argparse
    
    parser = argparse.ArgumentParser(description='Send OI alerts to Telegram')
    parser.add_argument('trade_type', choices=['intraday', 'btst', 'stbt'],
                        help='Type of trade alert')
    
    args = parser.parse_args()
    
    # Load signals
    signals = load_latest_signals(args.trade_type)
    
    # Format message
    message = format_alert_message(signals, args.trade_type)
    
    # Print message (will be captured by cron and sent via OpenClaw)
    print(message)
    
    # Also save to a file for reference
    alert_dir = "projects/trading-bot/nse_oialerts/alerts_sent"
    os.makedirs(alert_dir, exist_ok=True)
    
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    filename = f"{alert_dir}/{args.trade_type}_{timestamp}.txt"
    
    with open(filename, 'w') as f:
        f.write(message)
    
    print(f"\n💾 Alert saved to: {filename}")


if __name__ == "__main__":
    main()
