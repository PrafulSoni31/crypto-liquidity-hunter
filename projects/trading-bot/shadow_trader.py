#!/usr/bin/env python3
"""
Shadow Trader - Main Runner
Entry point for the full trading intelligence system.
Called by cron job every hour to process news, generate alerts, and deliver to Telegram.
"""

import sys
import os

# Add paths for imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'analysis'))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'alerts'))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'scrapers'))

from alert_engine import ShadowTraderAlertEngine
from telegram_bridge import TelegramBridge

def main():
    """Main Shadow Trader execution cycle"""
    print("🎃 SHADOW TRADER - MAIN EXECUTION CYCLE 🎃")
    print("=" * 60)
    print(f"Timestamp: {__import__('datetime').datetime.now().strftime('%Y-%m-%d %H:%M:%S UTC')}")
    print("=" * 60)
    
    # Step 1: Run scraper (already done by cron, but we verify)
    print("\n[1/4] Verifying data streams...")
    log_file = "logs/raw_feeds.jsonl"
    if os.path.exists(log_file):
        with open(log_file, 'r') as f:
            lines = sum(1 for _ in f)
        print(f"      ✓ Raw feeds active: {lines} entries")
    else:
        print(f"      ⚠ No raw feed data found")
    
    # Step 2: Process alerts
    print("\n[2/4] Processing volatility signals...")
    engine = ShadowTraderAlertEngine()
    alerts = engine.process_news_cycle()
    
    if alerts:
        print(f"      ✓ Generated {len(alerts)} volatility signal(s)")
        for alert in alerts:
            print(f"        - {alert.asset}: {alert.alert_level}")
    else:
        print(f"      ℹ No high-probability signals detected")
    
    # Step 3: Deliver to Telegram
    print("\n[3/4] Delivering alerts to Telegram...")
    bridge = TelegramBridge()
    sent = bridge.send_pending_alerts()
    
    if sent > 0:
        print(f"      ✓ Delivered {sent} alert(s) to Charlie")
    else:
        print(f"      ℹ No pending alerts to deliver")
    
    # Step 4: Status report
    print("\n[4/4] System status...")
    stats = bridge.get_delivery_stats()
    print(f"      Total alerts delivered (all time): {stats['total_delivered']}")
    print(f"      Critical: {stats['by_level']['CRITICAL']} | High: {stats['by_level']['HIGH']} | Medium: {stats['by_level']['MEDIUM']}")
    
    print("\n" + "=" * 60)
    print("🎃 CYCLE COMPLETE - The Shadow Trader watches. 🎃")
    print("=" * 60)


if __name__ == "__main__":
    main()
