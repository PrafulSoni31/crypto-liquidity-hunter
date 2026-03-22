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
from core.auto_trader import AutoTrader
from alerts.telegram import AlertDispatcher
from data.store import DataStore

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
    """Scan a specific pair for liquidity zones and sweeps across configured timeframes."""
    config = load_config()
    setup_logging()

    pair = args.pair if args.pair else config['pairs'][0]
    exchange_str, symbol = pair.split(':', 1) if ':' in pair else ('binance', pair)

    # Determine timeframes to scan
    if args.tf:
        timeframes = [args.tf]
    else:
        timeframes = config['data_fetch'].get('timeframes', [config['data_fetch'].get('timeframe', '1h')])

    all_results = []

    for tf in timeframes:
        fetcher = MarketDataFetcher(exchange_str)
        df = fetcher.fetch_ohlcv(symbol, timeframe=tf, limit=args.limit or config['data_fetch']['ohlcv_limit'])
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
        sig_cfg = config['signal_engine']
        paper_cfg = config.get('paper_trading', {})
        engine = SignalEngine(
            risk_per_trade=sig_cfg['risk_per_trade'],
            retracement_levels=sig_cfg['retracement_levels'],
            stop_buffer_pct=sig_cfg['stop_buffer_pct'],
            min_risk_reward=sig_cfg['min_risk_reward'],
            position_sizing=paper_cfg.get('position_sizing', 'risk_percent'),
            fixed_notional_usd=paper_cfg.get('fixed_notional_usd', 50.0),
            margin_leverage=paper_cfg.get('margin_leverage', 1.0),
            commission_pct=paper_cfg.get('commission_per_trade', 0.001)
        )
        latest_price = df.iloc[-1]['close']
        signals = []
        for sweep in sweeps[-5:]:  # last 5 sweeps
            signal = engine.generate_signal(sweep, zones, latest_price, capital=args.capital or 10000, pair=pair)
            if signal:
                signals.append(signal)

        result = {
            'pair': pair,
            'timeframe': tf,
            'current_price': latest_price,
            'zones': zones,
            'sweeps': sweeps,
            'signals': signals
        }
        all_results.append(result)

    # Print consolidated report
    print(f"\n=== SCAN REPORT: {pair} ===")
    for res in all_results:
        print(f"\n--- Timeframe: {res['timeframe']} ---")
        print(f" Price: ${res['current_price']:,.2f}")
        print(f" Liquidity zones: {len(res['zones'])}")
        print(f" Sweeps detected: {len(res['sweeps'])}")
        print(f" New signals: {len(res['signals'])}")
        print("\n Zones (top 5):")
        for z in res['zones'][:5]:
            print(f"  {z.zone_type:15} price={z.price:,.2f} strength={z.strength}")
        print("\n Recent Sweeps (last 5):")
        for s in res['sweeps'][-5:]:
            print(f"  {s.timestamp} {s.direction:5} sweep={s.sweep_price:,.2f} close={s.close_price:,.2f} confirmed={s.confirmed}")
        print("\n Active Signals:")
        for sig in res['signals']:
            print(f"  {sig.direction.upper()} Entry={sig.entry_price:,.2f} SL={sig.stop_loss:,.2f} TP={sig.target:,.2f} R:R={sig.risk_reward:.2f}")

    # Send alerts if requested
    dispatcher = AlertDispatcher(config['alerts'])
    if args.alert:
        for res in all_results:
            for s in res['sweeps'][-3:]:
                dispatcher.send_sweep({
                    'direction': s.direction,
                    'sweep_price': s.sweep_price,
                    'close_price': s.close_price,
                    'volume': s.volume,
                    'volume_ratio': s.volume_ratio,
                    'sweep_depth_pct': s.sweep_depth_pct,
                    'notes': s.notes
                })
            for sig in res['signals']:
                dispatcher.send_signal(sig)

    # Save to data store if needed
    # data_store = DataStore()
    # ... (store results)
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

def cmd_scan_all(args):
    """Scan all pairs across configured timeframes and send alerts for new signals."""
    config = load_config()
    setup_logging()
    pairs = config['pairs']
    timeframes = config['data_fetch'].get('timeframes', [config['data_fetch'].get('timeframe', '1h')])

    dispatcher = AlertDispatcher(config['alerts'])
    all_signals = []

    # Auto-trade setup (if enabled)
    trader = None
    open_positions = {}  # pair -> dict of order ids
    if args.auto_trade:
        trading_cfg = config.get('trading', {})
        if trading_cfg.get('enabled', False):
            # Get API keys from env
            api_key = os.getenv(trading_cfg.get('api_key_env', 'BINANCE_TESTNET_API_KEY'))
            api_secret = os.getenv(trading_cfg.get('api_secret_env', 'BINANCE_TESTNET_SECRET'))
            trader = AutoTrader(
                exchange_id=trading_cfg.get('exchange', 'binance'),
                testnet=trading_cfg.get('testnet', True),
                config={'apiKey': api_key, 'secret': api_secret}
            )
            trader.load_markets()
            logger.info("Auto-trader initialized on testnet")
        else:
            logger.warning("Auto-trade requested but trading.enabled is false in config")
            args.auto_trade = False
            trader = None

    for pair in pairs:
        exchange_str, symbol = pair.split(':', 1) if ':' in pair else ('binance', pair)
        for tf in timeframes:
            try:
                fetcher = MarketDataFetcher(exchange_str)
                df = fetcher.fetch_ohlcv(symbol, timeframe=tf, limit=config['data_fetch']['ohlcv_limit'])
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

                sig_cfg = config['signal_engine']
                paper_cfg = config.get('paper_trading', {})
                engine = SignalEngine(
                    risk_per_trade=sig_cfg['risk_per_trade'],
                    retracement_levels=sig_cfg['retracement_levels'],
                    stop_buffer_pct=sig_cfg['stop_buffer_pct'],
                    min_risk_reward=sig_cfg['min_risk_reward'],
                    position_sizing=paper_cfg.get('position_sizing', 'risk_percent'),
                    fixed_notional_usd=paper_cfg.get('fixed_notional_usd', 50.0),
                    margin_leverage=paper_cfg.get('margin_leverage', 1.0),
                    commission_pct=paper_cfg.get('commission_per_trade', 0.001)
                )
                latest_price = df.iloc[-1]['close']

                for sweep in sweeps[-5:]:
                    signal = engine.generate_signal(sweep, zones, latest_price, capital=args.capital or 10000, pair=pair)
                    if signal:
                        signal_record = {
                            'pair': pair,
                            'timeframe': tf,
                            'timestamp': sweep.timestamp,
                            'signal': signal
                        }
                        all_signals.append(signal_record)
                        # Send alert immediately
                        if args.alert:
                            dispatcher.send_signal(signal)
                        # Auto-trade execution
                        if trader and len(open_positions) < config.get('trading', {}).get('max_concurrent', 3):
                            try:
                                # Get Futures symbol ID
                                market = trader.exchange.market(pair)
                                symbol_id = market['id']
                                side = 'buy' if signal.direction == 'long' else 'sell'
                                entry_size = signal.position_size

                                # Market order for immediate fill
                                entry_order = trader.exchange.create_market_order(
                                    symbol_id, side, entry_size, params={'reduceOnly': False}
                                )
                                # Bracket orders
                                exit_side = 'sell' if signal.direction == 'long' else 'buy'
                                stop_order = trader.exchange.create_order(
                                    symbol_id, 'stop_market', exit_side, entry_size, None,
                                    params={'stopPrice': signal.stop_loss, 'reduceOnly': True, 'workingType': 'MARK_PRICE'}
                                )
                                tp_order = trader.exchange.create_order(
                                    symbol_id, 'take_profit_market', exit_side, entry_size, None,
                                    params={'stopPrice': signal.target, 'reduceOnly': True, 'workingType': 'MARK_PRICE'}
                                )
                                open_positions[pair] = {
                                    'entry_id': entry_order['id'],
                                    'stop_id': stop_order['id'],
                                    'tp_id': tp_order['id']
                                }
                                logger.info(f"Auto-traded {pair} {signal.direction} size={entry_size} entry_id={entry_order['id']}")
                            except Exception as e:
                                logger.error(f"Auto-trade failed for {pair}: {e}")
            except Exception as e:
                logger.error(f"Error scanning {pair} {tf}: {e}")
                continue

    # Save latest signals to cache for dashboard
    store = DataStore()
    # Convert TradeSignal objects to serializable dicts
    serializable_signals = []
    for item in all_signals:
        sig = item['signal']
        # Convert dataclass to dict if needed
        sig_dict = sig.__dict__ if hasattr(sig, '__dict__') else dict(sig)
        serializable_signals.append({
            'pair': item['pair'],
            'timeframe': item['timeframe'],
            'timestamp': item['timestamp'].isoformat() if hasattr(item['timestamp'], 'isoformat') else item['timestamp'],
            'direction': sig_dict['direction'],
            'entry': sig_dict['entry_price'],
            'sl': sig_dict['stop_loss'],
            'tp': sig_dict['target'],
            'rr': sig_dict['risk_reward'],
            'confidence': sig_dict['confidence'],
            'notional_usd': sig_dict.get('notional_usd', 0),
            'margin_required_usd': sig_dict.get('margin_required_usd', 0),
            'commission_estimated_usd': sig_dict.get('commission_estimated_usd', 0),
            'zone_strength': sig_dict.get('zone_strength', 0)
        })
    store.save_latest_signals(serializable_signals)

    # Print summary
    print(f"\n=== SCAN ALL: {len(pairs)} pairs, {len(timeframes)} timeframes ===")
    print(f"Total signals generated: {len(all_signals)}")
    for item in all_signals[-10:]:  # last 10
        sig = item['signal']
        print(f"  {item['pair']} {item['timeframe']} {sig.direction.upper()} Entry={sig.entry_price:.2f} SL={sig.stop_loss:.2f} TP={sig.target:.2f} R:R={sig.risk_reward:.2f}")

    return all_signals

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

    # scan-all
    parser_sa = subparsers.add_parser('scan-all', help='Scan all pairs across all timeframes')
    parser_sa.add_argument('--capital', type=float, help='Capital for sizing')
    parser_sa.add_argument('--alert', action='store_true', help='Send alerts for each signal')
    parser_sa.add_argument('--auto-trade', action='store_true', help='Place trades automatically (testnet)')

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
    elif args.command == 'scan-all':
        cmd_scan_all(args)
    elif args.command == 'status':
        cmd_status(args)

if __name__ == '__main__':
    main()
