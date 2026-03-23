"""
Data persistence layer for scans and backtests.
SQLite database with tables: zones, sweeps, signals, trades.
"""
import sqlite3
import pandas as pd
from datetime import datetime
from typing import List, Optional, Dict
from pathlib import Path
import json
import logging

logger = logging.getLogger(__name__)

class DataStore:
    def __init__(self, db_path: str = 'data/store.db', cache_path: str = 'data/latest_signals.json'):
        self.db_path = Path(db_path)
        self.cache_path = Path(cache_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _init_db(self):
        """Create tables if they don't exist."""
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS zones (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    pair TEXT NOT NULL,
                    timeframe TEXT NOT NULL,
                    price REAL NOT NULL,
                    zone_type TEXT NOT NULL,
                    strength INTEGER NOT NULL,
                    last_touch TIMESTAMP NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    touches_json TEXT,
                    notes TEXT
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS sweeps (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    pair TEXT NOT NULL,
                    timeframe TEXT NOT NULL,
                    timestamp TIMESTAMP NOT NULL,
                    direction TEXT NOT NULL,
                    sweep_price REAL NOT NULL,
                    close_price REAL NOT NULL,
                    volume REAL NOT NULL,
                    volume_ratio REAL NOT NULL,
                    sweep_depth_pct REAL NOT NULL,
                    confirmed BOOLEAN NOT NULL,
                    confirmation_time TIMESTAMP,
                    notes TEXT
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS signals (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    pair TEXT NOT NULL,
                    timeframe TEXT NOT NULL,
                    generated_at TIMESTAMP NOT NULL,
                    direction TEXT NOT NULL,
                    entry_price REAL NOT NULL,
                    stop_loss REAL NOT NULL,
                    target REAL NOT NULL,
                    risk_reward REAL NOT NULL,
                    confidence REAL NOT NULL,
                    zone_strength INTEGER,
                    sweep_id INTEGER,
                    FOREIGN KEY(sweep_id) REFERENCES sweeps(id)
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS trades (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    pair TEXT NOT NULL,
                    timeframe TEXT NOT NULL,
                    entry_time TIMESTAMP NOT NULL,
                    exit_time TIMESTAMP,
                    direction TEXT NOT NULL,
                    entry_price REAL NOT NULL,
                    exit_price REAL,
                    sl REAL NOT NULL,
                    tp REAL NOT NULL,
                    notional_usd REAL NOT NULL,
                    commission_usd REAL NOT NULL,
                    pnl_usd REAL,
                    status TEXT NOT NULL DEFAULT 'open',
                    notes TEXT,
                    signal_id INTEGER,
                    FOREIGN KEY(signal_id) REFERENCES signals(id)
                )
            """)
            conn.commit()
            # Migration guard: ensure trades table has all required columns
            self._migrate_trades(conn)

    def _migrate_trades(self, conn):
        """Add any missing columns to trades table (safe, idempotent)."""
        cur = conn.cursor()
        cur.execute("PRAGMA table_info(trades)")
        existing = {row[1] for row in cur.fetchall()}
        required = {
            'sl':           'REAL NOT NULL DEFAULT 0',
            'tp':           'REAL NOT NULL DEFAULT 0',
            'notional_usd': 'REAL NOT NULL DEFAULT 50',
            'commission_usd': 'REAL NOT NULL DEFAULT 0.05',
            'pnl_usd':      'REAL',
            'status':       "TEXT NOT NULL DEFAULT 'open'",
            'notes':        'TEXT',
            'signal_id':    'INTEGER',
        }
        for col, col_def in required.items():
            if col not in existing:
                try:
                    conn.execute(f"ALTER TABLE trades ADD COLUMN {col} {col_def}")
                    logger.info(f"DB migration: added column trades.{col}")
                except Exception as e:
                    logger.warning(f"DB migration skip {col}: {e}")
        conn.commit()

    def save_zones(self, pair: str, timeframe: str, zones: List, timestamp: datetime):
        """Batch insert liquidity zones."""
        with sqlite3.connect(self.db_path) as conn:
            for z in zones:
                conn.execute("""
                    INSERT INTO zones
                    (pair, timeframe, price, zone_type, strength, last_touch, touches_json, notes)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    pair, timeframe, float(z.price), z.zone_type, z.strength,
                    z.last_touch, json.dumps(z.touches), z.notes
                ))
            conn.commit()

    def save_sweeps(self, pair: str, timeframe: str, sweeps: List):
        """Batch insert sweep events."""
        with sqlite3.connect(self.db_path) as conn:
            for s in sweeps:
                conn.execute("""
                    INSERT INTO sweeps
                    (pair, timeframe, timestamp, direction, sweep_price, close_price,
                     volume, volume_ratio, sweep_depth_pct, confirmed, confirmation_time, notes)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    pair, timeframe, s.timestamp, s.direction, s.sweep_price, s.close_price,
                    s.volume, s.volume_ratio, s.sweep_depth_pct, s.confirmed,
                    s.confirmation_time, s.notes
                ))
            conn.commit()

    def save_signal(self, pair: str, timeframe: str, signal, sweep_id: Optional[int] = None):
        """Insert a trade signal."""
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                INSERT INTO signals
                (pair, timeframe, generated_at, direction, entry_price, stop_loss,
                 target, risk_reward, confidence, zone_strength, sweep_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                pair, timeframe, signal.timestamp.isoformat(), signal.direction, signal.entry_price,
                signal.stop_loss, signal.target, signal.risk_reward, signal.confidence,
                signal.zone_strength, sweep_id
            ))
            conn.commit()
            return conn.execute("SELECT last_insert_rowid()").fetchone()[0]

    def save_trade(self, pair: str, timeframe: str, trade: Dict, signal_id: Optional[int] = None):
        """
        Insert a completed trade (backtest or manual). For closed trades only.
        Expected keys in trade dict:
          entry_time, exit_time, direction, entry_price, exit_price,
          pnl, pnl_pct, exit_reason
        """
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                INSERT INTO trades
                (pair, timeframe, entry_time, exit_time, direction, entry_price, exit_price,
                 sl, tp, notional_usd, commission_usd, pnl_usd, status, notes, signal_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                pair, timeframe,
                trade['entry_time'], trade['exit_time'], trade['direction'],
                trade['entry_price'], trade['exit_price'],
                trade.get('sl', 0), trade.get('tp', 0),
                trade.get('notional_usd', 0), trade.get('commission_usd', 0),
                trade.get('pnl', 0),  # pnl maps to pnl_usd
                'closed',  # status
                trade.get('notes', ''),
                signal_id
            ))
            conn.commit()

    # --- New open trade tracking for paper trading ---

    def create_open_trade(self, pair: str, timeframe: str, direction: str,
                         entry_price: float, sl: float, tp: float,
                         entry_time: datetime, notional_usd: float,
                         commission_usd: float, signal_id: Optional[int] = None,
                         notes: str = '') -> int:
        """Create an open trade (paper trade). Returns trade ID, or None if duplicate."""
        # Avoid duplicate open trades for the same signal
        if signal_id is not None:
            with sqlite3.connect(self.db_path) as conn:
                cur = conn.cursor()
                cur.execute("SELECT id FROM trades WHERE signal_id = ? AND status='open'", (signal_id,))
                if cur.fetchone():
                    return None
        with sqlite3.connect(self.db_path) as conn:
            cur = conn.cursor()
            cur.execute("""
                INSERT INTO trades
                (pair, timeframe, entry_time, direction, entry_price, sl, tp,
                 notional_usd, commission_usd, status, notes, signal_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', ?, ?)
            """, (
                pair, timeframe, entry_time.isoformat(), direction, entry_price,
                sl, tp, notional_usd, commission_usd, notes, signal_id
            ))
            conn.commit()
            return cur.lastrowid

    def get_open_trades(self) -> List[Dict]:
        """Return all open trades."""
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()
            cur.execute("SELECT * FROM trades WHERE status='open' ORDER BY entry_time DESC")
            rows = cur.fetchall()
            return [dict(row) for row in rows]

    def get_closed_trades(self, limit: int = 50) -> List[Dict]:
        """Return recent closed trades."""
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()
            cur.execute("""
                SELECT * FROM trades WHERE status!='open'
                ORDER BY exit_time DESC LIMIT ?
            """, (limit,))
            rows = cur.fetchall()
            return [dict(row) for row in rows]

    def close_trade(self, trade_id: int, exit_price: float, exit_time: datetime,
                   status: str, pnl_usd: Optional[float] = None):
        """
        Close an open trade.
        status: 'target_hit' or 'stop_loss'
        """
        with sqlite3.connect(self.db_path) as conn:
            cur = conn.cursor()
            cur.execute("""
                UPDATE trades
                SET exit_price = ?, exit_time = ?, status = ?, pnl_usd = ?
                WHERE id = ? AND status = 'open'
            """, (exit_price, exit_time.isoformat(), status, pnl_usd, trade_id))
            conn.commit()

    def get_recent_sweeps(self, pair: str, timeframe: str, hours: int = 24) -> pd.DataFrame:
        """Fetch recent sweeps."""
        cutoff = datetime.utcnow() - pd.Timedelta(hours=hours)
        with sqlite3.connect(self.db_path) as conn:
            df = pd.read_sql("""
                SELECT * FROM sweeps
                WHERE pair = ? AND timeframe = ? AND timestamp >= ?
                ORDER BY timestamp DESC
            """, conn, params=(pair, timeframe, cutoff))
        return df

    def get_trades(self, pair: str = None) -> pd.DataFrame:
        """Fetch trades, optionally filtered by pair."""
        with sqlite3.connect(self.db_path) as conn:
            query = "SELECT * FROM trades"
            params = []
            if pair:
                query += " WHERE pair = ?"
                params.append(pair)
            query += " ORDER BY entry_time DESC"
            df = pd.read_sql(query, conn, params=params)
        return df

    def save_latest_signals(self, signals: List[Dict], scan_timestamp: datetime = None):
        """Save latest scan results to JSON cache for dashboard/API."""
        data = {
            'last_updated': (scan_timestamp or datetime.utcnow()).isoformat(),
            'signals': signals
        }
        with open(self.cache_path, 'w') as f:
            json.dump(data, f, indent=2)

    def get_latest_signals(self) -> Dict:
        """Read latest signals from cache. Returns {'last_updated': ..., 'signals': [...]}."""
        if not self.cache_path.exists():
            return {'last_updated': None, 'signals': []}
        try:
            with open(self.cache_path, 'r') as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"Failed to read signals cache: {e}")
            return {'last_updated': None, 'signals': []}

if __name__ == '__main__':
    store = DataStore()
    print("Database initialized at", store.db_path)
    print("Tables: zones, sweeps, signals, trades")
