# Crypto Liquidity Hunter - Project Plan

## Overview
Build an institutional-grade liquidity hunting system for crypto futures/perps.
Detects liquidity sweeps, generates entries with calculated TPs/SLs, and backtests strategies.

## Architecture
```
crypto-liquidity-hunter/
├── core/
│   ├── liquidity_mapper.py      # Identify liquidity zones
│   ├── sweep_detector.py        # Detect sweep events
│   ├── signal_engine.py         # Entry/exit logic
│   └── backtester.py            # Historical simulation
├── data/
│   ├── fetch.py                 # Market data via ccxt
│   └── store.py                 # SQLite/CSV persistence
├── config/
│   └── pairs.yaml               # Trading pairs & params
├── alerts/
│   └── telegram.py              # Alert dispatcher
├── dashboard/
│   └── app.py                   # Flask web dashboard
├── tests/
├── requirements.txt
├── .env.example
└── README.md
```

## Phases

### Phase 1: Liquidity Mapper (Week 1)
Detect liquidity zones from OHLCV:
- Equal highs/lows (multi-touch levels)
- Recent swing highs/lows (fractals)
- Round numbers (configurable % buckets)
- Option expiry strikes (if data available)
- Store in `liquidity_zones` table

**Output:** JSON of zones with strength, distance, type

### Phase 2: Sweep Detector (Week 1)
Real-time + historical sweep detection:
- Price spike > threshold (% of ATR)
- Long wick exceeding previous high by X%
- Immediate close back inside range
- Volume spike (>3x avg)
- Sweep confirmed if closes within 50% of wick within N bars

**Output:** Sweep events (timestamp, direction, sweep_level, volume, pair)

### Phase 3: Signal Engine (Week 2)
After sweep → entry rules:
- Wait 1-3 candles for retrace confirmation
- Entry: limit at 50-78.6% retrace of sweep range
- Stop: beyond sweep extreme + buffer (0.1-0.3%)
- Target: next opposing liquidity zone (or 2x risk)
- Position sizing: risk per trade (default 1%)

**Filters:**
- Minimum sweep depth (ATR multiple)
- Minimum volume
- Time-of-day (avoid low liquidity periods)
- Exclude high-impact news windows

### Phase 4: Backtester (Week 2)
- Run historical sweep detection → signals → P&L
- Metrics: win rate, avg R:R, Sharpe, max DD
- Optimize sweep_threshold, retrace_entry, target_factor
- Slippage & fee modeling

### Phase 5: Alerts & Dashboard (Week 3)
- Telegram alerts: "SWEEP: BTC 15m long @ $X", "ENTRY: @ $Y", "TP/SL"
- Web dashboard: charts with zones, sweep markers, open signals
- Cron: scan every 5 minutes (intraday) or 1h (higher TF)

### Phase 6: Optional Auto-Trade (Week 4)
- Connect to exchange API (Binance/Bybit testnet first)
- Place limit orders with OCO (one-cancels-other)
- Track fills & P&L

## Data Sources
- **CCXT** for OHLCV + orderbook snapshots
- **Funding rates** (if available via exchange API)
- Store: SQLite (lightweight, portable)

## Risk Rules
- Max 3 concurrent trades
- Daily loss limit: -3%
- No trading first 15 min after sweep sweep (high volatility)
- Minimum liquidity zone strength: 2 touches

## Monitoring
- Telegram alerts on every sweep + entry + TP/SL
- Daily performance summary
- Zone refresh: recalc every 4h (zones shift)

## Deliverables
1. Core library (`crypto_liquidity_hunter/`)
2. CLI: `python -m cli scan BTC/USDT --tf 15m`
3. Backtest report generator
4. Dashboard (Flask)
5. Deployment scripts (cron + docker optional)

---

Let's start coding Phase 1 & 2.
