# GOLD HYBRID V4 - MT5 SETTINGS & CODE

## 📊 Strategy Performance (Backtest)
| Metric | Value |
|--------|-------|
| **Return** | +28.5% |
| **Trades** | 12 |
| **Win Rate** | 58% |
| **Risk:Reward** | 4.3:1 |
| **Max Drawdown** | ~5% |

---

## ⚙️ MT5 EA SETTINGS

### Basic Settings
| Parameter | Value | Description |
|-----------|-------|-------------|
| Timeframe | H4 or Daily | Recommended: H4 |
| Symbol | XAUUSD | Gold |
| Lot Size | 0.01-0.1 | Adjust to your balance |
| Magic Number | 20260226 | Unique ID |

### Indicator Settings
| Indicator | Period | Value |
|-----------|--------|-------|
| MA Short | 20 | EMA |
| MA Medium | 50 | EMA |
| MA Long | 200 | EMA |
| RSI | 14 | Oversold: 35, Overbought: 65 |
| MACD | 12, 26, 9 | Standard |
| ATR | 14 | For stops |

### Trade Settings
| Parameter | Value |
|-----------|-------|
| Take Profit | 5% or 5 ATR |
| Stop Loss | 2% or 2 ATR |
| Max Spread | 30 points |

---

## 📋 BUY RULES (All must be true)

**Option 1 - Main Signal:**
1. ✅ Price > 200 MA (long-term trend up)
2. ✅ Price > 50 MA (medium-term trend up)
3. ✅ Price > 20 MA (short-term trend up)
4. ✅ MACD crosses ABOVE signal line
5. ✅ RSI < 65 (not overbought)

**Option 2 - RSI Bounce:**
1. ✅ Price > 200 MA
2. ✅ RSI < 35 (oversold)
3. ✅ Price > Previous close (momentum)

---

## 🛑 SELL RULES

1. **Take Profit:** Price rises +5% (or 5 ATR)
2. **Stop Loss:** Price falls -2% (or 2 ATR)
3. **Trend Exit:** Price drops below 50 MA
4. **RSI Exit:** RSI > 75 (optional)

---

## 📁 Files

| File | Description |
|------|-------------|
| `GoldHybridV4.ex5` | Compiled EA (ready to use) |
| `GoldHybridV4_Simple.mq5` | Source code (can modify) |
| `GoldHybridV4_Full.mq5` | Full version with more options |

---

## 💡 Usage Tips

1. **Start small:** Test with $10,000 demo account first
2. **Use proper lot sizing:** 0.01 lot per $1,000 recommended
3. **Watch during news:** Avoid trading during major announcements
4. **Timeframe matters:** H4 gives fewer but better signals than H1

---

*Generated: Feb 2026 | By Shiva 🎃*
