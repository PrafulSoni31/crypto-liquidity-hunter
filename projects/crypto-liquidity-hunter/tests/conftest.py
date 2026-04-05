"""Shared fixtures for all tests. No real API calls."""
import sys, os, sqlite3, pytest
from unittest.mock import MagicMock, patch
from datetime import datetime, timezone

# Add project root to path
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

EXCHANGE_INFO = {
    'BTCUSDT':  {'tick': 0.1,    'step': 0.001},
    'ETHUSDT':  {'tick': 0.01,   'step': 0.001},
    'SUIUSDT':  {'tick': 0.0001, 'step': 0.1},
    'THEUSDT':  {'tick': 0.0001, 'step': 0.1},
    'POLYXUSDT':{'tick': 0.0001, 'step': 1.0},
    'ARBUSDT':  {'tick': 0.0001, 'step': 0.1},
    'RENDERUSDT':{'tick':0.001,  'step': 0.1},
}


@pytest.fixture
def mock_exchange_info():
    return EXCHANGE_INFO


@pytest.fixture
def mock_config():
    return {
        'signal_engine': {
            'min_risk_reward': 3.0,
            'risk_per_trade': 0.01,
            'stop_buffer_pct': 0.001,
            'target_buffer_pct': 0.001,
            'retracement_levels': [0.5, 0.618, 0.786],
            'require_confluence': False,
        },
        'signal_execution': {
            'mode': 'auto',
            'auto_execute': True,
            'entry_tolerance_pct': 0.3,
            'min_sl_gap_pct': 0.3,
            'sl_tp_mode': 'binance_bracket',
            'sl_tp_delay_sec': 3,
            'monitor_interval_sec': 5,
        },
        'alerts': {'telegram': {'min_confidence': 0.7, 'enabled': False}},
        'live_trading': {
            'fixed_notional_usd': 50.0,
            'margin_leverage': 10.0,
            'commission_per_trade': 0.001,
            'position_sizing': 'fixed_notional',
            'max_notional_usd': 50.0,
        },
        'paper_trading': {
            'fixed_notional_usd': 20.0,
            'margin_leverage': 20.0,
            'commission_per_trade': 0.001,
            'position_sizing': 'fixed_notional',
        },
        'liquidity_mapper': {
            'equal_touch_tolerance': 0.005,
            'swing_lookback': 5,
            'round_tolerance': 0.005,
            'min_swing_strength': 2,
        },
        'sweep_detector': {
            'sweep_multiplier': 0.5,
            'volume_multiplier': 2.5,
            'wick_ratio': 0.4,
            'min_sweep_pct': 0.2,
            'confirmation_bars': 5,
            'min_body_ratio': 0.4,
            'lookback_bars': 24,
        },
        'data_fetch': {'ohlcv_limit': 300, 'atr_period': 14, 'timeframes': ['15m']},
        'backtester': {'max_concurrent_trades': 3},
        'active_account_id': 2,
        'pairs': ['binance:BTC/USDT'],
    }


@pytest.fixture
def in_memory_db():
    """In-memory SQLite DB with the same schema as production."""
    conn = sqlite3.connect(':memory:')
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS accounts (
            id INTEGER PRIMARY KEY, name TEXT, api_key TEXT, api_secret TEXT,
            mode TEXT DEFAULT 'paper', environment TEXT DEFAULT 'mainnet',
            testnet INTEGER DEFAULT 0, is_demo INTEGER DEFAULT 0,
            enabled INTEGER DEFAULT 1, balance_usdt REAL DEFAULT 0,
            last_connected TEXT, created_at TEXT, notes TEXT
        );
        CREATE TABLE IF NOT EXISTS trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            pair TEXT, timeframe TEXT, entry_time TEXT, exit_time TEXT,
            direction TEXT, entry_price REAL, exit_price REAL,
            sl REAL, tp REAL, notional_usd REAL, commission_usd REAL,
            pnl_usd REAL, status TEXT DEFAULT 'open', notes TEXT,
            signal_id INTEGER, mode TEXT, order_id TEXT, account_id INTEGER
        );
        CREATE TABLE IF NOT EXISTS pending_signals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            pair TEXT, timeframe TEXT, direction TEXT,
            entry_price REAL, stop_loss REAL, target REAL,
            confidence REAL, notional_usd REAL,
            signal_id INTEGER, account_id INTEGER,
            status TEXT DEFAULT 'pending', expires_at TEXT,
            trade_id INTEGER, created_at TEXT
        );
        INSERT INTO accounts (id, name, api_key, api_secret, mode, environment)
        VALUES (2, 'TestAccount', 'fake_key', 'fake_secret', 'live', 'mainnet');
    """)
    yield conn
    conn.close()


@pytest.fixture
def mock_store(in_memory_db):
    """DataStore that uses in-memory SQLite."""
    from data.store import DataStore
    store = DataStore.__new__(DataStore)
    store.db_path = ':memory:'
    store.cache_path = '/dev/null'
    store._conn = in_memory_db
    # Monkey-patch _get_conn to return our in-memory connection
    store._get_conn = lambda: in_memory_db
    store.conn = in_memory_db
    return store


@pytest.fixture
def mock_connector():
    """Fake BinanceConnector that doesn't hit real API."""
    conn = MagicMock()
    conn.mode = 'live'
    conn.api_key = 'fake_key'
    conn.api_secret = 'fake_secret'
    conn.connected = True
    conn._connected = True
    conn.exchange = MagicMock()
    conn.place_market_order = MagicMock(return_value={
        'id': '12345', 'status': 'closed', 'average': 1.0, 'price': 1.0,
        'amount': 100, 'filled': 100,
    })
    conn.paper_execute = MagicMock(return_value={
        'id': 'paper_001', 'status': 'closed', 'price': 1.0,
        'amount': 100, 'filled': 100, 'mode': 'paper',
    })
    conn.get_balance = MagicMock(return_value={'free': 100.0, 'total': 100.0})
    conn.get_positions = MagicMock(return_value=[])
    conn._round_qty = MagicMock(side_effect=lambda sym, qty: round(qty, 1))
    return conn
