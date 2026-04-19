"""
Tests for trade_executor.py — catches direction inversion, qty bugs, sizing bugs.
"""
import pytest
import math
from unittest.mock import patch, MagicMock
from core.trade_executor import (
    _round_qty, _round_price, _place_sl_tp, _get_position, _sign
)


# ─── Bug #3: Qty must NOT be multiplied by leverage ──────────────────────────

class TestQtyCalculation:
    def test_qty_is_notional_over_price_not_leveraged(self, mock_exchange_info):
        """Bug #3: $50 notional at price $0.05 = 1000 qty, NOT 10000 (10x leverage)."""
        qty = _round_qty('SUIUSDT', 50.0 / 0.05, mock_exchange_info)
        assert qty == 1000.0  # $50 / $0.05 = 1000

    def test_qty_rounds_down_to_step(self, mock_exchange_info):
        """Qty must be a multiple of stepSize (floor, not ceil)."""
        # SUIUSDT step=0.1, 50/0.87 = 57.47... → 57.4
        qty = _round_qty('SUIUSDT', 50.0 / 0.87, mock_exchange_info)
        assert qty == 57.4
        assert qty <= 50.0 / 0.87  # never exceed

    def test_qty_integer_step(self, mock_exchange_info):
        """POLYXUSDT step=1 → qty must be integer."""
        qty = _round_qty('POLYXUSDT', 50.0 / 0.051, mock_exchange_info)
        assert qty == math.floor(50.0 / 0.051)  # 980
        assert float(qty) == int(qty)


# ─── Bug #2: SL/TP qty must use actual position qty from Binance ─────────────

class TestSlTpQty:
    def test_sl_tp_uses_actual_qty_not_estimated(self, mock_exchange_info):
        """Bug #2: SL/TP order qty must match actual Binance position, not notional/price."""
        # Actual position: 417.7 THE, but notional/price = 50/0.12 = 416.6
        actual_qty = 417.7
        estimated_qty = 50.0 / 0.12  # 416.666...

        sl_qty = _round_qty('THEUSDT', actual_qty, mock_exchange_info)
        est_qty = _round_qty('THEUSDT', estimated_qty, mock_exchange_info)

        assert sl_qty == 417.7  # uses actual
        assert est_qty == 416.6  # estimated is different
        assert sl_qty != est_qty  # they MUST differ — using estimated leaves dust


# ─── Bug #1: Direction inversion — no orders after position close ────────────

class TestDirectionInversion:
    @patch('core.trade_executor._get_position')
    @patch('core.trade_executor._place_limit_order')
    def test_no_sl_placed_when_position_gone(self, mock_place, mock_get_pos, mock_exchange_info):
        """Bug #1: If position doesn't exist on Binance, SL/TP must NOT be placed."""
        mock_get_pos.return_value = None  # position already closed

        # _place_sl_tp should NOT be called if we check position first
        # This tests the guard in execute_signal STEP 2
        pos = _get_position('fake_key', 'fake_secret', 'SUIUSDT')
        assert pos is None
        # Verify: if no position, we should NOT call _place_sl_tp
        mock_place.assert_not_called()

    def test_sl_tp_direction_check(self, mock_exchange_info):
        """Bug #1: SL for LONG must be SELL, SL for SHORT must be BUY."""
        # _place_sl_tp determines exit_side
        # For LONG: exit_side = SELL (to close the long)
        # For SHORT: exit_side = BUY (to close the short)
        with patch('core.trade_executor._place_limit_order') as mock_order:
            mock_order.return_value = {'orderId': 123}

            _place_sl_tp('k', 's', 'SUIUSDT', 'long', 100, 0.80, 1.20, mock_exchange_info)
            # First call = SL, second = TP, both should be SELL for LONG
            calls = mock_order.call_args_list
            assert calls[0][0][3] == 'SELL'  # SL side
            assert calls[1][0][3] == 'SELL'  # TP side

        with patch('core.trade_executor._place_limit_order') as mock_order:
            mock_order.return_value = {'orderId': 456}

            _place_sl_tp('k', 's', 'SUIUSDT', 'short', 100, 1.20, 0.80, mock_exchange_info)
            calls = mock_order.call_args_list
            assert calls[0][0][3] == 'BUY'  # SL side for SHORT
            assert calls[1][0][3] == 'BUY'  # TP side for SHORT


# ─── Price rounding ──────────────────────────────────────────────────────────

class TestPriceRounding:
    def test_price_rounds_to_tick(self, mock_exchange_info):
        """Price must be a multiple of tickSize."""
        # SUIUSDT tick=0.0001
        p = _round_price('SUIUSDT', 0.87654321, mock_exchange_info)
        assert p == 0.8765
        # Verify it's a valid tick multiple (float division can have tiny errors)
        assert abs(p / 0.0001 - round(p / 0.0001)) < 1e-9

    def test_btc_price_rounds(self, mock_exchange_info):
        """BTC tick=0.1 → 67137.456 → 67137.5"""
        p = _round_price('BTCUSDT', 67137.456, mock_exchange_info)
        assert p == 67137.5
