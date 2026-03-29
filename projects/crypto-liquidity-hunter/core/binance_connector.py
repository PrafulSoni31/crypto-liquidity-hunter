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
      - demo        → Binance Demo Trading (demo-fapi.binance.com)
      - testnet     → real API calls to Binance Futures Testnet
      - live        → real mainnet Binance Futures
    """

    # Binance Demo Trading base URL (paper trading with real API keys)
    DEMO_BASE_URL = 'https://demo-fapi.binance.com'
    TESTNET_BASE_URL = 'https://testnet.binancefuture.com'

    def __init__(self, api_key: str = '', api_secret: str = '',
                 testnet: bool = True, mode: str = 'paper',
                 is_demo: bool = False, environment: str = 'mainnet'):
        """
        mode: 'paper' | 'demo' | 'testnet' | 'live'
        is_demo: True if using Binance Demo Trading (demo-fapi.binance.com)
        environment: 'mainnet' | 'testnet' | 'demo'
        testnet flag only used when mode != 'paper'.
        """
        self.api_key      = api_key
        self.api_secret   = api_secret
        self.testnet      = testnet
        self.mode         = mode  # paper / demo / testnet / live
        self.is_demo      = is_demo or (environment == 'demo') or (mode == 'demo')
        self.environment  = environment
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

            # ── Demo Trading mode (Binance Demo — demo-fapi.binance.com) ──────
            if self.is_demo:
                try:
                    ex = ccxt.binanceusdm({
                        **creds,
                        'options': {'defaultType': 'future'},
                        'urls': {
                            'api': {
                                'public':  self.DEMO_BASE_URL,
                                'private': self.DEMO_BASE_URL,
                                'fapiPublic':  f'{self.DEMO_BASE_URL}/fapi/v1',
                                'fapiPrivate': f'{self.DEMO_BASE_URL}/fapi/v1',
                                'fapiPrivateV2': f'{self.DEMO_BASE_URL}/fapi/v2',
                            }
                        }
                    })
                    ex.fetch_balance()
                    self.exchange    = ex
                    self.account_type = 'futures'
                    self._connected  = True
                    logger.info("BinanceConnector: connected [DEMO TRADING]")
                    return True
                except ccxt.AuthenticationError as e:
                    err_str = str(e)
                    self.last_error = self._parse_auth_error(err_str, hint='demo')
                    self._connected = False
                    return False
                except Exception as e:
                    self.last_error = f"Demo connection error: {str(e)[:150]}"
                    self._connected = False
                    return False

            # ── Testnet mode ──────────────────────────────────────────────────
            if self.testnet or self.mode == 'testnet':
                try:
                    ex = ccxt.binanceusdm({
                        **creds,
                        'options': {'defaultType': 'future'},
                        'urls': {
                            'api': {
                                'fapiPublic':  f'{self.TESTNET_BASE_URL}/fapi/v1',
                                'fapiPrivate': f'{self.TESTNET_BASE_URL}/fapi/v1',
                                'fapiPrivateV2': f'{self.TESTNET_BASE_URL}/fapi/v2',
                            }
                        }
                    })
                    ex.fetch_balance()
                    self.exchange    = ex
                    self.account_type = 'futures'
                    self._connected  = True
                    logger.info("BinanceConnector: connected [TESTNET FUTURES]")
                    return True
                except ccxt.AuthenticationError as e:
                    err_str = str(e)
                    self.last_error = self._parse_auth_error(err_str, hint='testnet')
                    self._connected = False
                    return False
                except Exception as e:
                    self.last_error = f"Testnet connection error: {str(e)[:150]}"
                    self._connected = False
                    return False

            # ── Live mode — Try Futures first, fall back to Spot ──────────────
            # Step 1: try Futures USDM (force mainnet URLs — never testnet)
            MAINNET_FAPI = 'https://fapi.binance.com'
            try:
                ex = ccxt.binanceusdm({
                    **creds,
                    'options': {'defaultType': 'future', 'adjustForTimeDifference': True},
                })
                # Force mainnet URLs AFTER construction (ccxt merges deep; post-set overrides)
                ex.urls['api']['fapiPublic']    = f'{MAINNET_FAPI}/fapi/v1'
                ex.urls['api']['fapiPublicV2']  = f'{MAINNET_FAPI}/fapi/v2'
                ex.urls['api']['fapiPublicV3']  = f'{MAINNET_FAPI}/fapi/v3'
                ex.urls['api']['fapiPrivate']   = f'{MAINNET_FAPI}/fapi/v1'
                ex.urls['api']['fapiPrivateV2'] = f'{MAINNET_FAPI}/fapi/v2'
                ex.urls['api']['fapiPrivateV3'] = f'{MAINNET_FAPI}/fapi/v3'
                ex.fetch_balance()
                self.exchange    = ex
                self.account_type = 'futures'
                self.markets     = {}
                self._connected  = True
                logger.info("BinanceConnector: connected [LIVE FUTURES MAINNET]")
                return True
            except ccxt.AuthenticationError as e:
                err_str = str(e)
                if '-2015' in err_str:
                    logger.info("Futures permission error, trying Spot fallback...")
                elif '-2008' in err_str or 'Invalid Api-Key' in err_str:
                    self.last_error = self._parse_auth_error(err_str, hint='live')
                    self._connected = False
                    return False
                else:
                    logger.warning(f"Futures auth error: {err_str[:100]}")
            except Exception as e:
                logger.warning(f"Futures connect error: {e}")

            # Step 2: fall back to Spot
            try:
                ex = ccxt.binance({**creds})
                ex.fetch_balance()
                self.exchange     = ex
                self.account_type = 'spot'
                self.markets      = {}
                self._connected   = True
                logger.info("BinanceConnector: connected [LIVE SPOT]")
                return True
            except ccxt.AuthenticationError as e:
                err_str = str(e)
                self.last_error = self._parse_auth_error(err_str, hint='live')
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

    def _parse_auth_error(self, err_str: str, hint: str = 'live') -> str:
        """Convert raw Binance error codes into actionable human-readable messages."""
        if '-2008' in err_str or 'Invalid Api-Key' in err_str:
            return ("Invalid API Key — the key may be wrong, expired, or deleted. "
                    "Check Binance → API Management.")
        if '-2015' in err_str:
            # Most common cause on a cloud server = IP whitelist restriction
            return ("IP not whitelisted — your Binance Demo API key has IP restrictions set. "
                    "Fix: In Binance Demo Trading → API Management → Edit key → "
                    "under 'Access Restrictions' select 'Unrestricted (Less Secure)' OR "
                    "add this server's IP: 76.13.247.112 to the whitelist. "
                    "Then click Save and wait 1-2 minutes.")
        if '-1021' in err_str or 'Timestamp' in err_str:
            return "Timestamp error — server clock is out of sync. Check system time."
        if 'IP' in err_str:
            return ("IP not whitelisted — your API key has IP restrictions. "
                    "Add this server's IP to the whitelist in Binance API Management, "
                    "or remove IP restrictions entirely for the bot key.")
        return f"Authentication failed: {err_str[:150]}"

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

    def check_trade_permissions(self) -> Dict:
        """
        Check if the API key has trading enabled.
        Returns {'can_trade': bool, 'can_futures': bool, 'reason': str}
        """
        if self.mode == 'paper':
            return {'can_trade': True, 'can_futures': False, 'reason': 'paper'}
        try:
            import time, hashlib, hmac, requests as _req
            ts  = int(time.time() * 1000)
            par = f"timestamp={ts}"
            sig = hmac.new(self.api_secret.encode(), par.encode(), hashlib.sha256).hexdigest()
            r = _req.get(
                f"https://api.binance.com/sapi/v1/account/apiRestrictions?{par}&signature={sig}",
                headers={"X-MBX-APIKEY": self.api_key}, timeout=5
            )
            d = r.json()
            if 'code' in d:
                return {'can_trade': False, 'can_futures': False, 'reason': str(d)}
            can_futures = bool(d.get('enableFutures', False))
            can_spot    = bool(d.get('enableSpotAndMarginTrading', False))
            can_trade   = can_futures or can_spot
            reason = []
            if not can_futures: reason.append('Futures trading NOT enabled')
            if not can_spot:    reason.append('Spot trading NOT enabled')
            return {
                'can_trade':   can_trade,
                'can_futures': can_futures,
                'can_spot':    can_spot,
                'reason':      ' | '.join(reason) if reason else 'OK',
                'raw':         d,
            }
        except Exception as e:
            return {'can_trade': False, 'can_futures': False, 'reason': str(e)}

    def place_market_order(self, symbol: str, side: str, qty: float) -> Dict:
        """Place a market order. In paper mode returns simulated fill."""
        if self.mode == 'paper':
            try:
                ticker = self.exchange.fetch_ticker(symbol)
                price  = float(ticker['last'])
            except Exception:
                price = 0.0
            return self.paper_execute(symbol, side, qty, price)
        try:
            qty_r = self._round_qty(symbol, qty)
            order = self.exchange.create_order(
                symbol=symbol, type='market', side=side, amount=qty_r
            )
            logger.info(f"Market order: {side} {qty_r} {symbol} → {order['id']}")
            return order
        except Exception as e:
            err = str(e)
            if '-2015' in err:
                return {'error': (
                    "Trading NOT enabled on this API key. "
                    "Go to Binance → API Management → Edit key → "
                    "check 'Enable Futures' (and/or 'Enable Spot & Margin Trading'). "
                    "Save and wait 30 seconds."
                )}
            logger.error(f"place_market_order error: {e}")
            return {'error': err}

    def place_entry_with_sl(self, symbol: str, direction: str, qty: float,
                            sl_price: float, tp_price: float = 0) -> Dict:
        """
        Place entry MARKET order + SL immediately via Binance batchOrders.
        This is atomic — SL is placed in the same API call as entry,
        protecting against sudden spikes before a separate set_sl_tp call.

        For accounts where STOP_MARKET is blocked (-4120), falls back to:
          entry MARKET → SL as price-watch (monitor) → TP as LIMIT

        Returns: {entry_order, sl_order_id, tp_order_id, entry_price, sl_price, tp_price, error?}
        """
        import json as _json
        import time as _time
        import hmac as _hmac
        import hashlib as _hashlib
        import requests as _req

        if self.mode == 'paper':
            try:
                ticker = self.exchange.fetch_ticker(symbol)
                price  = float(ticker['last'])
            except Exception:
                price = sl_price * (1.02 if direction == 'long' else 0.98)
            paper_r = self.paper_execute(symbol, 'buy' if direction == 'long' else 'sell', qty, price)
            return {
                'entry_order':  paper_r,
                'entry_price':  price,
                'sl_order_id':  f'paper_sl_{symbol}',
                'tp_order_id':  f'paper_tp_{symbol}' if tp_price else None,
                'sl_price':     sl_price,
                'tp_price':     tp_price,
                'mode':         'paper',
            }

        # Normalise symbol to Binance format
        raw_sym   = symbol.split(':')[0].replace('/', '')   # BTC/USDT:USDT → BTCUSDT
        exit_side = 'BUY' if direction == 'short' else 'SELL'
        entry_side = 'BUY' if direction == 'long' else 'SELL'

        qty_r  = self._round_qty(symbol, qty)
        sl_r   = self._round_price(symbol, sl_price)
        tp_r   = self._round_price(symbol, tp_price) if tp_price else 0

        api_key = self.api_key
        secret  = self.api_secret

        def _sign(params: str) -> str:
            ts = int(_time.time() * 1000)
            p  = params + f'&timestamp={ts}'
            sig = _hmac.new(secret.encode(), p.encode(), _hashlib.sha256).hexdigest()
            return p + '&signature=' + sig

        headers = {'X-MBX-APIKEY': api_key, 'Content-Type': 'application/x-www-form-urlencoded'}

        # ── Try batchOrders: entry + SL in one shot ────────────────────────
        batch_orders = [
            {
                'symbol':    raw_sym,
                'side':      entry_side,
                'type':      'MARKET',
                'quantity':  str(qty_r),
            }
        ]
        # Add SL stop order if stop_market is supported
        sl_batch = {
            'symbol':       raw_sym,
            'side':         exit_side,
            'type':         'STOP_MARKET',
            'quantity':     str(qty_r),
            'stopPrice':    str(sl_r),
            'reduceOnly':   'true',
            'closePosition':'false',
        }
        batch_orders.append(sl_batch)
        if tp_r > 0:
            batch_orders.append({
                'symbol':       raw_sym,
                'side':         exit_side,
                'type':         'TAKE_PROFIT_MARKET',
                'quantity':     str(qty_r),
                'stopPrice':    str(tp_r),
                'reduceOnly':   'true',
                'closePosition':'false',
            })

        params = _sign(f'batchOrders={_req.utils.quote(_json.dumps(batch_orders))}')
        r = _req.post(f'https://fapi.binance.com/fapi/v1/batchOrders?{params}',
                      headers=headers, timeout=10)
        batch_result = r.json()

        if isinstance(batch_result, list) and len(batch_result) >= 2:
            entry_r = batch_result[0]
            sl_r_   = batch_result[1]
            tp_r_   = batch_result[2] if len(batch_result) > 2 else {}

            if 'orderId' in entry_r:
                fill_price = float(entry_r.get('avgPrice') or entry_r.get('price') or 0)
                logger.info(f"[Batch] Entry+SL placed atomically for {symbol}: "
                            f"entry={entry_r['orderId']} sl={sl_r_.get('orderId')} fill={fill_price}")
                return {
                    'entry_order':  entry_r,
                    'entry_price':  fill_price,
                    'sl_order_id':  sl_r_.get('orderId', 'error'),
                    'tp_order_id':  tp_r_.get('orderId') if tp_r_ else None,
                    'sl_price':     sl_r,
                    'tp_price':     tp_r,
                    'method':       'batch_atomic',
                }
            else:
                logger.warning(f"[Batch] batchOrders failed: {batch_result[0]}")

        # ── Fallback: entry MARKET → then try SL separately ───────────────
        logger.warning(f"[Entry+SL] Batch failed, placing entry then SL separately")
        entry_order = self.place_market_order(symbol, 'buy' if direction == 'long' else 'sell', qty)
        if 'error' in entry_order:
            return {'error': entry_order['error']}

        fill_price = float(entry_order.get('average') or entry_order.get('price') or 0)
        sl_result  = self.set_sl_tp(symbol, direction, qty, sl_price, tp_price)

        return {
            'entry_order':  entry_order,
            'entry_price':  fill_price,
            'sl_order_id':  sl_result.get('sl_order_id', 'price_watch'),
            'tp_order_id':  sl_result.get('tp_order_id'),
            'sl_price':     sl_price,
            'tp_price':     tp_price,
            'method':       'sequential_fallback',
            'sl_error':     sl_result.get('error'),
        }

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

    def get_positions(self) -> List[Dict]:
        """
        Fetch all open futures positions using v2/positionRisk (works on multi-assets margin accounts).
        Returns list of position dicts with contracts > 0.
        """
        if self.mode == 'paper':
            return []
        try:
            import time as _time, hmac as _hmac, hashlib as _hashlib, requests as _req
            ts  = int(_time.time() * 1000)
            par = f"timestamp={ts}"
            sig = _hmac.new(self.api_secret.encode(), par.encode(), _hashlib.sha256).hexdigest()
            r   = _req.get(
                f"https://fapi.binance.com/fapi/v2/positionRisk?{par}&signature={sig}",
                headers={"X-MBX-APIKEY": self.api_key}, timeout=10
            )
            raw = r.json()
            if isinstance(raw, dict) and raw.get('code'):
                logger.error(f"get_positions error: {raw}")
                return []
            out = []
            for p in raw:
                amt = float(p.get('positionAmt', 0))
                if amt == 0:
                    continue
                sym  = p['symbol']
                # Convert SYRUPUSDT → SYRUP/USDT:USDT for ccxt compat
                for quote in ('USDT','BUSD','USDC','BNB','BTC','ETH'):
                    if sym.endswith(quote):
                        base = sym[:-len(quote)]
                        ccxt_sym = f"{base}/{quote}:{quote}"
                        break
                else:
                    ccxt_sym = sym
                side = 'long' if amt > 0 else 'short'
                out.append({
                    'symbol':            ccxt_sym,
                    'raw_symbol':        sym,
                    'side':              side,
                    'direction':         side,
                    'contracts':         abs(amt),
                    'entry_price':       float(p.get('entryPrice', 0)),
                    'mark_price':        float(p.get('markPrice', 0)),
                    'unrealized_pnl':    float(p.get('unRealizedProfit', 0)),
                    'leverage':          float(p.get('leverage', 1)),
                    'liquidation_price': float(p.get('liquidationPrice', 0)),
                    'notional':          abs(float(p.get('notional', 0))),
                    'margin_type':       p.get('marginType', 'cross'),
                })
            logger.info(f"get_positions: {len(out)} active positions")
            return out
        except Exception as e:
            logger.error(f"get_positions error: {e}")
            return []

    def set_sl_tp(self, symbol: str, side: str, qty: float,
                  sl_price: float, tp_price: float) -> Dict:
        """
        Place take-profit (as LIMIT reduceOnly) and register SL for price-watch.

        On multi-assets margin accounts (and some others), STOP_MARKET / TAKE_PROFIT_MARKET
        are blocked (-4120). We fall back to:
          - TP → LIMIT reduceOnly order (fills exactly at TP price, better than market)
          - SL → price-watch via PositionMonitor (market close when price hits SL)

        side = position side ('long' or 'short') — exit is opposite.
        Returns {sl_order_id, tp_order_id, sl_price, tp_price} or {error}.
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
            qty_r = self._round_qty(symbol, qty)
            sl_r  = self._round_price(symbol, sl_price) if sl_price else 0
            tp_r  = self._round_price(symbol, tp_price) if tp_price else 0
            result = {'sl_price': sl_r, 'tp_price': tp_r}

            # ── Try STOP_MARKET / TAKE_PROFIT_MARKET first ──────────────────
            stop_market_ok = True
            if sl_r > 0:
                try:
                    sl_order = self.exchange.create_order(
                        symbol=symbol, type='stop_market', side=exit_side, amount=qty_r,
                        params={'stopPrice': sl_r, 'reduceOnly': True, 'closePosition': False}
                    )
                    result['sl_order_id'] = sl_order['id']
                    logger.info(f"SL stop_market placed {symbol}: {sl_r}")
                except Exception as e:
                    stop_market_ok = False
                    if '-4120' in str(e) or '-4045' in str(e):
                        logger.warning(f"stop_market blocked ({e}), SL will be enforced by price-watch monitor")
                        result['sl_order_id'] = 'price_watch'
                    else:
                        logger.error(f"SL order error: {e}")
                        result['sl_order_id'] = f'error:{e}'

            if tp_r > 0:
                try:
                    tp_order = self.exchange.create_order(
                        symbol=symbol, type='take_profit_market', side=exit_side, amount=qty_r,
                        params={'stopPrice': tp_r, 'reduceOnly': True, 'closePosition': False}
                    )
                    result['tp_order_id'] = tp_order['id']
                    logger.info(f"TP take_profit_market placed {symbol}: {tp_r}")
                except Exception as e:
                    if '-4120' in str(e) or '-4045' in str(e):
                        logger.warning(f"take_profit_market blocked, falling back to LIMIT TP")
                        # ── Fallback: LIMIT reduceOnly ───────────────────────
                        try:
                            tp_limit = self.exchange.create_order(
                                symbol=symbol, type='limit', side=exit_side, amount=qty_r,
                                price=tp_r,
                                params={'reduceOnly': True, 'timeInForce': 'GTC'}
                            )
                            result['tp_order_id'] = tp_limit['id']
                            result['tp_type'] = 'limit'
                            logger.info(f"TP LIMIT placed {symbol}: {tp_r} id={tp_limit['id']}")
                        except Exception as e2:
                            logger.error(f"TP limit fallback error: {e2}")
                            result['tp_order_id'] = f'error:{e2}'
                    else:
                        logger.error(f"TP order error: {e}")
                        result['tp_order_id'] = f'error:{e}'

            logger.info(f"set_sl_tp complete for {symbol}: {result}")
            return result
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
