"""
Binance Connector — Phase 3A
Wraps ccxt for Binance Spot + Futures (Paper / Testnet / Live).
Zero changes to existing strategy logic.
"""
import ccxt
import logging
import math
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)


class BinanceConnector:
    """
    Thin ccxt wrapper for Binance Futures USDT-M (perpetuals).
    Supports:
      - paper mode  → simulated fills, no API calls needed
      - testnet     → real API calls to Binance Futures Testnet
      - live        → real mainnet Binance Futures
    """

    def __init__(self, api_key: str = '', api_secret: str = '',
                 testnet: bool = True, mode: str = 'paper'):
        """
        mode: 'paper' | 'testnet' | 'live'
        testnet flag only used when mode != 'paper'.
        """
        self.api_key      = api_key
        self.api_secret   = api_secret
        self.testnet      = testnet
        self.mode         = mode  # paper / testnet / live
        self.exchange     = None
        self.markets      = {}
        self._connected   = False
        self.last_error   = None   # human-readable last error
        self.account_type = None   # 'paper' / 'spot' / 'futures'

    # ─── Connection ────────────────────────────────────────────────────────────

    def connect(self) -> bool:
        """Initialise ccxt exchange and test connectivity. Returns True if OK."""
        self.last_error = None
        try:
            if self.mode == 'paper':
                # Paper mode — no real API calls, instant
                self.markets = {}
                self._connected = True
                self.account_type = 'paper'
                logger.info("BinanceConnector: paper mode — no real API calls")
                return True

            creds = {
                'apiKey': self.api_key,
                'secret': self.api_secret,
                'enableRateLimit': True,
            }

            # Try Futures (USDM) first, fall back to Spot if Futures not enabled
            tried_futures = False
            tried_spot    = False

            # Step 1: try Futures USDM
            try:
                tried_futures = True
                ex = ccxt.binanceusdm({**creds, 'options': {'defaultType': 'future'}})
                if self.testnet:
                    ex.set_sandbox_mode(True)
                ex.fetch_balance()
                self.exchange    = ex
                self.account_type = 'futures'
                self.markets     = {}  # lazy-load
                self._connected  = True
                env = 'TESTNET' if self.testnet else 'MAINNET'
                logger.info(f"BinanceConnector: connected [FUTURES {env}]")
                return True
            except ccxt.AuthenticationError as e:
                err_str = str(e)
                # Code -2015 = invalid permissions (Spot key used on Futures)
                # Code -2008 = invalid API key entirely
                if '-2015' in err_str or 'futures' in err_str.lower():
                    logger.info("Futures API not enabled, trying Spot fallback...")
                elif '-2008' in err_str or 'Invalid Api-Key' in err_str:
                    self.last_error = f"Invalid API Key — double-check the key is correct. ({err_str.split('msg')[1][:60] if 'msg' in err_str else err_str[:80]})"
                    self._connected = False
                    return False
                else:
                    # Unknown auth error — still try spot
                    logger.warning(f"Futures auth error: {err_str[:100]}")
            except Exception as e:
                logger.warning(f"Futures connect error: {e}")

            # Step 2: fall back to Spot
            try:
                tried_spot = True
                ex = ccxt.binance({**creds})
                if self.testnet:
                    ex.set_sandbox_mode(True)
                ex.fetch_balance()
                self.exchange     = ex
                self.account_type = 'spot'
                self.markets      = {}
                self._connected   = True
                env = 'TESTNET' if self.testnet else 'MAINNET'
                logger.info(f"BinanceConnector: connected [SPOT {env}]")
                return True
            except ccxt.AuthenticationError as e:
                err_str = str(e)
                if '-2008' in err_str or 'Invalid Api-Key' in err_str:
                    self.last_error = "Invalid API Key ID — key may be deleted or wrong. Check Binance API Management."
                elif '-2015' in err_str:
                    self.last_error = "API key permissions insufficient. Enable 'Enable Reading' in Binance API Management."
                else:
                    self.last_error = f"Authentication failed: {err_str[:120]}"
                self._connected = False
                return False
            except Exception as e:
                self.last_error = f"Connection error: {str(e)[:120]}"
                self._connected = False
                return False

        except Exception as e:
            self.last_error = str(e)[:200]
            logger.error(f"BinanceConnector connect error: {e}")
            self._connected = False
            return False

    # ─── Account ───────────────────────────────────────────────────────────────

    def get_balance(self, currency: str = 'USDT') -> Dict:
        """Returns {free, used, total, account_type} for given currency."""
        if self.mode == 'paper':
            return {'free': 10000.0, 'used': 0.0, 'total': 10000.0,
                    'currency': currency, 'account_type': 'paper'}
        try:
            bal = self.exchange.fetch_balance()
            cur = bal.get(currency, {})
            # For futures, total equity may be in 'info'
            if self.account_type == 'futures':
                info = bal.get('info', {})
                assets = info.get('assets', [])
                for a in assets:
                    if a.get('asset') == currency:
                        return {
                            'free':         float(a.get('availableBalance', 0) or 0),
                            'used':         float(a.get('initialMargin',    0) or 0),
                            'total':        float(a.get('walletBalance',    0) or 0),
                            'unrealized_pnl': float(a.get('unrealizedProfit', 0) or 0),
                            'currency':     currency,
                            'account_type': 'futures',
                        }
            return {
                'free':         float(cur.get('free',  0) or 0),
                'used':         float(cur.get('used',  0) or 0),
                'total':        float(cur.get('total', 0) or 0),
                'currency':     currency,
                'account_type': self.account_type or 'spot',
            }
        except Exception as e:
            logger.error(f"get_balance error: {e}")
            return {'free': 0.0, 'used': 0.0, 'total': 0.0,
                    'currency': currency, 'account_type': self.account_type}

    def get_positions(self) -> List[Dict]:
        """Return open futures positions."""
        if self.mode == 'paper':
            return []
        try:
            positions = self.exchange.fetch_positions()
            open_pos = []
            for p in positions:
                contracts = float(p.get('contracts') or p.get('contractSize') or 0)
                if contracts != 0:
                    open_pos.append({
                        'symbol':        p.get('symbol'),
                        'side':          p.get('side'),
                        'contracts':     contracts,
                        'entry_price':   float(p.get('entryPrice') or 0),
                        'mark_price':    float(p.get('markPrice') or 0),
                        'unrealized_pnl':float(p.get('unrealizedPnl') or 0),
                        'leverage':      float(p.get('leverage') or 1),
                        'notional':      float(p.get('notional') or 0),
                        'margin':        float(p.get('initialMargin') or 0),
                    })
            return open_pos
        except Exception as e:
            logger.error(f"get_positions error: {e}")
            return []

    def get_open_orders(self, symbol: str = None) -> List[Dict]:
        """Return open orders for a symbol (or all if None)."""
        if self.mode == 'paper':
            return []
        try:
            if symbol:
                orders = self.exchange.fetch_open_orders(symbol)
            else:
                orders = self.exchange.fetch_open_orders()
            return [{
                'id':     o['id'],
                'symbol': o['symbol'],
                'side':   o['side'],
                'type':   o['type'],
                'price':  o.get('price'),
                'amount': o.get('amount'),
                'status': o.get('status'),
            } for o in orders]
        except Exception as e:
            logger.error(f"get_open_orders error: {e}")
            return []

    # ─── Lot Size / Precision ─────────────────────────────────────────────────

    # Hardcoded sensible defaults for common symbols (used in paper mode to avoid API calls)
    _KNOWN_LOTS = {
        'BTC/USDT':  {'min_qty':0.001,  'step_size':0.001,  'tick_size':0.1,     'min_notional':100.0, 'precision_qty':3, 'precision_price':1},
        'ETH/USDT':  {'min_qty':0.001,  'step_size':0.001,  'tick_size':0.01,    'min_notional':5.0,   'precision_qty':3, 'precision_price':2},
        'SOL/USDT':  {'min_qty':0.01,   'step_size':0.01,   'tick_size':0.001,   'min_notional':5.0,   'precision_qty':2, 'precision_price':3},
        'BNB/USDT':  {'min_qty':0.01,   'step_size':0.01,   'tick_size':0.001,   'min_notional':5.0,   'precision_qty':2, 'precision_price':3},
        'XRP/USDT':  {'min_qty':1.0,    'step_size':1.0,    'tick_size':0.0001,  'min_notional':5.0,   'precision_qty':0, 'precision_price':4},
        'DOGE/USDT': {'min_qty':1.0,    'step_size':1.0,    'tick_size':0.00001, 'min_notional':5.0,   'precision_qty':0, 'precision_price':5},
    }

    def get_lot_info(self, symbol: str) -> Dict:
        """
        Return lot-size / precision info for a symbol.
        {min_qty, step_size, tick_size, min_notional, precision_qty, precision_price}
        """
        defaults = {
            'min_qty': 1.0,
            'step_size': 1.0,
            'tick_size': 0.0001,
            'min_notional': 5.0,
            'precision_qty': 0,
            'precision_price': 4,
        }
        # In paper mode, use hardcoded table first to avoid API calls
        sym_clean = symbol.replace(':USDT', '/USDT').upper()
        if self.mode == 'paper' and sym_clean in self._KNOWN_LOTS:
            return dict(self._KNOWN_LOTS[sym_clean])

        try:
            if not self.markets and self.mode != 'paper':
                try:
                    self.markets = self.exchange.load_markets()
                except Exception:
                    return self._KNOWN_LOTS.get(sym_clean, defaults)
            # Normalise symbol (e.g. BTC/USDT → BTC/USDT:USDT for futures)
            mkt = self.markets.get(symbol) or self.markets.get(symbol + ':USDT')
            if not mkt:
                return defaults
            limits = mkt.get('limits', {})
            precision = mkt.get('precision', {})
            amount_p = precision.get('amount', 3)
            price_p  = precision.get('price', 1)
            step_size = 10 ** (-amount_p) if isinstance(amount_p, int) else 0.001
            tick_size = 10 ** (-price_p)  if isinstance(price_p,  int) else 0.1
            min_qty   = float(limits.get('amount', {}).get('min', step_size) or step_size)
            min_notional = float(limits.get('cost', {}).get('min', 5.0) or 5.0)
            return {
                'min_qty':        min_qty,
                'step_size':      step_size,
                'tick_size':      tick_size,
                'min_notional':   min_notional,
                'precision_qty':  amount_p if isinstance(amount_p, int) else 3,
                'precision_price':price_p  if isinstance(price_p,  int) else 1,
            }
        except Exception as e:
            logger.error(f"get_lot_info error for {symbol}: {e}")
            return defaults

    def calc_qty(self, symbol: str, notional_usd: float, price: float, leverage: float = 1.0) -> float:
        """
        Calculate order quantity (base asset units) from notional USDT.
        Rounds DOWN to step_size.
        qty = (notional * leverage) / price
        """
        if price <= 0:
            return 0.0
        lot = self.get_lot_info(symbol)
        raw_qty = (notional_usd * leverage) / price
        step    = lot['step_size']
        # Floor to step
        qty = math.floor(raw_qty / step) * step
        qty = round(qty, lot['precision_qty'])
        return max(qty, lot['min_qty'])

    def _round_qty(self, symbol: str, qty: float) -> float:
        """Round qty to exchange precision."""
        if self.exchange and symbol in (self.markets or {}):
            try:
                return float(self.exchange.amount_to_precision(symbol, qty))
            except Exception:
                pass
        lot = self.get_lot_info(symbol)
        step = lot['step_size']
        return round(math.floor(qty / step) * step, lot['precision_qty'])

    def _round_price(self, symbol: str, price: float) -> float:
        """Round price to exchange tick precision."""
        if self.exchange and symbol in (self.markets or {}):
            try:
                return float(self.exchange.price_to_precision(symbol, price))
            except Exception:
                pass
        lot = self.get_lot_info(symbol)
        tick = lot['tick_size']
        return round(round(price / tick) * tick, lot['precision_price'])

    # ─── Order Placement ──────────────────────────────────────────────────────

    def paper_execute(self, symbol: str, side: str, qty: float, price: float) -> Dict:
        """Simulate a fill. Returns a fake order dict."""
        import uuid
        return {
            'id':        f"paper_{uuid.uuid4().hex[:8]}",
            'symbol':    symbol,
            'side':      side,
            'type':      'market',
            'status':    'closed',
            'price':     price,
            'amount':    qty,
            'filled':    qty,
            'cost':      qty * price,
            'fee':       {'cost': qty * price * 0.0004, 'currency': 'USDT'},
            'mode':      'paper',
        }

    def place_market_order(self, symbol: str, side: str, qty: float) -> Dict:
        """Place a market order. In paper mode returns simulated fill."""
        if self.mode == 'paper':
            # Use last price from exchange as fill price if available
            try:
                ticker = self.exchange.fetch_ticker(symbol)
                price  = float(ticker['last'])
            except Exception:
                price = 0.0
            return self.paper_execute(symbol, side, qty, price)
        try:
            qty_r = self._round_qty(symbol, qty)
            order = self.exchange.create_order(
                symbol=symbol, type='market', side=side, amount=qty_r,
                params={'reduceOnly': False}
            )
            logger.info(f"Market order: {side} {qty_r} {symbol} → {order['id']}")
            return order
        except Exception as e:
            logger.error(f"place_market_order error: {e}")
            return {'error': str(e)}

    def place_limit_order(self, symbol: str, side: str, qty: float, price: float) -> Dict:
        """Place a limit order."""
        if self.mode == 'paper':
            return self.paper_execute(symbol, side, qty, price)
        try:
            qty_r   = self._round_qty(symbol, qty)
            price_r = self._round_price(symbol, price)
            order   = self.exchange.create_order(
                symbol=symbol, type='limit', side=side,
                amount=qty_r, price=price_r
            )
            logger.info(f"Limit order: {side} {qty_r}@{price_r} {symbol} → {order['id']}")
            return order
        except Exception as e:
            logger.error(f"place_limit_order error: {e}")
            return {'error': str(e)}

    def set_sl_tp(self, symbol: str, side: str, qty: float,
                  sl_price: float, tp_price: float) -> Dict:
        """
        After entry fills, place stop-loss + take-profit orders.
        side = position side ('long' or 'short') — exit is opposite.
        Returns {sl_order_id, tp_order_id} or paper equivalents.
        """
        exit_side = 'sell' if side == 'long' else 'buy'
        if self.mode == 'paper':
            return {
                'sl_order_id': f'paper_sl_{symbol}',
                'tp_order_id': f'paper_tp_{symbol}',
                'sl_price': sl_price,
                'tp_price': tp_price,
                'mode': 'paper',
            }
        try:
            qty_r  = self._round_qty(symbol, qty)
            sl_r   = self._round_price(symbol, sl_price)
            tp_r   = self._round_price(symbol, tp_price)
            sl_order = self.exchange.create_order(
                symbol=symbol, type='stop_market', side=exit_side, amount=qty_r,
                params={'stopPrice': sl_r, 'reduceOnly': True, 'closePosition': False}
            )
            tp_order = self.exchange.create_order(
                symbol=symbol, type='take_profit_market', side=exit_side, amount=qty_r,
                params={'stopPrice': tp_r, 'reduceOnly': True, 'closePosition': False}
            )
            logger.info(f"SL/TP set for {symbol}: SL={sl_r} TP={tp_r}")
            return {
                'sl_order_id': sl_order['id'],
                'tp_order_id': tp_order['id'],
                'sl_price': sl_r,
                'tp_price': tp_r,
            }
        except Exception as e:
            logger.error(f"set_sl_tp error: {e}")
            return {'error': str(e)}

    def cancel_order(self, order_id: str, symbol: str) -> bool:
        """Cancel an open order. Returns True on success."""
        if self.mode == 'paper':
            return True
        try:
            self.exchange.cancel_order(order_id, symbol)
            logger.info(f"Cancelled order {order_id} for {symbol}")
            return True
        except Exception as e:
            logger.error(f"cancel_order error: {e}")
            return False

    def place_order(self, symbol: str, side: str, order_type: str,
                    qty: float, price: float = None, params: Dict = None) -> Dict:
        """Generic order placement."""
        params = params or {}
        if order_type == 'market':
            return self.place_market_order(symbol, side, qty)
        elif order_type == 'limit':
            return self.place_limit_order(symbol, side, qty, price or 0)
        else:
            if self.mode == 'paper':
                return self.paper_execute(symbol, side, qty, price or 0)
            try:
                qty_r   = self._round_qty(symbol, qty)
                price_r = self._round_price(symbol, price) if price else None
                order   = self.exchange.create_order(
                    symbol=symbol, type=order_type, side=side,
                    amount=qty_r, price=price_r, params=params
                )
                return order
            except Exception as e:
                logger.error(f"place_order error: {e}")
                return {'error': str(e)}

    @property
    def connected(self) -> bool:
        return self._connected
