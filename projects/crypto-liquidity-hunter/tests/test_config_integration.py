"""
Tests for config integration — all settings must be read and used correctly.
"""
import pytest
import yaml


class TestConfigIntegration:
    def test_config_has_all_required_keys(self):
        """All dashboard-configurable settings must exist in pairs.yaml."""
        with open('config/pairs.yaml') as f:
            cfg = yaml.safe_load(f)

        # Signal execution
        se = cfg.get('signal_execution', {})
        assert 'mode' in se
        assert 'auto_execute' in se
        assert 'entry_tolerance_pct' in se
        assert 'min_sl_gap_pct' in se
        assert 'sl_tp_mode' in se
        assert 'sl_tp_delay_sec' in se
        assert 'monitor_interval_sec' in se

        # Signal engine
        sig = cfg.get('signal_engine', {})
        assert 'min_risk_reward' in sig
        assert 'stop_buffer_pct' in sig
        assert 'retracement_levels' in sig
        assert 'require_confluence' in sig

        # Alerts
        assert cfg['alerts']['telegram']['min_confidence'] >= 0
        assert cfg['alerts']['telegram']['min_confidence'] <= 1.0

        # Live trading
        lt = cfg.get('live_trading', {})
        assert 'fixed_notional_usd' in lt
        assert 'margin_leverage' in lt

    def test_stop_buffer_not_too_large(self):
        """stop_buffer_pct > 1% causes all R:R to be exactly 3.0 (fallback)."""
        with open('config/pairs.yaml') as f:
            cfg = yaml.safe_load(f)
        buf = cfg['signal_engine']['stop_buffer_pct']
        assert buf <= 0.01, f"stop_buffer_pct={buf} is too large (>1%). Should be 0.001-0.005"

    def test_equal_touch_tolerance_not_too_wide(self):
        """equal_touch_tolerance > 1% merges entire price ranges into one zone."""
        with open('config/pairs.yaml') as f:
            cfg = yaml.safe_load(f)
        tol = cfg['liquidity_mapper']['equal_touch_tolerance']
        assert tol <= 0.01, f"equal_touch_tolerance={tol} too wide. Should be ≤0.01"

    def test_min_confidence_above_50pct(self):
        """min_confidence should be at least 50% to avoid garbage signals."""
        with open('config/pairs.yaml') as f:
            cfg = yaml.safe_load(f)
        mc = cfg['alerts']['telegram']['min_confidence']
        assert mc >= 0.5, f"min_confidence={mc} too low"

    def test_sl_tp_mode_valid(self):
        """sl_tp_mode must be 'binance_bracket' or 'monitor_only'."""
        with open('config/pairs.yaml') as f:
            cfg = yaml.safe_load(f)
        mode = cfg['signal_execution']['sl_tp_mode']
        assert mode in ('binance_bracket', 'monitor_only'), f"Invalid sl_tp_mode: {mode}"

    def test_sl_tp_delay_reasonable(self):
        """sl_tp_delay_sec should be 1-60 seconds."""
        with open('config/pairs.yaml') as f:
            cfg = yaml.safe_load(f)
        delay = cfg['signal_execution']['sl_tp_delay_sec']
        assert 1 <= delay <= 60, f"sl_tp_delay_sec={delay} out of range"

    def test_notional_not_leveraged(self):
        """fixed_notional_usd should be the actual trade size, not leveraged value."""
        with open('config/pairs.yaml') as f:
            cfg = yaml.safe_load(f)
        n = cfg['live_trading']['fixed_notional_usd']
        m = cfg['live_trading'].get('max_notional_usd', 500)
        assert n <= m, f"notional {n} exceeds max {m}"
        assert n <= 100, f"notional={n} — check if accidentally leveraged"
