"""
Crypto Liquidity Hunter Dashboard
Flask app with Plotly charts, real-time sweep/signal display.
"""
import os
import json
from datetime import datetime, timedelta
from flask import Flask, render_template, jsonify, send_from_directory, request
import pandas as pd
import plotly.graph_objs as go
from plotly.utils import PlotlyJSONEncoder
import logging

from core.data_fetcher import MarketDataFetcher
from core.liquidity_mapper import LiquidityMapper
from core.sweep_detector import SweepDetector
from core.signal_engine import SignalEngine
from alerts.telegram import AlertDispatcher

logger = logging.getLogger(__name__)

app = Flask(__name__, static_folder='static', template_folder='templates')

# Load config
import yaml
CONFIG_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'config', 'pairs.yaml')
with open(CONFIG_PATH, 'r') as f:
    config = yaml.safe_load(f)

# Initialize components
fetcher = MarketDataFetcher('binance')
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
engine = SignalEngine(
    risk_per_trade=config['signal_engine']['risk_per_trade'],
    retracement_levels=config['signal_engine']['retracement_levels'],
    stop_buffer_pct=config['signal_engine']['stop_buffer_pct'],
    min_risk_reward=config['signal_engine']['min_risk_reward'],
    position_sizing=config.get('paper_trading', {}).get('position_sizing', 'risk_percent'),
    fixed_notional_usd=config.get('paper_trading', {}).get('fixed_notional_usd', 50.0),
    margin_leverage=config.get('paper_trading', {}).get('margin_leverage', 1.0),
    commission_pct=config.get('paper_trading', {}).get('commission_per_trade', 0.001)
)
dispatcher = AlertDispatcher(config['alerts'])

@app.route('/')
def index():
    """Dashboard home: list pairs, recent sweeps, signals."""
    pairs = config['pairs']
    timeframes = config['data_fetch']['timeframes']
    return render_template('index.html', pairs=pairs, timeframes=timeframes)

@app.route('/api/scan/<pair>')
def scan_pair(pair):
    """Run live scan for a specific pair (with optional tf query param)."""
    tf = request.args.get('tf', config['data_fetch']['timeframes'][0])
    exchange_str, symbol = pair.split(':', 1)
    df = fetcher.fetch_ohlcv(symbol, timeframe=tf, limit=config['data_fetch']['ohlcv_limit'])
    atr = fetcher.calculate_atr(df, period=config['data_fetch']['atr_period'])
    zones = mapper.map_liquidity(df)
    sweeps = detector.detect_sweeps(df, atr, zones)
    latest_price = df.iloc[-1]['close']
    signals = []
    for sweep in sweeps[-5:]:
        signal = engine.generate_signal(sweep, zones, latest_price, capital=10000)
        if signal:
            signals.append(signal)

    # Convert to serializable
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

    signals_data = []
    for sig in signals:
        signals_data.append({
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
            'commission_estimated_usd': sig.commission_estimated_usd
        })

    return jsonify({
        'pair': pair,
        'timeframe': tf,
        'current_price': latest_price,
        'zones': zones_data,
        'sweeps': sweeps_data,
        'signals': signals_data,
        'last_updated': datetime.utcnow().isoformat()
    })

@app.route('/api/chart/<pair>')
def chart_pair(pair):
    """Generate candlestick chart with zones and sweeps."""
    tf = request.args.get('tf', config['data_fetch']['timeframes'][0])
    exchange_str, symbol = pair.split(':', 1)
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

    # Add zone lines
    zone_shapes = []
    for z in zones[:10]:
        zone_shapes.append(dict(
            type='line',
            x0=df.index[0],
            x1=df.index[-1],
            y0=z.price,
            y1=z.price,
            line=dict(color='rgba(0, 200, 0, 0.5)', width=1, dash='dash'),
            name=f"Zone {z.zone_type}"
        ))

    # Add sweep markers
    sweep_x = [s.timestamp for s in sweeps[-20:]]
    sweep_y = [s.sweep_price for s in sweeps[-20:]]
    sweep_markers = go.Scatter(
        x=sweep_x,
        y=sweep_y,
        mode='markers',
        marker=dict(symbol='triangle-down', size=12, color='red'),
        name='Sweeps'
    )

    layout = go.Layout(
        title=f'{pair} - {tf}',
        xaxis_title='Time',
        yaxis_title='Price',
        shapes=zone_shapes
    )

    fig = go.Figure(data=[candlestick, sweep_markers], layout=layout)
    fig_json = json.dumps(fig, cls=PlotlyJSONEncoder)

    return jsonify({'chart': fig_json})

@app.route('/api/signals')
def get_signals():
    """Get latest signals from shared cache (same as Telegram alerts)."""
    from data.store import DataStore
    store = DataStore()
    cache = store.get_latest_signals()
    return jsonify(cache)

@app.route('/health')
def health():
    return jsonify({'status': 'ok', 'timestamp': datetime.utcnow().isoformat()})

if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO)
    app.run(host='0.0.0.0', port=5000, debug=False)
