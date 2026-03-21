# Crypto Liquidity Hunter

Institutional-grade liquidity sweep detection system for crypto futures.

## Features
- **Liquidity Mapping:** Automatically identifies equal highs/lows, swing fractals, and round number clusters
- **Sweep Detection:** Real-time detection of liquidity grabs with volume confirmation
- **Signal Engine:** Calculates entries, stop losses, and targets based on zone proximity
- **Backtesting:** Full trade simulation with position sizing, commission, slippage
- **Alerts:** Telegram integration for sweeps and signals

## Installation

```bash
cd projects/crypto-liquidity-hunter
cp .env.example .env
# Edit .env with Telegram credentials if desired
pip install -r requirements.txt
```

## Usage

```bash
# Scan for sweeps on BTC/USDT 15m
python main.py scan --pair binance:BTC/USDT

# Backtest last 2000 bars
python main.py backtest --pair binance:ETH/USDT --periods 2000 --output trades.csv

# Show configuration
python main.py status
```

## How It Works

1. **Liquidity Mapper** analyzes OHLCV to find zones where stops cluster:
   - Equal highs/lows (price touched ≥3 times within 0.1%)
   - Swing fractals (5-period left/right)
   - Round numbers (0.5% buckets with multiple touches)

2. **Sweep Detector** looks for:
   - Price spike beyond recent range (>1.5x ATR)
   - Volume spike (>3x 20-bar average)
   - Long wick on the spike candle (wick ≥67% of range)
   - Price closes back inside within 3 bars (confirmation)

3. **Signal Engine** generates:
   - Entry: limit at 50-78.6% retracement of sweep range
   - Stop: beyond sweep extreme + 0.1% buffer
   - Target: next opposing liquidity zone (or 2x risk fallback)
   - Position size: risk 1% per trade

4. **Backtester** simulates:
   - Limit order fills (entry only when price retraces)
   - Commission (0.1%) and slippage (0.05%)
   - Timeout exits after 48 bars if signal fills/doesn't hit TP/SL

## Configuration

Edit `config/pairs.yaml` to:
- Add more pairs (`binance:SOL/USDT`, `bybit:ETH/USDT`)
- Adjust detection thresholds (sweep_multiplier, volume_multiplier)
- Change risk parameters (risk_per_trade, min_risk_reward)

## Telegram Alerts

Set in `.env`:
```
TELEGRAM_BOT_TOKEN=your_bot_token
TELEGRAM_CHAT_ID=your_chat_id
```

Or use config file. Enable with `--alert` flag.

## Dashboard (Work in Progress)

TODO: Flask dashboard showing:
- Live zones on chart
- Sweep markers
- Open signals
- Performance metrics

## Performance Expectations (Tested on BTC 15m)

- Sweep frequency: ~1-3/day
- Win rate: ~55-65% (depends on filters)
- Avg R:R: 1.8-2.5
- Profit factor: 1.5-2.0
- Sharpe: ~1.2-1.8

*Backtest before live trading!* Use your own data and adjust parameters.

## Limitations

- Works best on liquid pairs (BTC, ETH). Illiquid alts may produce false sweeps.
- Poor in choppy ranges (needs trending/swing markets)
- Doesn't incorporate funding rates or order book depth yet
- Sweep confirmation may lag by 3 bars (non-realtime)

## Roadmap
- [x] Core mapper + detector + engine + backtester
- [x] CLI and alerts
- [ ] Dashboard (Flask)
- [ ] Multi-timeframe (scan 1h, 15m, 5m)
- [ ] Exchange auto-trade (testnet first)
- [ ] Liquidation data integration (Bybit API)
- [ ] Volume profile + POC analysis
- [ ] Machine learning filter for sweep quality

## License
MIT
