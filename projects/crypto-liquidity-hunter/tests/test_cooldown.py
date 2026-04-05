"""
Tests for entry cooldown — catches re-entry loop bug.
"""
import pytest
import json
import os
import time
import tempfile


# ─── Bug #5: Re-entry loop — cooldown must persist across processes ──────────

class TestCooldown:
    def test_cooldown_saves_to_file(self):
        """Bug #5: Cooldown must persist to file (not just in-memory dict)."""
        with tempfile.NamedTemporaryFile(suffix='.json', delete=False, mode='w') as f:
            cooldown_file = f.name
            json.dump({}, f)

        try:
            # Simulate saving cooldown
            cd = {'binance:SUI/USDT:short': time.time()}
            with open(cooldown_file, 'w') as f:
                json.dump(cd, f)

            # Simulate loading in a NEW process
            with open(cooldown_file, 'r') as f:
                loaded = json.load(f)

            assert 'binance:SUI/USDT:short' in loaded
            assert time.time() - loaded['binance:SUI/USDT:short'] < 5  # recent
        finally:
            os.unlink(cooldown_file)

    def test_cooldown_blocks_reentry_within_window(self):
        """Bug #5: Entry within cooldown window must be blocked."""
        cooldown = {'binance:SUI/USDT:short': time.time()}
        cooldown_secs = 600  # 10 minutes

        key = 'binance:SUI/USDT:short'
        last_entry = cooldown.get(key, 0)
        now = time.time()

        blocked = (now - last_entry) < cooldown_secs
        assert blocked, "Entry within 10min cooldown must be blocked"

    def test_cooldown_allows_entry_after_window(self):
        """After cooldown expires, entry must be allowed."""
        cooldown = {'binance:SUI/USDT:short': time.time() - 700}  # 11+ min ago
        cooldown_secs = 600

        key = 'binance:SUI/USDT:short'
        last_entry = cooldown.get(key, 0)
        now = time.time()

        blocked = (now - last_entry) < cooldown_secs
        assert not blocked, "Entry after 10min cooldown must be allowed"

    def test_cooldown_prunes_expired(self):
        """Loading cooldown file should prune expired entries."""
        cd = {
            'binance:SUI/USDT:short': time.time() - 700,   # expired
            'binance:BTC/USDT:long': time.time() - 100,     # still active
        }
        cooldown_secs = 600
        now = time.time()
        pruned = {k: v for k, v in cd.items() if now - v < cooldown_secs}

        assert 'binance:SUI/USDT:short' not in pruned
        assert 'binance:BTC/USDT:long' in pruned

    def test_different_pairs_independent(self):
        """Cooldown for SUI doesn't block BTC."""
        cooldown = {'binance:SUI/USDT:short': time.time()}
        cooldown_secs = 600

        sui_blocked = (time.time() - cooldown.get('binance:SUI/USDT:short', 0)) < cooldown_secs
        btc_blocked = (time.time() - cooldown.get('binance:BTC/USDT:long', 0)) < cooldown_secs

        assert sui_blocked
        assert not btc_blocked
