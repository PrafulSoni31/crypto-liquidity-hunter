#!/usr/bin/env python3
"""
Quick test of all modules (no actual market API calls).
"""
import sys
import os

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    from core.data_fetcher import MarketDataFetcher
    print("✓ data_fetcher")
except Exception as e:
    print(f"✗ data_fetcher: {e}")

try:
    from core.liquidity_mapper import LiquidityMapper, LiquidityZone
    print("✓ liquidity_mapper")
except Exception as e:
    print(f"✗ liquidity_mapper: {e}")

try:
    from core.sweep_detector import SweepDetector, SweepEvent
    print("✓ sweep_detector")
except Exception as e:
    print(f"✗ sweep_detector: {e}")

try:
    from core.signal_engine import SignalEngine, TradeSignal
    print("✓ signal_engine")
except Exception as e:
    print(f"✗ signal_engine: {e}")

try:
    from core.backtester import Backtester, TradeResult
    print("✓ backtester")
except Exception as e:
    print(f"✗ backtester: {e}")

try:
    from alerts.telegram import TelegramAlerter, AlertDispatcher
    print("✓ alerts.telegram")
except Exception as e:
    print(f"✗ alerts.telegram: {e}")

try:
    import yaml
    print("✓ pyyaml")
except Exception as e:
    print(f"✗ pyyaml: {e}")

try:
    import ccxt
    print("✓ ccxt")
except Exception as e:
    print(f"✗ ccxt: {e}")

print("\nAll imports successful.")
