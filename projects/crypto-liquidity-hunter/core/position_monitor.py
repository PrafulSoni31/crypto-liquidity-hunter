"""
Position Monitor — Price-based SL/TP monitoring + auto-close.

Two modes depending on API key permissions:

MODE A (full API access — preferred):
  - Polls Binance via ccxt every interval seconds
  - Reconciles live positions, places bracket orders, detects closes

MODE B (IP-restricted key — fallback):
  - Uses Binance PUBLIC price API (no auth needed)
  - Watches prices against SL/TP stored in DB
  - When price hits SL or TP → places market close order
  - Falls back to MODE A when permissions are restored

Runs as daemon background thread. Thread-safe via SQLite DataStore.
"""
import threading
import logging
import time
import json
import urllib.request
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


class PositionMonitor:
    """
    Background thread: monitors open positions and enforces SL/TP.
    """

    def __init__(self, connector, store, account_id: int = None,
                 interval: int = 5):
        self.connector   = connector
        self.store       = store
        self.account_id  = account_id
        self.interval    = interval
        self._stop_event = threading.Event()
        self._thread     = None
        self._lock       = threading.Lock()
        self._sltp_placed: set = set()
        self._closed_trades: set = set()
        self._api_ok: bool = True
        # Debounce: require N consecutive "not found on exchange" checks before closing DB
        # Prevents false closes from transient API glitches
        self._missing_count: Dict[int, int] = {}   # trade_id → consecutive missing count
        self._CLOSE_THRESHOLD = 2                   # must be missing 2× in a row

    # ── Public API ─────────────────────────────────────────────────────────────

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run, name=f"pos-monitor-{self.account_id}", daemon=True
        )
        self._thread.start()
        logger.info(f"[PositionMonitor] Started (account={self.account_id}, interval={self.interval}s)")
        # Run a cleanup on startup to cancel orphaned orders from previous sessions
        threading.Thread(target=self._startup_cleanup, daemon=True, name=f"pos-cleanup-{self.account_id}").start()

    def _startup_cleanup(self):
        """
        On startup: cancel open orders for symbols that have no active position.
        Fixes accumulated orphaned bracket orders from previous sessions.
        """
        import time as _time
        _time.sleep(10)   # wait for connector to be ready
        try:
            if not self._api_ok or not self.connector or self.connector.mode == 'paper':
                return
            import hmac as _hmac, hashlib as _hashlib, requests as _req
            logger.info("[Monitor] Running startup orphaned-order cleanup...")

            # Get all active positions
            positions = self.connector.get_positions()
            active_syms = {p.get('raw_symbol', '') for p in positions}

            # Get all open orders
            ts  = int(_time.time() * 1000)
            par = f"timestamp={ts}&recvWindow=5000"
            sig = _hmac.new(self.connector.api_secret.encode(), par.encode(), _hashlib.sha256).hexdigest()
            r = _req.get(
                f"https://fapi.binance.com/fapi/v1/openOrders?{par}&signature={sig}",
                headers={"X-MBX-APIKEY": self.connector.api_key}, timeout=10
            )
            all_orders = r.json()
            if not isinstance(all_orders, list):
                return

            # Find symbols with open orders but no position
            order_syms = {o['symbol'] for o in all_orders if o.get('reduceOnly')}
            orphan_syms = order_syms - active_syms
            if orphan_syms:
                logger.info(f"[Monitor] Found orphaned orders for: {orphan_syms}")
                for sym in orphan_syms:
                    self._cancel_orphaned_orders(sym)
            else:
                logger.info("[Monitor] No orphaned orders found at startup")
        except Exception as e:
            logger.warning(f"[Monitor] Startup cleanup error: {e}")

    def stop(self):
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=15)
        logger.info(f"[PositionMonitor] Stopped (account={self.account_id})")

    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def mark_sltp_placed(self, trade_id: int):
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
        if not self.connector or self.connector.mode == 'paper':
            return

        db_trades = self._get_open_trades()
        if not db_trades:
            return

        # Fetch live positions from Binance (uses v2/positionRisk — works on multi-assets accounts)
        live_positions = {}
        binance_fetch_ok = False
        if self._api_ok:
            try:
                pos_list = self.connector.get_positions()
                binance_fetch_ok = True   # fetch succeeded (even if empty = 0 positions)
                for p in pos_list:
                    live_positions[p.get('raw_symbol', '')] = p
                    live_positions[p.get('symbol', '')]     = p
                logger.debug(f"[Monitor] Live positions from Binance: {len(pos_list)}")
            except Exception as e:
                logger.warning(f"[Monitor] get_positions failed: {e}")
                binance_fetch_ok = False

        # Fetch current prices from Binance public API (no auth needed)
        symbols_binance = []
        for t in db_trades:
            sym = t['pair'].split(':', 1)[1] if ':' in t['pair'] else t['pair']
            symbols_binance.append(sym.replace('/', ''))
        price_map = self._fetch_public_prices(symbols_binance)

        for trade in db_trades:
            if trade['id'] in self._closed_trades:
                continue

            sym      = trade['pair'].split(':', 1)[1] if ':' in trade['pair'] else trade['pair']
            raw_sym  = sym.replace('/', '')
            ccxt_sym = self._normalise_symbol(sym)

            # ── Check against live Binance positions ─────────────────────────
            # Use Binance as source of truth ONLY if the fetch actually succeeded.
            # An empty result (0 positions) is valid and means ALL positions closed.
            if binance_fetch_ok:
                trade_id = trade['id']
                live_p = live_positions.get(raw_sym) or live_positions.get(ccxt_sym)
                if not live_p:
                    # Position NOT found on Binance this cycle
                    # Increment missing counter — require _CLOSE_THRESHOLD consecutive
                    # misses before marking closed (prevents false closes on API glitch)
                    self._missing_count[trade_id] = self._missing_count.get(trade_id, 0) + 1
                    miss_count = self._missing_count[trade_id]
                    logger.debug(f"[Monitor] {sym} not in live positions (miss #{miss_count}/{self._CLOSE_THRESHOLD})")
                    if miss_count < self._CLOSE_THRESHOLD:
                        logger.info(f"[Monitor] {sym} missing {miss_count}/{self._CLOSE_THRESHOLD} — waiting for confirmation")
                        continue  # don't close yet — wait for next cycle
                    # Confirmed missing — position was closed on exchange
                    self._missing_count.pop(trade_id, None)
                    mark = price_map.get(raw_sym, 0.0)
                    exit_price = self._get_actual_exit_price(sym, float(trade.get('entry_price', 0)))
                    if exit_price <= 0:
                        exit_price = mark or float(trade.get('entry_price', 0))
                    direction = trade.get('direction', 'short')
                    sl = float(trade.get('sl') or 0)
                    tp = float(trade.get('tp') or 0)
                    reason = 'closed_on_exchange'
                    if sl > 0 and exit_price > 0:
                        if (direction == 'long'  and exit_price <= sl * 1.005) or \
                           (direction == 'short' and exit_price >= sl * 0.995):
                            reason = 'stop_loss'
                    if tp > 0 and exit_price > 0:
                        if (direction == 'long'  and exit_price >= tp * 0.995) or \
                           (direction == 'short' and exit_price <= tp * 1.005):
                            reason = 'target_hit'
                    logger.info(f"[Monitor] Position {trade_id} {sym} confirmed closed: "
                                f"exit={exit_price:.6g} reason={reason}")
                    self._close_trade_now(trade, exit_price, reason, sym)
                    # Cancel any remaining bracket orders for this symbol
                    self._cancel_orphaned_orders(raw_sym)
                    continue
                # Position found on Binance — reset missing counter
                self._missing_count.pop(trade.get('id'), None)
                # Update unrealised PnL
                mark = float(live_p.get('mark_price', 0))
                if mark > 0:
                    price_map[raw_sym] = mark

            # ── Price-based SL/TP check (always runs as safety net) ──────────
            self._check_trade(trade, price_map)

        # Try to place SL/TP bracket orders if API is accessible
        if self._api_ok:
            self._try_place_bracket_orders(db_trades)

    # ── Price-based SL/TP enforcement ─────────────────────────────────────────

    def _check_trade(self, trade: Dict, price_map: Dict):
        """
        Check if current price has hit SL or TP.
        If yes → place market close order (or update DB if API restricted).
        """
        trade_id  = trade['id']
        pair      = trade['pair']
        sym       = pair.split(':', 1)[1] if ':' in pair else pair
        binance_sym = sym.replace('/', '')
        direction = trade.get('direction', 'long')
        sl        = float(trade.get('sl') or 0)
        tp        = float(trade.get('tp') or 0)
        ep        = float(trade.get('entry_price') or 0)

        mark = price_map.get(binance_sym, 0.0)
        if mark <= 0:
            return  # price not available

        hit_sl = False
        hit_tp = False

        if direction == 'long':
            if sl > 0 and mark <= sl:
                hit_sl = True
                logger.warning(f"[Monitor] SL hit: {sym} mark={mark} <= sl={sl} (LONG)")
            if tp > 0 and mark >= tp:
                hit_tp = True
                logger.info(f"[Monitor] TP hit: {sym} mark={mark} >= tp={tp} (LONG)")
        else:  # short
            if sl > 0 and mark >= sl:
                hit_sl = True
                logger.warning(f"[Monitor] SL hit: {sym} mark={mark} >= sl={sl} (SHORT)")
            if tp > 0 and mark <= tp:
                hit_tp = True
                logger.info(f"[Monitor] TP hit: {sym} mark={mark} <= tp={tp} (SHORT)")

        if hit_tp or hit_sl:
            status = 'target_hit' if hit_tp else 'stop_loss'
            exit_price = mark
            self._close_trade_now(trade, exit_price, status, sym)

    def _get_actual_exit_price(self, sym: str, entry_price: float) -> float:
        """
        Try to get the actual exit price from Binance recent trade history.
        Falls back to current mark price from public API.
        """
        # Try Binance user trade history for this symbol
        try:
            import hmac as _hmac, hashlib as _hashlib, time as _time, requests as _req
            raw_sym = sym.replace('/', '')
            ts  = int(_time.time() * 1000)
            par = f"symbol={raw_sym}&limit=5&timestamp={ts}&recvWindow=5000"
            sig = _hmac.new(self.connector.api_secret.encode(), par.encode(), _hashlib.sha256).hexdigest()
            r = _req.get(
                f"https://fapi.binance.com/fapi/v1/userTrades?{par}&signature={sig}",
                headers={"X-MBX-APIKEY": self.connector.api_key}, timeout=8
            )
            trades = r.json()
            if isinstance(trades, list) and trades:
                # Most recent trade
                latest = sorted(trades, key=lambda x: x.get('time', 0), reverse=True)[0]
                price = float(latest.get('price', 0))
                if price > 0:
                    logger.info(f"[Monitor] Actual exit price from trade history: {sym} @ {price}")
                    return price
        except Exception as e:
            logger.debug(f"[Monitor] Trade history fetch failed: {e}")

        # Fallback to current public price
        raw_sym = sym.replace('/', '')
        prices  = self._fetch_public_prices([raw_sym])
        return prices.get(raw_sym, entry_price)

    def _close_trade_now(self, trade: Dict, exit_price: float, status: str, sym: str):
        """
        Update DB with actual exit price from Binance trade history.
        Position is already closed on exchange — no need to place orders.
        """
        trade_id  = trade['id']
        direction = trade.get('direction', 'long')
        notional  = float(trade.get('notional_usd', 0) or 0)
        ep        = float(trade.get('entry_price', exit_price) or exit_price)
        commission= float(trade.get('commission_usd', 0) or 0)

        # Try to get actual exit price from Binance trade history (more accurate)
        if exit_price <= 0 or exit_price == ep:
            actual = self._get_actual_exit_price(sym, ep)
            if actual > 0:
                exit_price = actual

        # Compute PnL
        if ep > 0 and notional > 0:
            if direction == 'long':
                pnl = (exit_price - ep) / ep * notional - commission * 2
            else:
                pnl = (ep - exit_price) / ep * notional - commission * 2
        else:
            pnl = 0.0

        # Position already closed on exchange — no market close needed
        close_placed = True

        # Always update DB
        try:
            self.store.close_trade(
                trade_id   = trade_id,
                exit_price = exit_price,
                exit_time  = datetime.now(timezone.utc),
                status     = status,
                pnl_usd    = round(pnl, 4),
            )
            self._closed_trades.add(trade_id)
            self._sltp_placed.discard(trade_id)
            logger.info(
                f"[Monitor] Trade {trade_id} {sym} closed "
                f"exit={exit_price:.6g} pnl=${pnl:.2f} status={status} "
                f"exchange_close={'ok' if close_placed else 'FAILED-IP-restricted'}"
            )
        except Exception as e:
            logger.error(f"[Monitor] DB close error trade {trade_id}: {e}")

    def _cancel_orphaned_orders(self, raw_sym: str):
        """
        Cancel all open orders for a symbol when its position is closed.
        This cleans up surviving TP/SL bracket orders after the other leg fills.
        e.g. SL fills → TP limit order stays open forever → cancel it now.
        """
        if not self._api_ok or not self.connector or self.connector.mode == 'paper':
            return
        try:
            import time as _time, hmac as _hmac, hashlib as _hashlib, requests as _req
            ts  = int(_time.time() * 1000)
            par = f"symbol={raw_sym}&timestamp={ts}&recvWindow=5000"
            sig = _hmac.new(self.connector.api_secret.encode(), par.encode(), _hashlib.sha256).hexdigest()
            # GET open orders for this symbol
            r_get = _req.get(
                f"https://fapi.binance.com/fapi/v1/openOrders?{par}&signature={sig}",
                headers={"X-MBX-APIKEY": self.connector.api_key}, timeout=8
            )
            orders = r_get.json()
            if not isinstance(orders, list) or not orders:
                return
            logger.info(f"[Monitor] Cancelling {len(orders)} orphaned orders for {raw_sym}")
            # Cancel all open orders for this symbol
            ts2  = int(_time.time() * 1000)
            par2 = f"symbol={raw_sym}&timestamp={ts2}&recvWindow=5000"
            sig2 = _hmac.new(self.connector.api_secret.encode(), par2.encode(), _hashlib.sha256).hexdigest()
            r_del = _req.delete(
                f"https://fapi.binance.com/fapi/v1/allOpenOrders?{par2}&signature={sig2}",
                headers={"X-MBX-APIKEY": self.connector.api_key,
                         'Content-Type': 'application/x-www-form-urlencoded'}, timeout=8
            )
            result = r_del.json()
            logger.info(f"[Monitor] Cancel all orders {raw_sym}: {result}")
        except Exception as e:
            logger.warning(f"[Monitor] _cancel_orphaned_orders error for {raw_sym}: {e}")

    def _place_market_close(self, sym: str, direction: str, trade: Dict,
                            exit_price: float) -> bool:
        """Place a reduceOnly market order to close position. Returns success."""
        try:
            ccxt_sym   = self._normalise_symbol(sym)
            close_side = 'sell' if direction == 'long' else 'buy'
            notional   = float(trade.get('notional_usd', 0) or 0)
            ep         = float(trade.get('entry_price', exit_price) or exit_price)
            qty        = notional / ep if ep > 0 else 0
            if qty <= 0:
                return False
            qty_r = self.connector._round_qty(ccxt_sym, qty)
            order = self.connector.exchange.create_order(
                ccxt_sym, 'market', close_side, qty_r,
                params={'reduceOnly': True}
            )
            actual_exit = float(order.get('average') or order.get('price') or exit_price)
            logger.info(f"[Monitor] Market close placed: {close_side} {qty_r} {ccxt_sym} @ {actual_exit}")
            return True
        except Exception as e:
            err = str(e)
            if '-2015' in err or 'Invalid API-key' in err or 'IP' in err:
                logger.warning(f"[Monitor] API IP-restricted, cannot place close order: {e}")
                self._api_ok = False   # stop trying until reconnect
            else:
                logger.error(f"[Monitor] Market close error for {sym}: {e}")
            return False

    # ── Bracket order placement ────────────────────────────────────────────────

    def _try_place_bracket_orders(self, db_trades: List[Dict]):
        """Try to place SL+TP bracket orders for trades that don't have them yet."""
        for trade in db_trades:
            trade_id = trade['id']
            if trade_id in self._sltp_placed:
                continue
            sl = float(trade.get('sl') or 0)
            tp = float(trade.get('tp') or 0)
            if sl <= 0 and tp <= 0:
                continue

            sym     = trade['pair'].split(':', 1)[1] if ':' in trade['pair'] else trade['pair']
            ccxt_sym = self._normalise_symbol(sym)
            direction = trade.get('direction', 'long')

            # Estimate qty from notional
            notional = float(trade.get('notional_usd', 0) or 0)
            ep       = float(trade.get('entry_price', 0) or 0)
            qty      = notional / ep if ep > 0 else 0

            if qty > 0:
                try:
                    result = self.connector.set_sl_tp(ccxt_sym, direction, qty, sl, tp)
                    if 'error' in result:
                        logger.error(f"[Monitor] Bracket order failed trade {trade_id}: {result['error']}")
                        err = result['error']
                        if '-2015' in err or 'Invalid API-key' in err or 'IP' in err:
                            self._api_ok = False
                    else:
                        logger.info(f"[Monitor] Bracket placed trade {trade_id}: SL={result.get('sl_price')} TP={result.get('tp_price')}")
                        self._sltp_placed.add(trade_id)
                except Exception as e:
                    logger.error(f"[Monitor] _try_place_bracket error: {e}")

    # ── Helpers ────────────────────────────────────────────────────────────────

    def _get_open_trades(self) -> List[Dict]:
        all_open = self.store.get_open_trades()
        result = []
        for t in all_open:
            if t['id'] in self._closed_trades:
                continue
            # Include live trades (not paper) for this account
            if t.get('mode', 'paper') == 'paper':
                continue
            if self.account_id is not None and t.get('account_id') != self.account_id:
                continue
            result.append(t)
        return result

    def _normalise_symbol(self, symbol: str) -> str:
        """BTC/USDT → BTC/USDT:USDT for futures ccxt."""
        if ':' not in symbol and '/' in symbol:
            base, quote = symbol.split('/', 1)
            return f"{base}/{quote}:{quote}"
        return symbol

    def _fetch_public_prices(self, binance_symbols: List[str]) -> Dict[str, float]:
        """
        Fetch current prices from Binance public REST (no auth, no IP restriction).
        Returns {BINANCESYM: price} e.g. {'BTCUSDT': 66500.0}
        """
        try:
            with urllib.request.urlopen(
                'https://api.binance.com/api/v3/ticker/price', timeout=5
            ) as resp:
                data = json.loads(resp.read())
            price_map = {item['symbol']: float(item['price']) for item in data}
            return price_map
        except Exception as e:
            logger.warning(f"[Monitor] Public price fetch error: {e}")
            return {}

    def _get_exit_details(self, symbol: str, trade: Dict) -> Tuple[float, str]:
        """Try to find exit price from order history. Fallback to last ticker price."""
        ccxt_sym   = self._normalise_symbol(symbol)
        direction  = trade.get('direction', 'long')
        entry_price = float(trade.get('entry_price', 0))
        sl         = float(trade.get('sl') or 0)
        tp         = float(trade.get('tp') or 0)
        exit_price = 0.0

        if self._api_ok:
            try:
                orders = self.connector.exchange.fetch_orders(
                    ccxt_sym, limit=20,
                    params={'startTime': int(time.time() * 1000) - 3_600_000}
                )
                close_side = 'sell' if direction == 'long' else 'buy'
                filled = [
                    o for o in orders
                    if o.get('status') == 'closed'
                    and o.get('side') == close_side
                    and float(o.get('filled', 0) or 0) > 0
                ]
                if filled:
                    filled.sort(key=lambda o: o.get('timestamp', 0), reverse=True)
                    exit_price = float(filled[0].get('average') or filled[0].get('price') or 0)
            except Exception as e:
                if '-2015' in str(e):
                    self._api_ok = False

        if exit_price <= 0:
            # Fallback: use public price
            bsym = symbol.replace('/', '')
            prices = self._fetch_public_prices([bsym])
            exit_price = prices.get(bsym, entry_price)

        # Determine status
        status = 'closed'
        if tp > 0 and exit_price > 0:
            if (direction == 'long' and exit_price >= tp * 0.995) or \
               (direction == 'short' and exit_price <= tp * 1.005):
                status = 'target_hit'
        if sl > 0 and exit_price > 0:
            if (direction == 'long' and exit_price <= sl * 1.005) or \
               (direction == 'short' and exit_price >= sl * 0.995):
                status = 'stop_loss'

        return exit_price, status


# ── Global registry ────────────────────────────────────────────────────────────
_monitors: Dict[int, 'PositionMonitor'] = {}
_monitors_lock = threading.Lock()


def get_monitor(account_id: int) -> Optional[PositionMonitor]:
    with _monitors_lock:
        return _monitors.get(account_id)


def start_monitor(connector, store, account_id: int,
                  interval: int = 5) -> PositionMonitor:
    """Start (or restart) a position monitor for an account."""
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
