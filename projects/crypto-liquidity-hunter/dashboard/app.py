"""
Crypto Liquidity Hunter Dashboard
Flask app with Plotly charts, real-time sweep/signal display.
Config is reloaded on every request to reflect Telegram parameter changes.
"""
import os
import json
import yaml
from datetime import datetime
from flask import Flask, render_template, jsonify, request
import plotly.graph_objs as go
from plotly.utils import PlotlyJSONEncoder
import logging

from core.data_fetcher import MarketDataFetcher
from core.liquidity_mapper import LiquidityMapper
from core.sweep_detector import SweepDetector
from core.signal_engine import SignalEngine
from data.store import DataStore

logger = logging.getLogger(__name__)

PROJECT_ROOT = os.path.dirname(os.path.dirname(__file__))
CONFIG_PATH = os.path.join(PROJECT_ROOT, 'config', 'pairs.yaml')

def load_config():
    """Load config fresh each time to pick up runtime changes."""
    with open(CONFIG_PATH, 'r') as f:
        return yaml.safe_load(f)

app = Flask(__name__, static_folder='static', template_folder='templates')

@app.route('/')
def index():
    """Dashboard home: list pairs, timeframes."""
    config = load_config()
    pairs = config['pairs']
    timeframes = config['data_fetch']['timeframes']
    return render_template('index.html', pairs=pairs, timeframes=timeframes)

@app.route('/api/scan/<path:pair>')
def scan_pair(pair):
    """Run live scan for a specific pair (with optional tf query param)."""
    config = load_config()
    tf = request.args.get('tf', config['data_fetch']['timeframes'][0])
    exchange_str, symbol = pair.split(':', 1) if ':' in pair else ('binance', pair)

    # Build components with current config
    fetcher = MarketDataFetcher(exchange_str)
    mapper = LiquidityMapper(
        equal_touch_tolerance=config['liquidity_mapper']['equal_touch_tolerance'],
        swing_lookback=config['liquidity_mapper']['swing_lookback'],
        round_tolerance=config['liquidity_mapper']['round_tolerance'],
        min_swing_strength=config['liquidity_mapper']['min_swing_strength']
    )
    detector = SweepDetector(
        sweep_multiplier=config['sweep_detector']['sweep_multiplier'],
        volume_multiplier=config['sweep_detector']['volume_multiplier'],
        confirmation_bars=config['sweep_detector']['confirmation_bars'],
        wick_ratio=config['sweep_detector']['wick_ratio'],
        min_sweep_pct=config['sweep_detector']['min_sweep_pct']
    )
    paper_cfg = config.get('paper_trading', {})
    engine = SignalEngine(
        risk_per_trade=config['signal_engine']['risk_per_trade'],
        retracement_levels=config['signal_engine']['retracement_levels'],
        stop_buffer_pct=config['signal_engine']['stop_buffer_pct'],
        min_risk_reward=config['signal_engine']['min_risk_reward'],
        position_sizing=paper_cfg.get('position_sizing', 'risk_percent'),
        fixed_notional_usd=paper_cfg.get('fixed_notional_usd', 50.0),
        margin_leverage=paper_cfg.get('margin_leverage', 1.0),
        commission_pct=paper_cfg.get('commission_per_trade', 0.001)
    )

    # Fetch data
    df = fetcher.fetch_ohlcv(symbol, timeframe=tf,
                             limit=config['data_fetch']['ohlcv_limit'])
    atr = fetcher.calculate_atr(df, period=config['data_fetch']['atr_period'])
    zones = mapper.map_liquidity(df)
    sweeps = detector.detect_sweeps(df, atr, zones)
    latest_price = df.iloc[-1]['close']

    # Generate signals — only from recent sweeps (no stale historical setups)
    tf_max_age = {'15m': 4, '1h': 12, '4h': 24, '1d': 72}
    max_sweep_age = tf_max_age.get(tf, 6)
    signals = []
    for sweep in sweeps[-10:]:
        signal = engine.generate_signal(
            sweep, zones, latest_price,
            capital=10000, pair=pair,
            max_sweep_age_hours=max_sweep_age
        )
        if signal:
            signals.append(signal)

    # Serialize results
    zones_data = [{
        'price': z.price,
        'type': z.zone_type,
        'strength': z.strength,
        'last_touch': z.last_touch.isoformat()
    } for z in zones[:20]]

    sweeps_data = [{
        'timestamp': s.timestamp.isoformat(),
        'direction': s.direction,
        'sweep_price': s.sweep_price,
        'close_price': s.close_price,
        'volume': s.volume,
        'volume_ratio': s.volume_ratio,
        'depth_pct': s.sweep_depth_pct,
        'confirmed': s.confirmed
    } for s in sweeps[-10:]]

    # Phase 2: OBs, FVGs, HTF bias for scan tab
    obs  = detector.detect_order_blocks(df)
    fvgs = detector.detect_fvgs(df)
    htf_bias = 'neutral'
    if tf in ('15m', '1h'):
        try:
            df_htf   = fetcher.fetch_ohlcv(symbol, timeframe='4h', limit=100)
            htf_bias = detector.get_htf_bias(df_htf)
        except Exception:
            htf_bias = 'neutral'

    signals_data = [{
        'pair': pair,
        'timeframe': tf,
        'direction': sig.direction,
        'entry_price': sig.entry_price,
        'current_price': latest_price,
        'stop_loss': sig.stop_loss,
        'target': sig.target,
        'risk_reward': sig.risk_reward,
        'confidence': sig.confidence,
        'zone_strength': sig.zone_strength,
        'notional_usd': sig.notional_usd,
        'margin_required_usd': sig.margin_required_usd,
        'commission_estimated_usd': sig.commission_estimated_usd,
        'ob_confluence':  getattr(sig, 'ob_confluence', False),
        'fvg_confluence': getattr(sig, 'fvg_confluence', False),
        'htf_bias':       getattr(sig, 'htf_bias', htf_bias),
    } for sig in signals]

    return jsonify({
        'pair': pair,
        'timeframe': tf,
        'current_price': latest_price,
        'zones': zones_data,
        'sweeps': sweeps_data,
        'signals': signals_data,
        'htf_bias': htf_bias,
        'ob_count': len(obs),
        'fvg_count': len(fvgs),
        'last_updated': datetime.utcnow().isoformat()
    })

@app.route('/api/chart/<path:pair>')
def chart_pair(pair):
    """Generate candlestick chart with zones and sweeps."""
    config = load_config()
    tf = request.args.get('tf', config['data_fetch']['timeframes'][0])
    exchange_str, symbol = pair.split(':', 1) if ':' in pair else ('binance', pair)
    fetcher = MarketDataFetcher(exchange_str)
    mapper = LiquidityMapper(
        equal_touch_tolerance=config['liquidity_mapper']['equal_touch_tolerance'],
        swing_lookback=config['liquidity_mapper']['swing_lookback'],
        round_tolerance=config['liquidity_mapper']['round_tolerance'],
        min_swing_strength=config['liquidity_mapper']['min_swing_strength']
    )
    detector = SweepDetector(
        sweep_multiplier=config['sweep_detector']['sweep_multiplier'],
        volume_multiplier=config['sweep_detector']['volume_multiplier'],
        confirmation_bars=config['sweep_detector']['confirmation_bars'],
        wick_ratio=config['sweep_detector']['wick_ratio'],
        min_sweep_pct=config['sweep_detector']['min_sweep_pct']
    )

    df = fetcher.fetch_ohlcv(symbol, timeframe=tf, limit=200)
    atr = fetcher.calculate_atr(df, period=config['data_fetch']['atr_period'])
    zones = mapper.map_liquidity(df)
    sweeps = detector.detect_sweeps(df, atr, zones)

    # Build candlestick trace
    candlestick = go.Candlestick(
        x=df.index,
        open=df['open'],
        high=df['high'],
        low=df['low'],
        close=df['close'],
        name=symbol
    )

    # Zone lines
    zone_shapes = []
    for z in zones[:10]:
        zone_shapes.append(dict(
            type='line',
            x0=df.index[0],
            x1=df.index[-1],
            y0=z.price,
            y1=z.price,
            line=dict(color='rgba(0, 200, 0, 0.5)', width=z.strength, dash='dash'),
            name=f"Zone {z.zone_type}"
        ))

    # Sweep markers
    if sweeps:
        sweep_x = [s.timestamp for s in sweeps[-20:]]
        sweep_y = [s.sweep_price for s in sweeps[-20:]]
        sweep_markers = go.Scatter(
            x=sweep_x,
            y=sweep_y,
            mode='markers',
            marker=dict(symbol='triangle-down', size=12, color='red'),
            name='Sweeps'
        )
        data = [candlestick, sweep_markers]
    else:
        data = [candlestick]

    layout = go.Layout(
        title=f'{pair} - {tf}',
        xaxis_title='Time',
        yaxis_title='Price',
        shapes=zone_shapes
    )

    fig = go.Figure(data=data, layout=layout)
    fig_json = json.dumps(fig, cls=PlotlyJSONEncoder)

    return jsonify({'chart': fig_json})

@app.route('/api/signals')
def get_signals():
    """Get latest signals from shared cache."""
    from data.store import DataStore
    store = DataStore()
    cache = store.get_latest_signals()
    return jsonify(cache)

@app.route('/api/trades')
def get_trades():
    """Get trades (open or closed)."""
    store = DataStore()
    status = request.args.get('status', 'all')  # all, open, closed
    if status == 'open':
        trades = store.get_open_trades()
    elif status == 'closed':
        limit = int(request.args.get('limit', 50))
        trades = store.get_closed_trades(limit=limit)
    else:  # all
        open_trades = store.get_open_trades()
        closed_trades = store.get_closed_trades(limit=100)
        trades = open_trades + closed_trades
        # Sort by entry_time descending
        trades.sort(key=lambda x: x['entry_time'], reverse=True)
    # Serialize datetime fields — always append +00:00 so browser knows UTC
    def serialize(t):
        t_ser = dict(t)
        for field in ['entry_time', 'exit_time']:
            val = t_ser.get(field)
            if not val:
                continue
            if isinstance(val, datetime):
                s = val.isoformat()
            else:
                s = str(val)
            # Ensure UTC marker present so JS Date() parses as UTC (not local)
            if s and '+' not in s and not s.endswith('Z'):
                s += '+00:00'
            t_ser[field] = s
        return t_ser
    return jsonify([serialize(t) for t in trades])

@app.route('/api/clear_trades', methods=['POST'])
def clear_trades():
    """Wipe only the trades table (paper trades). Signals/sweeps/zones untouched."""
    import sqlite3
    db_path = os.path.join(PROJECT_ROOT, 'data', 'store.db')
    try:
        with sqlite3.connect(db_path) as conn:
            row = conn.execute('SELECT COUNT(*) FROM trades').fetchone()
            count = row[0] if row else 0
            conn.execute('DELETE FROM trades')
            conn.execute("DELETE FROM sqlite_sequence WHERE name='trades'")
            conn.commit()
        logger.info(f"Dashboard clear_trades: wiped {count} trades")
        return jsonify({'status': 'ok', 'wiped': count,
                        'message': f'Cleared {count} paper trade(s)'})
    except Exception as e:
        logger.error(f"clear_trades error: {e}")
        return jsonify({'status': 'error', 'message': str(e)}), 500

@app.route('/api/clear_data', methods=['POST'])
def clear_data():
    """
    Wipe all trades, signals, sweeps, zones from DB and reset the signal cache.
    Called from the dashboard 'Clear All Data' button.
    """
    import sqlite3
    from pathlib import Path

    db_path    = os.path.join(PROJECT_ROOT, 'data', 'store.db')
    cache_path = os.path.join(PROJECT_ROOT, 'data', 'latest_signals.json')

    counts_before = {}
    try:
        with sqlite3.connect(db_path) as conn:
            for tbl in ('trades', 'signals', 'sweeps', 'zones'):
                row = conn.execute(f'SELECT COUNT(*) FROM {tbl}').fetchone()
                counts_before[tbl] = row[0] if row else 0

            conn.execute('DELETE FROM trades')
            conn.execute('DELETE FROM signals')
            conn.execute('DELETE FROM sweeps')
            conn.execute('DELETE FROM zones')
            # Reset auto-increment counters
            conn.execute("DELETE FROM sqlite_sequence WHERE name IN ('trades','signals','sweeps','zones')")
            conn.commit()

        # Reset signal JSON cache
        with open(cache_path, 'w') as f:
            json.dump({
                'last_updated': datetime.utcnow().isoformat(),
                'signals': []
            }, f)

        logger.info(f"Dashboard clear_data: wiped trades={counts_before.get('trades',0)} "
                    f"signals={counts_before.get('signals',0)}")

        return jsonify({
            'status': 'ok',
            'wiped': counts_before,
            'message': (f"Cleared: {counts_before.get('trades',0)} trades, "
                        f"{counts_before.get('signals',0)} signals, "
                        f"{counts_before.get('sweeps',0)} sweeps")
        })
    except Exception as e:
        logger.error(f"clear_data error: {e}")
        return jsonify({'status': 'error', 'message': str(e)}), 500

@app.route('/api/performance')
def get_performance():
    """
    Compute live performance metrics from trades table.
    Returns: win_rate, sharpe, sortino, max_drawdown, profit_factor,
             equity_curve, daily_pnl, monthly_pnl, avg_rr, total_trades.
    """
    import sqlite3, math
    db_path = os.path.join(PROJECT_ROOT, 'data', 'store.db')
    try:
        with sqlite3.connect(db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute("""
                SELECT pnl_usd, entry_time, exit_time, status, direction, pair,
                       entry_price, sl, tp
                FROM trades
                WHERE status IN ('target_hit','stop_loss','closed')
                AND pnl_usd IS NOT NULL
                ORDER BY COALESCE(exit_time, entry_time) ASC
            """).fetchall()
            open_count = conn.execute("SELECT COUNT(*) FROM trades WHERE status='open'").fetchone()[0]
    except Exception as e:
        return jsonify({'error': str(e)}), 500

    trades = [dict(r) for r in rows]
    if not trades:
        return jsonify({
            'total_trades': 0, 'open_trades': open_count,
            'win_rate': 0, 'profit_factor': 0,
            'sharpe': 0, 'sortino': 0, 'max_drawdown_pct': 0,
            'total_pnl': 0, 'avg_pnl': 0, 'avg_rr': 0,
            'equity_curve': [], 'daily_pnl': [], 'monthly_pnl': []
        })

    pnls  = [t['pnl_usd'] for t in trades]
    wins  = [p for p in pnls if p > 0]
    losses= [p for p in pnls if p < 0]
    total = len(pnls)
    win_rate = round(len(wins) / total * 100, 1)

    gross_profit = sum(wins) if wins else 0
    gross_loss   = abs(sum(losses)) if losses else 0
    pf = round(gross_profit / gross_loss, 2) if gross_loss > 0 else 999.0

    mean = sum(pnls) / total
    variance = sum((p - mean) ** 2 for p in pnls) / total
    std = math.sqrt(variance) if variance > 0 else 0
    sharpe = round(mean / std * math.sqrt(252), 2) if std > 0 else 0.0

    downside = [p for p in pnls if p < 0]
    d_var = sum(p**2 for p in downside) / len(downside) if downside else 0
    d_std = math.sqrt(d_var) if d_var > 0 else 0
    sortino = round(mean / d_std * math.sqrt(252), 2) if d_std > 0 else 0.0

    # Equity curve + max drawdown
    equity = 0.0
    peak   = 0.0
    max_dd_abs = 0.0
    eq_curve = []
    for t in trades:
        equity += t['pnl_usd']
        if equity > peak:
            peak = equity
        dd = peak - equity
        if dd > max_dd_abs:
            max_dd_abs = dd
        ts = t.get('exit_time') or t.get('entry_time') or ''
        eq_curve.append({'t': ts[:16], 'v': round(equity, 2)})

    max_dd_pct = round(max_dd_abs / peak * 100, 2) if peak > 0 else 0.0

    # Daily P&L
    daily = {}
    for t in trades:
        day = (t.get('exit_time') or t.get('entry_time') or '')[:10]
        if day:
            daily[day] = round(daily.get(day, 0) + t['pnl_usd'], 2)
    daily_pnl = [{'date': k, 'pnl': v} for k, v in sorted(daily.items())[-30:]]

    # Monthly P&L
    monthly = {}
    for t in trades:
        mo = (t.get('exit_time') or t.get('entry_time') or '')[:7]
        if mo:
            monthly[mo] = round(monthly.get(mo, 0) + t['pnl_usd'], 2)
    monthly_pnl = [{'month': k, 'pnl': v} for k, v in sorted(monthly.items())]

    # Avg R:R — compute from entry/sl/tp
    rrs = []
    for t in trades:
        try:
            ep, sl, tp = t.get('entry_price'), t.get('sl'), t.get('tp')
            if ep and sl and tp and abs(ep - sl) > 0:
                rr = abs(tp - ep) / abs(ep - sl)
                rrs.append(rr)
        except Exception:
            pass
    avg_rr = round(sum(rrs) / len(rrs), 2) if rrs else 0

    return jsonify({
        'total_trades':    total,
        'open_trades':     open_count,
        'win_rate':        win_rate,
        'profit_factor':   pf,
        'sharpe':          sharpe,
        'sortino':         sortino,
        'max_drawdown_pct': max_dd_pct,
        'total_pnl':       round(sum(pnls), 2),
        'avg_pnl':         round(mean, 2),
        'avg_rr':          avg_rr,
        'equity_curve':    eq_curve,
        'daily_pnl':       daily_pnl,
        'monthly_pnl':     monthly_pnl,
    })

@app.route('/api/ohlcv/<path:pair>')
def get_ohlcv(pair):
    """Return raw OHLCV for TradingView Lightweight Charts."""
    config  = load_config()
    tf      = request.args.get('tf', config['data_fetch']['timeframes'][0])
    limit   = int(request.args.get('limit', 200))
    exchange_str, symbol = pair.split(':', 1) if ':' in pair else ('binance', pair)
    try:
        fetcher = MarketDataFetcher(exchange_str)
        mapper  = LiquidityMapper(
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
            min_sweep_pct=config['sweep_detector']['min_sweep_pct']
        )
        df  = fetcher.fetch_ohlcv(symbol, timeframe=tf, limit=limit)
        atr = fetcher.calculate_atr(df)
        zones  = mapper.map_liquidity(df)
        sweeps = detector.detect_sweeps(df, atr, zones)
        obs    = detector.detect_order_blocks(df)
        fvgs   = detector.detect_fvgs(df)

        # OHLCV for LightweightCharts (time must be unix seconds)
        candles = []
        for ts, row in df.iterrows():
            candles.append({
                'time':  int(ts.timestamp()),
                'open':  float(row['open']),
                'high':  float(row['high']),
                'low':   float(row['low']),
                'close': float(row['close']),
            })

        # Zones
        zones_out = [{'price': z.price, 'type': z.zone_type, 'strength': z.strength}
                     for z in zones[:30]]

        # Sweeps as markers
        sweep_markers = []
        for s in sweeps[-20:]:
            sweep_markers.append({
                'time':      int(s.timestamp.timestamp()),
                'direction': s.direction,
                'price':     float(s.sweep_price),
                'vol_ratio': round(float(s.volume_ratio), 1),
                'depth':     round(float(s.sweep_depth_pct), 2),
                'confirmed': s.confirmed,
            })

        # OBs
        obs_out = [{'time': int(ob.timestamp.timestamp()),
                    'direction': ob.direction,
                    'high': float(ob.high), 'low': float(ob.low)} for ob in obs[-10:]]

        # FVGs
        fvgs_out = [{'time': int(fvg.timestamp.timestamp()),
                     'direction': fvg.direction,
                     'top': float(fvg.top), 'bottom': float(fvg.bottom),
                     'size_pct': fvg.size_pct} for fvg in fvgs[-10:]]

        return jsonify({
            'pair': pair, 'timeframe': tf,
            'current_price': float(df.iloc[-1]['close']),
            'candles': candles,
            'zones': zones_out,
            'sweeps': sweep_markers,
            'order_blocks': obs_out,
            'fvgs': fvgs_out,
            'last_updated': datetime.utcnow().isoformat(),
        })
    except Exception as e:
        logger.error(f"ohlcv error {pair}: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/health')
def health():
    return jsonify({'status': 'ok', 'timestamp': datetime.utcnow().isoformat()})


# ═══════════════════════════════════════════════════════════════════════════════
# Phase 3A — Binance Trading Routes
# These routes are NEW and completely isolated from existing strategy logic.
# ═══════════════════════════════════════════════════════════════════════════════

def _get_connector():
    """Build a BinanceConnector from current config. Fresh each call."""
    from core.binance_connector import BinanceConnector
    config = load_config()
    bc = config.get('binance_connection', {})
    return BinanceConnector(
        api_key    = bc.get('api_key', ''),
        api_secret = bc.get('api_secret', ''),
        testnet    = bc.get('testnet', True),
        mode       = bc.get('mode', 'paper'),
    )


@app.route('/api/binance/status')
def binance_status():
    """Return Binance connection status, balance, mode."""
    try:
        connector = _get_connector()
        config    = load_config()
        bc_cfg    = config.get('binance_connection', {})
        mode      = bc_cfg.get('mode', 'paper')
        connected = connector.connect()
        balance   = connector.get_balance('USDT') if connected else {'free': 0, 'total': 0}
        positions = connector.get_positions() if connected and mode != 'paper' else []
        return jsonify({
            'connected':           connected,
            'mode':                mode,
            'testnet':             bc_cfg.get('testnet', True),
            'enabled':             bc_cfg.get('enabled', False),
            'balance_usdt_free':   round(balance.get('free',  0), 2),
            'balance_usdt_total':  round(balance.get('total', 0), 2),
            'balance_usdt_locked': round(balance.get('used',  0), 2),
            'positions_count':     len(positions),
        })
    except Exception as e:
        logger.error(f"binance_status error: {e}")
        return jsonify({'connected': False, 'error': str(e), 'mode': 'paper'}), 200


@app.route('/api/binance/connect', methods=['POST'])
def binance_connect():
    """Save and test Binance API credentials."""
    from core.trade_executor import TradeExecutor
    from core.binance_connector import BinanceConnector
    data       = request.get_json() or {}
    api_key    = data.get('api_key', '')
    api_secret = data.get('api_secret', '')
    testnet    = bool(data.get('testnet', True))
    mode       = data.get('mode', 'paper')

    try:
        connector = BinanceConnector(api_key=api_key, api_secret=api_secret,
                                     testnet=testnet, mode=mode)
        ok = connector.connect()
        if ok:
            TradeExecutor.save_connection_config(api_key, api_secret, testnet, mode)
            balance = connector.get_balance('USDT')
            return jsonify({
                'status':    'connected',
                'mode':      mode,
                'testnet':   testnet,
                'balance':   round(balance.get('free', 0), 2),
                'message':   f"Connected in {mode.upper()} mode ({'Testnet' if testnet else 'Live'})"
            })
        else:
            return jsonify({'status': 'error', 'message': 'Connection failed — check API keys'}), 400
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 400


@app.route('/api/binance/execute', methods=['POST'])
def binance_execute():
    """Execute a trade signal via Binance (paper or live)."""
    from core.trade_executor import TradeExecutor
    data = request.get_json() or {}
    try:
        executor = TradeExecutor()
        pair     = data.get('pair', 'binance:BTC/USDT')
        signal_dict = {
            'direction':   data.get('direction', 'long'),
            'entry_price': float(data.get('entry_price', 0)),
            'stop_loss':   float(data.get('stop_loss', 0)),
            'target':      float(data.get('target', 0)),
            'timeframe':   data.get('timeframe', '1h'),
            'confidence':  float(data.get('confidence', 0.7)),
        }
        notional = float(data.get('notional_usd', 0)) or None
        leverage = float(data.get('leverage', 0)) or None
        result   = executor.execute_signal(signal_dict, pair, notional_usd=notional,
                                           leverage=leverage)
        return jsonify(result)
    except Exception as e:
        logger.error(f"binance_execute error: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/api/binance/close', methods=['POST'])
def binance_close():
    """Close an open trade."""
    from core.trade_executor import TradeExecutor
    data = request.get_json() or {}
    trade_id   = int(data.get('trade_id', 0))
    symbol     = data.get('symbol', '')
    exit_price = float(data.get('exit_price', 0)) or None
    try:
        executor = TradeExecutor()
        ok = executor.close_trade(trade_id, symbol, exit_price=exit_price, reason='manual')
        return jsonify({'status': 'ok' if ok else 'error', 'trade_id': trade_id})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/binance/positions')
def binance_positions():
    """Return open Binance futures positions (or open paper trades in DB)."""
    try:
        config = load_config()
        bc_cfg = config.get('binance_connection', {})
        mode   = bc_cfg.get('mode', 'paper')
        if mode == 'paper':
            from data.store import DataStore
            store  = DataStore()
            trades = store.get_open_trades()
            # Mark all as paper
            for t in trades:
                t['mode'] = 'paper'
            return jsonify(trades)
        else:
            connector = _get_connector()
            connector.connect()
            positions = connector.get_positions()
            return jsonify(positions)
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/binance/cancel', methods=['POST'])
def binance_cancel():
    """Cancel an open order."""
    data     = request.get_json() or {}
    order_id = data.get('order_id', '')
    symbol   = data.get('symbol', '')
    try:
        connector = _get_connector()
        connector.connect()
        ok = connector.cancel_order(order_id, symbol)
        return jsonify({'status': 'ok' if ok else 'error', 'order_id': order_id})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/binance/lot_info/<path:symbol>')
def binance_lot_info(symbol):
    """Return lot-size/precision info for a symbol."""
    try:
        connector = _get_connector()
        connector.connect()
        info = connector.get_lot_info(symbol)
        # Also compute estimated qty
        price_hint = float(request.args.get('price', 0))
        notional   = float(request.args.get('notional', 100))
        leverage   = float(request.args.get('leverage', 20))
        if price_hint > 0:
            info['estimated_qty'] = connector.calc_qty(symbol, notional, price_hint, leverage)
            info['margin_required'] = round(notional / leverage, 2)
        return jsonify(info)
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/binance/save_settings', methods=['POST'])
def binance_save_settings():
    """Save position-sizing settings to pairs.yaml."""
    data = request.get_json() or {}
    try:
        config = load_config()
        paper_cfg = config.get('paper_trading', {})
        if 'notional_usd' in data:
            paper_cfg['fixed_notional_usd'] = float(data['notional_usd'])
        if 'leverage' in data:
            paper_cfg['margin_leverage'] = float(data['leverage'])
        if 'max_concurrent' in data:
            config['backtester']['max_concurrent_trades'] = int(data['max_concurrent'])
        if 'risk_pct' in data:
            config['signal_engine']['risk_per_trade'] = float(data['risk_pct']) / 100
        config['paper_trading'] = paper_cfg
        import yaml
        with open(CONFIG_PATH, 'w') as f:
            yaml.dump(config, f, default_flow_style=False, sort_keys=False)
        return jsonify({'status': 'ok', 'message': 'Settings saved'})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO)
    app.run(host='0.0.0.0', port=5000, debug=False)
