"""
Tests for position_monitor.py — catches orphan cleanup, close qty, direction bugs.
"""
import pytest
from unittest.mock import MagicMock, patch, call
from datetime import datetime, timezone
from core.position_monitor import PositionMonitor


def _make_trade(id=1, pair='binance:SUI/USDT', direction='long',
                entry_price=1.0, sl=0.95, tp=1.15,
                notional_usd=50, commission_usd=0.05,
                entry_time=None, account_id=2):
    return {
        'id': id, 'pair': pair, 'direction': direction,
        'entry_price': entry_price, 'sl': sl, 'tp': tp,
        'notional_usd': notional_usd, 'commission_usd': commission_usd,
        'entry_time': (entry_time or datetime.now(timezone.utc)).isoformat(),
        'account_id': account_id, 'mode': 'live', 'status': 'open',
    }


# ─── Bug #8: Orphaned orders must be cancelled when position closes ──────────

class TestOrphanedOrders:
    def test_cancel_orphaned_orders_called_on_close(self):
        """Bug #8: When monitor detects position closed, it must cancel all orders."""
        conn = MagicMock()
        conn.mode = 'live'
        conn.api_key = 'k'
        conn.api_secret = 's'
        store = MagicMock()

        monitor = PositionMonitor(conn, store, account_id=2, interval=60)

        # Mock the inline requests import used inside _cancel_orphaned_orders
        import requests as real_requests
        mock_get = MagicMock(return_value=MagicMock(
            json=MagicMock(return_value=[
                {'symbol': 'SUIUSDT', 'orderId': 1, 'side': 'SELL', 'price': '0.95',
                 'origQty': '100', 'type': 'LIMIT', 'reduceOnly': False}
            ])
        ))
        mock_delete = MagicMock(return_value=MagicMock(
            json=MagicMock(return_value={'code': 200})
        ))

        with patch.dict('sys.modules', {}):
            with patch('requests.get', mock_get), patch('requests.delete', mock_delete):
                monitor._cancel_orphaned_orders('SUIUSDT')

        # Verify DELETE was called to cancel orders
        assert mock_delete.called, "Must cancel orphaned orders"


# ─── Bug #2: Market close must use actual Binance qty, not estimated ─────────

class TestMarketCloseQty:
    def test_close_uses_actual_position_qty(self):
        """Bug #2: _place_market_close must fetch actual qty from Binance."""
        conn = MagicMock()
        conn.mode = 'live'
        conn.api_key = 'k'
        conn.api_secret = 's'
        conn._round_qty = MagicMock(side_effect=lambda sym, qty: qty)
        conn.exchange.create_order.return_value = {'average': 0.12, 'price': 0.12}

        store = MagicMock()
        monitor = PositionMonitor(conn, store, account_id=2)
        trade = _make_trade(pair='binance:THE/USDT', direction='short',
                            entry_price=0.12, notional_usd=50)

        # Mock the inline requests.get call inside _place_market_close
        mock_resp = MagicMock()
        mock_resp.json.return_value = [
            {'symbol': 'THEUSDT', 'positionAmt': '-417.7', 'entryPrice': '0.12'}
        ]

        with patch('requests.get', return_value=mock_resp):
            result = monitor._place_market_close('THE/USDT', 'short', trade, 0.12)

        assert result is True
        # Verify the qty passed to create_order is 417.7 (actual), not 50/0.12=416.6
        create_args = conn.exchange.create_order.call_args
        qty_used = create_args[0][3]  # 4th positional arg = amount
        assert qty_used == 417.7, f"Expected actual qty 417.7, got {qty_used}"


# ─── SL/TP price-check logic ────────────────────────────────────────────────

class TestPriceCheck:
    def test_long_sl_triggers_when_price_below(self):
        """LONG: SL hit when low <= SL price."""
        conn = MagicMock()
        conn.mode = 'paper'
        store = MagicMock()
        monitor = PositionMonitor(conn, store, account_id=2)
        monitor._close_trade_now = MagicMock()

        trade = _make_trade(direction='long', entry_price=1.0, sl=0.95, tp=1.15)
        price_map = {'SUIUSDT': {'mark': 0.94, 'high': 0.96, 'low': 0.94}}

        monitor._check_trade(trade, price_map)

        # SL should have triggered (low=0.94 < sl=0.95)
        monitor._close_trade_now.assert_called_once()
        args = monitor._close_trade_now.call_args[0]
        assert args[2] == 'stop_loss'

    def test_short_sl_triggers_when_price_above(self):
        """SHORT: SL hit when high >= SL price."""
        conn = MagicMock()
        conn.mode = 'paper'
        store = MagicMock()
        monitor = PositionMonitor(conn, store, account_id=2)
        monitor._close_trade_now = MagicMock()

        trade = _make_trade(direction='short', entry_price=1.0, sl=1.05, tp=0.85)
        price_map = {'SUIUSDT': {'mark': 1.06, 'high': 1.06, 'low': 1.01}}

        monitor._check_trade(trade, price_map)

        monitor._close_trade_now.assert_called_once()
        args = monitor._close_trade_now.call_args[0]
        assert args[2] == 'stop_loss'

    def test_tp_triggers_for_long(self):
        """LONG: TP hit when high >= TP price."""
        conn = MagicMock()
        conn.mode = 'paper'
        store = MagicMock()
        monitor = PositionMonitor(conn, store, account_id=2)
        monitor._close_trade_now = MagicMock()

        trade = _make_trade(direction='long', entry_price=1.0, sl=0.95, tp=1.15)
        price_map = {'SUIUSDT': {'mark': 1.16, 'high': 1.16, 'low': 1.10}}

        monitor._check_trade(trade, price_map)

        monitor._close_trade_now.assert_called_once()
        args = monitor._close_trade_now.call_args[0]
        assert args[2] == 'target_hit'

    def test_no_trigger_when_price_between_sl_tp(self):
        """No exit when price is between SL and TP."""
        conn = MagicMock()
        conn.mode = 'paper'
        store = MagicMock()
        monitor = PositionMonitor(conn, store, account_id=2)
        monitor._close_trade_now = MagicMock()

        trade = _make_trade(direction='long', entry_price=1.0, sl=0.95, tp=1.15)
        price_map = {'SUIUSDT': {'mark': 1.05, 'high': 1.06, 'low': 0.99}}

        monitor._check_trade(trade, price_map)

        monitor._close_trade_now.assert_not_called()
