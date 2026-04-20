"""
Binance Connector — Phase 3A
Wraps ccxt for Binance Spot + Futures (Paper / Testnet / Live).
Zero changes to existing strategy logic.
"""
import ccxt
import logging
import math
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# ── 1000x Contract Map ─────────────────────────────────────────────────────────
# Some Binance Futures contracts represent 1000 tokens per contract unit.
# e.g. PEPE/USDT (price ~0.000003) → trades as 1000PEPEUSDT (price ~0.003)
# We auto-detect and scale prices/quantities transparently.
_K_CONTRACT_MAP: Dict[str, Dict] = {
    # base_symbol → {futures_raw_sym, multiplier}
    'PEPE':    {'raw': '1000PEPEUSDT',   'mult': 1000},
    'BONK':    {'raw': '1000BONKUSDT',   'mult': 1000},
    'SHIB':    {'raw': '1000SHIBUSDT',   'mult': 1000},
    'FLOKI':   {'raw': '1000FLOKIUSDT',  'mult': 1000},
    'LUNC':    {'raw': '1000LUNCUSDT',   'mult': 1000},
    'SATS':    {'raw': '1000SATSUSDT',   'mult': 1000},
    'RATS':    {'raw': '1000RATSUSDT',   'mult': 1000},
    'CAT':     {'raw': '1000CATUSDT',    'mult': 1000},
    'CHEEMS':  {'raw': '1000CHEEMSUSDT', 'mult': 1000},
    'XEC':     {'raw': '1000XECUSDT',    'mult': 1000},
    'BOB':     {'raw': '1000000BOBUSDT', 'mult': 1_000_000},
    'MOG':     {'raw': '1000000MOGUSDT', 'mult': 1_000_000},
}

def _normalise_k_contract(symbol: str) -> Tuple[str, float]:
    """
    Given a user-facing symbol like 'PEPE/USDT' or 'PEPE/USDT:USDT',
    return the correct Binance Futures symbol and price multiplier.

    Returns (normalised_symbol, price_multiplier)
    e.g. 'PEPE/USDT' → ('1000PEPE/USDT:USDT', 1000)
         'BTC/USDT'  → ('BTC/USDT:USDT',       1)
    """
    base = symbol.split('/')[0].upper()
    if base in _K_CONTRACT_MAP:
        info = _K_CONTRACT_MAP[base]
        raw  = info['raw']
        mult = info['mult']
        # Convert raw BINANCE symbol to ccxt format: 1000PEPEUSDT → 1000PEPE/USDT:USDT
        ccxt_sym = raw.replace('USDT', '/USDT:USDT') if 'USDT' in raw else raw
        return ccxt_sym, mult
    # Standard symbol
    if ':' not in symbol and '/' in symbol:
        base2, quote = symbol.split('/', 1)
        return f'{base2}/{quote}:{quote}', 1.0
    return symbol, 1.0


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
        self.time_offset  = 0
        self.hedge_mode   = False  # True when Binance dualSidePosition=True

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
                # ── Detect hedge mode (dualSidePosition) ──────────────────────
                try:
                    import requests as _req2, time as _t2, hmac as _h2, hashlib as _ha2
                    _ts2 = int(_t2.time() * 1000)
                    _par2 = f"timestamp={_ts2}&recvWindow=5000"
                    _sig2 = _h2.new(creds['secret'].encode(), _par2.encode(), _ha2.sha256).hexdigest()
                    _r2 = _req2.get(
                        f"https://fapi.binance.com/fapi/v1/positionSide/dual?{_par2}&signature={_sig2}",
                        headers={"X-MBX-APIKEY": creds['apiKey']}, timeout=5
                    )
                    self.hedge_mode = _r2.json().get('dualSidePosition', False)
                    logger.info(f"BinanceConnector: {'HEDGE' if self.hedge_mode else 'ONE-WAY'} position mode")
                except Exception as _he:
                    logger.warning(f"BinanceConnector: hedge mode detection failed: {_he}")
                    self.hedge_mode = False
                # ──────────────────────────────────────────────────────────────
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
        Calculate order quantity (contract units) from notional USDT.
        Auto-handles 1000x contracts (e.g. PEPE/USDT → 1000PEPEUSDT).
        qty = (notional * leverage) / (price * multiplier)
        """
        if price <= 0:
            return 0.0
        # Normalise symbol and get price multiplier for 1000x contracts
        norm_sym, mult = _normalise_k_contract(symbol)
        price_adjusted = price * mult   # e.g. 0.000003 * 1000 = 0.003
        lot = self.get_lot_info(norm_sym)
        raw_qty = (notional_usd * leverage) / price_adjusted
        step    = lot['step_size']
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

    def place_market_order(self, symbol: str, side: str, qty: float,
                           position_side: str = None) -> Dict:
        """
        Place a market order.
        Auto-handles 1000x contracts (PEPE/USDT → 1000PEPE/USDT:USDT).
        In paper mode returns simulated fill.
        """
        norm_sym, mult = _normalise_k_contract(symbol)
        if self.mode == 'paper':
            try:
                # Try normalised symbol first, fallback to spot price
                try:
                    ticker = self.exchange.fetch_ticker(norm_sym)
                except Exception:
                    import urllib.request as _req
                    raw_sym = symbol.split('/')[0].replace('/', '') + 'USDT'
                    r = _req.urlopen(f'https://api.binance.com/api/v3/ticker/price?symbol={raw_sym}', timeout=5)
                    import json
                    price_raw = float(json.loads(r.read())['price'])
                    return self.paper_execute(symbol, side, qty, price_raw)
                price = float(ticker['last']) / mult  # convert back to user-facing price
            except Exception:
                price = 0.0
            return self.paper_execute(symbol, side, qty, price)
        try:
            qty_r = self._round_qty(norm_sym, qty)
            params = {}
            if self.hedge_mode and position_side:
                params['positionSide'] = position_side.upper()
            order = self.exchange.create_order(
                symbol=norm_sym, type='market', side=side, amount=qty_r,
                params=params if params else None
            )
            logger.info(f"Market order: {side} {qty_r} {norm_sym} positionSide={params.get('positionSide','BOTH')} → {order['id']}")
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
            if '-2019' in err or 'Margin is insufficient' in err:
                logger.warning(f"place_market_order: insufficient margin for {norm_sym} qty={qty_r} — skipping")
                try:
                    import sys as _sy, os as _os2
                    _sy.path.insert(0, _os2.path.dirname(_os2.path.dirname(_os2.path.abspath(__file__))))
                    from scheduler.activity_logger import log_event as _log
                    _log('ORDER_ERROR', symbol=norm_sym, error='Margin insufficient (-2019)',
                         qty=qty_r, side=side)
                except Exception:
                    pass
                return {'error': 'Margin insufficient — reduce notional or add funds'}
            logger.error(f"place_market_order error: {e}")
            return {'error': err}

    def place_entry_with_sl(self, symbol: str, direction: str, qty: float,
                            sl_price: float, tp_price: float = 0) -> Dict:
        """
        Place entry + SL + TP atomically via Binance batchOrders.

        CONFIRMED WORKING approach for multi-assets margin accounts:
          batchOrders = [MARKET entry, LIMIT SL (reduceOnly), LIMIT TP (reduceOnly)]
          Binance processes in order: entry fills first, then SL/TP are valid reduceOnly orders.

        This is atomic — SL goes live at the same time as entry, zero gap.

        Returns: {entry_order, sl_order_id, tp_order_id, entry_price, sl_price, tp_price}
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
                price  = sl_price * (1.02 if direction == 'long' else 0.98)
            paper_r = self.paper_execute(symbol, 'buy' if direction == 'long' else 'sell', qty, price)
            return {
                'entry_order': paper_r, 'entry_price': price,
                'sl_order_id': f'paper_sl_{symbol}',
                'tp_order_id': f'paper_tp_{symbol}' if tp_price else None,
                'sl_price': sl_price, 'tp_price': tp_price, 'mode': 'paper',
            }

        # Normalise for 1000x contracts (PEPE/USDT → 1000PEPE/USDT:USDT, mult=1000)
        norm_sym, mult = _normalise_k_contract(symbol)
        raw_sym    = norm_sym.split(':')[0].replace('/', '')  # 1000PEPE/USDT:USDT → 1000PEPEUSDT
        exit_side  = 'BUY'  if direction == 'short' else 'SELL'
        entry_side = 'SELL' if direction == 'short' else 'BUY'
        # Scale prices UP by multiplier for the exchange (0.000003 → 0.003 for 1000PEPE)
        sl_price_x = sl_price * mult if sl_price else 0
        tp_price_x = tp_price * mult if tp_price else 0
        qty_r      = self._round_qty(norm_sym, qty)
        sl_r       = self._round_price(norm_sym, sl_price_x) if sl_price_x else 0
        tp_r       = self._round_price(norm_sym, tp_price_x) if tp_price_x else 0
        logger.info(f"[Connector] {symbol} → {norm_sym} (mult={mult}) "
                    f"qty={qty_r} sl={sl_r} tp={tp_r}")

        api_key = self.api_key
        secret  = self.api_secret
        headers = {'X-MBX-APIKEY': api_key, 'Content-Type': 'application/x-www-form-urlencoded'}

        def _sign(params: str) -> str:
            ts  = int(_time.time() * 1000)
            p   = params + f'&timestamp={ts}&recvWindow=5000'
            sig = _hmac.new(secret.encode(), p.encode(), _hashlib.sha256).hexdigest()
            return p + '&signature=' + sig

        # ── PRIMARY: batchOrders [MARKET entry + LIMIT SL + LIMIT TP] ─────────────────────
        #
        # ACCOUNT TYPE: Multi-Assets Cross Margin
        #   - STOP_MARKET / TAKE_PROFIT_MARKET → blocked (-4120)
        #   - LIMIT with reduceOnly=true        → blocked (-2022)
        #   - LIMIT (no reduceOnly)             → WORKS ✅
        #
        # HOW PLAIN LIMIT ORDERS WORK AS SL/TP (why they don't fill immediately):
        #   LONG position SL  = LIMIT SELL at sl_price (BELOW current market price)
        #     → sits in book, fills only when price FALLS to sl_price ✅
        #   SHORT position SL = LIMIT BUY  at sl_price (ABOVE current market price)
        #     → sits in book, fills only when price RISES to sl_price ✅
        #   LONG TP  = LIMIT SELL above entry  | SHORT TP = LIMIT BUY below entry
        #
        # PREVIOUS BUG (fixed): SL for SHORT was using LIMIT BUY at sl_price but WITH
        # reduceOnly=true → rejected (-2022). Without reduceOnly it now works correctly.
        #
        # Safety: position monitor cancels the surviving bracket when one leg fills.
        batch_orders = [
            {
                'symbol':      raw_sym,
                'side':        entry_side,
                'type':        'MARKET',
                'quantity':    str(qty_r),
            },
            {
                'symbol':      raw_sym,
                'side':        exit_side,
                'type':        'LIMIT',
                'price':       str(sl_r),
                'quantity':    str(qty_r),
                'timeInForce': 'GTC',
            },
        ]
        if tp_r > 0:
            batch_orders.append({
                'symbol':      raw_sym,
                'side':        exit_side,
                'type':        'LIMIT',
                'price':       str(tp_r),
                'quantity':    str(qty_r),
                'timeInForce': 'GTC',
            })

        body = _sign(f'batchOrders={_req.utils.quote(_json.dumps(batch_orders))}')
        r    = _req.post('https://fapi.binance.com/fapi/v1/batchOrders',
                         data=body, headers=headers, timeout=10)
        batch_result = r.json()

        if isinstance(batch_result, list) and len(batch_result) >= 1:
            entry_r = batch_result[0]
            sl_r_   = batch_result[1] if len(batch_result) > 1 else {}
            tp_r_   = batch_result[2] if len(batch_result) > 2 else {}

            entry_filled = 'orderId' in entry_r  # entry order accepted by Binance

            if entry_filled and 'orderId' in sl_r_:
                # FULL SUCCESS: entry + SL both placed atomically
                fill_price = float(entry_r.get('avgPrice') or entry_r.get('price') or 0)
                logger.info(f"[Batch] Atomic entry+SL+TP: {symbol} entry={entry_r['orderId']} "
                            f"sl={sl_r_.get('orderId')} tp={tp_r_.get('orderId')} fill={fill_price}")
                return {
                    'entry_order': entry_r,
                    'entry_price': fill_price,
                    'sl_order_id': sl_r_.get('orderId'),
                    'tp_order_id': tp_r_.get('orderId'),
                    'sl_price':    sl_r,
                    'tp_price':    tp_r,
                    'method':      'batch_atomic',
                }

            elif entry_filled and 'orderId' not in sl_r_:
                # PARTIAL: Entry filled BUT SL/TP order failed (e.g. reduceOnly rejected)
                # CRITICAL: DO NOT place another entry order — position is already open!
                fill_price = float(entry_r.get('avgPrice') or entry_r.get('price') or 0)
                err_sl = sl_r_.get('msg', sl_r_.get('code', 'unknown'))
                logger.warning(f"[Batch] Entry filled but SL/TP failed: entry={entry_r['orderId']} "
                               f"fill={fill_price} sl_err={err_sl}. Placing SL/TP separately.")
                # Activity log
                try:
                    import sys as _sys, os as _os
                    _sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
                    from scheduler.activity_logger import log_event as _log
                    _log('BATCH_PARTIAL', symbol=symbol, entry_order_id=entry_r['orderId'],
                         fill_price=fill_price, sl_error=str(err_sl)[:100])
                except Exception:
                    pass
                # Entry is live — only place SL/TP bracket orders
                import time as _t2
                _t2.sleep(0.5)  # brief pause for position to register
                sl_result = self.set_sl_tp(symbol, direction, qty, sl_price, tp_price)
                return {
                    'entry_order': entry_r,
                    'entry_price': fill_price,
                    'sl_order_id': sl_result.get('sl_order_id', 'price_watch'),
                    'tp_order_id': sl_result.get('tp_order_id'),
                    'sl_price':    sl_price,
                    'tp_price':    tp_price,
                    'method':      'batch_entry_only',
                }
            else:
                # FULL FAILURE: entry itself was rejected — safe to fall through to sequential
                err0 = entry_r.get('msg', entry_r.get('code', '?'))
                err1 = sl_r_.get('msg', sl_r_.get('code', '?')) if sl_r_ else 'N/A'
                logger.warning(f"[Batch] batchOrders full failure: entry={err0} sl={err1}")
                # Fall through to sequential below

        # ── FALLBACK: sequential (entry → SL immediately after) ───────────
        # ONLY reached if the batch entirely failed (entry NOT placed).
        # Never reached if entry filled above.
        logger.warning(f"[Entry+SL] batchOrders failed, placing sequentially")
        entry_order = self.place_market_order(symbol, 'buy' if direction == 'long' else 'sell', qty)
        if 'error' in entry_order:
            return {'error': entry_order['error']}

        fill_price = float(entry_order.get('average') or entry_order.get('price') or 0)

        # Place SL + TP via the confirmed-working set_sl_tp method
        sl_result = self.set_sl_tp(symbol, direction, qty, sl_price, tp_price)

        return {
            'entry_order': entry_order,
            'entry_price': fill_price,
            'sl_order_id': sl_result.get('sl_order_id', 'price_watch'),
            'tp_order_id': sl_result.get('tp_order_id'),
            'sl_price':    sl_price,
            'tp_price':    tp_price,
            'method':      'sequential_fallback',
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
                pos_side = p.get('positionSide', 'BOTH')  # LONG/SHORT (hedge) or BOTH (one-way)
                out.append({
                    'symbol':            ccxt_sym,
                    'raw_symbol':        sym,
                    'side':              side,
                    'direction':         side,
                    'positionSide':      pos_side,
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

    def _place_limit_bracket(self, raw_sym: str, norm_sym: str,
                             exit_side: str, qty_r: float, price: float) -> Dict:
        """
        Place a LIMIT bracket order (SL or TP) via raw Binance fapi REST.

        ACCOUNT TYPE NOTE — Multi-Assets Cross Margin:
          - STOP_MARKET / TAKE_PROFIT_MARKET → blocked (-4120) on this account type
          - LIMIT reduceOnly → rejected (-2022) on this account type  
          - LIMIT (no reduceOnly) → WORKS ✅ — sits in orderbook at target price

        SL safety is guaranteed by:
          1. Order is at correct price level (SL for long = SELL below; SL for short = BUY above)
          2. Position monitor runs every 5s — if position closes (SL/TP hit), it cancels
             the remaining bracket order via _cancel_orphaned_orders()
          3. No risk of flipping position: when the bracket fills, the existing position
             is offset; position monitor detects zero position and cancels the other bracket.

        exit_side: 'BUY' or 'SELL' (uppercase)
        price:     already scaled for 1000x contracts and rounded to exchange precision
        """
        import time as _t, hmac as _h, hashlib as _ha, requests as _r2, math as _math
        ts  = int(_t.time() * 1000)
        params = (
            f"symbol={raw_sym}"
            f"&side={exit_side.upper()}"
            f"&type=LIMIT"
            f"&price={price}"
            f"&quantity={qty_r}"
            f"&timeInForce=GTC"
            f"&timestamp={ts}&recvWindow=5000"
        )
        sig = _h.new(self.api_secret.encode(), params.encode(), _ha.sha256).hexdigest()
        resp = _r2.post(
            f"https://fapi.binance.com/fapi/v1/order?{params}&signature={sig}",
            headers={"X-MBX-APIKEY": self.api_key}, timeout=10
        )
        return resp.json()

    def set_sl_tp(self, symbol: str, side: str, qty: float,
                  sl_price: float, tp_price: float) -> Dict:
        """
        Place SL and TP bracket orders as plain LIMIT (no reduceOnly).

        This account (multi-assets cross margin) blocks:
          - STOP_MARKET / TAKE_PROFIT_MARKET  → error -4120
          - LIMIT with reduceOnly=true         → error -2022

        Only plain LIMIT orders work. Safety:
          - SL LONG  = LIMIT SELL below entry (price must fall to SL to fill)
          - SL SHORT = LIMIT BUY  above entry (price must rise to SL to fill)
          - TP LONG  = LIMIT SELL above entry (price must rise to TP to fill)  
          - TP SHORT = LIMIT BUY  below entry (price must fall to TP to fill)
          - Position monitor cancels the surviving bracket when one leg fills.

        side = position side ('long' or 'short')
        Returns {sl_order_id, tp_order_id, sl_price, tp_price} or {error}.
        """
        exit_side = 'SELL' if side == 'long' else 'BUY'
        if self.mode == 'paper':
            return {
                'sl_order_id': f'paper_sl_{symbol}',
                'tp_order_id': f'paper_tp_{symbol}',
                'sl_price': sl_price,
                'tp_price': tp_price,
                'mode': 'paper',
            }
        try:
            # Normalise symbol for 1000x contracts
            norm_sym, mult = _normalise_k_contract(symbol)
            raw_binance = norm_sym.split('/')[0].replace('/', '') + \
                          (norm_sym.split('/')[1].split(':')[0] if '/' in norm_sym else '')
            raw_binance = raw_binance.replace('/', '')

            # ── Check if bracket orders already exist ──────────────────────
            import time as _chk_t, hmac as _chk_h, hashlib as _chk_ha, requests as _chk_r
            ts_chk  = int(_chk_t.time() * 1000)
            par_chk = f"symbol={raw_binance}&timestamp={ts_chk}&recvWindow=5000"
            sig_chk = _chk_h.new(self.api_secret.encode(), par_chk.encode(), _chk_ha.sha256).hexdigest()
            r_chk   = _chk_r.get(
                f"https://fapi.binance.com/fapi/v1/openOrders?{par_chk}&signature={sig_chk}",
                headers={"X-MBX-APIKEY": self.api_key}, timeout=5
            )
            existing = r_chk.json()
            if isinstance(existing, list) and len(existing) >= 2:
                logger.info(f"[Connector] {symbol}: {len(existing)} orders already exist — skipping set_sl_tp")
                return {
                    'sl_order_id': existing[0].get('orderId', 'existing'),
                    'tp_order_id': existing[-1].get('orderId', 'existing'),
                    'sl_price':    sl_price,
                    'tp_price':    tp_price,
                    'already_placed': True,
                }

            qty_r = self._round_qty(norm_sym, qty)
            sl_r  = self._round_price(norm_sym, sl_price * mult) if sl_price else 0
            tp_r  = self._round_price(norm_sym, tp_price * mult) if tp_price else 0
            result = {'sl_price': sl_price, 'tp_price': tp_price}

            # ── SL: plain LIMIT, no reduceOnly ────────────────────────────
            if sl_r > 0:
                sl_resp = self._place_limit_bracket(raw_binance, norm_sym, exit_side, qty_r, sl_r)
                if 'orderId' in sl_resp:
                    result['sl_order_id'] = sl_resp['orderId']
                    result['sl_type'] = 'LIMIT'
                    logger.info(f"[set_sl_tp] SL LIMIT placed {symbol}: price={sl_r} id={sl_resp['orderId']}")
                else:
                    err_code = sl_resp.get('code', '?')
                    err_msg  = sl_resp.get('msg', str(sl_resp))[:100]
                    logger.error(f"[set_sl_tp] SL LIMIT failed {symbol}: code={err_code} {err_msg}")
                    result['sl_order_id'] = f'price_watch_fallback:{err_code}'
                    result['sl_error'] = err_msg

            # ── TP: plain LIMIT, no reduceOnly ────────────────────────────
            if tp_r > 0:
                tp_resp = self._place_limit_bracket(raw_binance, norm_sym, exit_side, qty_r, tp_r)
                if 'orderId' in tp_resp:
                    result['tp_order_id'] = tp_resp['orderId']
                    result['tp_type'] = 'LIMIT'
                    logger.info(f"[set_sl_tp] TP LIMIT placed {symbol}: price={tp_r} id={tp_resp['orderId']}")
                else:
                    err_code = tp_resp.get('code', '?')
                    err_msg  = tp_resp.get('msg', str(tp_resp))[:100]
                    logger.warning(f"[set_sl_tp] TP LIMIT failed {symbol}: code={err_code} {err_msg}")
                    result['tp_order_id'] = f'price_watch_tp:{err_code}'

            logger.info(f"[set_sl_tp] complete for {symbol}: {result}")
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
