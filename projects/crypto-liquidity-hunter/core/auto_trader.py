"""
Auto Trader: Execute trades on Binance/Bybit testnet.
Supports OCO (one-cancels-other) orders, position sizing, risk limits.
"""
import ccxt
import time
import logging
from typing import Dict, Optional
from dataclasses import dataclass

logger = logging.getLogger(__name__)

@dataclass
class TradeOrder:
    """Represents an active trade."""
    id: str
    pair: str
    direction: str  # 'long' or 'short'
    entry_price: float
    stop_loss: float
    target: float
    position_size: float
    status: str = 'open'  # 'open', 'filled', 'cancelled', 'stopped', 'targeted'

class AutoTrader:
    def __init__(self, exchange_id: str = 'binance', testnet: bool = True, config: Dict = None):
        """
        Initialize exchange connection.
        config: {'apiKey': ..., 'secret': ..., 'password': ... (for futures)}
        """
        self.exchange_id = exchange_id
        self.testnet = testnet
        self.exchange_class = getattr(ccxt, exchange_id)
        self.exchange = self.exchange_class(config or {})
        if testnet:
            self.exchange.set_sandbox_mode(True)
        # Set futures market type for binance
        if exchange_id == 'binance':
            self.exchange.options['defaultType'] = 'future'
        # Bybit: swap
        if exchange_id == 'bybit':
            self.exchange.options['defaultType'] = 'swap'
        self.markets = None
        self.active_trades = {}  # order_id -> TradeOrder

    def load_markets(self):
        self.markets = self.exchange.load_markets()
        return self.markets

    def calculate_position_size(self, entry: float, stop: float, capital: float, risk_pct: float = 0.01) -> float:
        """Size in base currency (e.g., BTC)."""
        risk_per_unit = abs(entry - stop)
        if risk_per_unit == 0:
            return 0
        risk_amount = capital * risk_pct
        position = risk_amount / risk_per_unit
        return position

    def place_oco_order(self, pair: str, direction: str, entry: float, stop: float, target: float,
                        position_size: float, reduce_only: bool = True) -> Dict:
        """
        Place OCO order (one-cancels-other) for take profit and stop loss.
        For binance futures: use 'stop_market' and 'take_profit_market' orders with 'positionSide'.
        Returns dict with order IDs.
        """
        try:
            # First, place limit entry order
            side = 'buy' if direction == 'long' else 'sell'
            entry_order = self.exchange.create_order(
                symbol=pair,
                type='limit',
                side=side,
                amount=position_size,
                price=entry,
                params={'reduceOnly': reduce_only}
            )
            logger.info(f"Entry order placed: {entry_order['id']}")

            # Store pending trade
            trade = TradeOrder(
                id=entry_order['id'],
                pair=pair,
                direction=direction,
                entry_price=entry,
                stop_loss=stop,
                target=target,
                position_size=position_size,
                status='open'
            )
            self.active_trades[entry_order['id']] = trade

            # We'll need to attach SL/TP after entry fills (use OCO or separate orders)
            # Binance supports OCO via create_market? Actually for futures, you can set
            # stopMarket and takeProfitMarket params on order, but simpler: after fill, place two orders.

            return {
                'entry_order_id': entry_order['id'],
                'status': 'pending_entry'
            }
        except Exception as e:
            logger.error(f"Failed to place OCO order: {e}")
            return {'error': str(e)}

    def place_exit_orders(self, entry_order_id: str):
        """After entry fills, place stop loss and take profit orders."""
        trade = self.active_trades.get(entry_order_id)
        if not trade:
            logger.warning(f"Trade {entry_order_id} not found")
            return

        # Determine sides for exits
        if trade.direction == 'long':
            stop_side = 'sell'
            target_side = 'sell'
        else:
            stop_side = 'buy'
            target_side = 'buy'

        try:
            # Stop loss (market order when triggered)
            stop_order = self.exchange.create_order(
                symbol=trade.pair,
                type='stop_market',
                side=stop_side,
                amount=trade.position_size,
                params={
                    'stopPrice': trade.stop_loss,
                    'reduceOnly': True
                }
            )
            # Take profit (market order)
            target_order = self.exchange.create_order(
                symbol=trade.pair,
                type='take_profit_market',
                side=target_side,
                amount=trade.position_size,
                params={
                    'stopPrice': trade.target,
                    'reduceOnly': True
                }
            )
            logger.info(f"Exit orders placed: SL={stop_order['id']}, TP={target_order['id']}")
            trade.status = 'filled'
            trade.stop_order_id = stop_order['id']
            trade.target_order_id = target_order['id']
        except Exception as e:
            logger.error(f"Failed to place exit orders: {e}")

    def cancel_all(self, pair: str = None):
        """Cancel all open orders (optionally for a specific pair)."""
        try:
            if pair:
                orders = self.exchange.fetch_open_orders(pair)
            else:
                orders = self.exchange.fetch_open_orders()
            for order in orders:
                self.exchange.cancel_order(order['id'], order['symbol'])
            logger.info(f"Cancelled {len(orders)} open orders")
        except Exception as e:
            logger.error(f"Cancel all failed: {e}")

    def get_account_balance(self, currency: str = 'USDT') -> float:
        """Get free balance."""
        try:
            balance = self.exchange.fetch_balance()
            return balance.get(currency, {}).get('free', 0)
        except Exception as e:
            logger.error(f"Fetch balance failed: {e}")
            return 0

    def monitor_trades(self):
        """
        Periodically check active trades and update status.
        Should be run in a separate thread/cron.
        """
        for order_id, trade in list(self.active_trades.items()):
            try:
                order = self.exchange.fetch_order(order_id, trade.pair)
                if order['status'] == 'closed':
                    # Entry filled
                    if trade.status == 'open':
                        logger.info(f"Entry filled for {trade.pair}, placing exit orders")
                        self.place_exit_orders(order_id)

                    # Check if exits filled
                    stop_filled = self._check_exit_filled(trade.stop_order_id, trade.pair) if hasattr(trade, 'stop_order_id') else False
                    target_filled = self._check_exit_filled(trade.target_order_id, trade.pair) if hasattr(trade, 'target_order_id') else False

                    if stop_filled:
                        trade.status = 'stopped'
                        logger.info(f"Trade {trade.pair} stopped")
                        del self.active_trades[order_id]
                    elif target_filled:
                        trade.status = 'targeted'
                        logger.info(f"Trade {trade.pair} target hit")
                        del self.active_trades[order_id]
            except Exception as e:
                logger.error(f"Monitor trade {order_id} error: {e}")

    def _check_exit_filled(self, order_id: str, pair: str) -> bool:
        try:
            order = self.exchange.fetch_order(order_id, pair)
            return order['status'] == 'closed'
        except:
            return False

if __name__ == '__main__':
    # Quick test (testnet)
    config = {
        'apiKey': os.getenv('BINANCE_TESTNET_API_KEY'),
        'secret': os.getenv('BINANCE_TESTNET_SECRET'),
    }
    trader = AutoTrader('binance', testnet=True, config=config)
    trader.load_markets()
    print("Markets loaded")
    balance = trader.get_account_balance()
    print(f"Balance: {balance} USDT")
