# 🔥 Shiva OI Momentum Scanner

## Deep Research Stock Picks for BTST/STBT/Intraday Trading

**For:** Charlie (Prafulkumar Soni)  
**Market:** NSE India (F&O Segment)  
**Timezone:** IST (Indian Standard Time)

---

## 📅 Alert Schedule

| Time (IST) | Trade Type | Description |
|------------|-----------|-------------|
| **9:30 AM** | INTRADAY | Same-day exit opportunities |
| **3:15 PM** | BTST/STBT | Overnight positions (Buy Today Sell Tomorrow / Sell Today Buy Tomorrow) |

**Note:** Alerts run Monday-Friday only (market days). No weekend alerts.

---

## 🎯 Signal Methodology

### OI-Based Signal Framework (Charlie's Rules)

| Price | OI | Signal | Action |
|-------|-----|--------|--------|
| ↑ | ↑ | **Long Buildup** | ✅ BUY (Strong) |
| ↓ | ↑ | **Short Buildup** | ✅ SELL (Strong) |
| ↑ | ↓ | **Short Covering** | ❌ Avoid (Weak rally) |
| ↓ | ↓ | **Long Unwinding** | ❌ Avoid (Weak dip) |

### Conviction Scoring (0-100)

Each signal is scored based on:
- **Price Momentum** (max 30 pts)
- **OI Buildup Strength** (max 35 pts)
- **Volume Confirmation** (max 20 pts)
- **Directional Alignment** (max 15 pts)

**Only signals with conviction ≥ 60 are shared.**

---

## 📊 Data Sources

- **Primary:** NSE India OI Spurts API
- **Backup:** NSE F&O securities data
- **Real-time:** Price, OI, Volume data

---

## 🚀 How It Works

### Automated Daily Flow:

```
9:30 AM IST / 3:15 PM IST
        ↓
   Cron Trigger
        ↓
   OI Scanner Runs
        ↓
   Analyzes All F&O Stocks
        ↓
   Filters High-Conviction (>60)
        ↓
   Formats Telegram Alert
        ↓
   Sends to Charlie
```

---

## 📁 File Structure

```
nse_oialerts/
├── oi_momentum_scanner.py    # Main scanner engine
├── send_alert.py              # Telegram formatter
├── run_scanner.sh             # Shell runner
├── cache/                     # Daily signal storage
├── alerts_sent/               # Sent alert history
└── README.md                  # This file
```

---

## 🔧 Manual Usage

### Run Intraday Scan Now:
```bash
cd /root/.openclaw/workspace
python3 projects/trading-bot/nse_oialerts/oi_momentum_scanner.py intraday --top 5 --save
```

### Run BTST Scan Now:
```bash
python3 projects/trading-bot/nse_oialerts/oi_momentum_scanner.py btst --top 5 --save
```

### Send Test Alert:
```bash
python3 projects/trading-bot/nse_oialerts/send_alert.py intraday
```

---

## 🎃 Trader's Rules (Embedded in Every Alert)

1. **Risk Per Trade:** Maximum 2% of capital
2. **Position Sizing:** Calculate based on stop loss distance
3. **Market Context:** Always check Nifty 50 trend first
4. **Partial Profits:** Book 50% at 1:1 Risk:Reward
5. **Stop Loss:** Move to breakeven after 1:1 R:R achieved

---

## ⚠️ Disclaimer

> These alerts are generated based on Open Interest data analysis for educational purposes. Always perform your own analysis and risk management. Past performance does not guarantee future results.

---

## 📞 Support

**Creator:** Shiva 🎃  
**System:** OpenClaw Agent  
**Updates:** Automatic via cron jobs

---

*Happy Trading, Charlie! May the OI be with you.* 🚀
