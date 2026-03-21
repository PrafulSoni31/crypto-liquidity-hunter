#!/usr/bin/env python3
"""
Shadow Trader - Telegram Alert Bridge
Delivers high-priority volatility alerts to Charlie's Telegram.
"""

import json
import os
import sys
from datetime import datetime
from typing import List, Dict, Optional

# Charlie's Telegram ID (from project context)
CHARLIE_TELEGRAM_ID = "686482312"

class TelegramBridge:
    """
    Bridges the Shadow Trader alert engine to Telegram delivery.
    Reads from alert_history.jsonl and delivers high-priority alerts.
    """
    
    def __init__(self):
        self.alert_history_file = "projects/trading-bot/logs/alert_history.jsonl"
        self.delivery_log_file = "projects/trading-bot/logs/telegram_delivered.jsonl"
        self.target_user_id = CHARLIE_TELEGRAM_ID
        self.min_alert_level = "MEDIUM"  # Only send MEDIUM and above
        
        os.makedirs(os.path.dirname(self.delivery_log_file), exist_ok=True)
    
    def get_undelivered_alerts(self) -> List[Dict]:
        """Get alerts that haven't been delivered yet"""
        if not os.path.exists(self.alert_history_file):
            return []
        
        # Get already delivered alert IDs
        delivered_ids = set()
        if os.path.exists(self.delivery_log_file):
            with open(self.delivery_log_file, 'r') as f:
                for line in f:
                    try:
                        entry = json.loads(line.strip())
                        delivered_ids.add(entry.get('alert_timestamp'))
                    except json.JSONDecodeError:
                        continue
        
        # Filter for undelivered, high-priority alerts
        level_priority = {'LOW': 1, 'MEDIUM': 2, 'HIGH': 3, 'CRITICAL': 4}
        min_priority = level_priority.get(self.min_alert_level, 2)
        
        undelivered = []
        
        with open(self.alert_history_file, 'r') as f:
            for line in f:
                try:
                    alert = json.loads(line.strip())
                    if alert['timestamp'] not in delivered_ids:
                        if level_priority.get(alert['alert_level'], 0) >= min_priority:
                            undelivered.append(alert)
                except json.JSONDecodeError:
                    continue
        
        return undelivered
    
    def format_telegram_message(self, alert: Dict) -> str:
        """Format alert for Telegram delivery"""
        return alert.get('formatted_message', self._fallback_format(alert))
    
    def _fallback_format(self, alert: Dict) -> str:
        """Fallback formatting if formatted_message is missing"""
        emoji_map = {
            'LOW': 'ℹ️',
            'MEDIUM': '⚠️',
            'HIGH': '🚨',
            'CRITICAL': '🔥'
        }
        
        emoji = emoji_map.get(alert['alert_level'], '📊')
        timestamp_str = datetime.fromtimestamp(alert['timestamp']).strftime('%H:%M UTC')
        
        return f"""{emoji} **SHIVA ALERT: {alert['asset']}**

**Level:** {alert['alert_level']}
**Time:** {timestamp_str}

**Price Action:** {alert['price_change_pct']:+.2f}%
**Sentiment Score:** {alert['sentiment_score']:.2f}/1.0
**Volume Spike:** {'YES' if alert.get('volume_spike') else 'No'}

**Triggers:**
{alert.get('trigger_reason', 'Market anomaly detected')}

_The Shadow Trader has spoken. 🎃_"""
    
    def mark_as_delivered(self, alert: Dict):
        """Mark an alert as delivered"""
        delivery_entry = {
            'alert_timestamp': alert['timestamp'],
            'delivered_at': datetime.now().timestamp(),
            'target_user': self.target_user_id,
            'alert_level': alert['alert_level'],
            'asset': alert['asset']
        }
        
        with open(self.delivery_log_file, 'a') as f:
            f.write(json.dumps(delivery_entry) + '\n')
    
    def send_pending_alerts(self) -> int:
        """
        Process and 'send' pending alerts.
        In production, this would use the OpenClaw message tool.
        For now, we simulate delivery and log to file.
        """
        alerts = self.get_undelivered_alerts()
        sent_count = 0
        
        for alert in alerts:
            message = self.format_telegram_message(alert)
            
            # In production, this would be:
            # message.send(to=CHARLIE_TELEGRAM_ID, content=message)
            
            # For now, log to console and mark delivered
            print(f"\n{'='*60}")
            print(f"📤 SIMULATED TELEGRAM DELIVERY")
            print(f"To: Charlie (ID: {self.target_user_id})")
            print(f"{'='*60}")
            print(message)
            print(f"{'='*60}\n")
            
            self.mark_as_delivered(alert)
            sent_count += 1
        
        return sent_count
    
    def get_delivery_stats(self) -> Dict:
        """Get delivery statistics"""
        stats = {
            'total_delivered': 0,
            'by_level': {'CRITICAL': 0, 'HIGH': 0, 'MEDIUM': 0, 'LOW': 0},
            'last_delivery': None
        }
        
        if not os.path.exists(self.delivery_log_file):
            return stats
        
        with open(self.delivery_log_file, 'r') as f:
            for line in f:
                try:
                    entry = json.loads(line.strip())
                    stats['total_delivered'] += 1
                    level = entry.get('alert_level', 'LOW')
                    if level in stats['by_level']:
                        stats['by_level'][level] += 1
                    
                    if not stats['last_delivery'] or entry['delivered_at'] > stats['last_delivery']:
                        stats['last_delivery'] = entry['delivered_at']
                except json.JSONDecodeError:
                    continue
        
        return stats


def main():
    """Run the Telegram bridge"""
    print("🎃 Shadow Trader - Telegram Alert Bridge\n")
    print(f"Target User: Charlie (ID: {CHARLIE_TELEGRAM_ID})")
    print(f"Minimum Alert Level: MEDIUM\n")
    
    bridge = TelegramBridge()
    
    # Check for pending alerts
    pending = bridge.get_undelivered_alerts()
    print(f"📥 Pending alerts to deliver: {len(pending)}")
    
    if pending:
        print(f"\n🚀 Delivering {len(pending)} alert(s)...\n")
        sent = bridge.send_pending_alerts()
        print(f"✅ Delivered {sent} alert(s)")
    else:
        print("\n✅ No pending alerts. All caught up!")
    
    # Show stats
    stats = bridge.get_delivery_stats()
    print(f"\n📊 Delivery Statistics:")
    print(f"   Total Alerts Delivered: {stats['total_delivered']}")
    print(f"   Critical: {stats['by_level']['CRITICAL']}")
    print(f"   High: {stats['by_level']['HIGH']}")
    print(f"   Medium: {stats['by_level']['MEDIUM']}")
    
    if stats['last_delivery']:
        last_time = datetime.fromtimestamp(stats['last_delivery']).strftime('%H:%M:%S UTC')
        print(f"   Last Delivery: {last_time}")


if __name__ == "__main__":
    main()
