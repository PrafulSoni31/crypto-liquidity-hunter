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
from core.trade_executor import TradeExecutor
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
            commission_pct=paper_cfg.get('commission_per_trade', 0.001),
            min_sl_gap_pct=config.get('signal_execution', {}).get('min_sl_gap_pct', 0.5)
        )
        latest_price = df.iloc[-1]['close']
        tf_max_age = {'15m': 4, '1h': 12, '4h': 24, '1d': 72}
        max_sweep_age = tf_max_age.get(tf, 6)
        signals = []
        for sweep in sweeps[-10:]:  # check last 10 but age-filtered
            signal = engine.generate_signal(
                sweep, zones, latest_price,
                capital=args.capital or 10000,
                pair=pair,
                max_sweep_age_hours=max_sweep_age
            )
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
        min_risk_reward=config['signal_engine']['min_risk_reward'],
        min_sl_gap_pct=config.get('signal_execution', {}).get('min_sl_gap_pct', 0.5)
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
    logger = logging.getLogger(__name__)
    pairs = config['pairs']
    timeframes = config['data_fetch'].get('timeframes', [config['data_fetch'].get('timeframe', '1h')])

    # --- Volume Filter: skip pairs below 24h volume threshold ---
    vol_cfg = config.get('volume_filter', {})
    if vol_cfg.get('enabled', False):
        min_vol = vol_cfg.get('min_24h_volume_usd', 0)
        if min_vol > 0:
            # Determine exchange from first pair (assume all pairs use same exchange)
            first_pair = pairs[0] if pairs else 'binance:BTC/USDT'
            exchange_str = first_pair.split(':', 1)[0] if ':' in first_pair else 'binance'
            # Use futures exchange for volume check
            if exchange_str == 'binance':
                exchange_str = 'binanceusdm'
            
            # Batch-fetch all tickers in ONE API call (much faster than per-pair loop)
            fetcher = MarketDataFetcher(exchange_str)
            active_pairs = []
            logger.info(f"Volume filter: fetching all tickers in one call from {exchange_str}...")
            try:
                all_tickers = fetcher.exchange.fetch_tickers()  # single batch call
                vol_map = {}
                for sym, ticker in all_tickers.items():
                    quoteVolume = ticker.get('quoteVolume') or 0
                    vol_map[sym] = float(quoteVolume)

                before = len(pairs)
                for pair in pairs:
                    exchange_str, symbol = pair.split(':', 1) if ':' in pair else ('binance', pair)
                    # For futures, symbol format is like BTC/USDT:USDT
                    # Try multiple formats
                    vol = vol_map.get(symbol, 0)  # Try as-is (spot format)
                    if vol == 0:
                        # Try futures format: add :USDT suffix
                        vol = vol_map.get(symbol + ':USDT', 0)
                    if vol == 0:
                        # Try with USDC suffix for USDC pairs
                        vol = vol_map.get(symbol + ':USDC', 0)
                    if vol >= min_vol:
                        active_pairs.append(pair)
                    else:
                        logger.debug(f"Skip {pair}: vol ${vol:,.0f} < ${min_vol:,.0f}")
                pairs = active_pairs
                logger.info(f"Volume filter: {len(pairs)}/{before} pairs pass ${min_vol/1e6:.0f}M threshold")
            except Exception as e:
                logger.warning(f"Batch volume fetch failed ({e}), using all pairs")
    # -----------------------------------------------------------

    # Initialize data store for trade tracking
    store = DataStore()
    scan_start = datetime.utcnow()   # for duration tracking
    # Track latest prices for open trade exit checks
    latest_prices = {}

    dispatcher = AlertDispatcher(config['alerts'])
    all_signals = []

    # Activity logging
    try:
        from scheduler.activity_logger import log_event as _log
    except ImportError:
        def _log(evt, **kw): pass
    _log('SCAN_START', pairs_count=len(pairs), timeframes=timeframes)

    # Auto-trade setup — load active account from config
    trader = None
    trade_executor = None
    open_positions = {}  # pair -> dict of order ids
    active_account_id = config.get('active_account_id', None)
    exec_cfg        = config.get('signal_execution', {})
    exec_mode       = exec_cfg.get('mode', 'pending')          # 'pending' or 'immediate'
    entry_tol       = float(exec_cfg.get('entry_tolerance_pct', 0.3)) / 100
    auto_exec       = exec_cfg.get('auto_execute', True)
    min_sl_gap_pct  = float(exec_cfg.get('min_sl_gap_pct', 0.5)) / 100

    # ── Per-symbol entry cooldown (persisted to file) ────────────────────────
    # Prevents re-entering the same pair within 10 minutes of a previous entry.
    # Persisted to JSON file so it survives cron restarts (each scan is a fresh process).
    import time as _time_mod
    import json as _json_mod
    _COOLDOWN_FILE = os.path.join(PROJECT_ROOT, 'data', 'entry_cooldown.json')
    _ENTRY_COOLDOWN_SECS = 600   # 10 minutes

    def _load_cooldown() -> dict:
        try:
            with open(_COOLDOWN_FILE, 'r') as f:
                cd = _json_mod.load(f)
            # Prune expired entries
            now = _time_mod.time()
            return {k: v for k, v in cd.items() if now - v < _ENTRY_COOLDOWN_SECS}
        except Exception:
            return {}

    def _save_cooldown(cd: dict):
        try:
            with open(_COOLDOWN_FILE, 'w') as f:
                _json_mod.dump(cd, f)
        except Exception:
            pass

    _entry_cooldown = _load_cooldown()

    if active_account_id:
        try:
            trade_executor = TradeExecutor(account_id=active_account_id)
            acct = store.get_account(active_account_id)
            acct_name = acct['name'] if acct else f'#{active_account_id}'
            acct_mode = acct.get('mode', 'paper') if acct else 'paper'
            logger.info(f"Auto-executor: account '{acct_name}' mode={acct_mode} exec={exec_mode}")
        except Exception as e:
            logger.warning(f"Could not load active account {active_account_id}: {e}")
            trade_executor = None

    # Expire old pending signals at start of each scan run
    expired_count = store.expire_old_pending_signals()
    if expired_count > 0:
        logger.info(f"Expired {expired_count} pending signals")

    # Build shared components (reuse per pair to save memory)
    sig_cfg   = config['signal_engine']

    # ── Use LIVE trading config when account is live, paper otherwise ─────────
    # FIX: was always using paper_trading values even when account mode = 'live'
    _acct_mode = 'paper'
    if active_account_id:
        try:
            _acct = store.get_account(active_account_id)
            _acct_mode = _acct.get('mode', 'paper') if _acct else 'paper'
        except Exception:
            pass
    trade_cfg = config.get('live_trading' if _acct_mode == 'live' else 'paper_trading', {})
    logger.info(f"[scan-all] Using {'LIVE' if _acct_mode == 'live' else 'PAPER'} trading config "
                f"for signal sizing: notional=${trade_cfg.get('fixed_notional_usd')} "
                f"lev={trade_cfg.get('margin_leverage')}x")

    engine = SignalEngine(
        risk_per_trade=sig_cfg['risk_per_trade'],
        retracement_levels=sig_cfg['retracement_levels'],
        stop_buffer_pct=sig_cfg['stop_buffer_pct'],
        min_risk_reward=sig_cfg['min_risk_reward'],
        position_sizing=trade_cfg.get('position_sizing', 'fixed_notional'),
        fixed_notional_usd=trade_cfg.get('fixed_notional_usd', 20.0),
        margin_leverage=trade_cfg.get('margin_leverage', 10.0),
        commission_pct=trade_cfg.get('commission_per_trade', 0.001),
        require_confluence=config.get('signal_engine', {}).get('require_confluence', True),
        min_sl_gap_pct=config.get('signal_execution', {}).get('min_sl_gap_pct', 0.5)
    )
    mapper = LiquidityMapper(
        equal_touch_tolerance=config['liquidity_mapper']['equal_touch_tolerance'],
        swing_lookback=config['liquidity_mapper']['swing_lookback'],
        round_tolerance=config['liquidity_mapper']['round_tolerance'],
        min_swing_strength=config['liquidity_mapper']['min_swing_strength']
    )
    detector = SweepDetector(
        sweep_multiplier=config['sweep_detector']['sweep_multiplier'],
        volume_multiplier=config['sweep_detector'].get('volume_multiplier', 2.5),
        confirmation_bars=config['sweep_detector'].get('confirmation_bars', 5),
        wick_ratio=config['sweep_detector'].get('wick_ratio', 0.5),
        min_sweep_pct=config['sweep_detector']['min_sweep_pct'],
        min_body_ratio=config['sweep_detector'].get('min_body_ratio', 0.4),
        lookback_bars=config['sweep_detector'].get('lookback_bars', 24)
    )

    # Timeframe-aware sweep age limits
    tf_max_age = {'15m': 4, '1h': 12, '4h': 24, '1d': 72}

    for pair in pairs:
        exchange_str, symbol = pair.split(':', 1) if ':' in pair else ('binance', pair)
        # For futures, use binanceusdm exchange
        if exchange_str == 'binance':
            exchange_str = 'binanceusdm'
        for tf in timeframes:
            try:
                fetcher = MarketDataFetcher(exchange_str)
                df  = fetcher.fetch_ohlcv(symbol, timeframe=tf, limit=config['data_fetch']['ohlcv_limit'])
                atr = fetcher.calculate_atr(df, period=config['data_fetch']['atr_period'])

                zones  = mapper.map_liquidity(df)
                sweeps = detector.detect_sweeps(df, atr, zones)

                # Phase 2: Order Blocks + FVGs + HTF bias
                obs  = detector.detect_order_blocks(df)
                fvgs = detector.detect_fvgs(df)

                # HTF bias: fetch 4h data for trend context (only for sub-4h timeframes)
                htf_bias = 'neutral'
                if tf in ('15m', '1h'):
                    try:
                        df_htf   = fetcher.fetch_ohlcv(symbol, timeframe='4h', limit=100)
                        htf_bias = detector.get_htf_bias(df_htf)
                    except Exception as e:
                        logger.debug(f"HTF bias fetch failed for {symbol}: {e}")
                        htf_bias = 'neutral'

                latest_price = df.iloc[-1]['close']
                latest_prices[symbol] = latest_price

                max_sweep_age = tf_max_age.get(tf, 6)

                # Only process recent sweeps (last 10, age-filtered inside signal engine)
                for sweep in sweeps[-10:]:
                    signal = engine.generate_signal(
                        sweep, zones, latest_price,
                        capital=args.capital or 10000,
                        pair=pair,
                        max_sweep_age_hours=max_sweep_age,
                        htf_bias=htf_bias,
                        order_blocks=obs,
                        fvgs=fvgs
                    )
                    if signal:
                        # ── CONFIDENCE THRESHOLD CHECK ──────────────────────────────────────
                        # Primary source: signal_execution.min_confidence (execution bar)
                        # Fallback: alerts.telegram.min_confidence, then 0.7
                        _exec_cfg_conf = config.get('signal_execution', {})
                        _min_conf = float(
                            _exec_cfg_conf.get('min_confidence') or
                            config.get('alerts', {}).get('telegram', {}).get('min_confidence', 0.7)
                        )
                        if signal.confidence < _min_conf:
                            logger.info(f"Signal {pair} {signal.direction} REJECTED: "
                                        f"confidence {signal.confidence:.0%} < threshold {_min_conf:.0%}")
                            _log('DUPLICATE_BLOCKED', pair=pair, tf=tf,
                                 direction=signal.direction,
                                 reason=f'confidence_{signal.confidence:.2f}_below_threshold_{_min_conf:.2f}')
                            continue

                        # ── R:R THRESHOLD CHECK (signal_execution.min_risk_reward) ──────────
                        _min_rr_exec = float(_exec_cfg_conf.get('min_risk_reward', 2.0))
                        if getattr(signal, 'risk_reward', 0) < _min_rr_exec:
                            logger.info(f"Signal {pair} {signal.direction} REJECTED: "
                                        f"R:R {signal.risk_reward:.2f} < exec threshold {_min_rr_exec}")
                            _log('DUPLICATE_BLOCKED', pair=pair, tf=tf,
                                 direction=signal.direction,
                                 reason=f'rr_{signal.risk_reward:.2f}_below_exec_threshold_{_min_rr_exec}')
                            continue

                        # ── ZONE STRENGTH CHECK ─────────────────────────────────────────────
                        if getattr(signal, 'zone_strength', 0) < 1:
                            logger.info(f"Signal {pair} {signal.direction} REJECTED: "
                                        f"zone_strength={signal.zone_strength} < 1 (no valid liquidity zone)")
                            _log('DUPLICATE_BLOCKED', pair=pair, tf=tf,
                                 direction=signal.direction,
                                 reason=f'zone_strength_{signal.zone_strength}_below_1')
                            continue

                        # Save signal to DB
                        signal_id = store.save_signal(pair, tf, signal)
                        _log('SIGNAL_FOUND', pair=pair, tf=tf,
                             direction=signal.direction,
                             entry=signal.entry_price, sl=signal.stop_loss,
                             tp=signal.target, confidence=signal.confidence,
                             rr=getattr(signal,'risk_reward',0))

                        # Timeframe expiry map
                        tf_expires = {'15m': 2, '1h': 8, '4h': 24, '1d': 72}
                        expires_h  = tf_expires.get(tf, 4)

                        if exec_mode == 'immediate':
                            # Legacy: execute at market immediately
                            max_concurrent = config.get('backtester', {}).get('max_concurrent_trades', 3)
                            open_trades_count = len(store.get_open_trades())
                            if trade_executor and open_trades_count < max_concurrent:
                                try:
                                    result = trade_executor.execute_signal(
                                        {'direction': signal.direction, 'entry_price': 0,  # 0=market
                                         'stop_loss': signal.stop_loss, 'target': signal.target,
                                         'timeframe': tf, 'confidence': signal.confidence},
                                        pair, notional_usd=signal.notional_usd, signal_id=signal_id
                                    )
                                    if 'error' not in result:
                                        logger.info(f"Immediate [{result.get('mode','?').upper()}] {pair} {signal.direction} trade_id={result.get('trade_id')}")
                                except Exception as e:
                                    logger.error(f"Immediate execute error {pair}: {e}")
                        else:
                            # ── Pre-filter: reject signals with tight SL gap at creation time ──
                            _sl_gap_chk = abs(signal.entry_price - signal.stop_loss) / max(signal.entry_price, 1e-12)
                            if min_sl_gap_pct > 0 and _sl_gap_chk < min_sl_gap_pct:
                                logger.info(f"Signal {pair} {signal.direction} REJECTED: "
                                            f"SL gap {_sl_gap_chk*100:.2f}% < min {min_sl_gap_pct*100:.1f}%")
                                _log('DUPLICATE_BLOCKED', pair=pair, tf=tf,
                                     direction=signal.direction,
                                     reason=f'signal_sl_gap_too_tight_{_sl_gap_chk*100:.2f}pct')
                                continue

                            # PENDING mode: wait for price to reach fib entry
                            pending_id = store.create_pending_signal(
                                pair=pair, timeframe=tf, direction=signal.direction,
                                entry_price=signal.entry_price, stop_loss=signal.stop_loss,
                                target=signal.target, confidence=signal.confidence,
                                notional_usd=signal.notional_usd, signal_id=signal_id,
                                account_id=active_account_id, expires_hours=expires_h
                            )
                            if pending_id:
                                dist_pct = abs(latest_price - signal.entry_price) / max(latest_price, 1e-9) * 100
                                logger.info(f"Pending signal #{pending_id}: {pair} {signal.direction} "
                                            f"entry={signal.entry_price:.6g} current={latest_price:.6g} "
                                            f"dist={dist_pct:.2f}% expires={expires_h}h")
                                _log('PENDING_CREATED', pair=pair, tf=tf,
                                     direction=signal.direction,
                                     entry=signal.entry_price, sl=signal.stop_loss,
                                     tp=signal.target, pending_id=pending_id,
                                     dist_pct=round(dist_pct, 2), expires_h=expires_h)
                            else:
                                logger.info(f"Pending signal deduped for {pair} {signal.direction} — open trade or duplicate exists")
                                _log('DUPLICATE_BLOCKED', pair=pair, tf=tf,
                                     direction=signal.direction, reason='pending_dedup')

                        signal_record = {
                            'pair':      pair,
                            'timeframe': tf,
                            'timestamp': signal.timestamp,
                            'signal':    signal
                        }
                        all_signals.append(signal_record)
                        # Send alert immediately
                        if args.alert:
                            dispatcher.send_signal(signal)

                # ── Check pending signals for this pair ──────────────────────
                if auto_exec and trade_executor:
                    pending = store.get_pending_signals(status='pending')
                    pair_pending = [p for p in pending if p['pair'] == pair]
                    for ps in pair_pending:
                        ep  = float(ps['entry_price'])
                        # FIX: use `symbol` (current pair variable), NOT `sym` (was undefined/stale)
                        cur = latest_prices.get(symbol, 0) or latest_price
                        if cur <= 0 or ep <= 0:
                            continue
                        dist = abs(cur - ep) / ep
                        if dist <= entry_tol:
                            # ── DUPLICATE GUARD: skip if open trade already exists for this pair ─
                            # Prevents 2+ simultaneous scanner processes from both entering.
                            existing_open = [t for t in store.get_open_trades()
                                             if t['pair'] == pair and t['status'] == 'open']
                            if existing_open:
                                logger.info(f"[Pending] {pair} already has open trade #{existing_open[0]['id']} — skipping entry")
                                _log('DUPLICATE_BLOCKED', pair=pair, tf=tf,
                                     direction=ps.get('direction'), reason='open_trade_exists')
                                continue

                            # ── MIN SL GAP FILTER ────────────────────────────────────────────────
                            # Skip entries where the gap between entry and SL is too tight.
                            # Tight SL = poor risk management; these get stopped out immediately
                            # by normal price noise. min_sl_gap_pct configurable in pairs.yaml.
                            sl_val = float(ps.get('stop_loss') or 0)
                            if min_sl_gap_pct > 0 and sl_val > 0 and ep > 0:
                                sl_gap = abs(ep - sl_val) / ep  # fractional gap
                                if sl_gap < min_sl_gap_pct:
                                    logger.info(f"Pending #{ps['id']} SKIPPED: SL gap {sl_gap*100:.2f}% < "
                                                f"min {min_sl_gap_pct*100:.1f}% ({pair} {ps['direction']} "
                                                f"entry={ep:.6g} sl={sl_val:.6g})")
                                    _log('DUPLICATE_BLOCKED', pair=pair, pending_id=ps['id'],
                                         direction=ps['direction'],
                                         reason=f'SL_gap_too_tight_{sl_gap*100:.2f}pct',
                                         sl_gap_pct=round(sl_gap*100, 3),
                                         min_required_pct=min_sl_gap_pct*100)
                                    store.cancel_pending_signal(ps['id'])
                                    continue

                            # ── HIGH-CONFIDENCE GATE (signal_execution config) ───────────────
                            # Blocks low-quality pending signals from executing even if price
                            # is in range. All 3 checks must pass for entry to proceed.
                            _pe_min_conf  = float(exec_cfg.get('min_confidence', 0.7))
                            _pe_min_rr    = float(exec_cfg.get('min_risk_reward', 2.0))
                            _pe_conf      = float(ps.get('confidence') or 0)

                            # R:R and zone_strength are in signals table (not pending_signals)
                            # Look them up via signal_id
                            _pe_rr = 0.0
                            _pe_zs = 0
                            _pe_sig_id = ps.get('signal_id')
                            if _pe_sig_id:
                                import sqlite3 as _sqlite3
                                with _sqlite3.connect(store.db_path) as _scon:
                                    _sr = _scon.execute(
                                        "SELECT risk_reward, zone_strength FROM signals WHERE id=?",
                                        (_pe_sig_id,)
                                    ).fetchone()
                                    if _sr:
                                        _pe_rr = float(_sr[0] or 0)
                                        _pe_zs = int(_sr[1] or 0)

                            if _pe_conf < _pe_min_conf:
                                logger.info(f"Pending #{ps['id']} SKIPPED: confidence {_pe_conf:.2f} < min {_pe_min_conf:.2f}")
                                _log('DUPLICATE_BLOCKED', pair=pair, pending_id=ps['id'],
                                     direction=ps['direction'],
                                     reason=f'confidence_{_pe_conf:.2f}_below_{_pe_min_conf:.2f}')
                                store.cancel_pending_signal(ps['id'])
                                continue
                            if _pe_min_rr > 0 and _pe_rr > 0 and _pe_rr < _pe_min_rr:
                                logger.info(f"Pending #{ps['id']} SKIPPED: R:R {_pe_rr:.2f} < min {_pe_min_rr:.2f}")
                                _log('DUPLICATE_BLOCKED', pair=pair, pending_id=ps['id'],
                                     direction=ps['direction'],
                                     reason=f'rr_{_pe_rr:.2f}_below_{_pe_min_rr:.2f}')
                                store.cancel_pending_signal(ps['id'])
                                continue
                            if _pe_sig_id and _pe_zs < 1:
                                logger.info(f"Pending #{ps['id']} SKIPPED: zone_strength={_pe_zs} < 1 (no valid zone)")
                                _log('DUPLICATE_BLOCKED', pair=pair, pending_id=ps['id'],
                                     direction=ps['direction'],
                                     reason=f'zone_strength_{_pe_zs}_below_1')
                                store.cancel_pending_signal(ps['id'])
                                continue
                            logger.info(
                                f"Pending #{ps['id']} HIGH-CONF GATE ✅ "
                                f"conf={_pe_conf:.2f}/{_pe_min_conf:.2f} "
                                f"RR={_pe_rr:.2f}/{_pe_min_rr:.2f} "
                                f"zone_str={_pe_zs}"
                            )
                            # ─────────────────────────────────────────────────────────────────

                            max_concurrent = config.get('backtester', {}).get('max_concurrent_trades', 3)
                            open_trades_count = len(store.get_open_trades())
                            if open_trades_count >= max_concurrent:
                                logger.info(f"Pending #{ps['id']} skipped: {open_trades_count}/{max_concurrent} max trades open")
                                continue
                            # ── DEDUP CHECK: do NOT fire if open trade already exists for this pair+direction ──
                            # This prevents the same pending signal firing multiple times across cron runs.
                            # The trade dedup in store.create_open_trade() only catches same entry_price ±0.5%
                            # but can miss if previous entry closed and pending signal is still alive.
                            existing_open = [t for t in store.get_open_trades()
                                             if t['pair'] == pair
                                             and t['direction'] == ps['direction']
                                             and t.get('account_id') == ps.get('account_id')]
                            if existing_open:
                                logger.info(f"Pending #{ps['id']} skipped: open trade already exists for {pair} {ps['direction']}")
                                _log('DUPLICATE_BLOCKED', pair=pair, pending_id=ps['id'],
                                     direction=ps['direction'], reason='open_trade_exists',
                                     existing_trade_id=existing_open[0]['id'])
                                continue

                            # ── COOLDOWN CHECK: block re-entry within 10min of last entry ──
                            # Prevents rapid re-entry loop when trades close before 90s SL delay.
                            _cooldown_key = f"{pair}:{ps['direction']}"
                            _last_entry   = _entry_cooldown.get(_cooldown_key, 0)
                            _now_ts       = _time_mod.time()
                            if _now_ts - _last_entry < _ENTRY_COOLDOWN_SECS:
                                _remaining = int(_ENTRY_COOLDOWN_SECS - (_now_ts - _last_entry))
                                logger.info(f"Pending #{ps['id']} COOLDOWN: {pair} {ps['direction']} "
                                            f"— {_remaining}s remaining before next entry allowed")
                                _log('DUPLICATE_BLOCKED', pair=pair, pending_id=ps['id'],
                                     direction=ps['direction'], reason=f'cooldown_{_remaining}s_remaining')
                                continue

                            try:
                                result = trade_executor.execute_signal(
                                    {'direction': ps['direction'], 'entry_price': 0,
                                     'stop_loss': ps['stop_loss'], 'target': ps['target'],
                                     'timeframe': ps['timeframe'], 'confidence': ps['confidence']},
                                    pair, notional_usd=ps['notional_usd'],
                                    signal_id=ps.get('signal_id')
                                )
                                if 'error' not in result:
                                    store.trigger_pending_signal(ps['id'], result.get('trade_id'))
                                    # Record cooldown timestamp — blocks re-entry for 10min (persisted)
                                    _entry_cooldown[f"{pair}:{ps['direction']}"] = _time_mod.time()
                                    _save_cooldown(_entry_cooldown)
                                    logger.info(f"Triggered pending #{ps['id']}: {pair} {ps['direction']} "
                                                f"@ {cur:.6g} (entry was {ep:.6g}) "
                                                f"trade_id={result.get('trade_id')} mode={result.get('mode')}")
                                    _log('PENDING_TRIGGERED', pair=pair, pending_id=ps['id'],
                                         direction=ps['direction'], entry_was=ep, current=round(cur, 6),
                                         trade_id=result.get('trade_id'),
                                         sl=ps['stop_loss'], tp=ps['target'],
                                         method=result.get('method', '?'))
                                    _log('ENTRY_PLACED', pair=pair,
                                         direction=ps['direction'],
                                         entry_price=result.get('entry_price', cur),
                                         sl=ps['stop_loss'], tp=ps['target'],
                                         trade_id=result.get('trade_id'),
                                         order_id=result.get('order_id'),
                                         sl_order_id=result.get('sl_order_id'),
                                         tp_order_id=result.get('tp_order_id'),
                                         method=result.get('method', '?'))
                                    # Cancel all other pending signals for same pair+direction
                                    dupes = [p for p in pair_pending
                                             if p['id'] != ps['id']
                                             and p['direction'] == ps['direction']
                                             and p['status'] == 'pending']
                                    for dup in dupes:
                                        store.cancel_pending_signal(dup['id'])
                                        logger.info(f"Cancelled duplicate pending #{dup['id']} for {pair} {ps['direction']}")
                                        _log('PENDING_CANCELLED', pair=pair, pending_id=dup['id'],
                                             reason='entry_placed_cancel_dupes')
                                    break  # Only fire ONE entry per pair per scan run
                                else:
                                    err = result['error']
                                    logger.error(f"Pending trigger failed #{ps['id']}: {err}")
                                    _log('ORDER_ERROR', pair=pair, pending_id=ps['id'],
                                         direction=ps['direction'], error=str(err)[:200])
                            except Exception as e:
                                logger.error(f"Pending trigger error #{ps['id']}: {e}")
                                _log('ORDER_ERROR', pair=pair, pending_id=ps['id'],
                                     direction=ps['direction'], error=str(e)[:200])
            except Exception as e:
                logger.error(f"Error scanning {pair} {tf}: {e}")
                continue

    # NOTE: Trade exit detection (SL/TP hit, position closed on exchange) is handled
    # EXCLUSIVELY by the PositionMonitor background thread (position_monitor.py).
    # The monitor uses LIVE Binance position data as source of truth — not stale OHLCV
    # candle prices from this scan. Closing trades here would:
    #   1. Use the wrong price (candle close ≠ real-time mark price)
    #   2. Conflict with the monitor which already handles this correctly
    #   3. Close trades before the broker position is actually closed (causing ghost trades)
    # The scan's only job: generate signals + trigger entries. NOT close trades.

    # Save latest signals to cache for dashboard
    store = DataStore()
    # Convert TradeSignal objects to serializable dicts
    serializable_signals = []
    for item in all_signals:
        sig = item['signal']
        # Convert dataclass to dict if needed
        sig_dict = sig.__dict__ if hasattr(sig, '__dict__') else dict(sig)
        serializable_signals.append({
            'pair':                    item['pair'],
            'timeframe':               item['timeframe'],
            'timestamp':               (item['timestamp'].isoformat()
                                        if hasattr(item['timestamp'], 'isoformat')
                                        else item['timestamp']),
            'direction':               sig_dict['direction'],
            'entry':                   sig_dict['entry_price'],
            'sl':                      sig_dict['stop_loss'],
            'tp':                      sig_dict['target'],
            'rr':                      sig_dict['risk_reward'],
            'confidence':              sig_dict['confidence'],
            'notional_usd':            sig_dict.get('notional_usd', 0),
            'margin_required_usd':     sig_dict.get('margin_required_usd', 0),
            'commission_estimated_usd': sig_dict.get('commission_estimated_usd', 0),
            'zone_strength':           sig_dict.get('zone_strength', 0),
            'htf_bias':                sig_dict.get('htf_bias', 'neutral'),
            'ob_confluence':           sig_dict.get('ob_confluence', False),
            'fvg_confluence':          sig_dict.get('fvg_confluence', False),
        })
    store.save_latest_signals(serializable_signals)

    # ── Log scan end ───────────────────────────────────────────────────────────
    import time as _scan_time_mod
    scan_duration = round(_scan_time_mod.time() - _scan_time_mod.mktime(scan_start.timetuple()), 1)
    _log('SCAN_END',
         signals_found=len(all_signals),
         pairs_scanned=len(pairs),
         duration_s=scan_duration)

    # ── Scan duration ──────────────────────────────────────────────────────────
    scan_end     = datetime.utcnow()
    scan_dur_sec = int((scan_end - scan_start).total_seconds()) if 'scan_start' in dir() else 0

    # ── Collect open trades + unrealized PnL for summary ──────────────────────
    open_trades_all = store.get_open_trades()
    open_pnl_usd    = 0.0
    for ot in open_trades_all:
        sym = ot['pair'].split(':', 1)[1] if ':' in ot['pair'] else ot['pair']
        cur = latest_prices.get(sym, 0)
        if cur > 0 and ot.get('entry_price', 0) > 0:
            ep  = float(ot['entry_price'])
            not_= float(ot.get('notional_usd', 0))
            com = float(ot.get('commission_usd', 0))
            if ot['direction'] == 'long':
                open_pnl_usd += (cur - ep) / ep * not_ - com * 2
            else:
                open_pnl_usd += (ep - cur) / ep * not_ - com * 2

    # ── Count confirmed sweeps this run ────────────────────────────────────────
    sweeps_confirmed = 0
    try:
        last_log = sorted([f for f in __import__('os').listdir('logs') if f.startswith('cron_')], reverse=True)
        if last_log:
            with open(f'logs/{last_log[0]}') as lf:
                sweeps_confirmed = lf.read().count('confirmed (vol')
    except Exception:
        pass

    # ── Pending signals count ──────────────────────────────────────────────────
    pending_count = len(store.get_pending_signals(status='pending'))

    # ── Active account name ────────────────────────────────────────────────────
    active_acct_name = '—'
    if active_account_id:
        acct_info = store.get_account(active_account_id)
        if acct_info:
            active_acct_name = f"{acct_info['name']} ({acct_info['mode']})"

    # ── Build top signals list for summary ─────────────────────────────────────
    top_sigs = []
    for item in all_signals[-5:]:
        sig = item['signal']
        top_sigs.append({
            'pair':        item['pair'],
            'timeframe':   item['timeframe'],
            'direction':   sig.direction,
            'entry_price': sig.entry_price,
            'risk_reward': sig.risk_reward,
            'confidence':  sig.confidence,
        })

    # ── Send Telegram scan summary ─────────────────────────────────────────────
    if args.alert:
        summary = {
            'pairs_scanned':  len(pairs),
            'timeframes':     timeframes,
            'signals_count':  len(all_signals),
            'pending_count':  pending_count,
            'open_trades':    len(open_trades_all),
            'sweeps_found':   sweeps_confirmed,
            'open_pnl_usd':   round(open_pnl_usd, 2),
            'active_account': active_acct_name,
            'duration_sec':   scan_dur_sec,
            'top_signals':    top_sigs,
        }
        # Send summary: always if signals found, otherwise every 30 min (every 6th scan at 5-min intervals)
        import time as _time
        _now_min = int(_time.time() // 60)
        _send_summary = len(all_signals) > 0 or (_now_min % 30 == 0)
        if _send_summary:
            dispatcher.send_scan_summary(summary)

    # ── Print log summary ──────────────────────────────────────────────────────
    print(f"\n=== SCAN ALL: {len(pairs)} pairs, {len(timeframes)} timeframes ===")
    print(f"Total signals generated: {len(all_signals)}")
    for item in all_signals[-10:]:
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
