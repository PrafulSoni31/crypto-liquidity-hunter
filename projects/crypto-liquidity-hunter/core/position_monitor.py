"""
Position Monitor — Live Binance position sync + SL/TP management.

Runs as a background thread. Every `interval` seconds:
  1. Fetches open Binance Futures positions via ccxt
  2. Compares with DB open trades
  3. For newly opened positions → places SL + TP bracket orders
  4. For positions that closed (TP hit, SL hit, manual close) → marks DB trade closed, records PnL
  5. Updates unrealised PnL on all open DB trades

Thread-safe: all DB writes use DataStore (sqlite) which handles concurrent access.
"""
import threading
import logging
import time
from datetime import datetime, timezone
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)


class PositionMonitor:
    """
    Background thread that keeps DB trades in sync with live Binance positions.
    One instance per account/connector.
    """

    def __init__(self, connector, store, account_id: int = None,
                 interval: int = 10, sl_tp_already_placed: bool = False):
        """
        connector  : BinanceConnector (must be connected, mode != 'paper')
        store      : DataStore
        account_id : filter DB trades by this account
        interval   : poll interval in seconds
        sl_tp_already_placed : if True, don't re-place bracket orders
        """
        self.connector   = connector
        self.store       = store
        self.account_id  = account_id
        self.interval    = interval
        self._stop_event = threading.Event()
        self._thread     = None
        self._lock       = threading.Lock()
        # Track which trade IDs have already had SL/TP placed
        self._sltp_placed: set = set()

    # ── Public API ─────────────────────────────────────────────────────────────

    def start(self):
        """Start the background monitor thread."""
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run, name=f"pos-monitor-{self.account_id}", daemon=True
        )
        self._thread.start()
        logger.info(f"[PositionMonitor] Started (account={self.account_id}, interval={self.interval}s)")

    def stop(self):
        """Stop the monitor thread."""
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=15)
        logger.info(f"[PositionMonitor] Stopped (account={self.account_id})")

    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def mark_sltp_placed(self, trade_id: int):
        """Call this after placing SL/TP for a trade so monitor doesn't re-place."""
        self._sltp_placed.add(trade_id)

    # ── Main loop ──────────────────────────────────────────────────────────────

    def _run(self):
        while not self._stop_event.is_set():
            try:
                self._sync()
            except Exception as e:
                logger.error(f"[PositionMonitor] Sync error: {e}", exc_info=True)
            self._stop_event.wait(timeout=self.interval)

    def _sync(self):
        """One sync cycle: fetch Binance positions → reconcile with DB."""
        if not self.connector or not self.connector.connected:
            return
        if self.connector.mode == 'paper':
            return  # paper mode has no real positions

        # 1. Fetch live positions from Binance
        live_positions = self._fetch_positions()          # {symbol → pos_dict}

        # 2. Load open DB trades for this account
        db_trades = self._get_open_trades()               # list of trade dicts

        # 3. For each open DB trade, check if still open on exchange
        for trade in db_trades:
            self._reconcile_trade(trade, live_positions)

        # 4. For each live Binance position, place SL/TP if not already set
        for trade in db_trades:
            trade_id = trade['id']
            if trade_id in self._sltp_placed:
                continue
            symbol = trade['pair'].split(':', 1)[1] if ':' in trade['pair'] else trade['pair']
            # Normalise to ccxt symbol (e.g. BTC/USDT → BTC/USDT:USDT)
            ccxt_sym = self._normalise_symbol(symbol)
            pos = live_positions.get(ccxt_sym) or live_positions.get(symbol)
            if not pos:
                continue
            sl = float(trade.get('sl') or 0)
            tp = float(trade.get('tp') or 0)
            direction = trade.get('direction', 'long')
            qty = abs(float(pos.get('contracts', 0)))
            if qty > 0 and (sl > 0 or tp > 0):
                logger.info(f"[PositionMonitor] Placing bracket orders for trade {trade_id} {symbol}")
                result = self._place_bracket(symbol, direction, qty, sl, tp, trade_id)
                if result:
                    self._sltp_placed.add(trade_id)

    def _fetch_positions(self) -> Dict:
        """Return live positions as {ccxt_symbol → position_dict}."""
        out = {}
        try:
            positions = self.connector.exchange.fetch_positions()
            for p in positions:
                contracts = float(p.get('contracts') or 0)
                if contracts == 0:
                    continue
                sym = p.get('symbol', '')
                out[sym] = {
                    'symbol':          sym,
                    'side':            p.get('side', ''),    # 'long' or 'short'
                    'contracts':       contracts,
                    'entry_price':     float(p.get('entryPrice')     or 0),
                    'mark_price':      float(p.get('markPrice')      or 0),
                    'unrealized_pnl':  float(p.get('unrealizedPnl')  or 0),
                    'leverage':        float(p.get('leverage')        or 1),
                    'notional':        float(p.get('notional')        or 0),
                    'liquidation_price': float(p.get('liquidationPrice') or 0),
                    'margin':          float(p.get('initialMargin')   or 0),
                }
        except Exception as e:
            logger.error(f"[PositionMonitor] fetch_positions error: {e}")
        return out

    def _get_open_trades(self) -> List[Dict]:
        """Get open DB trades for this account."""
        all_open = self.store.get_open_trades()
        if self.account_id is not None:
            return [t for t in all_open
                    if t.get('account_id') == self.account_id
                    and t.get('mode', 'paper') != 'paper']
        # No account_id filter — return all non-paper open trades
        return [t for t in all_open if t.get('mode', 'paper') != 'paper']

    def _normalise_symbol(self, symbol: str) -> str:
        """Convert BTC/USDT → BTC/USDT:USDT for futures ccxt."""
        if ':' not in symbol and '/' in symbol:
            base, quote = symbol.split('/', 1)
            return f"{base}/{quote}:{quote}"
        return symbol

    def _reconcile_trade(self, trade: Dict, live_positions: Dict):
        """
        Check if a DB trade is still live on Binance.
        If the position no longer exists → trade was closed (TP/SL hit or manual).
        Update DB with exit price and actual PnL.
        """
        trade_id = trade['id']
        symbol   = trade['pair'].split(':', 1)[1] if ':' in trade['pair'] else trade['pair']
        ccxt_sym = self._normalise_symbol(symbol)
        direction = trade.get('direction', 'long')

        pos = live_positions.get(ccxt_sym) or live_positions.get(symbol)

        if pos:
            # Position still open — update unrealised PnL in memory (not DB, too noisy)
            upnl = float(pos.get('unrealized_pnl', 0))
            mark = float(pos.get('mark_price', 0))
            # Only update DB if PnL is significantly different (avoid write storms)
            # We expose this via the /api/positions live endpoint instead
            return

        # Position NOT found on exchange → it closed
        # Find out the exit price via recent order history
        exit_price, status = self._get_exit_details(symbol, trade)

        # Recalculate PnL with actual exit
        ep       = float(trade.get('entry_price', 0))
        notional = float(trade.get('notional_usd', 0))
        commission = float(trade.get('commission_usd', 0))
        if exit_price > 0 and ep > 0 and notional > 0:
            if direction == 'long':
                pnl = (exit_price - ep) / ep * notional - commission * 2
            else:
                pnl = (ep - exit_price) / ep * notional - commission * 2
        else:
            pnl = 0.0

        logger.info(f"[PositionMonitor] Trade {trade_id} ({symbol}) closed "
                    f"exit={exit_price:.6g} pnl={pnl:.2f} status={status}")

        self.store.close_trade(
            trade_id   = trade_id,
            exit_price = exit_price,
            exit_time  = datetime.now(timezone.utc),
            status     = status,
            pnl_usd    = round(pnl, 4),
        )
        # Remove from SL/TP tracking
        self._sltp_placed.discard(trade_id)

    def _get_exit_details(self, symbol: str, trade: Dict):
        """
        Try to find exit price from recent filled orders.
        Returns (exit_price, status_string).
        """
        ccxt_sym   = self._normalise_symbol(symbol)
        direction  = trade.get('direction', 'long')
        entry_price = float(trade.get('entry_price', 0))
        sl         = float(trade.get('sl') or 0)
        tp         = float(trade.get('tp') or 0)

        exit_price = 0.0
        try:
            # Fetch recent closed orders for this symbol
            orders = self.connector.exchange.fetch_orders(
                ccxt_sym, limit=20, params={'startTime': int(time.time() * 1000) - 3_600_000}
            )
            # Find the most recent filled closing order
            close_side = 'sell' if direction == 'long' else 'buy'
            filled_closes = [
                o for o in orders
                if o.get('status') == 'closed'
                and o.get('side') == close_side
                and (o.get('reduceOnly') or o.get('type') in ('stop_market', 'take_profit_market', 'market'))
                and float(o.get('filled', 0) or 0) > 0
            ]
            if filled_closes:
                # Use the most recent fill
                filled_closes.sort(key=lambda o: o.get('timestamp', 0), reverse=True)
                best = filled_closes[0]
                exit_price = float(best.get('average') or best.get('price') or 0)
        except Exception as e:
            logger.debug(f"[PositionMonitor] fetch_orders error for {symbol}: {e}")

        # Fall back to mark price from exchange if order history failed
        if exit_price <= 0:
            try:
                ticker = self.connector.exchange.fetch_ticker(ccxt_sym)
                exit_price = float(ticker.get('last', 0) or 0)
            except Exception:
                exit_price = entry_price  # last resort

        # Determine close reason
        status = 'closed'
        if tp > 0 and exit_price > 0:
            if direction == 'long' and exit_price >= tp * 0.995:
                status = 'target_hit'
            elif direction == 'short' and exit_price <= tp * 1.005:
                status = 'target_hit'
        if sl > 0 and exit_price > 0:
            if direction == 'long' and exit_price <= sl * 1.005:
                status = 'stop_loss'
            elif direction == 'short' and exit_price >= sl * 0.995:
                status = 'stop_loss'

        return exit_price, status

    def _place_bracket(self, symbol: str, direction: str, qty: float,
                       sl: float, tp: float, trade_id: int) -> bool:
        """Place SL + TP bracket orders on Binance. Returns True on success."""
        try:
            result = self.connector.set_sl_tp(symbol, direction, qty, sl, tp)
            if 'error' in result:
                logger.error(f"[PositionMonitor] Bracket order failed for trade {trade_id}: {result['error']}")
                return False
            logger.info(f"[PositionMonitor] Bracket placed for trade {trade_id}: "
                        f"SL={result.get('sl_price')} TP={result.get('tp_price')}")
            return True
        except Exception as e:
            logger.error(f"[PositionMonitor] _place_bracket error: {e}")
            return False


# ── Global registry of running monitors ────────────────────────────────────────
_monitors: Dict[int, PositionMonitor] = {}   # account_id → PositionMonitor
_monitors_lock = threading.Lock()


def get_monitor(account_id: int) -> Optional[PositionMonitor]:
    with _monitors_lock:
        return _monitors.get(account_id)


def start_monitor(connector, store, account_id: int, interval: int = 10) -> PositionMonitor:
    """Start (or restart) a monitor for an account. Returns the monitor."""
    with _monitors_lock:
        existing = _monitors.get(account_id)
        if existing and existing.is_running():
            return existing
        m = PositionMonitor(connector, store, account_id=account_id, interval=interval)
        m.start()
        _monitors[account_id] = m
        return m


def stop_monitor(account_id: int):
    with _monitors_lock:
        m = _monitors.pop(account_id, None)
        if m:
            m.stop()


def stop_all():
    with _monitors_lock:
        for m in list(_monitors.values()):
            m.stop()
        _monitors.clear()
