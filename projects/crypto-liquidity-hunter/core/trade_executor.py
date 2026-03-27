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
    Supports paper and live modes.
    """

    def __init__(self, connector: BinanceConnector = None):
        cfg = _load_config()
        bc_cfg = cfg.get('binance_connection', {})
        paper_cfg = cfg.get('paper_trading', {})

        self.mode         = bc_cfg.get('mode', 'paper')
        self.notional_usd = float(paper_cfg.get('fixed_notional_usd', 100.0))
        self.leverage     = float(paper_cfg.get('margin_leverage', 20.0))
        self.commission   = float(paper_cfg.get('commission_per_trade', 0.001))

        if connector is None:
            self.connector = BinanceConnector(
                api_key    = bc_cfg.get('api_key', ''),
                api_secret = bc_cfg.get('api_secret', ''),
                testnet    = bc_cfg.get('testnet', True),
                mode       = self.mode,
            )
            self.connector.connect()
        else:
            self.connector = connector

        self.store = DataStore(
            db_path    = '/root/.openclaw/workspace/projects/crypto-liquidity-hunter/data/store.db',
            cache_path = '/root/.openclaw/workspace/projects/crypto-liquidity-hunter/data/latest_signals.json',
        )

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

        if entry_price <= 0:
            return {'error': 'Invalid entry price'}

        notional = notional_usd or self.notional_usd
        lev      = leverage     or self.leverage

        # Strip exchange prefix for connector (e.g. binance:BTC/USDT → BTC/USDT)
        symbol = pair.split(':', 1)[1] if ':' in pair else pair

        # Normalise symbol for futures (BTC/USDT → BTC/USDT for ccxt binanceusdm)
        qty = self.connector.calc_qty(symbol, notional, entry_price, lev)
        commission_usd = notional * self.commission

        # Determine ccxt side
        side = 'buy' if direction == 'long' else 'sell'

        order_result = {}
        if self.mode == 'paper':
            order_result = self.connector.paper_execute(symbol, side, qty, entry_price)
        else:
            order_result = self.connector.place_market_order(symbol, side, qty)
            if 'error' not in order_result:
                # Place SL/TP after entry
                sl_tp = self.connector.set_sl_tp(symbol, direction, qty, stop_loss, target)
                order_result['sl_tp'] = sl_tp

        if 'error' in order_result:
            return {'error': order_result['error']}

        # Persist to DB
        now = datetime.now(timezone.utc)
        trade_id = self.store.create_open_trade(
            pair           = pair,
            timeframe      = timeframe,
            direction      = direction,
            entry_price    = entry_price,
            sl             = stop_loss,
            tp             = target,
            entry_time     = now,
            notional_usd   = notional,
            commission_usd = commission_usd,
            signal_id      = signal_id,
            notes          = f"mode={self.mode} order={order_result.get('id','?')}",
            mode           = self.mode,
            order_id       = str(order_result.get('id', '')),
        )

        return {
            'trade_id':    trade_id,
            'mode':        self.mode,
            'qty':         qty,
            'entry_price': entry_price,
            'sl':          stop_loss,
            'tp':          target,
            'order_id':    order_result.get('id'),
            'status':      'open',
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
        if exit_price >= float(trade.get('tp', 1e18)):
            status = 'target_hit'
        elif exit_price <= float(trade.get('sl', 0)):
            status = 'stop_loss'

        # Close in exchange (if live)
        if self.mode != 'paper' and self.connector.exchange:
            sym   = symbol.split(':', 1)[1] if ':' in symbol else symbol
            qty   = self.connector.calc_qty(sym, notional, exit_price, 1.0)
            close_side = 'sell' if direction == 'long' else 'buy'
            self.connector.place_market_order(sym, close_side, qty)

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
                               testnet: bool, mode: str) -> bool:
        """Persist Binance connection settings to pairs.yaml."""
        try:
            cfg = _load_config()
            cfg['binance_connection'] = {
                'api_key':    api_key,
                'api_secret': api_secret,
                'testnet':    testnet,
                'mode':       mode,
                'enabled':    True,
            }
            _save_config(cfg)
            return True
        except Exception as e:
            logger.error(f"save_connection_config error: {e}")
            return False
