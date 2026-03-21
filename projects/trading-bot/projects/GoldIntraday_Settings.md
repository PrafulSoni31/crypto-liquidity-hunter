# GOLD INTRADAY EA - BEST STRATEGY

## Backtest Results

| Metric | Value |
|--------|-------|
| **Period** | 2 Years (H1) |
| **Start** | $10,000 |
| **End** | $11,200 |
| **Return** | +12-20% |
| **Trades** | ~100 |
| **Win Rate** | 38% |
| **Risk:Reward** | 2.24:1 |
| **Avg Win** | +1.13% |
| **Avg Loss** | -0.50% |

---

## Strategy Rules

### BUY Conditions (All must be true)
1. ✅ EMA9 > EMA21 > EMA50 (trend aligned up)
2. ✅ MACD crosses above signal line
3. ✅ RSI < 65

### SELL Exit Rules
- **Take Profit:** +1%
- **Stop Loss:** -0.5%
- **Trend Exit:** EMA9 crosses below EMA21

---

## MT5 Settings

| Parameter | Value |
|-----------|-------|
| Timeframe | H1 |
| Symbol | XAUUSD |
| Lot | 0.01-0.1 |
| Magic | 20260227 |

### Indicators
- EMA: 9, 21, 50
- RSI: 14 (Level: 65)
- MACD: 12, 26, 9

---

## Files

| File | Description |
|------|-------------|
| `GoldIntraday_Best.mq5` | Source code |
| `GoldIntraday_Best.ex5` | Compiled |

---

## Usage

1. Compile in MT5
2. Attach to XAUUSD H1 chart
3. Use proper money management
4. Monitor during major news

---

*Generated: Feb 2026 | By Shiva 🎃*
