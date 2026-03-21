#!/bin/bash
# Simple OI Momentum Alert Sender
# Runs scanner and sends results via Telegram

cd /root/.openclaw/workspace

# Run scanner
python3 projects/trading-bot/nse_oialerts/oi_momentum_scanner.py intraday --top 3 --save > /tmp/oi_scan.log 2>&1

# Get latest JSON file
LATEST_JSON=$(ls -t projects/trading-bot/nse_oialerts/cache/INTRADAY_*.json | head -1)

if [ -f "$LATEST_JSON" ]; then
    # Extract key data using Python
    python3 << 'EOF'
import json
import os

files = sorted([f for f in os.listdir('projects/trading-bot/nse_oialerts/cache/') if f.startswith('INTRADAY_')], reverse=True)
if not files:
    exit(1)

with open(f'projects/trading-bot/nse_oialerts/cache/{files[0]}') as f:
    data = json.load(f)

print(f"⚡ Shiva OI Momentum — {data['timestamp'][:10]} {data['timestamp'][11:16]} UTC")
print(f"")
print(f"🟢 BUY (Long Buildup):")
for s in data.get('buy_signals', [])[:2]:
    print(f"  {s['symbol']}: Conv {s['conviction_score']:.0f}% | ₹{s['current_price']} → ₹{s['target']:.2f} | Price {s['price_change_pct']:+.2f}% | OI +{s['oi_change_pct']:.2f}%")

print(f"")
print(f"🔴 SELL (Short Buildup):")
for s in data.get('sell_signals', [])[:3]:
    print(f"  {s['symbol']}: Conv {s['conviction_score']:.0f}% | ₹{s['current_price']} → ₹{s['target']:.2f} | Price {s['price_change_pct']:+.2f}% | OI +{s['oi_change_pct']:.2f}%")

print(f"")
print(f"🎃 Next scan in ~30 min")
EOF
fi
