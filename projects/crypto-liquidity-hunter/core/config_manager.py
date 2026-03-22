"""
Config manager for runtime parameter updates.
Allows changing sweep detection and signal engine parameters without restart.
"""
import yaml
from pathlib import Path

CONFIG_PATH = Path("/root/.openclaw/workspace/projects/crypto-liquidity-hunter/config/pairs.yaml")

class ConfigManager:
    def __init__(self):
        self._config = self.load()
    
    def load(self):
        with open(CONFIG_PATH, 'r') as f:
            return yaml.safe_load(f)
    
    def save(self):
        with open(CONFIG_PATH, 'w') as f:
            yaml.dump(self._config, f, default_flow_style=False, sort_keys=False)
    
    def get(self, key_path, default=None):
        """Get nested config value using dot notation, e.g., 'sweep_detector.sweep_multiplier'"""
        keys = key_path.split('.')
        cur = self._config
        for k in keys:
            if isinstance(cur, dict) and k in cur:
                cur = cur[k]
            else:
                return default
        return cur
    
    def set(self, key_path, value):
        """Set nested config value."""
        keys = key_path.split('.')
        cur = self._config
        for k in keys[:-1]:
            if k not in cur or not isinstance(cur[k], dict):
                cur[k] = {}
            cur = cur[k]
        cur[keys[-1]] = value
        self.save()
        return True

# Singleton
config_mgr = ConfigManager()
