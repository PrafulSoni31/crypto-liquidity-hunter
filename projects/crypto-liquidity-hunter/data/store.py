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
                CREATE TABLE IF NOT EXISTS accounts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    api_key TEXT NOT NULL,
                    api_secret TEXT NOT NULL,
                    mode TEXT NOT NULL DEFAULT 'paper',
                    environment TEXT NOT NULL DEFAULT 'mainnet',
                    testnet INTEGER NOT NULL DEFAULT 0,
                    is_demo INTEGER NOT NULL DEFAULT 0,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    balance_usdt REAL DEFAULT 0,
                    last_connected TEXT,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    notes TEXT
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS pending_signals (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    pair TEXT NOT NULL,
                    timeframe TEXT NOT NULL,
                    direction TEXT NOT NULL,
                    entry_price REAL NOT NULL,
                    stop_loss REAL NOT NULL,
                    target REAL NOT NULL,
                    confidence REAL NOT NULL,
                    notional_usd REAL NOT NULL,
                    signal_id INTEGER,
                    account_id INTEGER,
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    trade_id INTEGER
                )
            """)
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
            'mode':         "TEXT NOT NULL DEFAULT 'paper'",
            'order_id':     'TEXT',
            'account_id':   'INTEGER',  # multi-account support
        }
        for col, col_def in required.items():
            if col not in existing:
                try:
                    conn.execute(f"ALTER TABLE trades ADD COLUMN {col} {col_def}")
                    logger.info(f"DB migration: added column trades.{col}")
                except Exception as e:
                    logger.warning(f"DB migration skip {col}: {e}")
        conn.commit()

    # ─── Pending Signals ───────────────────────────────────────────────────────

    def create_pending_signal(self, pair: str, timeframe: str, direction: str,
                               entry_price: float, stop_loss: float, target: float,
                               confidence: float, notional_usd: float,
                               signal_id: int = None, account_id: int = None,
                               expires_hours: float = 4) -> int:
        """Create a pending signal waiting for price to reach entry. Returns id or None if deduped."""
        from datetime import datetime as _dt, timedelta as _td
        now     = _dt.utcnow()
        expires = now + _td(hours=expires_hours)
        with sqlite3.connect(self.db_path) as conn:
            # Dedup check 1: same pair+direction already has an open LIVE trade → block new pending
            # This prevents re-entering a position that is already open on the broker.
            if account_id is not None:
                open_trade = conn.execute(
                    "SELECT id FROM trades WHERE pair=? AND direction=? AND account_id=? AND status='open' AND mode!='paper'",
                    (pair, direction, account_id)
                ).fetchone()
            else:
                open_trade = conn.execute(
                    "SELECT id FROM trades WHERE pair=? AND direction=? AND status='open' AND mode!='paper'",
                    (pair, direction)
                ).fetchone()
            if open_trade:
                logger.debug(f"create_pending_signal: skipping {pair} {direction} — open trade #{open_trade[0]} already exists")
                return None

            # Dedup check 2: same pair+direction+entry already pending (within ±0.5%)
            existing = conn.execute(
                "SELECT id FROM pending_signals WHERE pair=? AND direction=? AND status='pending'",
                (pair, direction)
            ).fetchall()
            for (eid,) in existing:
                row = conn.execute("SELECT entry_price FROM pending_signals WHERE id=?", (eid,)).fetchone()
                if row and abs(row[0] - entry_price) / max(entry_price, 1e-9) < 0.005:
                    logger.debug(f"create_pending_signal: dedup — pending #{eid} already exists for {pair} {direction}")
                    return eid  # already pending, return existing id
            cur = conn.cursor()
            cur.execute("""
                INSERT INTO pending_signals
                (pair, timeframe, direction, entry_price, stop_loss, target,
                 confidence, notional_usd, signal_id, account_id, created_at, expires_at, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending')
            """, (pair, timeframe, direction, entry_price, stop_loss, target,
                  confidence, notional_usd, signal_id, account_id,
                  now.isoformat(), expires.isoformat()))
            conn.commit()
            return cur.lastrowid

    def get_pending_signals(self, status: str = 'pending') -> List[Dict]:
        """Return pending signals filtered by status."""
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            if status == 'all':
                rows = conn.execute(
                    "SELECT * FROM pending_signals ORDER BY created_at DESC LIMIT 200"
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM pending_signals WHERE status=? ORDER BY created_at DESC",
                    (status,)
                ).fetchall()
            return [dict(r) for r in rows]

    def trigger_pending_signal(self, pending_id: int, trade_id: int = None) -> bool:
        """Mark pending signal as triggered (entry price hit, trade placed)."""
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "UPDATE pending_signals SET status='triggered', trade_id=? WHERE id=?",
                (trade_id, pending_id)
            )
            conn.commit()
        return True

    def cancel_pending_signal(self, pending_id: int) -> bool:
        """Cancel a pending signal."""
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "UPDATE pending_signals SET status='cancelled' WHERE id=? AND status='pending'",
                (pending_id,)
            )
            conn.commit()
        return True

    def expire_old_pending_signals(self) -> int:
        """Mark expired pending signals. Returns count expired."""
        from datetime import datetime as _dt
        now = _dt.utcnow().isoformat()
        with sqlite3.connect(self.db_path) as conn:
            cur = conn.execute(
                "UPDATE pending_signals SET status='expired' WHERE status='pending' AND expires_at < ?",
                (now,)
            )
            conn.commit()
            return cur.rowcount

    # ─── Account Management ────────────────────────────────────────────────────

    def save_account(self, name: str, api_key: str, api_secret: str,
                     mode: str = 'paper', environment: str = 'mainnet',
                     testnet: bool = False, is_demo: bool = False,
                     notes: str = '') -> int:
        """Insert or update account. Returns account_id."""
        with sqlite3.connect(self.db_path) as conn:
            # Check if name already exists
            row = conn.execute("SELECT id FROM accounts WHERE name=?", (name,)).fetchone()
            if row:
                conn.execute("""
                    UPDATE accounts SET api_key=?, api_secret=?, mode=?, environment=?,
                    testnet=?, is_demo=?, enabled=1, notes=? WHERE id=?
                """, (api_key, api_secret, mode, environment,
                      1 if testnet else 0, 1 if is_demo else 0, notes, row[0]))
                conn.commit()
                return row[0]
            cur = conn.cursor()
            cur.execute("""
                INSERT INTO accounts (name, api_key, api_secret, mode, environment, testnet, is_demo, notes)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (name, api_key, api_secret, mode, environment,
                  1 if testnet else 0, 1 if is_demo else 0, notes))
            conn.commit()
            return cur.lastrowid

    def get_accounts(self, include_secrets: bool = False) -> List[Dict]:
        """Return ALL accounts including disabled (for management UI). Secrets masked unless include_secrets=True."""
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM accounts ORDER BY id"
            ).fetchall()
            result = []
            for r in rows:
                d = dict(r)
                if not include_secrets:
                    d['api_key']    = d['api_key'][:6] + '…' + d['api_key'][-4:] if len(d['api_key']) > 10 else '****'
                    d['api_secret'] = '****'
                result.append(d)
            return result

    def get_account(self, account_id: int) -> Optional[Dict]:
        """Return full account dict including secrets (for internal use)."""
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute("SELECT * FROM accounts WHERE id=?", (account_id,)).fetchone()
            return dict(row) if row else None

    def delete_account(self, account_id: int) -> bool:
        """Soft-delete account (set enabled=0)."""
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("UPDATE accounts SET enabled=0 WHERE id=?", (account_id,))
            conn.commit()
        return True

    def update_account_balance(self, account_id: int, balance: float, last_connected: str = None) -> bool:
        """Update balance and last_connected timestamp."""
        from datetime import datetime as dt
        ts = last_connected or dt.utcnow().isoformat()
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "UPDATE accounts SET balance_usdt=?, last_connected=? WHERE id=?",
                (balance, ts, account_id)
            )
            conn.commit()
        return True

    def get_trades_by_account(self, account_id: int = None, status: str = 'all', limit: int = 100) -> List[Dict]:
        """Get trades filtered by account_id and status."""
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            clauses = []
            params  = []
            if account_id is not None:
                clauses.append("account_id=?")
                params.append(account_id)
            if status == 'open':
                clauses.append("status='open'")
            elif status == 'closed':
                clauses.append("status!='open'")
            where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
            rows = conn.execute(
                f"SELECT * FROM trades {where} ORDER BY entry_time DESC LIMIT ?",
                params + [limit]
            ).fetchall()
            return [dict(r) for r in rows]

    def get_performance_by_account(self, account_id: int = None) -> Dict:
        """Compute performance metrics for a specific account (or all if None)."""
        import math as _math
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            if account_id is not None:
                rows = conn.execute("""
                    SELECT pnl_usd, entry_time, exit_time, status, direction, pair,
                           entry_price, sl, tp
                    FROM trades WHERE account_id=?
                    AND status IN ('target_hit','stop_loss','closed')
                    AND pnl_usd IS NOT NULL
                    ORDER BY COALESCE(exit_time, entry_time) ASC
                """, (account_id,)).fetchall()
                open_count = conn.execute(
                    "SELECT COUNT(*) FROM trades WHERE account_id=? AND status='open'",
                    (account_id,)
                ).fetchone()[0]
            else:
                rows = conn.execute("""
                    SELECT pnl_usd, entry_time, exit_time, status, direction, pair,
                           entry_price, sl, tp
                    FROM trades WHERE status IN ('target_hit','stop_loss','closed')
                    AND pnl_usd IS NOT NULL
                    ORDER BY COALESCE(exit_time, entry_time) ASC
                """).fetchall()
                open_count = conn.execute(
                    "SELECT COUNT(*) FROM trades WHERE status='open'"
                ).fetchone()[0]

        trades = [dict(r) for r in rows]
        if not trades:
            return {'total_trades': 0, 'open_trades': open_count,
                    'win_rate': 0, 'profit_factor': 0, 'sharpe': 0,
                    'sortino': 0, 'max_drawdown_pct': 0, 'total_pnl': 0,
                    'avg_pnl': 0, 'avg_rr': 0, 'equity_curve': [],
                    'daily_pnl': [], 'monthly_pnl': []}

        pnls   = [t['pnl_usd'] for t in trades]
        wins   = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p < 0]
        total  = len(pnls)
        win_rate = round(len(wins) / total * 100, 1)
        gross_profit = sum(wins) if wins else 0
        gross_loss   = abs(sum(losses)) if losses else 0
        pf = round(gross_profit / gross_loss, 2) if gross_loss > 0 else 999.0
        mean = sum(pnls) / total
        variance = sum((p - mean) ** 2 for p in pnls) / total
        std = _math.sqrt(variance) if variance > 0 else 0
        sharpe = round(mean / std * _math.sqrt(252), 2) if std > 0 else 0.0
        downside = [p for p in pnls if p < 0]
        d_var = sum(p**2 for p in downside) / len(downside) if downside else 0
        d_std = _math.sqrt(d_var) if d_var > 0 else 0
        sortino = round(mean / d_std * _math.sqrt(252), 2) if d_std > 0 else 0.0
        equity = 0.0; peak = 0.0; max_dd_abs = 0.0; eq_curve = []
        for t in trades:
            equity += t['pnl_usd']
            if equity > peak: peak = equity
            dd = peak - equity
            if dd > max_dd_abs: max_dd_abs = dd
            ts = t.get('exit_time') or t.get('entry_time') or ''
            eq_curve.append({'t': ts[:16], 'v': round(equity, 2)})
        max_dd_pct = round(max_dd_abs / peak * 100, 2) if peak > 0 else 0.0
        daily = {}
        for t in trades:
            day = (t.get('exit_time') or t.get('entry_time') or '')[:10]
            if day: daily[day] = round(daily.get(day, 0) + t['pnl_usd'], 2)
        rrs = []
        for t in trades:
            try:
                ep, sl, tp = t.get('entry_price'), t.get('sl'), t.get('tp')
                if ep and sl and tp and abs(ep - sl) > 0:
                    rrs.append(abs(tp - ep) / abs(ep - sl))
            except Exception: pass
        return {
            'total_trades': total, 'open_trades': open_count,
            'win_rate': win_rate, 'profit_factor': pf,
            'sharpe': sharpe, 'sortino': sortino,
            'max_drawdown_pct': max_dd_pct, 'total_pnl': round(sum(pnls), 2),
            'avg_pnl': round(mean, 2), 'avg_rr': round(sum(rrs)/len(rrs), 2) if rrs else 0,
            'equity_curve': eq_curve,
            'daily_pnl': [{'date': k, 'pnl': v} for k, v in sorted(daily.items())[-30:]],
            'monthly_pnl': [],
        }

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

    def save_signal(self, pair: str, timeframe: str, signal, sweep_id: Optional[int] = None) -> Optional[int]:
        """Insert a trade signal. Deduplicates: skips if same pair+tf+direction+~entry already exists
        within a timeframe-appropriate window. Returns signal_id or existing id if duplicate."""
        # Dedup window per timeframe — matches the sweep age limits in signal engine
        tf_dedup_hours = {'15m': 2, '1h': 6, '4h': 16, '1d': 48}
        dedup_hours = tf_dedup_hours.get(timeframe, 6)
        # Dedup: check for recent identical signal (same pair+tf+direction, entry within 0.5%)
        with sqlite3.connect(self.db_path) as conn:
            rows = conn.execute(
                f"""SELECT id, entry_price FROM signals
                   WHERE pair=? AND timeframe=? AND direction=?
                   AND generated_at >= datetime('now', '-{dedup_hours} hours')""",
                (pair, timeframe, signal.direction)
            ).fetchall()
            for (sig_id, ep) in rows:
                if abs(ep - signal.entry_price) / max(signal.entry_price, 1e-9) < 0.005:
                    logger.debug(f"Signal dedup: {pair} {timeframe} {signal.direction} ~{signal.entry_price:.6g} already saved (id={sig_id})")
                    return sig_id  # return existing id so trade can still link to it
            # Not a duplicate — insert
            cur = conn.cursor()
            cur.execute("""
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
            return cur.lastrowid

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
                         notes: str = '', mode: str = 'paper',
                         order_id: str = None, account_id: int = None) -> int:
        """Create an open trade. Returns trade ID, or None if duplicate."""
        # Dedup: block if an open trade already exists for same pair+direction+account+entry (±0.5%)
        with sqlite3.connect(self.db_path) as conn:
            if account_id is not None:
                existing = conn.execute(
                    "SELECT entry_price FROM trades WHERE pair=? AND direction=? AND account_id=? AND status='open'",
                    (pair, direction, account_id)
                ).fetchall()
            else:
                existing = conn.execute(
                    "SELECT entry_price FROM trades WHERE pair=? AND direction=? AND (account_id IS NULL) AND status='open'",
                    (pair, direction)
                ).fetchall()
            for (ep,) in existing:
                if abs(ep - entry_price) / max(entry_price, 1e-9) < 0.005:
                    logger.debug(f"Dedup: open trade already exists for {pair} {direction} ~{entry_price}")
                    return None
            if len(existing) >= 1:
                logger.debug(f"Dedup: max open trades reached for {pair} {direction}")
                return None
            cur = conn.cursor()
            cur.execute("""
                INSERT INTO trades
                (pair, timeframe, entry_time, direction, entry_price, sl, tp,
                 notional_usd, commission_usd, status, notes, signal_id, mode, order_id, account_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', ?, ?, ?, ?, ?)
            """, (
                pair, timeframe, entry_time.isoformat(), direction, entry_price,
                sl, tp, notional_usd, commission_usd, notes, signal_id, mode, order_id, account_id
            ))
            new_trade_id = cur.lastrowid
            # Cancel ALL remaining pending signals for this pair+direction
            # This is critical: once we enter, no more pending signals should fire for the same zone
            conn.execute(
                "UPDATE pending_signals SET status='cancelled' WHERE pair=? AND direction=? AND status='pending'",
                (pair, direction)
            )
            conn.commit()
            return new_trade_id

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
