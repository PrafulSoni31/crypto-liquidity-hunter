#!/usr/bin/env python3
"""
Shadow Trader - Alert Engine
Main integration module that combines sentiment analysis, price feeds, and volatility mapping
to generate high-probability trading alerts.
"""

import sys
import json
import os
from datetime import datetime
from typing import List, Dict, Optional

# Import our modules
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from volatility_mapper import VolatilityMapper, VolatilitySignal, PriceSnapshot
from price_fetcher import PriceFeedFetcher, PriceData

class ShadowTraderAlertEngine:
    """
    Main alert engine that processes news sentiment, tracks price movements,
    and generates high-probability volatility alerts.
    """
    
    def __init__(self):
        self.volatility_mapper = VolatilityMapper()
        self.price_fetcher = PriceFeedFetcher()
        self.alert_history_file = "projects/trading-bot/logs/alert_history.jsonl"
        os.makedirs(os.path.dirname(self.alert_history_file), exist_ok=True)
    
    def process_news_cycle(self) -> List[VolatilitySignal]:
        """
        Main processing cycle:
        1. Fetch latest prices
        2. Analyze sentiment from news feeds
        3. Generate volatility signals
        4. Store and return alerts
        """
        alerts = []
        
        # Step 1: Get current prices
        current_prices = self.price_fetcher.get_all_prices()
        
        # Step 2: Store price snapshots
        for symbol, price_data in current_prices.items():
            if price_data:
                snapshot = PriceSnapshot(
                    asset=price_data.asset,
                    price=price_data.price,
                    timestamp=price_data.timestamp,
                    volume_24h=None,  # Would come from API
                    change_pct_24h=price_data.change_pct_24h
                )
                self.volatility_mapper.store_price_snapshot(snapshot)
        
        # Step 3: Check for sentiment spikes
        sentiment_spikes = self.volatility_mapper.get_recent_sentiment_spikes(hours=1)
        
        # Step 4: Correlate sentiment with price action
        for spike in sentiment_spikes:
            for asset in spike.get('assets', []):
                symbol_map = {
                    'Gold': 'XAUUSD',
                    'Oil': 'USOIL'
                }
                symbol = symbol_map.get(asset)
                
                if symbol and symbol in current_prices:
                    price_data = current_prices[symbol]
                    
                    # Get previous price (simulated - would query DB)
                    previous_price = price_data.price * (1 - (price_data.change_pct_24h / 100))
                    
                    # Generate signal
                    signal = self.volatility_mapper.generate_volatility_signal(
                        asset=asset,
                        sentiment_score=spike['impact_score'],
                        current_price=price_data.price,
                        previous_price=previous_price,
                        current_volume=1000000  # Placeholder
                    )
                    
                    if signal:
                        alerts.append(signal)
                        self._log_alert(signal)
        
        return alerts
    
    def _log_alert(self, signal: VolatilitySignal):
        """Log alert to file for Telegram bridge pickup"""
        alert_entry = {
            'timestamp': signal.timestamp,
            'asset': signal.asset,
            'alert_level': signal.alert_level,
            'sentiment_score': signal.sentiment_score,
            'price_change_pct': signal.price_change_pct,
            'volume_spike': signal.volume_spike,
            'trigger_reason': signal.trigger_reason,
            'formatted_message': self.volatility_mapper.format_alert_message(signal)
        }
        
        with open(self.alert_history_file, 'a') as f:
            f.write(json.dumps(alert_entry) + '\n')
    
    def get_pending_alerts(self, min_level: str = 'MEDIUM') -> List[Dict]:
        """Get pending alerts for delivery"""
        level_priority = {'LOW': 1, 'MEDIUM': 2, 'HIGH': 3, 'CRITICAL': 4}
        min_priority = level_priority.get(min_level, 2)
        
        pending = []
        
        if not os.path.exists(self.alert_history_file):
            return pending
        
        # Get alerts from last hour
        cutoff = datetime.now().timestamp() - 3600
        
        with open(self.alert_history_file, 'r') as f:
            for line in f:
                try:
                    alert = json.loads(line.strip())
                    if alert['timestamp'] > cutoff:
                        if level_priority.get(alert['alert_level'], 0) >= min_priority:
                            pending.append(alert)
                except json.JSONDecodeError:
                    continue
        
        return pending
    
    def generate_daily_summary(self) -> str:
        """Generate end-of-day trading summary"""
        alerts = self.volatility_mapper.get_recent_alerts(hours=24)
        
        critical = sum(1 for a in alerts if a['alert_level'] == 'CRITICAL')
        high = sum(1 for a in alerts if a['alert_level'] == 'HIGH')
        medium = sum(1 for a in alerts if a['alert_level'] == 'MEDIUM')
        
        summary = f"""🎃 **SHIVA DAILY BRIEF** 🎃

**Alert Activity (24h):**
🔥 Critical: {critical}
🚨 High: {high}
⚠️ Medium: {medium}

**Asset Performance:**
"""
        
        # Add price snapshot
        prices = self.price_fetcher.get_all_prices()
        for symbol, data in prices.items():
            if data:
                emoji = "📈" if data.change_pct_24h > 0 else "📉"
                summary += f"{emoji} {data.asset}: ${data.price:.2f} ({data.change_pct_24h:+.2f}%)\n"
        
        summary += f"\n_The Shadow Trader watches. The Shadow Trader reports. 🎃_"
        
        return summary


def main():
    """Run the alert engine"""
    print("🎃 Shadow Trader - Alert Engine Test\n")
    
    engine = ShadowTraderAlertEngine()
    
    print("Running news cycle analysis...\n")
    alerts = engine.process_news_cycle()
    
    if alerts:
        print(f"✅ Generated {len(alerts)} volatility signal(s):\n")
        for alert in alerts:
            print(engine.volatility_mapper.format_alert_message(alert))
            print("-" * 50)
    else:
        print("⚠️ No high-probability signals detected in this cycle.\n")
    
    print("\nGenerating price snapshot...\n")
    print(engine.price_fetcher.format_price_report())
    
    print("\nPending alerts for delivery:")
    pending = engine.get_pending_alerts()
    print(f"  Found {len(pending)} alert(s) ready for Telegram bridge")


if __name__ == "__main__":
    main()
