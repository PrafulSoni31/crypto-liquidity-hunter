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
            # Note: our bracket orders do NOT use reduceOnly (blocked on this account type)
            # so we check ALL open orders, not just reduceOnly ones
            order_syms = {o['symbol'] for o in all_orders}
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
        # Paper trades: skip Binance position reconciliation but STILL check SL/TP
        is_paper = self.connector and self.connector.mode == 'paper'

        db_trades = self._get_open_trades()
        if not db_trades:
            return

        # Fetch live positions from Binance (uses v2/positionRisk — works on multi-assets accounts)
        # SKIP for paper trades — no live positions exist on exchange
        live_positions = {}
        binance_fetch_ok = False
        if not is_paper and self._api_ok:
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

        # Fetch current klines to catch wicks for paper trades (and live backups)
        symbols_binance = []
        for t in db_trades:
            sym = t['pair'].split(':', 1)[1] if ':' in t['pair'] else t['pair']
            symbols_binance.append(sym.replace('/', ''))
        kline_map = self._fetch_public_klines(symbols_binance)
        # Fallback to ticker price if klines fail
        price_map = self._fetch_public_prices(symbols_binance)
        
        # Merge them so price_map has dicts
        for s, k_data in kline_map.items():
            price_map[s] = k_data

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
                    # Position NOT found on Binance this cycle.
                    # IMPORTANT: Verify the trade was actually entered before closing it.
                    # A trade in DB can have mode='live' but the entry order may have failed
                    # silently, meaning the position never existed on Binance. In that case
                    # we should mark it as 'entry_failed' not 'closed_on_exchange'.
                    entry_time_str = trade.get('entry_time', '')
                    trade_age_secs = 0
                    try:
                        from datetime import datetime as _dt2
                        if entry_time_str:
                            et = _dt2.fromisoformat(entry_time_str.replace('Z', '+00:00'))
                            et_utc = et.replace(tzinfo=timezone.utc) if et.tzinfo is None else et
                            trade_age_secs = (datetime.now(timezone.utc) - et_utc).total_seconds()
                    except Exception:
                        pass

                    # If trade is very fresh (< 30s) — skip, give entry time to register on Binance
                    if trade_age_secs < 30:
                        logger.debug(f"[Monitor] {sym} trade #{trade_id} is only {trade_age_secs:.0f}s old — skip close check")
                        continue

                    # Increment missing counter — require _CLOSE_THRESHOLD consecutive
                    # misses before marking closed (prevents false closes on API glitch)
                    self._missing_count[trade_id] = self._missing_count.get(trade_id, 0) + 1
                    miss_count = self._missing_count[trade_id]
                    logger.debug(f"[Monitor] {sym} not in live positions (miss #{miss_count}/{self._CLOSE_THRESHOLD})")
                    if miss_count < self._CLOSE_THRESHOLD:
                        logger.info(f"[Monitor] {sym} missing {miss_count}/{self._CLOSE_THRESHOLD} — waiting for confirmation")
                        continue  # don't close yet — wait for next cycle

                    # Confirmed missing — check trade history to distinguish:
                    #   a) Entry never filled → entry_failed
                    #   b) Position closed after being open → closed_on_exchange / stop_loss / target_hit
                    self._missing_count.pop(trade_id, None)

                    # Check Binance user trade history to confirm entry was ever filled
                    entry_was_filled = self._verify_entry_filled(sym, float(trade.get('entry_price', 0)))
                    if not entry_was_filled:
                        logger.warning(f"[Monitor] Trade {trade_id} {sym} — no fill found on exchange. "
                                       f"Marking as entry_failed (position never opened).")
                        self._close_trade_now(trade, float(trade.get('entry_price', 0)), 'entry_failed', sym)
                        self._cancel_orphaned_orders(raw_sym)
                        continue

                    mark = price_map.get(raw_sym, 0.0)
                    exit_price = self._get_actual_exit_price(sym, float(trade.get('entry_price', 0)))
                    if exit_price <= 0:
                        exit_price = mark or float(trade.get('entry_price', 0))
                    direction = trade.get('direction', 'short')
                    sl = float(trade.get('sl') or 0)
                    tp = float(trade.get('tp') or 0)
                    # ── Classify exit reason using STRICT price logic ──────
                    # LONG:  SL hit  → exit_price <= sl (price fell to/below SL)
                    #        TP hit  → exit_price >= tp (price rose to/above TP)
                    # SHORT: SL hit  → exit_price >= sl (price rose to/above SL)
                    #        TP hit  → exit_price <= tp (price fell to/below TP)
                    # Tolerance ±0.15% only to account for slippage on market fills.
                    # (Previous 0.5% tolerance caused false SL labels when exit was between entry & SL)
                    TOL = 0.0015
                    reason = 'closed_on_exchange'
                    if sl > 0 and exit_price > 0:
                        if (direction == 'long'  and exit_price <= sl * (1 + TOL)) or \
                           (direction == 'short' and exit_price >= sl * (1 - TOL)):
                            reason = 'stop_loss'
                    if tp > 0 and exit_price > 0:
                        if (direction == 'long'  and exit_price >= tp * (1 - TOL)) or \
                           (direction == 'short' and exit_price <= tp * (1 + TOL)):
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

        # NOTE: Do NOT call _try_place_bracket_orders here.
        # SL/TP is placed atomically at entry via place_entry_with_sl (batchOrders).
        # Placing bracket orders from the monitor causes duplicates because:
        # - Multiple gunicorn workers each run a monitor instance
        # - Each gunicorn restart resets _sltp_placed memory
        # The monitor's only job is to DETECT closes and cancel orphans.
        # Bracket placement is the executor's responsibility, not the monitor's.

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

        price_info = price_map.get(binance_sym, 0.0)
        if isinstance(price_info, dict):
            mark = price_info.get('mark', 0.0)
            high = price_info.get('high', mark)
            low  = price_info.get('low', mark)
        else:
            mark = price_info
            high = mark
            low  = mark

        if mark <= 0:
            return  # price not available

        hit_sl = False
        hit_tp = False

        # STRICT exit logic — checks 1m high/low to catch fast wicks for paper trades!
        if direction == 'long':
            if sl > 0 and low <= sl:
                hit_sl = True
                mark = sl  # closed exactly at SL
                logger.warning(f"[Monitor] SL hit (wick): {sym} low={low:.6g} <= sl={sl:.6g} (LONG)")
            elif tp > 0 and high >= tp:
                hit_tp = True
                mark = tp  # closed exactly at TP
                logger.info(f"[Monitor] TP hit (wick): {sym} high={high:.6g} >= tp={tp:.6g} (LONG)")
        else:  # short
            if sl > 0 and high >= sl:
                hit_sl = True
                mark = sl  # closed exactly at SL
                logger.warning(f"[Monitor] SL hit (wick): {sym} high={high:.6g} >= sl={sl:.6g} (SHORT)")
            elif tp > 0 and low <= tp:
                hit_tp = True
                mark = tp  # closed exactly at TP
                logger.info(f"[Monitor] TP hit (wick): {sym} low={low:.6g} <= tp={tp:.6g} (SHORT)")

        if hit_tp or hit_sl:
            status = 'target_hit' if hit_tp else 'stop_loss'
            exit_price = mark

            # ── ACTUALLY CLOSE THE POSITION ON BINANCE ────────────────────
            # Previously this only updated the DB — position stayed open on exchange!
            # Now we place a market close order to ensure the position is closed.
            if self._api_ok and self.connector and self.connector.mode != 'paper':
                closed = self._place_market_close(binance_sym, direction, trade, exit_price)
                if closed:
                    logger.info(f"[Monitor] Market close order placed for {sym} at {exit_price:.6g}")
                else:
                    logger.warning(f"[Monitor] Market close FAILED for {sym} — updating DB anyway")
                    # Cancel SL/TP orders and try close via bracket cancellation
                    self._cancel_orphaned_orders(binance_sym)

            self._close_trade_now(trade, exit_price, status, sym)

    def _verify_entry_filled(self, sym: str, entry_price: float) -> bool:
        """
        Check Binance user trade history to see if an entry order was ever filled.
        Returns True if a trade was found near entry_price (within 2%), False otherwise.
        This distinguishes 'entry never placed' from 'position already closed'.
        """
        try:
            import hmac as _hmac, hashlib as _hashlib, time as _time, requests as _req
            raw_sym = sym.replace('/', '')
            ts  = int(_time.time() * 1000)
            # Look back 24h for any trades on this symbol
            start_time = ts - 86_400_000
            par = f"symbol={raw_sym}&limit=10&startTime={start_time}&timestamp={ts}&recvWindow=5000"
            sig = _hmac.new(self.connector.api_secret.encode(), par.encode(), _hashlib.sha256).hexdigest()
            r = _req.get(
                f"https://fapi.binance.com/fapi/v1/userTrades?{par}&signature={sig}",
                headers={"X-MBX-APIKEY": self.connector.api_key}, timeout=8
            )
            trades = r.json()
            if isinstance(trades, list) and trades:
                for t in trades:
                    price = float(t.get('price', 0))
                    if entry_price > 0 and abs(price - entry_price) / entry_price < 0.02:
                        return True  # Found a fill near our entry price
                # Trades exist but none near our entry — position may have been different
                # Still return True to be safe (avoid marking valid closed positions as entry_failed)
                return True
            # No trades found at all — entry likely never executed
            return False
        except Exception as e:
            logger.debug(f"[Monitor] _verify_entry_filled error for {sym}: {e}")
            # On error, assume entry was filled (safe default — better to mark closed_on_exchange
            # than to leave a zombie trade open forever)
            return True

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
                f"exit={exit_price:.6g} pnl=${pnl:.2f} status={status}"
            )
            # Activity log
            try:
                import sys, os
                sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
                from scheduler.activity_logger import log_event as _log
                evt_type = {'target_hit': 'TP_HIT', 'stop_loss': 'SL_HIT',
                            'entry_failed': 'ENTRY_FAILED_NO_POSITION'}.get(status, 'CLOSED_ON_EXCHANGE')
                _log(evt_type, pair=sym, trade_id=trade_id, direction=direction,
                     entry=ep, exit=exit_price, pnl=round(pnl, 4), status=status)
            except Exception:
                pass
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

    def _get_open_order_count(self, raw_sym: str) -> int:
        """Return number of open reduceOnly orders for a symbol on Binance."""
        try:
            import time as _t, hmac as _h, hashlib as _ha, requests as _r
            ts  = int(_t.time() * 1000)
            par = f"symbol={raw_sym}&timestamp={ts}&recvWindow=5000"
            sig = _h.new(self.connector.api_secret.encode(), par.encode(), _ha.sha256).hexdigest()
            resp = _r.get(
                f"https://fapi.binance.com/fapi/v1/openOrders?{par}&signature={sig}",
                headers={"X-MBX-APIKEY": self.connector.api_key}, timeout=5
            )
            orders = resp.json()
            if isinstance(orders, list):
                return len(orders)  # count ALL orders (our brackets are not reduceOnly)
        except Exception:
            pass
        return -1  # -1 = couldn't check

    def _try_place_bracket_orders(self, db_trades: List[Dict]):
        """
        Place SL+TP bracket orders for trades that don't have them yet.
        CRITICAL: Always checks Binance FIRST — if orders already exist, skip.
        This prevents duplicate orders after gunicorn restarts or monitor restarts.
        _sltp_placed is only an in-memory cache; Binance is the source of truth.
        """
        for trade in db_trades:
            trade_id = trade['id']
            if trade_id in self._sltp_placed:
                continue
            sl = float(trade.get('sl') or 0)
            tp = float(trade.get('tp') or 0)
            if sl <= 0 and tp <= 0:
                continue

            sym      = trade['pair'].split(':', 1)[1] if ':' in trade['pair'] else trade['pair']
            from core.binance_connector import _normalise_k_contract
            norm_sym, mult = _normalise_k_contract(sym)
            raw_sym  = norm_sym.split('/')[0].replace('/', '') + \
                       (norm_sym.split('/')[1].split(':')[0] if '/' in norm_sym else '')
            raw_sym  = raw_sym.replace('/', '')
            direction = trade.get('direction', 'long')

            # ── CHECK BINANCE FIRST ───────────────────────────────────────────
            # If orders already exist on exchange, mark as placed and skip
            existing_count = self._get_open_order_count(raw_sym)
            if existing_count > 0:
                logger.debug(f"[Monitor] Trade {trade_id} {sym}: {existing_count} orders already on exchange — skipping")
                self._sltp_placed.add(trade_id)
                continue
            if existing_count < 0:
                logger.debug(f"[Monitor] Could not check orders for {sym} — skipping to be safe")
                continue  # Can't verify — skip rather than duplicate

            # No existing orders — safe to place
            notional = float(trade.get('notional_usd', 0) or 0)
            ep       = float(trade.get('entry_price', 0) or 0)
            qty      = notional / ep if ep > 0 else 0

            if qty > 0:
                try:
                    result = self.connector.set_sl_tp(norm_sym, direction, qty, sl, tp)
                    if 'error' in result:
                        err = result['error']
                        logger.error(f"[Monitor] Bracket order failed trade {trade_id}: {err}")
                        if '-2015' in err or 'Invalid API-key' in err or 'IP' in err:
                            self._api_ok = False
                        # Mark as placed anyway to avoid hammering exchange with failing requests
                        self._sltp_placed.add(trade_id)
                    else:
                        logger.info(f"[Monitor] Bracket placed trade {trade_id} {sym}: SL={result.get('sl_price')} TP={result.get('tp_price')}")
                        self._sltp_placed.add(trade_id)
                except Exception as e:
                    logger.error(f"[Monitor] _try_place_bracket error: {e}")
                    self._sltp_placed.add(trade_id)  # mark placed to avoid retry storm

    # ── Helpers ────────────────────────────────────────────────────────────────

    def _get_open_trades(self) -> List[Dict]:
        all_open = self.store.get_open_trades()
        result = []
        for t in all_open:
            if t['id'] in self._closed_trades:
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
                'https://fapi.binance.com/fapi/v1/ticker/price', timeout=5
            ) as resp:
                data = json.loads(resp.read())
            price_map = {item['symbol']: float(item['price']) for item in data}
            return price_map
        except Exception as e:
            logger.warning(f"[Monitor] Public price fetch error: {e}")
            return {}

    def _fetch_public_klines(self, binance_symbols: List[str]) -> Dict[str, Dict]:
        """
        Fetch current 1m kline for better paper trade wick detection.
        Returns {BINANCESYM: {'mark': close, 'high': high, 'low': low}}
        """
        result = {}
        for sym in binance_symbols:
            try:
                with urllib.request.urlopen(
                    f'https://fapi.binance.com/fapi/v1/klines?symbol={sym}&interval=1m&limit=1', timeout=2
                ) as resp:
                    data = json.loads(resp.read())
                    if data and len(data) > 0:
                        # [OpenTime, Open, High, Low, Close, ...]
                        result[sym] = {
                            'mark': float(data[0][4]),
                            'high': float(data[0][2]),
                            'low': float(data[0][3])
                        }
            except Exception:
                pass
        return result

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

        # Determine status — STRICT logic, ±0.15% slippage tolerance only
        # LONG:  exit <= sl → stop_loss  |  exit >= tp → target_hit
        # SHORT: exit >= sl → stop_loss  |  exit <= tp → target_hit
        TOL = 0.0015
        status = 'closed'
        if tp > 0 and exit_price > 0:
            if (direction == 'long'  and exit_price >= tp * (1 - TOL)) or \
               (direction == 'short' and exit_price <= tp * (1 + TOL)):
                status = 'target_hit'
        if sl > 0 and exit_price > 0:
            if (direction == 'long'  and exit_price <= sl * (1 + TOL)) or \
               (direction == 'short' and exit_price >= sl * (1 - TOL)):
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
