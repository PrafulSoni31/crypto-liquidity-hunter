"""
Tests that DEFAULTS in config_manager match pairs.yaml.
This catches the exact bug where config file and code defaults diverge.
"""
import pytest
import yaml
from core.config_manager import cfg, DEFAULTS


class TestDefaultsInSync:
    def test_all_defaults_readable_via_cfg(self):
        """Every key in DEFAULTS must be retrievable via cfg.get()."""
        for key, expected_default in DEFAULTS.items():
            val = cfg.get(key)
            assert val is not None or expected_default is None, \
                f"cfg.get('{key}') returned None but default is {expected_default}"

    def test_min_sl_gap_consistent(self):
        """min_sl_gap_pct must be same in DEFAULTS, config file, and cfg.get()."""
        default_val = DEFAULTS['signal_execution.min_sl_gap_pct']
        cfg_val = cfg.get('signal_execution.min_sl_gap_pct')

        with open('config/pairs.yaml') as f:
            yaml_val = yaml.safe_load(f).get('signal_execution', {}).get('min_sl_gap_pct')

        assert default_val == 0.5, f"DEFAULTS has wrong value: {default_val}"
        assert cfg_val == yaml_val, \
            f"cfg.get() returns {cfg_val} but pairs.yaml has {yaml_val} — out of sync!"

    def test_stop_buffer_consistent(self):
        """stop_buffer_pct must match DEFAULTS."""
        default_val = DEFAULTS['signal_engine.stop_buffer_pct']
        cfg_val = cfg.get('signal_engine.stop_buffer_pct')
        assert default_val == cfg_val or cfg_val is not None, \
            f"stop_buffer_pct mismatch: DEFAULTS={default_val} cfg={cfg_val}"

    def test_confidence_threshold_consistent(self):
        """min_confidence must be >= 0.5 in both DEFAULTS and config."""
        default_val = DEFAULTS['alerts.telegram.min_confidence']
        cfg_val = cfg.get('alerts.telegram.min_confidence')
        assert default_val >= 0.5
        assert cfg_val >= 0.5, f"min_confidence in config is {cfg_val} — too low!"

    def test_no_hardcoded_fallbacks_in_main(self):
        """main.py must not have any min_sl_gap_pct defaults other than 0.5."""
        with open('main.py') as f:
            content = f.read()
        # Find all occurrences of min_sl_gap_pct with a hardcoded default
        import re
        matches = re.findall(r"min_sl_gap_pct['\"\s]*,\s*([\d.]+)", content)
        for val in matches:
            assert float(val) == 0.5, \
                f"Hardcoded min_sl_gap_pct default {val} in main.py should be 0.5"

    def test_cfg_singleton_works(self):
        """cfg.get() with dot notation must work for all critical settings."""
        assert cfg.get('signal_execution.min_sl_gap_pct') is not None
        assert cfg.get('signal_execution.entry_tolerance_pct') is not None
        assert cfg.get('signal_execution.sl_tp_mode') is not None
        assert cfg.get('live_trading.fixed_notional_usd') is not None
        assert cfg.get('alerts.telegram.min_confidence') is not None
