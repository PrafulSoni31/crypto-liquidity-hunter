"""
Config manager — single source of truth for all settings and their defaults.

Every setting has ONE canonical default here. No more scattered hardcoded
fallbacks across main.py, dashboard/app.py, trade_executor.py, etc.

Usage:
    from core.config_manager import cfg
    notional = cfg.get('live_trading.fixed_notional_usd')  # uses default if missing
    min_gap  = cfg.get('signal_execution.min_sl_gap_pct')
"""
import yaml
from pathlib import Path

CONFIG_PATH = Path("/root/.openclaw/workspace/projects/crypto-liquidity-hunter/config/pairs.yaml")

# ─── SINGLE SOURCE OF TRUTH FOR ALL DEFAULTS ─────────────────────────────────
# If a key is missing from pairs.yaml, this value is used everywhere.
# Update here ONLY — never hardcode a default anywhere else in the codebase.
DEFAULTS = {
    # Signal execution
    'signal_execution.mode':                'auto',
    'signal_execution.auto_execute':        True,
    'signal_execution.entry_tolerance_pct': 0.3,
    'signal_execution.min_sl_gap_pct':      0.5,   # 0.5% min gap between entry and SL
    'signal_execution.sl_tp_mode':          'monitor_only',
    'signal_execution.sl_tp_delay_sec':     3,
    'signal_execution.monitor_interval_sec':5,

    # Signal engine
    'signal_engine.min_risk_reward':        3.0,
    'signal_engine.risk_per_trade':         0.01,
    'signal_engine.stop_buffer_pct':        0.001,
    'signal_engine.target_buffer_pct':      0.001,
    'signal_engine.require_confluence':     True,
    'signal_engine.retracement_levels':     [0.5, 0.618, 0.786],

    # Alerts
    'alerts.telegram.min_confidence':       0.7,
    'alerts.telegram.enabled':              True,

    # Live trading
    'live_trading.fixed_notional_usd':      50.0,
    'live_trading.margin_leverage':         10.0,
    'live_trading.commission_per_trade':    0.001,
    'live_trading.position_sizing':         'fixed_notional',
    'live_trading.max_notional_usd':        50.0,
    'live_trading.risk_percent':            1.0,

    # Paper trading
    'paper_trading.fixed_notional_usd':     20.0,
    'paper_trading.margin_leverage':        20.0,
    'paper_trading.commission_per_trade':   0.001,
    'paper_trading.position_sizing':        'fixed_notional',

    # Liquidity mapper
    'liquidity_mapper.equal_touch_tolerance': 0.005,
    'liquidity_mapper.swing_lookback':       5,
    'liquidity_mapper.round_tolerance':      0.005,
    'liquidity_mapper.min_swing_strength':   2,

    # Sweep detector
    'sweep_detector.sweep_multiplier':       0.5,
    'sweep_detector.volume_multiplier':      2.5,
    'sweep_detector.wick_ratio':             0.4,
    'sweep_detector.min_sweep_pct':          0.2,
    'sweep_detector.confirmation_bars':      5,
    'sweep_detector.min_body_ratio':         0.4,
    'sweep_detector.lookback_bars':          24,

    # Data fetch
    'data_fetch.ohlcv_limit':               300,
    'data_fetch.atr_period':                14,

    # Backtester
    'backtester.max_concurrent_trades':     3,
    'backtester.commission_pct':            0.001,
    'backtester.slippage_pct':              0.0005,

    # Cron
    'cron.scan_interval_minutes':           5,
}


class ConfigManager:
    """
    Reads pairs.yaml, falls back to DEFAULTS for missing keys.
    Single place to add/change any default — no more scattered hardcodes.
    """

    def __init__(self):
        self._config = self.load()

    def load(self):
        try:
            with open(CONFIG_PATH, 'r') as f:
                return yaml.safe_load(f) or {}
        except Exception:
            return {}

    def save(self):
        # Safety: refuse to write config if pairs list is missing
        if 'pairs' not in self._config or not self._config['pairs']:
            import logging
            logging.getLogger(__name__).error(
                "SAFETY BLOCK: ConfigManager.save() — config missing pairs, refusing to write")
            return
        with open(CONFIG_PATH, 'w') as f:
            yaml.dump(self._config, f, default_flow_style=False, sort_keys=False)

    def get(self, key_path: str, default=None):
        """
        Get nested config value using dot notation.
        Falls back to DEFAULTS, then to `default` param.
        e.g.: cfg.get('signal_execution.min_sl_gap_pct') → 0.5
        """
        keys = key_path.split('.')
        cur = self._config
        for k in keys:
            if isinstance(cur, dict) and k in cur:
                cur = cur[k]
            else:
                # Not found in file — use DEFAULTS
                return DEFAULTS.get(key_path, default)
        return cur

    def set(self, key_path: str, value):
        """Set nested config value and save to file."""
        keys = key_path.split('.')
        cur = self._config
        for k in keys[:-1]:
            if k not in cur or not isinstance(cur[k], dict):
                cur[k] = {}
            cur = cur[k]
        cur[keys[-1]] = value
        self.save()
        # Reload so subsequent gets reflect the change
        self._config = self.load()
        return True

    def reload(self):
        """Force reload from disk (call after external config changes)."""
        self._config = self.load()

    def get_section(self, section: str) -> dict:
        """Get a whole config section as dict, with defaults filled in."""
        raw = self._config.get(section, {})
        # Fill in any missing keys from DEFAULTS
        result = dict(raw)
        prefix = section + '.'
        for key, val in DEFAULTS.items():
            if key.startswith(prefix):
                sub_key = key[len(prefix):]
                if sub_key not in result:
                    result[sub_key] = val
        return result


# ── Global singleton — import and use anywhere ────────────────────────────────
cfg = ConfigManager()
