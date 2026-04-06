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
app.secret_key = os.environ.get('FLASK_SECRET', 'clh-secret-2026-!@#')

# ── Admin password (set via env var ADMIN_PASS or config) ───────────────────
def get_admin_password():
    pw = os.environ.get('ADMIN_PASS', '')
    if not pw:
        try:
            cfg = load_config()
            pw = cfg.get('admin_password', '')
        except Exception:
            pass
    return pw or 'admin1234'   # default — change via ADMIN_PASS env or config admin_password

def check_admin_token(req):
    """Return True if request carries a valid admin token."""
    pw = get_admin_password()
    token = req.headers.get('X-Admin-Token') or req.args.get('admin_token') or \
            req.cookies.get('clh_admin_token')
    import hashlib
    expected = hashlib.sha256(pw.encode()).hexdigest()[:32]
    return token == expected

@app.route('/api/admin/login', methods=['POST'])
def admin_login():
    """Verify admin password, return token if correct."""
    import hashlib
    data = request.get_json() or {}
    pw = get_admin_password()
    if data.get('password') == pw:
        token = hashlib.sha256(pw.encode()).hexdigest()[:32]
        resp = jsonify({'status': 'ok', 'token': token})
        resp.set_cookie('clh_admin_token', token, max_age=86400*30,
                        httponly=False, samesite='Lax')
        return resp
    return jsonify({'status': 'error', 'message': 'Wrong password'}), 401

@app.route('/api/admin/check')
def admin_check():
    """Check if current session is admin."""
    return jsonify({'admin': check_admin_token(request)})


@app.route('/api/live_settings')
def get_live_settings():
    """
    Quick endpoint to get live trading settings.
    Used by trade_executor, Telegram bot, and dashboard lot-size preview.
    Always reads fresh from config (no cache) so changes apply immediately.
    """
    try:
        cfg      = load_config()
        live_cfg = cfg.get('live_trading', {})
        return jsonify({
            'fixed_notional_usd':  float(live_cfg.get('fixed_notional_usd', 20.0)),
            'margin_leverage':     float(live_cfg.get('margin_leverage', 20.0)),
            'commission_per_trade':float(live_cfg.get('commission_per_trade', 0.001)),
            'position_sizing':     live_cfg.get('position_sizing', 'fixed_notional'),
            'risk_percent':        float(live_cfg.get('risk_percent', 1.0)),
            'max_notional_usd':    float(live_cfg.get('max_notional_usd', 500.0)),
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500


def _auto_start_monitor():
    """
    Auto-start position monitor on gunicorn startup for the active live account.
    Called once per worker via with app.app_context().
    Uses monitor_interval_sec from config (Settings → Signal Execution).
    """
    try:
        config = load_config()
        active_id = config.get('active_account_id')
        if not active_id:
            return
        store = DataStore()
        acct  = store.get_account(int(active_id))
        if not acct:
            return
        is_paper = acct.get('mode', 'paper') == 'paper'
        monitor_interval = int(config.get('signal_execution', {}).get('monitor_interval_sec', 5))

        if is_paper:
            from core.position_monitor import PositionMonitor, _monitors, _monitors_lock
            try:
                connector = _make_connector_from_account(acct, connect=False)
            except Exception as ce:
                logger.error(f"[Startup] Connector creation failed: {ce}")
                connector = None
            m = PositionMonitor(connector, store, account_id=int(active_id), interval=monitor_interval)
            m._api_ok = False   # skip API calls, use public price only
            m.start()
            with _monitors_lock:
                _monitors[int(active_id)] = m
            logger.info(f"[Startup] Position monitor started (paper mode, price-watch) for account {active_id}")
            return

        connector = _make_connector_from_account(acct, connect=True)
        if connector and connector.connected:
            from core.position_monitor import start_monitor
            start_monitor(connector, store, account_id=int(active_id), interval=monitor_interval)
            logger.info(f"[Startup] Position monitor auto-started for account {active_id}")
        else:
            # API restricted — still start monitor in price-watch-only mode
            from core.position_monitor import start_monitor, PositionMonitor
            m = PositionMonitor(connector, store, account_id=int(active_id), interval=monitor_interval)
            m._api_ok = False   # skip API calls, use public price only
            m.start()
            from core.position_monitor import _monitors, _monitors_lock
            with _monitors_lock:
                _monitors[int(active_id)] = m
            logger.info(f"[Startup] Position monitor started (price-watch mode) for account {active_id}")
    except Exception as e:
        logger.warning(f"[Startup] Auto-start monitor failed: {e}")


# Auto-start monitor when gunicorn worker first handles a request
_monitor_started = False
_MONITOR_LOCK_FILE = '/tmp/clh_monitor.lock'

@app.before_request
def _on_first_request():
    global _monitor_started
    if _monitor_started:
        return
    # Use a file-based lock so only ONE gunicorn worker starts the monitor
    import os, fcntl
    try:
        # Open in append/read mode so we don't truncate it before acquiring the lock
        lock_fd = open(_MONITOR_LOCK_FILE, 'a+')
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        # Got the lock — we are the first worker
        # Check if another worker already wrote a PID
        lock_fd.seek(0)
        content = lock_fd.read().strip()
        if content:
            # Already started by another worker that exited cleanly but left file?
            # Actually, just assume we own it if we got the exclusive lock
            pass
        lock_fd.seek(0)
        lock_fd.truncate()
        _monitor_started = True
        _auto_start_monitor()
        # Write PID to lock file
        lock_fd.write(str(os.getpid()))
        lock_fd.flush()
        app._monitor_lock_fd = lock_fd
    except (IOError, OSError):
        # Another worker already has the lock — skip
        _monitor_started = True


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
        volume_multiplier=config['sweep_detector'].get('volume_multiplier', 2.5),
        confirmation_bars=config['sweep_detector'].get('confirmation_bars', 5),
        wick_ratio=config['sweep_detector'].get('wick_ratio', 0.5),
        min_sweep_pct=config['sweep_detector']['min_sweep_pct'],
        min_body_ratio=config['sweep_detector'].get('min_body_ratio', 0.4),
        lookback_bars=config['sweep_detector'].get('lookback_bars', 24)
    )
    # Use live_trading config when account is live, paper otherwise
    _active_acct_id = config.get('active_account_id')
    _acct_mode = 'paper'
    if _active_acct_id:
        try:
            _acct = store.get_account(_active_acct_id)
            _acct_mode = _acct.get('mode', 'paper') if _acct else 'paper'
        except Exception:
            pass
    trade_cfg = config.get('live_trading' if _acct_mode == 'live' else 'paper_trading', {})
    engine = SignalEngine(
        risk_per_trade=config['signal_engine']['risk_per_trade'],
        retracement_levels=config['signal_engine']['retracement_levels'],
        stop_buffer_pct=config['signal_engine']['stop_buffer_pct'],
        min_risk_reward=config['signal_engine']['min_risk_reward'],
        position_sizing=trade_cfg.get('position_sizing', 'fixed_notional'),
        fixed_notional_usd=trade_cfg.get('fixed_notional_usd', 20.0),
        margin_leverage=trade_cfg.get('margin_leverage', 10.0),
        commission_pct=trade_cfg.get('commission_per_trade', 0.001),
        min_sl_gap_pct=config.get('signal_execution', {}).get('min_sl_gap_pct', 0.5)
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


@app.route('/api/activity')
def get_activity():
    """Return recent activity log entries from the daily JSONL file."""
    import glob
    today = datetime.utcnow().strftime('%Y%m%d')
    log_dir = os.path.join(PROJECT_ROOT, 'logs')
    log_file = os.path.join(log_dir, f'activity_{today}.jsonl')
    entries = []
    limit = request.args.get('limit', 50, type=int)
    try:
        if os.path.exists(log_file):
            with open(log_file, 'r') as f:
                lines = f.readlines()
            for line in lines[-limit:]:
                line = line.strip()
                if line:
                    try:
                        entries.append(json.loads(line))
                    except json.JSONDecodeError:
                        pass
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    return jsonify(entries)


# ═══════════════════════════════════════════════════════════════════════════════
# Phase 3A — Binance Trading Routes
# These routes are NEW and completely isolated from existing strategy logic.
# ═══════════════════════════════════════════════════════════════════════════════

def _get_active_account_id() -> int:
    """Get the persisted active account id from config."""
    try:
        cfg = load_config()
        return cfg.get('active_account_id', None)
    except Exception:
        return None

def _set_active_account_id(account_id: int):
    """Persist the active account id to config."""
    try:
        import yaml
        cfg = load_config()
        cfg['active_account_id'] = account_id
        with open(CONFIG_PATH, 'w') as f:
            yaml.dump(cfg, f, default_flow_style=False, sort_keys=False)
    except Exception as e:
        logger.error(f"_set_active_account_id error: {e}")

def _get_connector(connect=False):
    """Build a BinanceConnector from current config. Fresh each call. connect=True tests the link."""
    from core.binance_connector import BinanceConnector
    config = load_config()
    bc = config.get('binance_connection', {})
    c = BinanceConnector(
        api_key    = bc.get('api_key', ''),
        api_secret = bc.get('api_secret', ''),
        testnet    = bc.get('testnet', True),
        mode       = bc.get('mode', 'paper'),
    )
    if connect:
        c.connect()
    return c


@app.route('/api/binance/status')
def binance_status():
    """Return Binance connection status, balance, mode. Fast — no network in paper mode."""
    try:
        config = load_config()
        bc_cfg = config.get('binance_connection', {})
        mode   = bc_cfg.get('mode', 'paper')

        if mode == 'paper':
            # Paper mode: instant response, no network call
            return jsonify({
                'connected':           True,
                'mode':                'paper',
                'account_type':        'paper',
                'testnet':             bc_cfg.get('testnet', True),
                'enabled':             bc_cfg.get('enabled', False),
                'balance_usdt_free':   10000.0,
                'balance_usdt_total':  10000.0,
                'balance_usdt_locked': 0.0,
                'positions_count':     0,
            })

        # Testnet/live: actually check
        from core.binance_connector import BinanceConnector
        connector = BinanceConnector(
            api_key    = bc_cfg.get('api_key', ''),
            api_secret = bc_cfg.get('api_secret', ''),
            testnet    = bc_cfg.get('testnet', True),
            mode       = mode,
        )
        connected = connector.connect()
        balance   = connector.get_balance('USDT') if connected else {'free': 0, 'total': 0, 'used': 0}
        positions = connector.get_positions() if connected else []
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
    """Save and test Binance API credentials. Returns detailed error on failure."""
    from core.trade_executor import TradeExecutor
    from core.binance_connector import BinanceConnector
    data        = request.get_json() or {}
    api_key     = data.get('api_key', '').strip()
    api_secret  = data.get('api_secret', '').strip()
    environment = data.get('environment', 'mainnet')  # mainnet / testnet / demo
    mode        = data.get('mode', 'paper')
    acct_name   = data.get('account_name', '').strip() or None

    # Derive flags from environment
    testnet = (environment == 'testnet')
    is_demo = (environment == 'demo') or (mode == 'demo')
    if is_demo: mode = 'demo'

    if mode == 'paper':
        TradeExecutor.save_connection_config('paper', 'paper', False, 'paper',
                                             is_demo=False, environment='mainnet')
        store = DataStore()
        if acct_name:
            aid = store.save_account(name=acct_name, api_key='paper', api_secret='paper',
                                     mode='paper', environment='mainnet')
            store.update_account_balance(aid, 10000.0)
        return jsonify({'status': 'connected', 'mode': 'paper', 'account_type': 'paper',
                        'testnet': False, 'balance': 10000.0, 'balance_total': 10000.0,
                        'message': '📝 Paper Trading active — no API key needed'})

    if not api_key or not api_secret:
        return jsonify({'status': 'error',
                        'message': '⚠️ API Key and Secret cannot be empty'}), 400

    try:
        connector = BinanceConnector(api_key=api_key, api_secret=api_secret,
                                     testnet=testnet, mode=mode,
                                     is_demo=is_demo, environment=environment)
        ok = connector.connect()
        if ok:
            TradeExecutor.save_connection_config(api_key, api_secret, testnet, mode,
                                                 is_demo=is_demo, environment=environment)
            # Also save to accounts table
            store = DataStore()
            name  = acct_name or f"Account ({environment.title()})"
            aid   = store.save_account(name=name, api_key=api_key, api_secret=api_secret,
                                       mode=mode, environment=environment,
                                       testnet=testnet, is_demo=is_demo)
            balance      = connector.get_balance('USDT')
            store.update_account_balance(aid, round(balance.get('free', 0), 2))
            account_type = connector.account_type or mode
            env_label    = {'demo': '🟡 Demo Trading', 'testnet': '🔵 Testnet',
                            'mainnet': '🔴 Mainnet LIVE'}.get(environment, environment)
            type_label   = {'futures': '📊 Futures USDM', 'spot': '🔵 Spot',
                            'paper': '📝 Paper', 'demo': '🟡 Demo Futures'}.get(account_type, account_type)
            return jsonify({
                'status':       'connected',
                'mode':         mode,
                'account_type': account_type,
                'account_id':   aid,
                'environment':  environment,
                'testnet':      testnet,
                'balance':      round(balance.get('free', 0), 2),
                'balance_total':round(balance.get('total', 0), 2),
                'message':      f"✅ Connected — {type_label} | {env_label} | Balance: ${balance.get('free',0):.2f} USDT free"
            })
        else:
            err = connector.last_error or 'Connection failed'
            # Smart tips based on error
            tips = []
            if 'IP not whitelisted' in err or 'IP' in err:
                tips.append("• In Binance Demo Trading → API Management → Edit key")
                tips.append("• Under 'Access Restrictions' → select 'Unrestricted (Less Secure)'")
                tips.append("• OR add server IP 76.13.247.112 to the whitelist")
                tips.append("• Save and wait 1-2 minutes, then try again")
            elif 'Invalid API Key' in err:
                tips.append("• Copy the full API key (no spaces, no line breaks)")
                tips.append("• Make sure the key isn't deleted or expired in Binance")
            elif 'Wrong environment' in err or 'Demo' in err:
                tips.append("• Use API keys created inside Binance Demo Trading mode")
                tips.append("• Demo keys only work with Demo environment setting")
            elif 'Testnet' in err:
                tips.append("• Testnet keys must be created at testnet.binancefuture.com")
            elif 'Futures' in err or 'permission' in err.lower():
                tips.append("• Go to Binance → API Management → Edit → check 'Enable Futures'")
            return jsonify({'status': 'error', 'message': f"❌ {err}", 'tips': tips}), 400
    except Exception as e:
        return jsonify({'status': 'error', 'message': f"❌ Unexpected error: {str(e)[:200]}",
                        'tips': ['Check your internet connection', 'Try Paper Trading mode first']}), 400


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
    """
    Return open positions.
    Live/demo/testnet: fetch direct from Binance + merge with DB trade metadata.
    Paper: return DB open trades with simulated PnL.
    """
    try:
        import urllib.request as _req
        config = load_config()
        store  = DataStore()

        # Try to get live Binance futures positions (real money / demo / testnet)
        live_positions = {}
        live_connected = False
        active_id = config.get('active_account_id')
        live_connector = None
        if active_id:
            try:
                acct = store.get_account(int(active_id))
                if acct and acct.get('mode', 'paper') != 'paper':
                    live_connector = _make_connector_from_account(acct, connect=True)
                    if live_connector and live_connector.connected:
                        live_connected = True
                        positions = live_connector.get_positions()
                        for p in positions:
                            sym = p.get('symbol', '')
                            clean = sym.split(':')[0] if ':' in sym else sym
                            live_positions[sym]   = p
                            live_positions[clean] = p
            except Exception as e:
                logger.warning(f"Live positions fetch failed: {e}")

        # Always fetch current public prices for paper PnL + any unmatched DB trades
        price_map = {}
        try:
            with _req.urlopen('https://api.binance.com/api/v3/ticker/price', timeout=5) as resp:
                for item in json.loads(resp.read()):
                    price_map[item['symbol']] = float(item['price'])
        except Exception:
            pass

        # Load DB open trades
        db_trades = store.get_open_trades()

        # For live accounts: if Binance has positions that aren't in DB, add them
        # This handles the case where DB got out of sync (false closes, restarts etc.)
        if live_connected and live_positions:
            db_pairs = {
                (t.get('pair','').split(':',1)[1] if ':' in t.get('pair','') else t.get('pair','')).replace('/','')
                for t in db_trades
            }
            for sym_key, live_p in list(live_positions.items()):
                raw_sym = live_p.get('raw_symbol', sym_key.replace('/','').replace(':USDT','USDT'))
                if raw_sym not in db_pairs and ':' not in sym_key:
                    # Live position exists on Binance but not in DB — add synthetic entry
                    clean_pair = sym_key.replace(':USDT','/USDT') if ':USDT' in sym_key else sym_key
                    db_trades.append({
                        'id':           None,
                        'pair':         f'binance:{clean_pair.split(":")[0]}',
                        'direction':    live_p.get('side', 'short'),
                        'entry_price':  live_p.get('entry_price', 0),
                        'sl':           None,
                        'tp':           None,
                        'notional_usd': live_p.get('notional', 0),
                        'commission_usd': 0,
                        'mode':         'live',
                        'status':       'open',
                        'entry_time':   None,
                        '_from_binance': True,   # marker: not in DB
                    })

        result = []
        for t in db_trades:
            pair   = t.get('pair', '')
            sym    = pair.split(':', 1)[1] if ':' in pair else pair
            ccxt_s = (sym.split('/')[0] + '/' + sym.split('/')[1] + ':' + sym.split('/')[1]) if '/' in sym else sym
            binance_sym = sym.replace('/', '')
            direction   = t.get('direction', 'long')
            ep          = float(t.get('entry_price', 0) or 0)
            notional    = float(t.get('notional_usd', 0) or 0)
            commission  = float(t.get('commission_usd', 0) or 0)
            trade_mode  = t.get('mode', 'paper')

            # Try live position first (gives exact mark price + real PnL)
            live_p = live_positions.get(ccxt_s) or live_positions.get(sym)
            if live_p:
                mark   = float(live_p.get('mark_price', 0))
                upnl   = float(live_p.get('unrealized_pnl', 0))
                pct    = upnl / max(abs(float(live_p.get('notional', 1))), 1) * 100
                contracts = float(live_p.get('contracts', 0))
                liq    = float(live_p.get('liquidation_price', 0))
                lev    = float(live_p.get('leverage', 1))
                pos_notional = float(live_p.get('notional', notional))
                display_mode = trade_mode
            else:
                # Compute from public Binance price
                mark = price_map.get(binance_sym, 0.0)
                if mark > 0 and ep > 0 and notional > 0:
                    if direction == 'long':
                        upnl = (mark - ep) / ep * notional - commission * 2
                    else:
                        upnl = (ep - mark) / ep * notional - commission * 2
                    pct = round((mark - ep) / ep * 100 * (1 if direction == 'long' else -1), 3)
                else:
                    upnl = 0.0; pct = 0.0
                contracts    = round(notional / ep, 6) if ep > 0 else 0
                liq          = 0.0; lev = 1.0
                pos_notional = notional
                display_mode = trade_mode

            result.append({
                **t,   # all DB trade fields
                'symbol':            ccxt_s,
                'pair':              pair,
                'direction':         direction,
                'side':              direction,
                'mark_price':        round(mark, 8),
                'unrealized_pnl':    round(upnl, 4),
                'pnl_pct':           round(pct, 3),
                'contracts':         contracts,
                'liquidation_price': round(liq, 6),
                'leverage':          lev,
                'notional':          pos_notional,
                'mode':              display_mode,
                'trade_id':          t.get('id'),
            })

        return jsonify(result)

    except Exception as e:
        logger.error(f"binance_positions error: {e}")
        return jsonify({'error': str(e)}), 500


# (old binance_positions stub removed — replaced by the new implementation above)


@app.route('/api/monitor/status')
def monitor_status():
    """Return position monitor status per account."""
    try:
        from core.position_monitor import _monitors
        result = {}
        for acct_id, m in _monitors.items():
            result[acct_id] = {
                'running':    m.is_running(),
                'api_ok':     m._api_ok,
                'interval':   m.interval,
                'sltp_placed': list(m._sltp_placed),
                'closed_this_session': list(m._closed_trades),
            }
        return jsonify(result)
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/monitor/restart', methods=['POST'])
def monitor_restart():
    """Restart position monitor (after fixing API key permissions)."""
    try:
        from core.position_monitor import stop_monitor, start_monitor, _monitors
        config    = load_config()
        store     = DataStore()
        active_id = int(config.get('active_account_id', 0))
        acct      = store.get_account(active_id) if active_id else None
        if not acct:
            return jsonify({'error': 'No active account'}), 400
        stop_monitor(active_id)
        connector = _make_connector_from_account(acct, connect=True)
        _mon_interval = int(config.get('signal_execution', {}).get('monitor_interval_sec', 5))
        m = start_monitor(connector, store, account_id=active_id, interval=_mon_interval)
        return jsonify({'status': 'ok', 'running': m.is_running(), 'api_ok': m._api_ok, 'interval': _mon_interval})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/binance/positions/live_pnl')
def binance_live_pnl():
    """
    Fast PnL-only refresh for open positions.
    Returns {ccxt_symbol → {mark_price, unrealized_pnl, pnl_pct}}.
    Uses Binance public price API so no auth needed.
    """
    try:
        import urllib.request as _req
        store    = DataStore()
        trades   = store.get_open_trades()
        if not trades:
            return jsonify({})

        # Build binance→ccxt symbol map
        sym_map = {}   # 'TWTUSDT' → 'TWT/USDT'
        for t in trades:
            pair = t.get('pair', '')
            sym  = pair.split(':', 1)[1] if ':' in pair else pair
            binance_sym = sym.replace('/', '')
            sym_map[binance_sym] = sym

        # Batch fetch all prices
        price_map = {}
        with _req.urlopen('https://api.binance.com/api/v3/ticker/price', timeout=5) as resp:
            for item in json.loads(resp.read()):
                price_map[item['symbol']] = float(item['price'])

        result = {}
        for t in trades:
            pair      = t.get('pair', '')
            ccxt_sym  = pair.split(':', 1)[1] if ':' in pair else pair
            binance_sym = ccxt_sym.replace('/', '')
            ep        = float(t.get('entry_price', 0) or 0)
            notional  = float(t.get('notional_usd', 0) or 0)
            commission= float(t.get('commission_usd', 0) or 0)
            direction = t.get('direction', 'long')
            mark      = price_map.get(binance_sym, 0.0)

            if mark > 0 and ep > 0 and notional > 0:
                if direction == 'long':
                    upnl = (mark - ep) / ep * notional - commission * 2
                else:
                    upnl = (ep - mark) / ep * notional - commission * 2
                pct = round((mark - ep) / ep * 100 * (1 if direction == 'long' else -1), 3)
            else:
                upnl = 0.0; pct = 0.0

            result[ccxt_sym] = {
                'mark_price':     round(mark, 8),
                'unrealized_pnl': round(upnl, 4),
                'pnl_pct':        pct,
            }
        return jsonify(result)
    except Exception as e:
        logger.warning(f"live_pnl error: {e}")
        return jsonify({})


@app.route('/api/binance/book_sltp', methods=['POST'])
def book_sltp():
    """
    Place SL + TP bracket orders for an existing live Binance position.
    Body: {symbol, direction, qty, sl_price, tp_price, trade_id (optional)}
    Works for live/demo/testnet positions.
    """
    data = request.get_json() or {}
    symbol    = data.get('symbol', '').strip()
    direction = data.get('direction', 'long')
    qty       = float(data.get('qty', 0))
    sl_price  = float(data.get('sl_price', 0))
    tp_price  = float(data.get('tp_price', 0))
    trade_id  = data.get('trade_id')

    if not symbol:
        return jsonify({'error': 'symbol required'}), 400
    if qty <= 0:
        return jsonify({'error': 'qty must be > 0'}), 400
    if sl_price <= 0 and tp_price <= 0:
        return jsonify({'error': 'at least sl_price or tp_price required'}), 400

    try:
        connector = _get_connector(connect=True)
        if not connector.connected:
            return jsonify({'error': 'Not connected to Binance'}), 400
        if connector.mode == 'paper':
            return jsonify({'error': 'Cannot place real bracket orders in paper mode'}), 400

        result = connector.set_sl_tp(symbol, direction, qty, sl_price, tp_price)
        if 'error' in result:
            return jsonify({'error': result['error']}), 400

        # Update DB trade with SL/TP if trade_id given
        if trade_id:
            import sqlite3
            from pathlib import Path
            db_path = Path('/root/.openclaw/workspace/projects/crypto-liquidity-hunter/data/store.db')
            with sqlite3.connect(db_path) as conn:
                conn.execute(
                    "UPDATE trades SET sl=?, tp=? WHERE id=? AND status='open'",
                    (sl_price or None, tp_price or None, trade_id)
                )
                conn.commit()

        return jsonify({
            'status':       'ok',
            'sl_order_id':  result.get('sl_order_id'),
            'tp_order_id':  result.get('tp_order_id'),
            'sl_price':     result.get('sl_price'),
            'tp_price':     result.get('tp_price'),
        })
    except Exception as e:
        logger.error(f"book_sltp error: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/api/binance/close_position', methods=['POST'])
def close_position():
    """
    Close a live Binance position immediately at market.
    Body: {symbol, direction, qty, trade_id (optional)}
    """
    data      = request.get_json() or {}
    symbol    = data.get('symbol', '').strip()
    direction = data.get('direction', 'long')
    qty       = float(data.get('qty', 0))
    trade_id  = data.get('trade_id')

    if not symbol or qty <= 0:
        return jsonify({'error': 'symbol and qty required'}), 400

    try:
        connector = _get_connector(connect=True)
        if not connector.connected:
            return jsonify({'error': 'Not connected to Binance'}), 400

        close_side = 'sell' if direction == 'long' else 'buy'

        if connector.mode == 'paper':
            # Paper close: just update DB
            exit_price = 0
            try:
                import ccxt as _ccxt
                _ex = _ccxt.binance({'enableRateLimit': False})
                ticker = _ex.fetch_ticker(symbol)
                exit_price = float(ticker['last'])
            except Exception:
                pass
            if trade_id:
                exec_inst = __import__('core.trade_executor', fromlist=['TradeExecutor'])
                te = exec_inst.TradeExecutor()
                te.close_trade(trade_id, symbol, exit_price, reason='manual')
            return jsonify({'status': 'ok', 'mode': 'paper', 'exit_price': exit_price})

        # Live: place reduceOnly market order
        qty_r  = connector._round_qty(symbol, qty)
        order  = connector.exchange.create_order(
            symbol, 'market', close_side, qty_r,
            params={'reduceOnly': True}
        )
        exit_price = float(order.get('average') or order.get('price') or 0)

        # Cancel any open SL/TP orders for this symbol
        try:
            open_orders = connector.exchange.fetch_open_orders(symbol)
            for o in open_orders:
                if o.get('reduceOnly'):
                    connector.exchange.cancel_order(o['id'], symbol)
        except Exception as e:
            logger.warning(f"Cancel bracket orders warning: {e}")

        # Close DB trade
        if trade_id:
            from core.trade_executor import TradeExecutor
            te = TradeExecutor(connector=connector)
            te.store = DataStore()
            te.close_trade(trade_id, symbol, exit_price, reason='manual_close')

        logger.info(f"Position closed: {close_side} {qty_r} {symbol} @ {exit_price}")
        return jsonify({
            'status':      'ok',
            'order_id':    order.get('id'),
            'exit_price':  exit_price,
            'side':        close_side,
            'qty':         qty_r,
        })
    except Exception as e:
        logger.error(f"close_position error: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/api/binance/cancel', methods=['POST'])
def binance_cancel():
    """Cancel an open order."""
    data     = request.get_json() or {}
    order_id = data.get('order_id', '')
    symbol   = data.get('symbol', '')
    try:
        connector = _get_connector(connect=True)
        ok = connector.cancel_order(order_id, symbol)
        return jsonify({'status': 'ok' if ok else 'error', 'order_id': order_id})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/binance/lot_info/<path:symbol>')
def binance_lot_info(symbol):
    """
    Return lot-size/precision info + fully calculated lot size for a symbol.
    Supports both paper and live mode.
    Also accounts for live balance when position_sizing=risk_percent.
    """
    try:
        cfg       = load_config()
        store     = DataStore()
        active_id = cfg.get('active_account_id')

        # Determine effective mode and connector
        mode = 'paper'
        connector = None
        live_balance = None
        if active_id:
            acct = store.get_account(int(active_id))
            if acct and acct.get('mode', 'paper') != 'paper':
                mode = acct.get('mode', 'live')
                connector = _make_connector_from_account(acct, connect=True)
                if connector and connector.connected:
                    try:
                        bal_info = connector.get_balance('USDT')
                        live_balance = float(bal_info.get('free', 0) or 0)
                    except Exception:
                        pass

        if connector is None:
            from core.binance_connector import BinanceConnector
            connector = BinanceConnector(mode='paper')
            connector.connect()

        # Get lot size rules from exchange
        info = connector.get_lot_info(symbol)

        # Parameters from request or config
        price_hint = float(request.args.get('price', 0))
        cfg_section = 'live_trading' if mode != 'paper' else 'paper_trading'
        trade_cfg  = cfg.get(cfg_section, cfg.get('paper_trading', {}))

        sizing_mode  = request.args.get('sizing_mode') or trade_cfg.get('position_sizing', 'fixed_notional')
        base_notional = float(request.args.get('notional')  or trade_cfg.get('fixed_notional_usd', 100))
        leverage     = float(request.args.get('leverage')   or trade_cfg.get('margin_leverage', 20))
        risk_pct     = float(request.args.get('risk_pct')   or trade_cfg.get('risk_percent', 1.0))
        commission   = float(trade_cfg.get('commission_per_trade', 0.001))
        max_notional = float(trade_cfg.get('max_notional_usd', 500))

        # Calculate effective notional
        if sizing_mode == 'risk_percent' and live_balance and live_balance > 0:
            notional = min(live_balance * (risk_pct / 100.0) * leverage, max_notional)
        else:
            notional = min(base_notional, max_notional)

        # Calculate qty using exchange rules
        if price_hint > 0:
            qty = connector.calc_qty(symbol, notional, price_hint, leverage)
            margin_required = round(notional / leverage, 4)
            commission_usd  = round(notional * commission, 4)
            notional_actual = round(qty * price_hint, 4)

            info['estimated_qty']    = qty
            info['notional_usd']     = round(notional, 2)
            info['notional_actual']  = notional_actual
            info['margin_required']  = margin_required
            info['commission_usd']   = commission_usd
            info['leverage']         = leverage
            info['sizing_mode']      = sizing_mode
            info['live_balance']     = round(live_balance, 2) if live_balance else None
            info['price_used']       = price_hint
            info['max_notional']     = max_notional

            # Validation warnings
            warnings = []
            if qty <= 0:
                warnings.append('Qty rounds to 0 — increase notional or reduce leverage')
            if notional_actual < 5:
                warnings.append(f'Notional too small (${notional_actual:.2f} < $5 min)')
            if margin_required > (live_balance or float('inf')):
                warnings.append(f'Insufficient margin: need ${margin_required:.2f}, have ${live_balance:.2f}')
            info['warnings'] = warnings
            info['valid'] = len(warnings) == 0

        return jsonify(info)
    except Exception as e:
        logger.error(f"lot_info error: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/api/binance/get_settings', methods=['GET'])
def binance_get_settings():
    """Return position-sizing settings for both paper and live modes."""
    try:
        config    = load_config()
        paper_cfg = config.get('paper_trading', {})
        live_cfg  = config.get('live_trading', {})
        return jsonify({
            # Paper
            'notional_usd':       float(paper_cfg.get('fixed_notional_usd', 20.0)),
            'leverage':           float(paper_cfg.get('margin_leverage', 20.0)),
            'commission':         float(paper_cfg.get('commission_per_trade', 0.001)),
            'position_sizing':    paper_cfg.get('position_sizing', 'fixed_notional'),
            # Live
            'live_notional_usd':  float(live_cfg.get('fixed_notional_usd', 20.0)),
            'live_leverage':      float(live_cfg.get('margin_leverage', 20.0)),
            'live_commission':    float(live_cfg.get('commission_per_trade', 0.001)),
            'live_position_sizing': live_cfg.get('position_sizing', 'fixed_notional'),
            'live_risk_percent':  float(live_cfg.get('risk_percent', 1.0)),
            'live_max_notional':  float(live_cfg.get('max_notional_usd', 500.0)),
            'max_concurrent':     int(config.get('backtester', {}).get('max_concurrent_trades', 3)),
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/binance/save_settings', methods=['POST'])
def binance_save_settings():
    """Save position-sizing settings for paper AND live modes to pairs.yaml."""
    data = request.get_json() or {}
    try:
        import yaml as _yaml
        config    = load_config()
        paper_cfg = config.get('paper_trading', {})
        live_cfg  = config.get('live_trading', {})

        # Paper settings
        if 'notional_usd'    in data: paper_cfg['fixed_notional_usd']   = float(data['notional_usd'])
        if 'leverage'        in data: paper_cfg['margin_leverage']       = float(data['leverage'])
        if 'commission'      in data: paper_cfg['commission_per_trade']  = float(data['commission'])
        if 'position_sizing' in data: paper_cfg['position_sizing']       = data['position_sizing']

        # Live settings
        if 'live_notional_usd'     in data: live_cfg['fixed_notional_usd']   = float(data['live_notional_usd'])
        if 'live_leverage'         in data: live_cfg['margin_leverage']       = float(data['live_leverage'])
        if 'live_commission'       in data: live_cfg['commission_per_trade']  = float(data['live_commission'])
        if 'live_position_sizing'  in data: live_cfg['position_sizing']       = data['live_position_sizing']
        if 'live_risk_percent'     in data: live_cfg['risk_percent']           = float(data['live_risk_percent'])
        if 'live_max_notional'     in data: live_cfg['max_notional_usd']      = float(data['live_max_notional'])

        if 'max_concurrent' in data:
            config.setdefault('backtester', {})['max_concurrent_trades'] = int(data['max_concurrent'])

        config['paper_trading'] = paper_cfg
        config['live_trading']  = live_cfg
        with open(CONFIG_PATH, 'w') as f:
            _yaml.dump(config, f, default_flow_style=False, sort_keys=False)
        return jsonify({'status': 'ok', 'message': 'Settings saved for paper + live'})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ═══════════════════════════════════════════════════════════════════════════════
# Multi-Account Management Routes
# ═══════════════════════════════════════════════════════════════════════════════

def _make_connector_from_account(acct: dict, connect: bool = True):
    """Build a BinanceConnector from an account dict."""
    from core.binance_connector import BinanceConnector
    c = BinanceConnector(
        api_key     = acct.get('api_key', ''),
        api_secret  = acct.get('api_secret', ''),
        testnet     = bool(acct.get('testnet', 0)),
        mode        = acct.get('mode', 'paper'),
        is_demo     = bool(acct.get('is_demo', 0)),
        environment = acct.get('environment', 'mainnet'),
    )
    if connect:
        c.connect()
    return c


@app.route('/api/accounts/active', methods=['GET'])
def get_active_account():
    """Return the persisted active account id."""
    return jsonify({'active_account_id': _get_active_account_id()})

@app.route('/api/accounts/active', methods=['POST'])
def set_active_account():
    """Persist the active account id server-side."""
    data = request.get_json() or {}
    account_id = data.get('account_id')
    _set_active_account_id(account_id)
    return jsonify({'status': 'ok', 'active_account_id': account_id})

@app.route('/api/accounts', methods=['GET'])
def list_accounts():
    """List all saved accounts (secrets masked)."""
    store = DataStore()
    accounts = store.get_accounts(include_secrets=False)
    # Inject active_account_id so frontend knows which is active on load
    active_id = _get_active_account_id()
    return jsonify({'accounts': accounts, 'active_account_id': active_id})


@app.route('/api/accounts', methods=['POST'])
def add_account():
    """Add a new account. Validates connectivity (unless paper mode)."""
    from core.binance_connector import BinanceConnector
    from core.trade_executor import TradeExecutor
    data = request.get_json() or {}
    name        = data.get('name', '').strip()
    api_key     = data.get('api_key', '').strip()
    api_secret  = data.get('api_secret', '').strip()
    mode        = data.get('mode', 'paper')
    environment = data.get('environment', 'mainnet')
    notes       = data.get('notes', '')

    if not name:
        return jsonify({'status': 'error', 'message': 'Account name is required'}), 400

    # Derive testnet/is_demo from environment
    testnet = (environment == 'testnet')
    is_demo = (environment == 'demo') or (mode == 'demo')
    if is_demo:
        mode = 'demo'

    store = DataStore()

    if mode == 'paper':
        account_id = store.save_account(
            name=name, api_key=api_key or 'paper', api_secret=api_secret or 'paper',
            mode='paper', environment=environment, testnet=False, is_demo=False, notes=notes
        )
        store.update_account_balance(account_id, 10000.0)
        return jsonify({
            'status':     'ok',
            'account_id': account_id,
            'message':    f'✅ Paper account "{name}" added (simulated $10,000 balance)',
            'balance':    10000.0,
        })

    if not api_key or not api_secret:
        return jsonify({'status': 'error', 'message': 'API Key and Secret required for non-paper mode'}), 400

    # Test connection
    connector = BinanceConnector(
        api_key=api_key, api_secret=api_secret,
        testnet=testnet, mode=mode, is_demo=is_demo, environment=environment
    )
    ok = connector.connect()
    if not ok:
        err = connector.last_error or 'Connection failed'
        tips = []
        if 'IP not whitelisted' in err or 'IP' in err:
            tips = [
                "In Binance Demo Trading → API Management → Edit key",
                "Under 'Access Restrictions' → select 'Unrestricted (Less Secure)'",
                "OR add server IP 76.13.247.112 to the whitelist, then wait 1-2 min",
            ]
        return jsonify({'status': 'error', 'message': f'❌ {err}', 'tips': tips}), 400

    balance_info = connector.get_balance('USDT')
    balance      = round(balance_info.get('free', 0), 2)
    account_id   = store.save_account(
        name=name, api_key=api_key, api_secret=api_secret,
        mode=mode, environment=environment, testnet=testnet, is_demo=is_demo, notes=notes
    )
    store.update_account_balance(account_id, balance)
    env_label  = {'demo': 'Demo Trading', 'testnet': 'Testnet', 'mainnet': 'Mainnet LIVE'}.get(environment, environment)
    acc_label  = {'futures': 'Futures USDM', 'spot': 'Spot', 'demo': 'Demo Futures'}.get(connector.account_type, connector.account_type or mode)
    return jsonify({
        'status':       'ok',
        'account_id':   account_id,
        'account_type': connector.account_type,
        'balance':      balance,
        'message':      f'✅ "{name}" connected — {acc_label} | {env_label} | Balance: ${balance} USDT',
    })


@app.route('/api/accounts/<int:account_id>', methods=['DELETE'])
def delete_account(account_id):
    """Delete (soft) an account."""
    store = DataStore()
    ok = store.delete_account(account_id)
    return jsonify({'status': 'ok' if ok else 'error', 'account_id': account_id})


@app.route('/api/accounts/<int:account_id>/connect', methods=['POST'])
def connect_account(account_id):
    """Re-test connection and refresh balance for a saved account."""
    store = DataStore()
    acct  = store.get_account(account_id)
    if not acct:
        return jsonify({'status': 'error', 'message': 'Account not found'}), 404

    if acct.get('mode') == 'paper':
        store.update_account_balance(account_id, 10000.0)
        return jsonify({'status': 'connected', 'balance': 10000.0, 'account_type': 'paper',
                        'message': '📝 Paper account active'})

    connector = _make_connector_from_account(acct, connect=True)
    if not connector.connected:
        return jsonify({'status': 'error', 'message': f'❌ {connector.last_error or "Connection failed"}'}), 400

    balance_info = connector.get_balance('USDT')
    balance      = round(balance_info.get('free', 0), 2)
    store.update_account_balance(account_id, balance)

    # Start position monitor for live/demo/testnet accounts
    if acct.get('mode', 'paper') != 'paper':
        try:
            from core.position_monitor import start_monitor
            _cfg_mon = load_config()
            _mon_int = int(_cfg_mon.get('signal_execution', {}).get('monitor_interval_sec', 5))
            start_monitor(connector, store, account_id=account_id, interval=_mon_int)
            logger.info(f"[App] Position monitor started for account {account_id}")
        except Exception as e:
            logger.warning(f"[App] Could not start position monitor: {e}")

    return jsonify({
        'status':       'connected',
        'balance':      balance,
        'account_type': connector.account_type,
        'message':      f'✅ Connected | Balance: ${balance} USDT | Position monitor active',
    })


@app.route('/api/accounts/<int:account_id>/status', methods=['GET'])
def account_status(account_id):
    """Get live balance + positions for a specific account."""
    store = DataStore()
    acct  = store.get_account(account_id)
    if not acct:
        return jsonify({'status': 'error', 'message': 'Account not found'}), 404

    mode = acct.get('mode', 'paper')
    if mode == 'paper':
        return jsonify({
            'account_id': account_id, 'name': acct['name'],
            'connected': True, 'mode': 'paper',
            'balance_usdt_free': 10000.0, 'balance_usdt_total': 10000.0,
            'positions_count': 0,
        })

    connector = _make_connector_from_account(acct, connect=True)
    if not connector.connected:
        return jsonify({'account_id': account_id, 'name': acct['name'],
                        'connected': False, 'error': connector.last_error or 'Failed',
                        'mode': mode})

    balance   = connector.get_balance('USDT')
    positions = connector.get_positions()
    perms     = connector.check_trade_permissions()
    store.update_account_balance(account_id, round(balance.get('free', 0), 2))
    return jsonify({
        'account_id':          account_id,
        'name':                acct['name'],
        'connected':           True,
        'mode':                mode,
        'environment':         acct.get('environment', 'mainnet'),
        'account_type':        connector.account_type,
        'balance_usdt_free':   round(balance.get('free',  0), 2),
        'balance_usdt_total':  round(balance.get('total', 0), 2),
        'balance_usdt_locked': round(balance.get('used',  0), 2),
        'positions_count':     len(positions),
        'can_trade':           perms.get('can_trade', False),
        'can_futures':         perms.get('can_futures', False),
        'permissions_warning': perms.get('reason', '') if not perms.get('can_trade') else '',
    })


@app.route('/api/accounts/<int:account_id>/execute', methods=['POST'])
def execute_on_account(account_id):
    """Execute a trade on a specific account."""
    from core.trade_executor import TradeExecutor
    data = request.get_json() or {}
    try:
        pair = data.get('pair', 'binance:BTC/USDT')
        # Pre-validate symbol is tradable on Binance Futures
        check = _check_symbol_tradable(pair)
        if not check.get('tradable'):
            return jsonify({
                'error': check.get('reason', f'{pair} cannot be traded'),
                'status': check.get('status', 'UNKNOWN'),
                'tip': 'This pair is not available for futures trading on Binance.'
            }), 400

        executor = TradeExecutor(account_id=account_id)
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
        result   = executor.execute_signal(signal_dict, pair, notional_usd=notional, leverage=leverage)
        return jsonify(result)
    except Exception as e:
        logger.error(f"execute_on_account error: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/api/accounts/<int:account_id>/trades', methods=['GET'])
def account_trades(account_id):
    """Get trades for a specific account."""
    store  = DataStore()
    status = request.args.get('status', 'all')
    limit  = int(request.args.get('limit', 100))
    trades = store.get_trades_by_account(account_id=account_id, status=status, limit=limit)

    def serialize(t):
        t_ser = dict(t)
        for field in ['entry_time', 'exit_time']:
            val = t_ser.get(field)
            if not val: continue
            s = val.isoformat() if isinstance(val, datetime) else str(val)
            if s and '+' not in s and not s.endswith('Z'): s += '+00:00'
            t_ser[field] = s
        return t_ser
    return jsonify([serialize(t) for t in trades])


@app.route('/api/accounts/<int:account_id>/positions', methods=['GET'])
def account_positions(account_id):
    """Get open positions for a specific account."""
    store = DataStore()
    acct  = store.get_account(account_id)
    if not acct:
        return jsonify({'error': 'Account not found'}), 404

    mode = acct.get('mode', 'paper')
    if mode == 'paper':
        trades = store.get_trades_by_account(account_id=account_id, status='open')
        for t in trades: t['mode'] = 'paper'
        return jsonify(trades)

    connector = _make_connector_from_account(acct, connect=True)
    if not connector.connected:
        return jsonify({'error': connector.last_error or 'Failed to connect'}), 400
    positions = connector.get_positions()
    return jsonify(positions)


@app.route('/api/accounts/<int:account_id>/performance', methods=['GET'])
def account_performance(account_id):
    """Get performance metrics for a specific account."""
    store = DataStore()
    return jsonify(store.get_performance_by_account(account_id=account_id))


@app.route('/api/accounts/<int:account_id>/toggle', methods=['POST'])
def toggle_account(account_id):
    """Enable or disable an account (toggle trading on/off)."""
    import sqlite3
    store   = DataStore()
    acct    = store.get_account(account_id)
    if not acct:
        return jsonify({'status': 'error', 'message': 'Account not found'}), 404
    # Toggle: if enabled=1 → set 0, if 0 → set 1
    new_state = 0 if acct.get('enabled', 1) == 1 else 1
    db_path = '/root/.openclaw/workspace/projects/crypto-liquidity-hunter/data/store.db'
    with sqlite3.connect(db_path) as conn:
        conn.execute("UPDATE accounts SET enabled=? WHERE id=?", (new_state, account_id))
        conn.commit()
    state_label = 'Active' if new_state == 1 else 'Paused'
    return jsonify({'status': 'ok', 'account_id': account_id, 'enabled': new_state, 'label': state_label})


@app.route('/api/accounts/<int:account_id>/close', methods=['POST'])
def close_trade_on_account(account_id):
    """Close an open trade for a specific account."""
    from core.trade_executor import TradeExecutor
    data = request.get_json() or {}
    trade_id   = int(data.get('trade_id', 0))
    symbol     = data.get('symbol', '')
    exit_price = float(data.get('exit_price', 0)) or None
    try:
        executor = TradeExecutor(account_id=account_id)
        ok = executor.close_trade(trade_id, symbol, exit_price=exit_price, reason='manual')
        return jsonify({'status': 'ok' if ok else 'error', 'trade_id': trade_id})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ═══════════════════════════════════════════════════════════════════════════════
# Pending Signals Routes
# ═══════════════════════════════════════════════════════════════════════════════

@app.route('/api/pending')
def get_pending_signals():
    """List pending signals."""
    store  = DataStore()
    status = request.args.get('status', 'pending')
    items  = store.get_pending_signals(status=status)
    # Attach current price + distance
    config  = load_config()
    results = []
    for p in items:
        try:
            pair = p['pair']
            exch, sym = pair.split(':', 1) if ':' in pair else ('binance', pair)
            from core.data_fetcher import MarketDataFetcher
            fetcher = MarketDataFetcher(exch)
            df = fetcher.fetch_ohlcv(sym, timeframe='1m', limit=2)
            cur_price = float(df.iloc[-1]['close'])
            dist_pct  = round(abs(cur_price - p['entry_price']) / p['entry_price'] * 100, 3)
            p['current_price'] = cur_price
            p['distance_pct']  = dist_pct
        except Exception:
            p['current_price'] = None
            p['distance_pct']  = None
        results.append(p)
    return jsonify(results)


@app.route('/api/signal_checklist')
def signal_checklist():
    """
    Signal checklist — shows which validation criteria each pending/recent signal meets.
    Derived from settings page thresholds.
    """
    store  = DataStore()
    config = load_config()
    sig_cfg    = config.get('signal_engine', {})
    alert_cfg  = config.get('alerts', {}).get('telegram', {})
    exec_cfg   = config.get('signal_execution', {})

    min_rr          = float(sig_cfg.get('min_risk_reward', 3))
    min_confidence  = float(alert_cfg.get('min_confidence', 0.7))
    min_sl_gap_pct  = float(exec_cfg.get('min_sl_gap_pct', 1.0))
    min_zone_str    = 1  # minimum zone strength to be tradable

    # Volume filter config
    vol_cfg = config.get('volume_filter', {})
    vol_filter_enabled = vol_cfg.get('enabled', False)
    min_volume = float(vol_cfg.get('min_24h_volume_usd', 0))

    # Batch-fetch all 24h volumes in ONE API call (cached for this request)
    import requests as _req
    _volume_map = {}
    try:
        if vol_filter_enabled and min_volume > 0:
            _tick_r = _req.get('https://fapi.binance.com/fapi/v1/ticker/24hr', timeout=10)
            for _t in _tick_r.json():
                _sym = _t.get('symbol', '')
                _qv = float(_t.get('quoteVolume', 0))
                _volume_map[_sym] = _qv
    except Exception:
        pass

    def _evaluate_signal(sig):
        """Evaluate a signal against all checklist criteria."""
        entry    = float(sig.get('entry_price', sig.get('entry', 0)) or 0)
        sl       = float(sig.get('stop_loss', sig.get('sl', 0)) or 0)
        tp       = float(sig.get('target', sig.get('tp', 0)) or 0)
        direction = sig.get('direction', 'long')
        conf     = float(sig.get('confidence', 0) or 0)
        rr       = float(sig.get('risk_reward', 0) or 0)
        zstr     = int(sig.get('zone_strength', sig.get('zone_str', 0)) or 0)
        cur      = sig.get('current_price')

        # Calculate R:R if not provided
        if rr == 0 and entry > 0 and sl > 0 and tp > 0:
            risk = abs(entry - sl)
            reward = abs(tp - entry)
            rr = reward / risk if risk > 0 else 0

        checks = {}

        # 1. SL Direction
        if direction == 'long':
            sl_ok = sl < entry if sl > 0 else False
            checks['sl_direction'] = {
                'pass': sl_ok,
                'label': 'SL Direction',
                'detail': f'SL {"below" if sl_ok else "ABOVE"} entry (LONG)',
                'rule': 'SL must be below entry'
            }
        else:
            sl_ok = sl > entry if sl > 0 else False
            checks['sl_direction'] = {
                'pass': sl_ok,
                'label': 'SL Direction',
                'detail': f'SL {"above" if sl_ok else "BELOW"} entry (SHORT)',
                'rule': 'SL must be above entry'
            }

        # 2. SL Gap %
        sl_gap_pct = abs(sl - entry) / entry * 100 if entry > 0 and sl > 0 else 0
        sl_gap_ok  = sl_gap_pct >= min_sl_gap_pct
        checks['sl_gap'] = {
            'pass': sl_gap_ok,
            'label': 'SL Gap %',
            'detail': f'{sl_gap_pct:.2f}% {"≥" if sl_gap_ok else "<"} {min_sl_gap_pct:.1f}%',
            'rule': f'≥ {min_sl_gap_pct:.1f}%'
        }

        # 3. Risk:Reward
        rr_ok = rr >= min_rr
        checks['risk_reward'] = {
            'pass': rr_ok,
            'label': 'Risk:Reward',
            'detail': f'{rr:.2f} {"≥" if rr_ok else "<"} {min_rr:.1f}',
            'rule': f'≥ {min_rr:.1f}'
        }

        # 4. Confidence
        conf_ok = conf >= min_confidence
        checks['confidence'] = {
            'pass': conf_ok,
            'label': 'Confidence',
            'detail': f'{conf:.0%} {"≥" if conf_ok else "<"} {min_confidence:.0%}',
            'rule': f'≥ {min_confidence:.0%}'
        }

        # 5. Zone Strength (non-critical — show N/A if not stored)
        if zstr > 0:
            zstr_ok = zstr >= min_zone_str
            checks['zone_strength'] = {
                'pass': zstr_ok,
                'label': 'Zone Strength',
                'detail': f'{zstr} {"≥" if zstr_ok else "<"} {min_zone_str}',
                'rule': f'≥ {min_zone_str}'
            }
        else:
            checks['zone_strength'] = {
                'pass': None,
                'label': 'Zone Strength',
                'detail': 'N/A (not stored)',
                'rule': f'≥ {min_zone_str}'
            }

        # 6a. Entry Tolerance — the ACTUAL gate used by main.py entry logic
        #     entry_tolerance_pct from config (default 0.3%). This is the real check
        #     that determines whether the bot will actually fire a pending signal.
        entry_tol = 0.3
        try:
            exec_cfg = cfg.get('signal_execution', {})
            entry_tol = float(exec_cfg.get('entry_tolerance_pct', 0.3))
        except Exception:
            pass
        if cur is not None and entry > 0:
            dist_pct = abs(cur - entry) / entry * 100
            tol_ok = dist_pct <= entry_tol
            checks['entry_tolerance'] = {
                'pass': tol_ok,
                'label': 'Entry Tolerance',
                'detail': f'{dist_pct:.2f}% {"≤" if tol_ok else ">"} {entry_tol}%',
                'rule': f'≤ {entry_tol}% (actual entry gate)'
            }
        else:
            checks['entry_tolerance'] = {
                'pass': None,
                'label': 'Entry Tolerance',
                'detail': 'N/A (no live price)',
                'rule': f'≤ {entry_tol}% (actual entry gate)'
            }

        # 6b. Entry Reachable (loose proximity — informational)
        if cur is not None and entry > 0:
            dist_pct = abs(cur - entry) / entry * 100
            reachable = dist_pct <= 3.0  # within 3% of entry
            checks['entry_reachable'] = {
                'pass': reachable,
                'label': 'Entry Reachable',
                'detail': f'Price ${cur:.6g} — {dist_pct:.1f}% from entry',
                'rule': 'Within ±3% of entry'
            }
        else:
            checks['entry_reachable'] = {
                'pass': None,
                'label': 'Entry Reachable',
                'detail': 'Price unavailable',
                'rule': 'Within ±3% of entry'
            }

        # 7. Entry Direction Valid
        if cur is not None and entry > 0:
            if direction == 'long':
                # For long, price should be at or below entry (need to dip to buy)
                dir_valid = cur >= entry * 0.97  # price not too far below
            else:
                # For short, price should be at or above entry (need to rally to sell)
                dir_valid = cur <= entry * 1.03  # price not too far above
            checks['entry_direction'] = {
                'pass': dir_valid,
                'label': 'Entry Setup Valid',
                'detail': f'{"Long" if direction == "long" else "Short"}: price {"in" if dir_valid else "out of"} range',
                'rule': 'Price in valid range for direction'
            }
        else:
            checks['entry_direction'] = {
                'pass': None,
                'label': 'Entry Setup Valid',
                'detail': 'N/A',
                'rule': 'Price in valid range'
            }

        # 9. 24h Volume (from volume_filter config)
        #     This is the EXACT filter used by main.py scanning loop.
        #     Pairs below this volume are SKIPPED entirely — no entry ever fires.
        if vol_filter_enabled and min_volume > 0:
            # Extract raw symbol from pair (e.g. "binance:SNX/USDT" → "SNXUSDT")
            raw_pair = sig.get('pair', '')
            if ':' in raw_pair:
                _, sym_part = raw_pair.split(':', 1)
            else:
                sym_part = raw_pair
            raw_sym = sym_part.replace('/', '').replace(':USDT', '')
            vol_24h = _volume_map.get(raw_sym, 0)
            vol_ok = vol_24h >= min_volume
            if vol_24h > 0:
                checks['volume_24h'] = {
                    'pass': vol_ok,
                    'label': '24h Volume',
                    'detail': f'${vol_24h:,.0f} {"≥" if vol_ok else "<"} ${min_volume:,.0f}',
                    'rule': f'≥ ${min_volume:,.0f}'
                }
            else:
                checks['volume_24h'] = {
                    'pass': False,
                    'label': '24h Volume',
                    'detail': 'Volume data unavailable',
                    'rule': f'≥ ${min_volume:,.0f}'
                }
        else:
            checks['volume_24h'] = {
                'pass': None,
                'label': '24h Volume',
                'detail': 'Filter disabled',
                'rule': 'N/A'
            }

        # Overall verdict — only check criteria that have real data
        critical = [checks['sl_direction']['pass'], checks['sl_gap']['pass'],
                    checks['risk_reward']['pass']]
        # Confidence is critical only when available (>0)
        if conf > 0:
            critical.append(checks['confidence']['pass'])
        # Volume is critical when filter is enabled — below threshold = bot skips pair entirely
        if vol_filter_enabled and min_volume > 0:
            critical.append(checks.get('volume_24h', {}).get('pass'))
        all_pass = all(c is True for c in critical)
        # Entry tolerance is CRITICAL — this is the actual gate used by main.py
        if cur is not None:
            all_pass = all_pass and checks['entry_tolerance']['pass'] is True

        return {
            'checks': checks,
            'all_pass': all_pass,
            'pass_count': sum(1 for c in checks.values() if c['pass'] is True),
            'total_count': sum(1 for c in checks.values() if c['pass'] is not None)
        }

    # --- Pending signals ---
    pending_items = store.get_pending_signals(status='pending')
    pending_results = []
    for p in pending_items:
        try:
            pair = p['pair']
            exch, sym = pair.split(':', 1) if ':' in pair else ('binance', pair)
            from core.data_fetcher import MarketDataFetcher
            fetcher = MarketDataFetcher(exch)
            df = fetcher.fetch_ohlcv(sym, timeframe='1m', limit=2)
            cur_price = float(df.iloc[-1]['close'])
            p['current_price'] = cur_price
        except Exception:
            p['current_price'] = None
        result = _evaluate_signal(p)
        result['id']       = p.get('id')
        result['pair']     = p.get('pair', '').replace('binance:', '')
        result['direction'] = p.get('direction', '')
        result['entry_price'] = p.get('entry_price', 0)
        result['current_price'] = p.get('current_price')
        result['confidence'] = p.get('confidence', 0)
        result['risk_reward'] = p.get('risk_reward', 0)
        result['created_at'] = p.get('created_at', '')
        pending_results.append(result)

    # --- Recent signals (recently closed trades) ---
    recent_trades = []
    try:
        closed = store.get_closed_trades(limit=10)
        for t in (closed or []):
            rr_val = 0
            ep = float(t.get('entry_price', 0) or 0)
            sl = float(t.get('sl', 0) or 0)
            tp = float(t.get('tp', 0) or 0)
            if ep > 0 and sl > 0:
                risk = abs(ep - sl)
                reward = abs(tp - ep)
                rr_val = reward / risk if risk > 0 else 0

            t_sig = {
                'entry_price': ep,
                'stop_loss': sl,
                'target': tp,
                'direction': t.get('direction', 'long'),
                'confidence': t.get('confidence', 0),
                'risk_reward': rr_val,
                'zone_strength': t.get('zone_strength', 0),
                'current_price': t.get('exit_price'),
            }
            result = _evaluate_signal(t_sig)
            result['id']        = t.get('id')
            result['pair']      = t.get('pair', '').replace('binance:', '')
            result['direction'] = t.get('direction', '')
            result['entry_price'] = ep
            result['exit_price'] = t.get('exit_price')
            result['status']    = t.get('status', '')
            result['pnl_usd']   = t.get('pnl_usd', 0)
            result['entry_time'] = t.get('entry_time', '')
            result['exit_time'] = t.get('exit_time', '')
            recent_trades.append(result)
    except Exception as e:
        logger.error(f"signal_checklist recent trades error: {e}")

    return jsonify({
        'settings': {
            'min_risk_reward': min_rr,
            'min_confidence': min_confidence,
            'min_sl_gap_pct': min_sl_gap_pct,
            'min_zone_strength': min_zone_str,
        },
        'pending': pending_results,
        'recent': recent_trades,
    })


@app.route('/api/pending/<int:pid>/cancel', methods=['POST'])
def cancel_pending_signal(pid):
    """Cancel a pending signal."""
    store = DataStore()
    ok = store.cancel_pending_signal(pid)
    return jsonify({'status': 'ok' if ok else 'error', 'id': pid})


def _check_symbol_tradable(pair: str) -> dict:
    """
    Check if a symbol can be traded on Binance Futures.
    Returns {'tradable': True} or {'tradable': False, 'reason': '...', 'status': '...'}
    """
    try:
        import urllib.request as _req, json as _json
        sym = pair.split(':', 1)[1] if ':' in pair else pair
        # Normalise for 1000x contracts
        from core.binance_connector import _normalise_k_contract
        norm_sym, mult = _normalise_k_contract(sym)
        raw_sym = norm_sym.split('/')[0].replace('/', '') + \
                  (norm_sym.split('/')[1].split(':')[0] if '/' in norm_sym else '')
        raw_sym = raw_sym.replace('/', '')

        url = 'https://fapi.binance.com/fapi/v1/exchangeInfo'
        with _req.urlopen(url, timeout=8) as r:
            info = _json.loads(r.read())
        for s in info['symbols']:
            if s['symbol'] == raw_sym:
                status   = s.get('status', 'UNKNOWN')
                contract = s.get('contractType', 'UNKNOWN')
                if status == 'TRADING' and contract == 'PERPETUAL':
                    return {'tradable': True, 'symbol': raw_sym, 'mult': mult}
                elif status == 'SETTLING':
                    return {
                        'tradable': False,
                        'symbol':   raw_sym,
                        'status':   status,
                        'reason':   (
                            f'{sym} futures contract is SETTLING (expiring). '
                            f'Binance blocks new orders on settling contracts. '
                            f'The contract will expire shortly.'
                        )
                    }
                else:
                    return {
                        'tradable': False,
                        'symbol':   raw_sym,
                        'status':   status,
                        'reason':   f'{sym} futures status is {status} (not TRADING)'
                    }
        # Symbol not found on Binance Futures at all
        return {
            'tradable': False,
            'symbol':   raw_sym,
            'status':   'NOT_LISTED',
            'reason':   (
                f'{sym} is not listed on Binance Futures. '
                f'It may be a spot-only token. '
                f'Futures trading is not available for this pair.'
            )
        }
    except Exception as e:
        logger.warning(f"Symbol check failed for {pair}: {e}")
        return {'tradable': True, 'note': f'Could not verify: {e}'}  # allow if can't check


@app.route('/api/pending/<int:pid>/execute', methods=['POST'])
def execute_pending_signal(pid):
    """Manually trigger execution of a pending signal at market price."""
    from core.trade_executor import TradeExecutor
    store = DataStore()
    rows  = store.get_pending_signals(status='pending')
    ps    = next((p for p in rows if p['id'] == pid), None)
    if not ps:
        return jsonify({'error': 'Pending signal not found or not pending'}), 404
    try:
        # Pre-validate symbol is tradable on Binance Futures
        check = _check_symbol_tradable(ps['pair'])
        if not check.get('tradable'):
            return jsonify({
                'error': check.get('reason', f'{ps["pair"]} cannot be traded'),
                'status': check.get('status', 'UNKNOWN'),
                'symbol': check.get('symbol', ''),
                'tip': 'Remove this signal or change the pair to a listed futures contract.'
            }), 400

        executor = TradeExecutor(account_id=ps.get('account_id'))
        result   = executor.execute_signal(
            {'direction': ps['direction'], 'entry_price': 0,
             'stop_loss': ps['stop_loss'], 'target': ps['target'],
             'timeframe': ps['timeframe'], 'confidence': ps['confidence']},
            ps['pair'], notional_usd=ps['notional_usd'], signal_id=ps.get('signal_id')
        )
        if 'error' in result:
            return jsonify({'error': result['error']}), 400
        store.trigger_pending_signal(pid, result.get('trade_id'))
        return jsonify({**result, 'pending_id': pid})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/trades/live_pnl')
def live_pnl():
    """Return live unrealized PnL for all open trades."""
    store  = DataStore()
    trades = store.get_open_trades()
    if not trades:
        return jsonify({})
    # Batch price fetch
    result = {}
    price_cache = {}
    for t in trades:
        try:
            pair = t.get('pair', '')
            exch, sym = pair.split(':', 1) if ':' in pair else ('binance', pair)
            if sym not in price_cache:
                from core.data_fetcher import MarketDataFetcher
                fetcher = MarketDataFetcher(exch)
                df = fetcher.fetch_ohlcv(sym, timeframe='1m', limit=2)
                price_cache[sym] = float(df.iloc[-1]['close'])
            cur  = price_cache[sym]
            ep   = float(t['entry_price'] or 0)
            not_ = float(t.get('notional_usd', 100))
            comm = float(t.get('commission_usd', 0.05))
            if ep > 0:
                if t['direction'] == 'long':
                    pnl = (cur - ep) / ep * not_ - comm * 2
                else:
                    pnl = (ep - cur) / ep * not_ - comm * 2
                pnl_pct = round((cur - ep) / ep * 100 * (1 if t['direction'] == 'long' else -1), 3)
                result[str(t['id'])] = {
                    'current_price': cur,
                    'pnl_usd':       round(pnl, 4),
                    'pnl_pct':       pnl_pct,
                }
        except Exception:
            pass
    return jsonify(result)


# ═══════════════════════════════════════════════════════════════════════════════
# Config API — unified GET/POST for all parameters
# Used by dashboard Settings tab and Telegram bot for bidirectional sync
# ═══════════════════════════════════════════════════════════════════════════════

@app.route('/api/config', methods=['GET'])
def get_config():
    """Return all configurable parameters as a flat JSON object."""
    try:
        c = load_config()
        return jsonify({
            # ── Sweep Detector ──
            'sweep_multiplier':    c.get('sweep_detector', {}).get('sweep_multiplier', 0.5),
            'volume_multiplier':   c.get('sweep_detector', {}).get('volume_multiplier', 1.5),
            'wick_ratio':          c.get('sweep_detector', {}).get('wick_ratio', 0.3),
            'min_sweep_pct':       c.get('sweep_detector', {}).get('min_sweep_pct', 0.1),
            'confirmation_bars':   c.get('sweep_detector', {}).get('confirmation_bars', 3),
            'min_body_ratio':      c.get('sweep_detector', {}).get('min_body_ratio', 0.4),
            'lookback_bars':       c.get('sweep_detector', {}).get('lookback_bars', 10),
            # ── Signal Engine ──
            'min_risk_reward':     c.get('signal_engine', {}).get('min_risk_reward', 2.0),
            'require_confluence':  c.get('signal_engine', {}).get('require_confluence', True),
            'risk_per_trade':      c.get('signal_engine', {}).get('risk_per_trade', 0.01),
            'stop_buffer_pct':     c.get('signal_engine', {}).get('stop_buffer_pct', 0.001),
            'target_buffer_pct':   c.get('signal_engine', {}).get('target_buffer_pct', 0.001),
            'retracement_levels':  c.get('signal_engine', {}).get('retracement_levels', [0.5, 0.618, 0.786]),
            # ── Alerts ──
            'min_confidence':      c.get('alerts', {}).get('telegram', {}).get('min_confidence', 0.7),
            'alerts_enabled':      c.get('alerts', {}).get('telegram', {}).get('enabled', True),
            # ── Data Fetch ──
            'ohlcv_limit':         c.get('data_fetch', {}).get('ohlcv_limit', 300),
            'atr_period':          c.get('data_fetch', {}).get('atr_period', 14),
            'timeframes':          c.get('data_fetch', {}).get('timeframes', ['4h','1h','15m']),
            # ── Volume Filter ──
            'volume_filter_enabled':   c.get('volume_filter', {}).get('enabled', True),
            'min_24h_volume_usd':      c.get('volume_filter', {}).get('min_24h_volume_usd', 1000000.0),
            # ── Paper Trading ──
            'fixed_notional_usd':  c.get('paper_trading', {}).get('fixed_notional_usd', 20.0),
            'margin_leverage':     c.get('paper_trading', {}).get('margin_leverage', 20.0),
            'commission_per_trade':c.get('paper_trading', {}).get('commission_per_trade', 0.001),
            'position_sizing':     c.get('paper_trading', {}).get('position_sizing', 'fixed_notional'),
            # ── Cron / Scan ──
            'scan_interval_minutes': c.get('cron', {}).get('scan_interval_minutes', 5),
            # ── Signal Execution ──
            'signal_execution_mode':    c.get('signal_execution', {}).get('mode', 'pending'),
            'auto_execute':             c.get('signal_execution', {}).get('auto_execute', False),
            'entry_tolerance_pct':      c.get('signal_execution', {}).get('entry_tolerance_pct', 0.3),
            'min_sl_gap_pct':           c.get('signal_execution', {}).get('min_sl_gap_pct', 0.3),
            'sl_tp_mode':               c.get('signal_execution', {}).get('sl_tp_mode', 'binance_bracket'),
            'sl_tp_delay_sec':          c.get('signal_execution', {}).get('sl_tp_delay_sec', 3),
            'monitor_interval_sec':     c.get('signal_execution', {}).get('monitor_interval_sec', 5),
            # ── Liquidity Mapper ──
            'equal_touch_tolerance':    c.get('liquidity_mapper', {}).get('equal_touch_tolerance', 0.001),
            'swing_lookback':           c.get('liquidity_mapper', {}).get('swing_lookback', 5),
            'round_tolerance':          c.get('liquidity_mapper', {}).get('round_tolerance', 0.005),
            'min_swing_strength':       c.get('liquidity_mapper', {}).get('min_swing_strength', 2),
            # ── Backtester ──
            'backtest_commission_pct':  c.get('backtester', {}).get('commission_pct', 0.001),
            'backtest_max_concurrent':  c.get('backtester', {}).get('max_concurrent_trades', 3),
            'backtest_slippage_pct':    c.get('backtester', {}).get('slippage_pct', 0.0005),
            'backtest_timeout_bars':    c.get('backtester', {}).get('timeout_bars', 48),
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/config', methods=['POST'])
def update_config():
    """
    Update one or more config parameters from dashboard or Telegram bot.
    Body: { "param_name": value, ... }
    Supports dot-notation keys OR the flat keys returned by GET /api/config.
    """
    from core.config_manager import cfg as config_mgr
    data = request.get_json() or {}
    if not data:
        return jsonify({'error': 'no data'}), 400

    # Map flat keys → yaml dot-paths
    KEY_MAP = {
        'sweep_multiplier':       'sweep_detector.sweep_multiplier',
        'volume_multiplier':      'sweep_detector.volume_multiplier',
        'wick_ratio':             'sweep_detector.wick_ratio',
        'min_sweep_pct':          'sweep_detector.min_sweep_pct',
        'confirmation_bars':      'sweep_detector.confirmation_bars',
        'min_body_ratio':         'sweep_detector.min_body_ratio',
        'lookback_bars':          'sweep_detector.lookback_bars',
        'min_risk_reward':        'signal_engine.min_risk_reward',
        'require_confluence':     'signal_engine.require_confluence',
        'risk_per_trade':         'signal_engine.risk_per_trade',
        'stop_buffer_pct':        'signal_engine.stop_buffer_pct',
        'target_buffer_pct':      'signal_engine.target_buffer_pct',
        'min_confidence':         'alerts.telegram.min_confidence',
        'alerts_enabled':         'alerts.telegram.enabled',
        'ohlcv_limit':            'data_fetch.ohlcv_limit',
        'atr_period':             'data_fetch.atr_period',
        'timeframes':             'data_fetch.timeframes',
        'volume_filter_enabled':  'volume_filter.enabled',
        'min_24h_volume_usd':     'volume_filter.min_24h_volume_usd',
        'fixed_notional_usd':     'paper_trading.fixed_notional_usd',
        'margin_leverage':        'paper_trading.margin_leverage',
        'commission_per_trade':   'paper_trading.commission_per_trade',
        'position_sizing':        'paper_trading.position_sizing',
        'scan_interval_minutes':  'cron.scan_interval_minutes',
        'signal_execution_mode':  'signal_execution.mode',
        'auto_execute':           'signal_execution.auto_execute',
        'entry_tolerance_pct':    'signal_execution.entry_tolerance_pct',
        'min_sl_gap_pct':         'signal_execution.min_sl_gap_pct',
        'sl_tp_mode':             'signal_execution.sl_tp_mode',
        'sl_tp_delay_sec':        'signal_execution.sl_tp_delay_sec',
        'monitor_interval_sec':   'signal_execution.monitor_interval_sec',
        'retracement_levels':     'signal_engine.retracement_levels',
        'equal_touch_tolerance':  'liquidity_mapper.equal_touch_tolerance',
        'swing_lookback':         'liquidity_mapper.swing_lookback',
        'round_tolerance':        'liquidity_mapper.round_tolerance',
        'min_swing_strength':     'liquidity_mapper.min_swing_strength',
        'backtest_commission_pct':  'backtester.commission_pct',
        'backtest_max_concurrent':  'backtester.max_concurrent_trades',
        'backtest_slippage_pct':    'backtester.slippage_pct',
        'backtest_timeout_bars':    'backtester.timeout_bars',
    }

    updated = {}
    errors  = {}
    for key, value in data.items():
        dot_path = KEY_MAP.get(key, key)  # fall back to raw dot-path if not in map
        try:
            config_mgr.set(dot_path, value)
            updated[key] = value
        except Exception as e:
            errors[key] = str(e)

    # Reload config_mgr so GET /api/config reflects changes immediately
    try:
        config_mgr._config = config_mgr.load()
    except Exception:
        pass

    result = {'status': 'ok', 'updated': updated}
    if errors:
        result['errors'] = errors
    return jsonify(result)


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO)
    app.run(host='0.0.0.0', port=5000, debug=False)
