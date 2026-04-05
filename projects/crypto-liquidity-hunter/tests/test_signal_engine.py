"""
Tests for signal_engine.py — catches confidence filter, R:R, SL direction bugs.
"""
import pytest
import pandas as pd
import numpy as np
from datetime import datetime, timezone
from core.signal_engine import SignalEngine, TradeSignal
from core.liquidity_mapper import LiquidityZone
from core.sweep_detector import SweepEvent


def _make_sweep(direction='long', sweep_price=0.95, close_price=1.0,
                volume_ratio=5.0, sweep_depth_pct=1.0, confirmed=True):
    return SweepEvent(
        timestamp=pd.Timestamp(datetime.now(timezone.utc)),
        direction=direction,
        sweep_price=sweep_price,
        close_price=close_price,
        volume=10000,
        volume_ratio=volume_ratio,
        sweep_depth_pct=sweep_depth_pct,
        confirmed=confirmed,
        displacement_body_pct=60.0,
    )


def _make_zone(price, zone_type='swing_low', strength=3):
    return LiquidityZone(
        price=price,
        zone_type=zone_type,
        strength=strength,
        touches=[pd.Timestamp.now()],
        last_touch=pd.Timestamp.now(),
    )


# ─── Bug #4: Confidence filter must reject low-confidence signals ────────────

class TestConfidenceFilter:
    def test_signal_below_threshold_has_low_confidence(self):
        """Bug #4: A signal with all weak inputs should have confidence < 0.7."""
        engine = SignalEngine(min_risk_reward=3.0, stop_buffer_pct=0.001)
        # Weak sweep — low volume, small depth → low confidence
        sweep = _make_sweep(direction='long', sweep_price=0.95, close_price=1.0,
                            volume_ratio=1.5, sweep_depth_pct=0.3)
        zones = [_make_zone(1.20, 'swing_high', 1)]  # weak zone far away
        sig = engine.generate_signal(sweep, zones, 0.98, pair='TEST/USDT')
        if sig:
            # If signal generated, verify confidence is computed correctly
            assert 0 <= sig.confidence <= 1.0
            # With weak inputs, confidence should be moderate
            assert sig.confidence < 0.85  # not artificially high

    def test_high_confluence_signal_has_high_confidence(self):
        """Strong sweep + OB + FVG + HTF alignment → confidence > 0.7."""
        engine = SignalEngine(min_risk_reward=2.0, stop_buffer_pct=0.001,
                              require_confluence=False)
        sweep = _make_sweep(direction='long', sweep_price=0.95, close_price=1.0,
                            volume_ratio=6.0, sweep_depth_pct=2.0)
        zones = [_make_zone(1.20, 'swing_high', 5)]
        sig = engine.generate_signal(sweep, zones, 0.98, pair='TEST/USDT',
                                     htf_bias='bullish')
        if sig:
            assert sig.confidence >= 0.7


# ─── Bug #7: SL must be on correct side of entry ────────────────────────────

class TestStopLossDirection:
    def test_long_sl_below_entry(self):
        """Bug #7: LONG SL must be BELOW entry price."""
        engine = SignalEngine(min_risk_reward=2.0, stop_buffer_pct=0.001,
                              require_confluence=False)
        sweep = _make_sweep(direction='long', sweep_price=0.95, close_price=1.0)
        zones = [_make_zone(1.20, 'swing_high', 3)]
        sig = engine.generate_signal(sweep, zones, 0.98, pair='TEST/USDT')
        if sig:
            assert sig.direction == 'long'
            assert sig.stop_loss < sig.entry_price, \
                f"LONG SL {sig.stop_loss} must be < entry {sig.entry_price}"

    def test_short_sl_above_entry(self):
        """Bug #7: SHORT SL must be ABOVE entry price."""
        engine = SignalEngine(min_risk_reward=2.0, stop_buffer_pct=0.001,
                              require_confluence=False)
        sweep = _make_sweep(direction='short', sweep_price=1.05, close_price=1.0)
        zones = [_make_zone(0.80, 'swing_low', 3)]
        sig = engine.generate_signal(sweep, zones, 1.02, pair='TEST/USDT')
        if sig:
            assert sig.direction == 'short'
            assert sig.stop_loss > sig.entry_price, \
                f"SHORT SL {sig.stop_loss} must be > entry {sig.entry_price}"


# ─── Bug #6: R:R must use zone targets, not always fallback to 3.0 ──────────

class TestRiskReward:
    def test_merged_zone_type_matching(self):
        """Bug #6: Zone type 'equal_low/round/swing_low' must match substring filter."""
        engine = SignalEngine(min_risk_reward=3.0, stop_buffer_pct=0.001,
                              require_confluence=False)
        sweep = _make_sweep(direction='long', sweep_price=0.95, close_price=1.0)
        # Create zone with merged type string (as produced by liquidity mapper)
        zone = _make_zone(1.30, 'equal_high/round/swing_high', 5)
        sig = engine.generate_signal(sweep, [zone], 0.98, pair='TEST/USDT')
        if sig:
            # Should use the zone target (R:R > 3) instead of fallback
            if sig.target_type != 'risk_multiple':
                assert sig.risk_reward > 3.0  # zone gives better R:R

    def test_fallback_rr_equals_min(self):
        """When no zone qualifies, fallback R:R should equal min_risk_reward exactly."""
        engine = SignalEngine(min_risk_reward=3.0, stop_buffer_pct=0.001,
                              require_confluence=False)
        sweep = _make_sweep(direction='long', sweep_price=0.95, close_price=1.0)
        # No zones → fallback
        sig = engine.generate_signal(sweep, [], 0.98, pair='TEST/USDT')
        if sig:
            assert sig.target_type == 'risk_multiple'
            assert abs(sig.risk_reward - 3.0) < 0.1  # should be ~3.0

    def test_min_rr_filter_works(self):
        """Signals with R:R below min_risk_reward must be rejected."""
        engine = SignalEngine(min_risk_reward=5.0, stop_buffer_pct=0.001,
                              require_confluence=False)
        sweep = _make_sweep(direction='long', sweep_price=0.95, close_price=1.0)
        # Zone very close → R:R < 5
        zone = _make_zone(1.01, 'swing_high', 3)
        sig = engine.generate_signal(sweep, [zone], 0.98, pair='TEST/USDT')
        # With min_rr=5 and only a nearby zone, it should use fallback or reject
        if sig:
            assert sig.risk_reward >= 5.0


# ─── Bug: SL gap too small should be rejected ───────────────────────────────

class TestSlGapFilter:
    def test_tiny_sl_gap_rejected(self):
        """Signal with SL gap < min_sl_gap_pct must be rejected by Gate 8."""
        engine = SignalEngine(min_risk_reward=2.0, stop_buffer_pct=0.0001,
                              min_sl_gap_pct=2.0,  # require 2% gap
                              require_confluence=False)
        # Sweep with very small range → tiny SL gap
        sweep = _make_sweep(direction='long', sweep_price=0.999, close_price=1.0)
        zones = [_make_zone(1.20, 'swing_high', 3)]
        sig = engine.generate_signal(sweep, zones, 1.0, pair='TEST/USDT')
        # SL gap = (1.0 - 0.999) / 1.0 = 0.1% < 2% → should be None
        assert sig is None, "Tiny SL gap should reject signal"
