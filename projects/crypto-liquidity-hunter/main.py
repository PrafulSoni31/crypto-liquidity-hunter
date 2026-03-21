#!/usr/bin/env python3
"""
Crypto Liquidity Hunter - Main CLI
Entry point for scanning, backtesting, and serving the dashboard.
"""
import argparse
import yaml
import os
import sys
import pandas as pd
from datetime import datetime, timedelta
import logging
from dotenv import load_dotenv

# Add project root to path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.data_fetcher import MarketDataFetcher
from core.liquidity_mapper import LiquidityMapper
from core.sweep_detector import SweepDetector
from core.signal_engine import SignalEngine
from core.backtester import Backtester
from alerts.telegram import AlertDispatcher

# Load config
PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(PROJECT_ROOT, 'config', 'pairs.yaml')
load_dotenv(os.path.join(PROJECT_ROOT, '.env'))

def load_config():
    with open(CONFIG_PATH, 'r') as f:
        return yaml.safe_load(f)

def setup_logging():
    log_level = os.getenv('LOG_LEVEL', 'INFO').upper()
    logging.basicConfig(
        level=getattr(logging, log_level),
        format='%(asctime)s %(name)s %(levelname)s: %(message)s',
        datefmt='%H:%M:%S'
    )

def cmd_scan(args):
    """Scan a specific pair for liquidity zones and sweeps."""
    config = load_config()
    setup_logging()

    pair = args.pair if args.pair else config['pairs'][0]
    exchange_str, symbol = pair.split(':', 1) if ':' in pair else ('binance', pair)

    fetcher = MarketDataFetcher(exchange_str)
    df = fetcher.fetch_ohlcv(symbol, timeframe=args.tf or config['data_fetch']['timeframe'], limit=args.limit or config['data_fetch']['ohlcv_limit'])
    atr = fetcher.calculate_atr(df, period=config['data_fetch']['atr_period'])

    mapper = LiquidityMapper(
        equal_touch_tolerance=config['liquidity_mapper']['equal_touch_tolerance'],
        swing_lookback=config['liquidity_mapper']['swing_lookback'],
        round_tolerance=config['liquidity_mapper']['round_tolerance'],
        min_swing_strength=config['liquidity_mapper']['min_swing_strength']
    )
    zones = mapper.map_liquidity(df)

    detector = SweepDetector(
        sweep_multiplier=config['sweep_detector']['sweep_multiplier'],
        volume_multiplier=config['sweep_detector']['volume_multiplier'],
        confirmation_bars=config['sweep_detector']['confirmation_bars'],
        wick_ratio=config['sweep_detector']['wick_ratio'],
        min_sweep_pct=config['sweep_detector']['min_sweep_pct']
    )
    sweeps = detector.detect_sweeps(df, atr, zones)

    # Generate signals for recent sweeps
    engine = SignalEngine(
        risk_per_trade=config['signal_engine']['risk_per_trade'],
        retracement_levels=config['signal_engine']['retracement_levels'],
        stop_buffer_pct=config['signal_engine']['stop_buffer_pct'],
        min_risk_reward=config['signal_engine']['min_risk_reward']
    )
    latest_price = df.iloc[-1]['close']
    signals = []
    for sweep in sweeps[-5:]:  # last 5 sweeps
        signal = engine.generate_signal(sweep, zones, latest_price, capital=args.capital or 10000)
        if signal:
            signals.append(signal)

    # Print report
    print(f"\n=== SCAN REPORT: {pair} ({df.index[-1]}) ===")
    print(f" Liquidity zones: {len(zones)}")
    print(f" Sweeps detected: {len(sweeps)}")
    print(f" New signals: {len(signals)}")
    print("\n--- Zones (top 10) ---")
    for z in zones[:10]:
        print(f"  {z.zone_type:12} price={z.price:,.2f} strength={z.strength} last={z.last_touch.date()}")
    print("\n--- Recent Sweeps (last 5) ---")
    for s in sweeps[-5:]:
        print(f"  {s.timestamp} {s.direction:5} sweep={s.sweep_price:,.2f} close={s.close_price:,.2f} confirmed={s.confirmed}")
    print("\n--- Active Signals ---")
    for sig in signals:
        print(f"  {sig.direction.upper()} Entry={sig.entry_price:,.2f} SL={sig.stop_loss:,.2f} TP={sig.target:,.2f} R:R={sig.risk_reward:.2f}")

    # Send alerts
    dispatcher = AlertDispatcher(config['alerts'])
    if args.alert:
        for s in sweeps[-3:]:
            dispatcher.send_sweep(asdict(s))
        for sig in signals:
            dispatcher.send_signal(sig)

def cmd_backtest(args):
    """Run backtest on historical data."""
    config = load_config()
    setup_logging()

    pair = args.pair if args.pair else config['pairs'][0]
    exchange_str, symbol = pair.split(':', 1) if ':' in pair else ('binance', pair)

    fetcher = MarketDataFetcher(exchange_str)
    df = fetcher.fetch_ohlcv(symbol, timeframe=args.tf or config['data_fetch']['timeframe'], limit=args.periods or 2000)
    atr = fetcher.calculate_atr(df, period=config['data_fetch']['atr_period'])

    mapper = LiquidityMapper(
        equal_touch_tolerance=config['liquidity_mapper']['equal_touch_tolerance'],
        swing_lookback=config['liquidity_mapper']['swing_lookback'],
        round_tolerance=config['liquidity_mapper']['round_tolerance'],
        min_swing_strength=config['liquidity_mapper']['min_swing_strength']
    )
    zones = mapper.map_liquidity(df)  # will be recalculated per bar in backtest

    detector = SweepDetector(
        sweep_multiplier=config['sweep_detector']['sweep_multiplier'],
        volume_multiplier=config['sweep_detector']['volume_multiplier'],
        confirmation_bars=config['sweep_detector']['confirmation_bars'],
        wick_ratio=config['sweep_detector']['wick_ratio'],
        min_sweep_pct=config['sweep_detector']['min_sweep_pct']
    )
    engine = SignalEngine(
        risk_per_trade=config['signal_engine']['risk_per_trade'],
        retracement_levels=config['signal_engine']['retracement_levels'],
        stop_buffer_pct=config['signal_engine']['stop_buffer_pct'],
        min_risk_reward=config['signal_engine']['min_risk_reward']
    )

    backtester = Backtester(
        initial_capital=args.capital or 10000,
        commission_pct=config['backtester']['commission_pct'],
        slippage_pct=config['backtester']['slippage_pct'],
        max_concurrent_trades=config['backtester']['max_concurrent_trades'],
        trade_timeout_bars=config['backtester']['timeout_bars']
    )

    result = backtester.run(df, atr, mapper, detector, engine)

    print(f"\n=== BACKTEST: {pair} ===")
    print(f"Periods: {len(df)} bars")
    print(f"Final Capital: ${result['final_capital']:,.2f}")
    print(f"Total Return: {result['total_return_pct']:.2f}%")
    print("\n--- Metrics ---")
    for k, v in result['metrics'].items():
        if k != 'exit_reasons':
            print(f" {k}: {v}")
    print("Exit Reasons:")
    for reason, count in result['metrics']['exit_reasons'].items():
        print(f"  {reason}: {count}")

    # Save trades
    if args.output:
        trades_df = pd.DataFrame(result['trades'])
        trades_df.to_csv(args.output, index=False)
        print(f"\nSaved trades to {args.output}")

    # Send alert
    dispatcher = AlertDispatcher(config['alerts'])
    if args.alert:
        dispatcher.send_backtest(result['metrics'])

def cmd_status(args):
    """Print configuration and status."""
    config = load_config()
    print("\n=== Crypto Liquidity Hunter Status ===")
    print(f"Active pairs: {', '.join(config['pairs'])}")
    print(f"Default timeframe: {config['data_fetch']['timeframe']}")
    print(f"Risk per trade: {config['signal_engine']['risk_per_trade']*100:.1f}%")
    print(f"Min R:R: {config['signal_engine']['min_risk_reward']}")
    print(f"Telegram alerts: {'enabled' if config['alerts']['telegram']['enabled'] else 'disabled'}")
    print("\nCommands:")
    print("  python main.py scan --pair binance:BTC/USDT")
    print("  python main.py backtest --pair binance:ETH/USDT --periods 2000")
    print("  python main.py status")

def main():
    parser = argparse.ArgumentParser(description='Crypto Liquidity Hunter')
    subparsers = parser.add_subparsers(dest='command', help='Command')

    # scan
    parser_scan = subparsers.add_parser('scan', help='Scan pair for sweeps & signals')
    parser_scan.add_argument('--pair', type=str, help='Pair (exchange:symbol)')
    parser_scan.add_argument('--tf', type=str, help='Timeframe (15m, 1h, etc)')
    parser_scan.add_argument('--limit', type=int, help='OHLCV bars')
    parser_scan.add_argument('--capital', type=float, help='Capital for sizing')
    parser_scan.add_argument('--alert', action='store_true', help='Send alerts')

    # backtest
    parser_bt = subparsers.add_parser('backtest', help='Backtest strategy')
    parser_bt.add_argument('--pair', type=str, help='Pair to backtest')
    parser_bt.add_argument('--tf', type=str, help='Timeframe')
    parser_bt.add_argument('--periods', type=int, help='Number of historical bars')
    parser_bt.add_argument('--capital', type=float, help='Initial capital')
    parser_bt.add_argument('--output', type=str, help='CSV output for trades')
    parser_bt.add_argument('--alert', action='store_true', help='Send Telegram backtest report')

    # status
    subparsers.add_parser('status', help='Show config')

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        return

    if args.command == 'scan':
        cmd_scan(args)
    elif args.command == 'backtest':
        cmd_backtest(args)
    elif args.command == 'status':
        cmd_status(args)

if __name__ == '__main__':
    main()
