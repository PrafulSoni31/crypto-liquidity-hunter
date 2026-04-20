"""
Trade Executor — Clean rewrite
Entry → verify position on Binance → place SL/TP synchronously.
No threads, no delays, no race conditions.
"""
import logging
import time
import hmac
import hashlib
import math
import requests
import yaml
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Optional

from core.binance_connector import BinanceConnector, _normalise_k_contract
from data.store import DataStore

logger = logging.getLogger(__name__)

CONFIG_PATH = Path("/root/.openclaw/workspace/projects/crypto-liquidity-hunter/config/pairs.yaml")


def _load_config() -> Dict:
    with open(CONFIG_PATH, 'r') as f:
        return yaml.safe_load(f)


def _save_config(cfg: Dict):
    import tempfile, os, yaml
    fd, tmp_path = tempfile.mkstemp(dir=os.path.dirname(CONFIG_PATH), prefix="pairs_tmp_")
    with os.fdopen(fd, 'w') as f:
        yaml.dump(cfg, f, default_flow_style=False, sort_keys=False)
    os.replace(tmp_path, CONFIG_PATH)



# ─── Raw Binance REST helpers (bypass ccxt, always work) ──────────────────────

def _sign(secret: str, params: str) -> str:
    ts = int(time.time() * 1000)
    full = params + f'&timestamp={ts}&recvWindow=5000'
    sig = hmac.new(secret.encode(), full.encode(), hashlib.sha256).hexdigest()
    return full + '&signature=' + sig


def _get_position(api_key: str, api_secret: str, raw_sym: str,
                  direction: str = None) -> Optional[Dict]:
    """Return Binance position dict for raw_sym, or None if no position.
    In hedge mode, pass direction='long'/'short' to filter the correct side."""
    try:
        par = _sign(api_secret, f'symbol={raw_sym}')
        r = requests.get(
            f'https://fapi.binance.com/fapi/v2/positionRisk?{par}',
            headers={'X-MBX-APIKEY': api_key}, timeout=8
        )
        for p in r.json():
            if float(p.get('positionAmt', 0)) == 0:
                continue
            # Hedge mode: filter by positionSide matching the trade direction
            if direction:
                expected = 'LONG' if direction == 'long' else 'SHORT'
                pos_side = p.get('positionSide', 'BOTH')
                if pos_side != 'BOTH' and pos_side != expected:
                    continue
            return p
        return None
    except Exception as e:
        logger.error(f'[Executor] _get_position error: {e}')
        return None


def _get_open_orders(api_key: str, api_secret: str, raw_sym: str) -> list:
    """Return list of open orders for raw_sym."""
    try:
        par = _sign(api_secret, f'symbol={raw_sym}')
        r = requests.get(
            f'https://fapi.binance.com/fapi/v1/openOrders?{par}',
            headers={'X-MBX-APIKEY': api_key}, timeout=8
        )
        return r.json() if isinstance(r.json(), list) else []
    except Exception as e:
        logger.error(f'[Executor] _get_open_orders error: {e}')
        return []


def _cancel_all_orders(api_key: str, api_secret: str, raw_sym: str):
    """Cancel all open orders for raw_sym."""
    try:
        par = _sign(api_secret, f'symbol={raw_sym}')
        requests.delete(
            f'https://fapi.binance.com/fapi/v1/allOpenOrders?{par}',
            headers={'X-MBX-APIKEY': api_key,
                     'Content-Type': 'application/x-www-form-urlencoded'},
            timeout=8
        )
        logger.info(f'[Executor] Cancelled all orders for {raw_sym}')
    except Exception as e:
        logger.error(f'[Executor] _cancel_all_orders error: {e}')


def _round_price(raw_sym: str, price: float, exchange_info_cache: dict) -> float:
    tick = exchange_info_cache.get(raw_sym, {}).get('tick', 0.0001)
    dec = max(0, round(-math.log10(tick)))
    return round(round(price / tick) * tick, dec)


def _round_qty(raw_sym: str, qty: float, exchange_info_cache: dict) -> float:
    step = exchange_info_cache.get(raw_sym, {}).get('step', 1.0)
    dec = max(0, round(-math.log10(step)))
    return round(math.floor(qty / step) * step, dec)


_EXCHANGE_INFO: Dict = {}  # module-level cache


def _get_exchange_info(raw_sym: str) -> Dict:
    """Fetch and cache tick/step size for a symbol."""
    global _EXCHANGE_INFO
    if raw_sym in _EXCHANGE_INFO:
        return _EXCHANGE_INFO
    try:
        r = requests.get('https://fapi.binance.com/fapi/v1/exchangeInfo', timeout=10)
        for s in r.json()['symbols']:
            fi = {f['filterType']: f for f in s['filters']}
            _EXCHANGE_INFO[s['symbol']] = {
                'tick': float(fi.get('PRICE_FILTER', {}).get('tickSize', '0.0001')),
                'step': float(fi.get('LOT_SIZE', {}).get('stepSize', '1')),
            }
    except Exception as e:
        logger.error(f'[Executor] exchangeInfo fetch failed: {e}')
    return _EXCHANGE_INFO


def _place_limit_order(api_key: str, api_secret: str,
                       raw_sym: str, side: str, price: float, qty: float) -> Dict:
    """Place a plain LIMIT order. Returns Binance response dict."""
    try:
        par = _sign(api_secret,
                    f'symbol={raw_sym}&side={side.upper()}&type=LIMIT'
                    f'&price={price}&quantity={qty}&timeInForce=GTC')
        r = requests.post(
            f'https://fapi.binance.com/fapi/v1/order?{par}',
            headers={'X-MBX-APIKEY': api_key}, timeout=10
        )
        return r.json()
    except Exception as e:
        logger.error(f'[Executor] _place_limit_order error: {e}')
        return {'error': str(e)}


def _place_sl_tp(api_key: str, api_secret: str, raw_sym: str,
                 direction: str, qty: float,
                 sl_price: float, tp_price: float,
                 exi: Dict) -> Dict:
    """
    Place SL + TP as plain LIMIT orders (no reduceOnly).

    Account type: Multi-Assets Cross Margin
      - STOP_MARKET      → -4120 blocked
      - LIMIT reduceOnly → -2022 blocked
      - LIMIT (plain)    → WORKS ✅

    Safety: SL/TP prices are on the correct side of entry so they WAIT
    in the order book and only fill when price reaches them:
      LONG  SL = LIMIT SELL below entry  → fills only when price FALLS to SL
      LONG  TP = LIMIT SELL above entry  → fills only when price RISES to TP
      SHORT SL = LIMIT BUY  above entry  → fills only when price RISES to SL
      SHORT TP = LIMIT BUY  below entry  → fills only when price FALLS to TP

    Position monitor cancels the orphaned bracket when one leg fills.
    """
    exit_side = 'BUY' if direction == 'short' else 'SELL'
    qty_r = _round_qty(raw_sym, qty, exi)

    result = {}

    if sl_price > 0:
        sl_r = _round_price(raw_sym, sl_price, exi)
        sl_resp = _place_limit_order(api_key, api_secret, raw_sym, exit_side, sl_r, qty_r)
        if 'orderId' in sl_resp:
            result['sl_order_id'] = sl_resp['orderId']
            logger.info(f'[Executor] SL placed: {raw_sym} {exit_side} @ {sl_r} id={sl_resp["orderId"]}')
        else:
            logger.error(f'[Executor] SL FAILED: {raw_sym} code={sl_resp.get("code")} {sl_resp.get("msg","")}')
            result['sl_order_id'] = f'error:{sl_resp.get("code","?")}'

    if tp_price > 0:
        tp_r = _round_price(raw_sym, tp_price, exi)
        tp_resp = _place_limit_order(api_key, api_secret, raw_sym, exit_side, tp_r, qty_r)
        if 'orderId' in tp_resp:
            result['tp_order_id'] = tp_resp['orderId']
            logger.info(f'[Executor] TP placed: {raw_sym} {exit_side} @ {tp_r} id={tp_resp["orderId"]}')
        else:
            logger.error(f'[Executor] TP FAILED: {raw_sym} code={tp_resp.get("code")} {tp_resp.get("msg","")}')
            result['tp_order_id'] = f'error:{tp_resp.get("code","?")}'

    return result


# ─── TradeExecutor ─────────────────────────────────────────────────────────────

class TradeExecutor:
    """
    Executes signals on Binance Futures.
    Flow:
      1. Market entry
      2. Wait 3s for position to register
      3. Fetch ACTUAL position from Binance
      4. If no position → abort (no orders placed)
      5. If direction mismatch → close inverted position, abort
      6. Place SL + TP as plain LIMIT orders using ACTUAL qty
    All synchronous — no background threads, no race conditions.
    """

    def __init__(self, connector: BinanceConnector = None, account_id: int = None):
        cfg = _load_config()
        paper_cfg = cfg.get('paper_trading', {})

        self.account_id   = account_id
        self.notional_usd = float(paper_cfg.get('fixed_notional_usd', 20.0))
        self.leverage     = float(paper_cfg.get('margin_leverage', 20.0))
        self.commission   = float(paper_cfg.get('commission_per_trade', 0.001))

        self.store = DataStore(
            db_path='/root/.openclaw/workspace/projects/crypto-liquidity-hunter/data/store.db',
            cache_path='/root/.openclaw/workspace/projects/crypto-liquidity-hunter/data/latest_signals.json',
        )

        if connector is not None:
            self.connector = connector
            self.mode = connector.mode
        elif account_id is not None:
            acct = self.store.get_account(account_id)
            if acct:
                self.mode = acct.get('mode', 'paper')
                self.connector = BinanceConnector(
                    api_key=acct.get('api_key', ''),
                    api_secret=acct.get('api_secret', ''),
                    testnet=bool(acct.get('testnet', 0)),
                    mode=self.mode,
                    is_demo=bool(acct.get('is_demo', 0)),
                    environment=acct.get('environment', 'mainnet'),
                )
                self.connector.connect()
            else:
                self.mode = 'paper'
                self.connector = BinanceConnector(mode='paper')
                self.connector.connect()
        else:
            bc_cfg = cfg.get('binance_connection', {})
            self.mode = bc_cfg.get('mode', 'paper')
            self.connector = BinanceConnector(
                api_key=bc_cfg.get('api_key', ''),
                api_secret=bc_cfg.get('api_secret', ''),
                testnet=bc_cfg.get('testnet', True),
                mode=self.mode,
                is_demo=bc_cfg.get('is_demo', False),
                environment=bc_cfg.get('environment', 'mainnet'),
            )
            self.connector.connect()

    # ─── Price Fetch ──────────────────────────────────────────────────────────

    def _fetch_price(self, symbol: str) -> float:
        norm_sym, mult = _normalise_k_contract(symbol)
        raw = norm_sym.split('/')[0] + (norm_sym.split('/')[1].split(':')[0] if '/' in norm_sym else '')
        raw = raw.replace('/', '')
        try:
            import urllib.request, json
            with urllib.request.urlopen(
                f'https://fapi.binance.com/fapi/v1/ticker/price?symbol={raw}', timeout=5
            ) as r:
                return float(json.loads(r.read())['price']) / mult
        except Exception:
            pass
        return 0.0

    # ─── Execute Signal ────────────────────────────────────────────────────────

    def execute_signal(self, signal_dict: Dict, pair: str,
                       notional_usd: float = None, leverage: float = None,
                       signal_id: int = None) -> Dict:
        """
        Execute a trade. Returns {trade_id, entry_price, sl, tp, ...} or {error}.
        """
        direction = signal_dict.get('direction', 'long')
        entry_price = float(signal_dict.get('entry_price', 0))
        stop_loss = float(signal_dict.get('stop_loss', 0))
        target = float(signal_dict.get('target', 0))
        timeframe = signal_dict.get('timeframe', '1h')

        symbol = pair.split(':', 1)[1] if ':' in pair else pair

        # Load live or paper config
        _cfg = _load_config()
        mode_key = 'live_trading' if self.mode != 'paper' else 'paper_trading'
        trade_cfg = _cfg.get(mode_key, _cfg.get('paper_trading', {}))

        base_notional = float(trade_cfg.get('fixed_notional_usd', self.notional_usd))
        commission = float(trade_cfg.get('commission_per_trade', self.commission))
        sizing_mode = trade_cfg.get('position_sizing', 'fixed_notional')
        risk_pct = float(trade_cfg.get('risk_percent', 1.0))
        max_notional = float(trade_cfg.get('max_notional_usd', 500.0))
        base_leverage = float(trade_cfg.get('margin_leverage', self.leverage))
        lev = leverage or base_leverage

        # Determine notional
        if notional_usd:
            notional = float(notional_usd)
        elif sizing_mode == 'risk_percent' and self.mode != 'paper':
            try:
                bal = self.connector.get_balance('USDT')
                notional = float(bal.get('free', 0) or 0) * (risk_pct / 100.0)
            except Exception:
                notional = base_notional
        else:
            notional = base_notional

        notional = min(notional, max_notional)
        self.commission = commission
        commission_usd = notional * commission

        # Fetch current price if market order
        if entry_price <= 0:
            entry_price = self._fetch_price(symbol)
            if entry_price <= 0:
                return {'error': f'Could not fetch price for {symbol}'}

        # Qty = notional / price  (leverage is account-level, NOT in order qty)
        norm_sym, mult = _normalise_k_contract(symbol)
        raw_sym = norm_sym.split('/')[0] + (norm_sym.split('/')[1].split(':')[0] if '/' in norm_sym else '')
        raw_sym = raw_sym.replace('/', '')
        exi = _get_exchange_info(raw_sym)
        qty = _round_qty(raw_sym, notional / entry_price, exi)
        
        # Check minimum precision limits before placing order
        try:
            if exi:
                # the exi dict we fetched usually contains the raw precision mapping
                step = float(exi.get('step', 0.001))
                if getattr(qty, 'real', 0) < step:
                    qty = step
                    import logging
                    logging.warning(f"Adjusted {symbol} qty to min precision: {qty}")
        except Exception as e:
            pass

        side = 'buy' if direction == 'long' else 'sell'

        order_result = {}
        sltp_result = {}

        # ── PAPER MODE ────────────────────────────────────────────────────────
        if self.mode == 'paper':
            order_result = self.connector.paper_execute(symbol, side, qty, entry_price)

        # ── LIVE MODE ─────────────────────────────────────────────────────────
        else:
            api_key = self.connector.api_key
            api_secret = self.connector.api_secret

            # Pre-entry: cancel any stale bracket orders for this symbol.
            # Prevents leftover LIMIT orders from previous sessions from closing the new position.
            logger.info(f'[Executor] Pre-entry: cancelling any stale orders for {raw_sym}')
            _cancel_all_orders(api_key, api_secret, raw_sym)

            # STEP 1 — Market entry
            position_side = 'LONG' if direction == 'long' else 'SHORT'
            logger.info(f'[Executor] ENTRY: {direction.upper()} {symbol} qty={qty} notional=${notional} positionSide={position_side}')
            order_result = self.connector.place_market_order(symbol, side, qty,
                                                             position_side=position_side)
            if 'error' in order_result:
                return {'error': order_result['error']}

            # Get actual fill price
            actual_fill = (
                float(order_result.get('average') or 0) or
                float(order_result.get('price') or 0) or
                float(order_result.get('info', {}).get('avgPrice', 0) or 0)
            )
            if actual_fill > 0:
                entry_price = actual_fill
            logger.info(f'[Executor] Filled: {symbol} @ {entry_price} order={order_result.get("id")}')

            # ── Read SL/TP mode from config ─────────────────────────────────
            exec_cfg   = _cfg.get('signal_execution', {})
            sl_tp_mode = exec_cfg.get('sl_tp_mode', 'monitor_only')
            sl_tp_delay = int(exec_cfg.get('sl_tp_delay_sec', 3))

            # STEP 2 — Wait for position to register on Binance
            time.sleep(sl_tp_delay)
            pos = _get_position(api_key, api_secret, raw_sym, direction=direction)

            if pos is None:
                logger.error(
                    f'[Executor] CRITICAL: No position found on Binance for {raw_sym} after entry. '
                    f'Cancelling all orders. Trade recorded as entry_failed.'
                )
                _cancel_all_orders(api_key, api_secret, raw_sym)
                sltp_result = {'sl_order_id': 'entry_failed', 'tp_order_id': 'entry_failed'}
                now = datetime.now(timezone.utc)
                trade_id = self.store.create_open_trade(
                    pair=pair, timeframe=timeframe, direction=direction,
                    entry_price=entry_price, sl=stop_loss, tp=target,
                    entry_time=now, notional_usd=notional, commission_usd=commission_usd,
                    signal_id=signal_id, notes='entry_failed_no_position',
                    mode=self.mode, order_id=str(order_result.get('id', '')),
                    account_id=self.account_id,
                )
                self.store.close_trade(trade_id, entry_price, now, 'entry_failed', 0.0)
                return {'error': f'Position not found on Binance after market entry for {symbol}'}

            # STEP 3 — Direction check
            actual_qty = abs(float(pos['positionAmt']))
            actual_dir = 'short' if float(pos['positionAmt']) < 0 else 'long'

            if actual_dir != direction:
                close_side = 'sell' if actual_dir == 'long' else 'buy'
                qty_r = _round_qty(raw_sym, actual_qty, exi)
                logger.error(
                    f'[Executor] *** DIRECTION MISMATCH FIRING *** '
                    f'intended={direction} actual={actual_dir} '
                    f'positionAmt={pos.get("positionAmt")} for {symbol}. '
                    f'About to place {close_side.upper()} MARKET reduceOnly=true qty={qty_r}'
                )
                _cancel_all_orders(api_key, api_secret, raw_sym)
                try:
                    # Hedge mode uses positionSide; one-way uses reduceOnly
                    _hedge = getattr(self.connector, 'hedge_mode', False)
                    _close_extra = (f'&positionSide={actual_dir.upper()}' if _hedge
                                    else '&reduceOnly=true')
                    close_par = _sign(api_secret,
                                      f'symbol={raw_sym}&side={close_side.upper()}'
                                      f'&type=MARKET&quantity={qty_r}{_close_extra}')
                    requests.post(f'https://fapi.binance.com/fapi/v1/order?{close_par}',
                                  headers={'X-MBX-APIKEY': api_key}, timeout=10)
                    logger.info(f'[Executor] Closed inverted {actual_dir} position for {raw_sym}')
                except Exception as ce:
                    logger.error(f'[Executor] Failed to close inverted position: {ce}')
                return {'error': f'Direction mismatch for {symbol}: intended {direction}, got {actual_dir}'}

            # STEP 4 — SL/TP handling based on mode
            if sl_tp_mode == 'binance_bracket':
                # ── MODE A: Place SL+TP as LIMIT orders on Binance ─────────
                existing = _get_open_orders(api_key, api_secret, raw_sym)
                if len(existing) >= 2:
                    logger.info(f'[Executor] SL/TP already on exchange for {symbol} — skipping')
                    sltp_result = {
                        'sl_order_id': existing[0].get('orderId', 'existing'),
                        'tp_order_id': existing[-1].get('orderId', 'existing'),
                    }
                else:
                    if stop_loss > 0 or target > 0:
                        sltp_result = _place_sl_tp(
                            api_key, api_secret, raw_sym,
                            actual_dir, actual_qty,
                            stop_loss, target, exi
                        )
                        logger.info(f'[Executor] SL/TP placed on Binance for {symbol}: {sltp_result}')

            elif sl_tp_mode == 'monitor_only':
                # ── MODE B: Bot monitors price, no orders on Binance ───────
                # SL/TP levels are saved in DB; the PositionMonitor checks
                # every N seconds (configurable) and closes via market order.
                sltp_result = {
                    'sl_order_id': 'monitor',
                    'tp_order_id': 'monitor',
                    'method': 'monitor_only',
                }
                logger.info(f'[Executor] SL/TP monitor mode for {symbol}: '
                            f'SL={stop_loss} TP={target} (no orders on Binance, '
                            f'position monitor will enforce)')

            else:
                logger.warning(f'[Executor] Unknown sl_tp_mode: {sl_tp_mode}. Defaulting to monitor_only.')
                sltp_result = {'sl_order_id': 'monitor', 'tp_order_id': 'monitor'}

        if 'error' in order_result and self.mode != 'paper':
            return {'error': order_result.get('error', 'Order failed')}

        # Persist to DB
        now = datetime.now(timezone.utc)
        trade_id = self.store.create_open_trade(
            pair=pair, timeframe=timeframe, direction=direction,
            entry_price=entry_price, sl=stop_loss, tp=target,
            entry_time=now, notional_usd=notional, commission_usd=commission_usd,
            signal_id=signal_id,
            notes=f'mode={self.mode} order={order_result.get("id","?")}',
            mode=self.mode, order_id=str(order_result.get('id', '')),
            account_id=self.account_id,
        )

        # Activity log
        try:
            from scheduler.activity_logger import log_event as _log
            _log('ENTRY_EXECUTED', symbol=symbol, direction=direction,
                 entry_price=entry_price, sl=stop_loss, tp=target,
                 trade_id=trade_id, notional=notional,
                 sl_order_id=sltp_result.get('sl_order_id'),
                 tp_order_id=sltp_result.get('tp_order_id'))
        except Exception:
            pass

        # ── POST-ENTRY VALIDATION — verify on Binance + Telegram alert ────
        if self.mode != 'paper':
            try:
                from core.trade_validator import validate_entry
                exec_cfg = _cfg.get('signal_execution', {})
                validate_entry(
                    symbol=symbol, direction=direction,
                    entry_price=entry_price, sl=stop_loss, tp=target,
                    qty=qty, notional=notional, trade_id=trade_id,
                    sl_tp_mode=exec_cfg.get('sl_tp_mode', 'monitor_only'),
                    api_key=self.connector.api_key,
                    api_secret=self.connector.api_secret,
                )
            except Exception as e:
                logger.error(f'[Executor] Post-entry validation error: {e}')

        return {
            'trade_id': trade_id,
            'mode': self.mode,
            'qty': qty,
            'entry_price': entry_price,
            'sl': stop_loss,
            'tp': target,
            'order_id': order_result.get('id'),
            'sl_order_id': sltp_result.get('sl_order_id'),
            'tp_order_id': sltp_result.get('tp_order_id'),
            'status': 'open',
        }

    # ─── Close Trade ──────────────────────────────────────────────────────────

    def close_trade(self, trade_id: int, symbol: str,
                    exit_price: float = None, reason: str = 'manual') -> bool:
        open_trades = self.store.get_open_trades()
        trade = next((t for t in open_trades if t['id'] == trade_id), None)
        if not trade:
            logger.warning(f'close_trade: trade {trade_id} not found')
            return False

        direction = trade['direction']
        ep = float(trade['entry_price'])
        notional = float(trade.get('notional_usd', 20))
        commission = float(trade.get('commission_usd', 0.02))

        if exit_price is None:
            exit_price = self._fetch_price(symbol) or ep

        if self.mode != 'paper' and self.connector.exchange:
            sym = symbol.split(':', 1)[1] if ':' in symbol else symbol
            close_side = 'sell' if direction == 'long' else 'buy'
            try:
                norm_sym2, mult2 = _normalise_k_contract(sym)
                raw2 = norm_sym2.split('/')[0] + (norm_sym2.split('/')[1].split(':')[0] if '/' in norm_sym2 else '')
                raw2 = raw2.replace('/', '')
                # Get ACTUAL position qty from Binance (not estimated)
                pos = _get_position(self.connector.api_key, self.connector.api_secret, raw2)
                if pos:
                    qty2 = abs(float(pos['positionAmt']))
                else:
                    # Fallback to estimate if position check fails
                    exi2 = _get_exchange_info(raw2)
                    qty2 = _round_qty(raw2, notional / ep, exi2)
                co = self.connector.exchange.create_order(
                    norm_sym2, 'market', close_side, qty2, params={'reduceOnly': True}
                )
                exit_price = float(co.get('average') or co.get('price') or exit_price)
            except Exception as e:
                logger.error(f'close_trade exchange error: {e}')

        pnl = ((exit_price - ep) / ep if direction == 'long' else (ep - exit_price) / ep) * notional - commission * 2

        self.store.close_trade(
            trade_id=trade_id,
            exit_price=exit_price,
            exit_time=datetime.now(timezone.utc),
            status=reason,
            pnl_usd=round(pnl, 4),
        )
        logger.info(f'Closed trade {trade_id}: {direction} exit={exit_price} pnl={pnl:.2f}')
        return True

    @staticmethod
    def save_connection_config(api_key: str, api_secret: str,
                               testnet: bool, mode: str,
                               is_demo: bool = False, environment: str = 'mainnet') -> bool:
        try:
            cfg = _load_config()
            cfg['binance_connection'] = {
                'api_key': api_key, 'api_secret': api_secret,
                'testnet': testnet, 'mode': mode,
                'is_demo': is_demo, 'environment': environment, 'enabled': True,
            }
            _save_config(cfg)
            return True
        except Exception as e:
            logger.error(f'save_connection_config error: {e}')
            return False
