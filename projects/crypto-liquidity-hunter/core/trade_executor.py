"""
Trade Executor — Phase 3A
Bridges a Signal → Binance order (paper or live).
Reads config from pairs.yaml `binance_connection` section.
Zero changes to existing strategy logic.
"""
import logging
import yaml
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Optional

from core.binance_connector import BinanceConnector
from data.store import DataStore

logger = logging.getLogger(__name__)

CONFIG_PATH = Path("/root/.openclaw/workspace/projects/crypto-liquidity-hunter/config/pairs.yaml")


def _load_config() -> Dict:
    with open(CONFIG_PATH, 'r') as f:
        return yaml.safe_load(f)


def _save_config(cfg: Dict):
    with open(CONFIG_PATH, 'w') as f:
        yaml.dump(cfg, f, default_flow_style=False, sort_keys=False)


class TradeExecutor:
    """
    Executes signals via BinanceConnector.
    Persists trades to DataStore.
    Supports paper, demo, testnet, and live modes.
    Can be initialised with a specific account_id for multi-account support.
    """

    def __init__(self, connector: BinanceConnector = None, account_id: int = None):
        cfg = _load_config()
        paper_cfg = cfg.get('paper_trading', {})
        live_cfg  = cfg.get('live_trading',  {})

        self.account_id  = account_id
        self._paper_cfg  = paper_cfg
        self._live_cfg   = live_cfg

        # Defaults from paper_trading (overridden per-mode in execute_signal)
        self.notional_usd = float(paper_cfg.get('fixed_notional_usd', 100.0))
        self.leverage     = float(paper_cfg.get('margin_leverage', 20.0))
        self.commission   = float(paper_cfg.get('commission_per_trade', 0.001))

        self.store = DataStore(
            db_path    = '/root/.openclaw/workspace/projects/crypto-liquidity-hunter/data/store.db',
            cache_path = '/root/.openclaw/workspace/projects/crypto-liquidity-hunter/data/latest_signals.json',
        )

        if connector is not None:
            self.connector = connector
            self.mode = connector.mode
        elif account_id is not None:
            # Load from accounts table
            acct = self.store.get_account(account_id)
            if acct:
                self.mode = acct.get('mode', 'paper')
                self.connector = BinanceConnector(
                    api_key     = acct.get('api_key', ''),
                    api_secret  = acct.get('api_secret', ''),
                    testnet     = bool(acct.get('testnet', 0)),
                    mode        = self.mode,
                    is_demo     = bool(acct.get('is_demo', 0)),
                    environment = acct.get('environment', 'mainnet'),
                )
                self.connector.connect()
            else:
                # Account not found — fall back to paper
                self.mode = 'paper'
                self.connector = BinanceConnector(mode='paper')
                self.connector.connect()
        else:
            # Legacy: load from pairs.yaml binance_connection
            bc_cfg = cfg.get('binance_connection', {})
            self.mode = bc_cfg.get('mode', 'paper')
            self.connector = BinanceConnector(
                api_key     = bc_cfg.get('api_key', ''),
                api_secret  = bc_cfg.get('api_secret', ''),
                testnet     = bc_cfg.get('testnet', True),
                mode        = self.mode,
                is_demo     = bc_cfg.get('is_demo', False),
                environment = bc_cfg.get('environment', 'mainnet'),
            )
            self.connector.connect()

    # ─── Execute Signal ────────────────────────────────────────────────────────

    def execute_signal(self, signal_dict: Dict, pair: str,
                       notional_usd: float = None, leverage: float = None,
                       signal_id: int = None) -> Dict:
        """
        Execute a trade from a signal dict.
        signal_dict keys: direction, entry_price, stop_loss, target, confidence, timeframe
        Returns: {trade_id, mode, qty, entry_price, sl, tp, order_id, error}
        """
        direction   = signal_dict.get('direction', 'long')
        entry_price = float(signal_dict.get('entry_price', 0))
        stop_loss   = float(signal_dict.get('stop_loss', 0))
        target      = float(signal_dict.get('target', 0))
        timeframe   = signal_dict.get('timeframe', '1h')

        # Strip exchange prefix for connector (e.g. binance:BTC/USDT → BTC/USDT)
        symbol = pair.split(':', 1)[1] if ':' in pair else pair

        # ── Load mode-specific trading config (live vs paper) ─────────────────
        # Always re-read config fresh so dashboard settings take effect immediately
        _cfg      = _load_config()
        mode_key  = 'live_trading' if self.mode != 'paper' else 'paper_trading'
        trade_cfg = _cfg.get(mode_key, _cfg.get('paper_trading', {}))

        base_notional = float(trade_cfg.get('fixed_notional_usd', self.notional_usd))
        base_leverage = float(trade_cfg.get('margin_leverage',     self.leverage))
        commission    = float(trade_cfg.get('commission_per_trade', self.commission))
        sizing_mode   = trade_cfg.get('position_sizing', 'fixed_notional')
        risk_pct      = float(trade_cfg.get('risk_percent', 1.0))
        max_notional  = float(trade_cfg.get('max_notional_usd', 500.0))

        # ── Determine notional and leverage ───────────────────────────────────
        lev = leverage or base_leverage

        if notional_usd:
            # Explicit override from caller (e.g. manual trade execution)
            notional = float(notional_usd)
        elif sizing_mode == 'risk_percent' and self.mode != 'paper':
            # Risk-percent: size based on % of live account balance
            try:
                bal_info = self.connector.get_balance('USDT')
                balance  = float(bal_info.get('free', 0) or 0)
                notional = balance * (risk_pct / 100.0) * lev
                logger.info(f"[Executor] Risk sizing: {risk_pct}% of ${balance:.2f} × {lev}× lev = ${notional:.2f}")
            except Exception as e:
                logger.warning(f"[Executor] Balance fetch failed ({e}), using fixed notional")
                notional = base_notional
        else:
            notional = base_notional

        # Enforce max notional safety cap
        if notional > max_notional:
            logger.warning(f"[Executor] Notional ${notional:.2f} exceeds cap ${max_notional:.2f}, capping")
            notional = max_notional

        self.commission = commission  # update for PnL calcs below

        # entry_price=0 means "market order" — fetch current price for record keeping
        if entry_price <= 0:
            try:
                if self.connector.exchange:
                    ticker = self.connector.exchange.fetch_ticker(symbol)
                    entry_price = float(ticker['last'])
                else:
                    # Paper fallback — use public price fetch via ccxt
                    import ccxt as _ccxt
                    _ex = _ccxt.binance({'enableRateLimit': True})
                    ticker = _ex.fetch_ticker(symbol)
                    entry_price = float(ticker['last'])
            except Exception:
                # Last resort — known approximate prices
                _KNOWN_PRICES = {
                    'BTC/USDT': 65000.0, 'ETH/USDT': 3500.0,
                    'SOL/USDT': 150.0,   'BNB/USDT': 580.0,
                }
                sym_clean = symbol.replace(':USDT', '/USDT').upper()
                entry_price = _KNOWN_PRICES.get(sym_clean, 1.0)
                if entry_price == 1.0:
                    return {'error': f'entry_price=0 and could not fetch current price for {symbol}'}

        # Normalise symbol for futures (BTC/USDT → BTC/USDT for ccxt binanceusdm)
        qty = self.connector.calc_qty(symbol, notional, entry_price, lev)
        commission_usd = notional * self.commission

        # Determine ccxt side
        side = 'buy' if direction == 'long' else 'sell'

        order_result = {}
        sltp_result  = {}

        if self.mode == 'paper':
            order_result = self.connector.paper_execute(symbol, side, qty, entry_price)
        else:
            if stop_loss > 0:
                # ── ATOMIC: entry + SL in single batchOrders call ────────────
                # SL is placed simultaneously with entry — no gap for spike protection
                logger.info(f"[Executor] Placing entry+SL atomically: {direction} {symbol} "
                            f"qty={qty} sl={stop_loss} tp={target}")
                result = self.connector.place_entry_with_sl(
                    symbol, direction, qty, stop_loss, target
                )
                if 'error' in result:
                    return {'error': result['error']}

                order_result  = result.get('entry_order', {})
                actual_fill   = float(result.get('entry_price') or 0)
                if actual_fill > 0:
                    entry_price = actual_fill
                sltp_result = {
                    'sl_order_id': result.get('sl_order_id'),
                    'tp_order_id': result.get('tp_order_id'),
                    'sl_price':    result.get('sl_price', stop_loss),
                    'tp_price':    result.get('tp_price', target),
                    'method':      result.get('method', 'unknown'),
                }
                logger.info(f"[Executor] Entry+SL result: fill={entry_price} "
                            f"sl_order={result.get('sl_order_id')} method={result.get('method')}")
            else:
                # No SL defined — plain market entry (not recommended)
                logger.warning(f"[Executor] No SL defined for {symbol} — entering without bracket orders")
                order_result = self.connector.place_market_order(symbol, side, qty)
                if 'error' in order_result:
                    return {'error': order_result['error']}
                actual_fill = (
                    float(order_result.get('average') or 0) or
                    float(order_result.get('price')   or 0) or
                    float(order_result.get('info', {}).get('avgPrice', 0) or 0)
                )
                if actual_fill > 0:
                    entry_price = actual_fill
                # Still try to place SL/TP separately if target defined
                if target > 0:
                    sltp_result = self.connector.set_sl_tp(symbol, direction, qty, 0, target)

        if 'error' in order_result and self.mode != 'paper':
            return {'error': order_result.get('error', 'Order failed')}

        # Persist to DB with actual fill price
        now = datetime.now(timezone.utc)
        trade_id = self.store.create_open_trade(
            pair           = pair,
            timeframe      = timeframe,
            direction      = direction,
            entry_price    = entry_price,  # actual fill price
            sl             = stop_loss,
            tp             = target,
            entry_time     = now,
            notional_usd   = notional,
            commission_usd = commission_usd,
            signal_id      = signal_id,
            notes          = f"mode={self.mode} order={order_result.get('id','?')}",
            mode           = self.mode,
            order_id       = str(order_result.get('id', '')),
            account_id     = self.account_id,
        )

        # Start position monitor for live accounts so SL/TP hits auto-close DB
        if self.mode != 'paper' and self.account_id and trade_id:
            try:
                from core.position_monitor import start_monitor, get_monitor
                m = start_monitor(self.connector, self.store,
                                  account_id=self.account_id, interval=10)
                if sltp_result and 'error' not in sltp_result:
                    m.mark_sltp_placed(trade_id)
            except Exception as e:
                logger.warning(f"Could not start position monitor: {e}")

        return {
            'trade_id':       trade_id,
            'mode':           self.mode,
            'qty':            qty,
            'entry_price':    entry_price,
            'sl':             stop_loss,
            'tp':             target,
            'order_id':       order_result.get('id'),
            'sl_order_id':    sltp_result.get('sl_order_id') if sltp_result else None,
            'tp_order_id':    sltp_result.get('tp_order_id') if sltp_result else None,
            'status':         'open',
        }

    # ─── Close Trade ──────────────────────────────────────────────────────────

    def close_trade(self, trade_id: int, symbol: str,
                    exit_price: float = None, reason: str = 'manual') -> bool:
        """
        Close an open trade. Fetches current price if exit_price not given.
        """
        # Find trade in DB
        open_trades = self.store.get_open_trades()
        trade = next((t for t in open_trades if t['id'] == trade_id), None)
        if not trade:
            logger.warning(f"close_trade: trade {trade_id} not found or not open")
            return False

        if exit_price is None:
            # Fetch current price
            try:
                if self.mode != 'paper' and self.connector.exchange:
                    ticker = self.connector.exchange.fetch_ticker(symbol)
                    exit_price = float(ticker['last'])
                else:
                    exit_price = float(trade['entry_price'])  # fallback
            except Exception:
                exit_price = float(trade['entry_price'])

        # Calculate PnL
        direction = trade['direction']
        ep        = float(trade['entry_price'])
        notional  = float(trade.get('notional_usd', 100))
        commission= float(trade.get('commission_usd', 0.1))

        if direction == 'long':
            pnl = (exit_price - ep) / ep * notional - commission * 2
        else:
            pnl = (ep - exit_price) / ep * notional - commission * 2

        status = 'closed'
        try:
            tp_val = float(trade.get('tp') or 1e18)
            sl_val = float(trade.get('sl') or 0)
            if exit_price >= tp_val and tp_val < 1e17:
                status = 'target_hit'
            elif exit_price <= sl_val and sl_val > 0:
                status = 'stop_loss'
        except (TypeError, ValueError):
            pass  # keep status = 'closed'

        # Close in exchange (if live/demo) — place reduceOnly closing order
        if self.mode not in ('paper',) and self.connector.exchange:
            sym        = symbol.split(':', 1)[1] if ':' in symbol else symbol
            close_side = 'sell' if direction == 'long' else 'buy'
            try:
                # Use the recorded qty from order_id if available, else calc from notional
                order_id = trade.get('order_id', '')
                qty = self.connector.calc_qty(sym, notional, exit_price or ep, 1.0)
                # Place reduceOnly closing market order
                close_order = self.connector.exchange.create_order(
                    sym, 'market', close_side, qty,
                    params={'reduceOnly': True}
                )
                # Use actual fill price
                actual_exit = (
                    float(close_order.get('average') or 0) or
                    float(close_order.get('price')   or 0) or
                    exit_price
                )
                if actual_exit > 0:
                    exit_price = actual_exit
                logger.info(f"Close order placed: {close_side} {qty} {sym} @ {exit_price}")
            except Exception as e:
                logger.error(f"Exchange close order failed for trade {trade_id}: {e}")
                # Still close in DB even if exchange order fails

        # Recalculate PnL with actual exit price
        if direction == 'long':
            pnl = (exit_price - ep) / ep * notional - commission * 2
        else:
            pnl = (ep - exit_price) / ep * notional - commission * 2

        self.store.close_trade(
            trade_id=trade_id,
            exit_price=exit_price,
            exit_time=datetime.now(timezone.utc),
            status=status,
            pnl_usd=round(pnl, 4),
        )
        logger.info(f"Closed trade {trade_id}: exit={exit_price} pnl={pnl:.2f} status={status}")
        return True

    # ─── Config Helpers ────────────────────────────────────────────────────────

    @staticmethod
    def save_connection_config(api_key: str, api_secret: str,
                               testnet: bool, mode: str,
                               is_demo: bool = False, environment: str = 'mainnet') -> bool:
        """Persist Binance connection settings to pairs.yaml (legacy single-account)."""
        try:
            cfg = _load_config()
            cfg['binance_connection'] = {
                'api_key':     api_key,
                'api_secret':  api_secret,
                'testnet':     testnet,
                'mode':        mode,
                'is_demo':     is_demo,
                'environment': environment,
                'enabled':     True,
            }
            _save_config(cfg)
            return True
        except Exception as e:
            logger.error(f"save_connection_config error: {e}")
            return False
