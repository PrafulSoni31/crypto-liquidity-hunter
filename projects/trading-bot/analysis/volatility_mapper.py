#!/usr/bin/env python3
"""
Shadow Trader - Volatility Trigger Mapper
Correlates sentiment signals with actual price movements to generate high-probability alerts.
"""

import json
import os
from datetime import datetime, timedelta
from dataclasses import dataclass, asdict
from typing import List, Dict, Optional
import sqlite3

@dataclass
class PriceSnapshot:
    asset: str
    price: float
    timestamp: float
    volume_24h: Optional[float] = None
    change_pct_24h: Optional[float] = None

@dataclass
class VolatilitySignal:
    asset: str
    sentiment_score: float
    price_change_pct: float
    volume_spike: bool
    alert_level: str  # 'LOW', 'MEDIUM', 'HIGH', 'CRITICAL'
    timestamp: float
    trigger_reason: str
    # Trade levels
    entry_price: Optional[float] = None
    stop_loss: Optional[float] = None
    target_price: Optional[float] = None
    risk_reward_ratio: Optional[str] = None
    direction: Optional[str] = None  # 'LONG' or 'SHORT'

class VolatilityMapper:
    def __init__(self, db_path: str = "projects/trading-bot/data/volatility.db"):
        self.db_path = db_path
        self.sentiment_threshold = 0.7  # High sentiment deviation
        self.volume_threshold = 1.5     # 1.5x average volume
        self.price_threshold = 0.5      # 0.5% price move
        self._init_db()
    
    def _init_db(self):
        """Initialize SQLite database for historical correlation data"""
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS price_data (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                asset TEXT NOT NULL,
                price REAL NOT NULL,
                timestamp REAL NOT NULL,
                volume_24h REAL,
                change_pct_24h REAL,
                UNIQUE(asset, timestamp)
            )
        ''')
        
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS volatility_alerts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                asset TEXT NOT NULL,
                sentiment_score REAL NOT NULL,
                price_change_pct REAL NOT NULL,
                volume_spike INTEGER,
                alert_level TEXT NOT NULL,
                timestamp REAL NOT NULL,
                trigger_reason TEXT,
                validated INTEGER DEFAULT 0
            )
        ''')
        
        conn.commit()
        conn.close()
    
    def store_price_snapshot(self, snapshot: PriceSnapshot):
        """Store a price snapshot for correlation analysis"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        cursor.execute('''
            INSERT OR REPLACE INTO price_data 
            (asset, price, timestamp, volume_24h, change_pct_24h)
            VALUES (?, ?, ?, ?, ?)
        ''', (snapshot.asset, snapshot.price, snapshot.timestamp,
              snapshot.volume_24h, snapshot.change_pct_24h))
        
        conn.commit()
        conn.close()
    
    def get_recent_sentiment_spikes(self, hours: int = 1) -> List[Dict]:
        """Analyze raw_feeds.jsonl for recent sentiment anomalies"""
        log_path = "logs/raw_feeds.jsonl"
        spikes = []
        
        if not os.path.exists(log_path):
            return spikes
        
        cutoff_time = datetime.now().timestamp() - (hours * 3600)
        
        with open(log_path, 'r') as f:
            for line in f:
                try:
                    entry = json.loads(line.strip())
                    if entry.get('timestamp', 0) > cutoff_time:
                        # Check for high-impact keywords in headlines
                        if 'headlines' in entry:
                            # Get assets from asset_keywords or legacy assets field
                            asset_keywords = entry.get('asset_keywords', {})
                            assets = list(asset_keywords.keys()) if asset_keywords else entry.get('assets', [])
                            
                            for headline in entry['headlines']:
                                impact_score = self._calculate_impact_score(headline)
                                if impact_score > self.sentiment_threshold:
                                    spikes.append({
                                        'timestamp': entry['timestamp'],
                                        'headline': headline,
                                        'impact_score': impact_score,
                                        'assets': assets,
                                        'source': entry.get('source', 'unknown')
                                    })
                except json.JSONDecodeError:
                    continue
        
        return spikes
    
    def _calculate_impact_score(self, headline: str) -> float:
        """Calculate impact score based on headline keywords"""
        headline_lower = headline.lower()
        
        # High-impact indicators - commodity specific
        critical_keywords = [
            'eia', 'inventory', 'surge', 'plunge', 'crash', 'spike',
            'tariff', 'sanctions', 'war', 'attack', 'disruption',
            'opec', 'production cut', 'supply shock', 'risk premium',
            'shortage', 'boost', 'cut', 'hike', 'output', 'stockpile',
            'iran', 'saudi', 'russia', 'china', 'trade war'
        ]
        
        # Medium-impact indicators
        medium_keywords = [
            'rise', 'fall', 'demand', 'supply', 'export', 'import',
            'pivot', 'shift', 'deal', 'agreement', 'oil', 'gas',
            'gold', 'coal', 'nuclear', 'battery', 'electricity', 'power'
        ]
        
        score = 0.0
        for keyword in critical_keywords:
            if keyword in headline_lower:
                score += 0.3
        
        for keyword in medium_keywords:
            if keyword in headline_lower:
                score += 0.15
        
        return min(score, 1.0)  # Cap at 1.0
    
    def check_volume_spike(self, asset: str, current_volume: float) -> bool:
        """Check if current volume represents a spike vs 7-day average"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        # Get 7-day average volume
        week_ago = datetime.now().timestamp() - (7 * 24 * 3600)
        cursor.execute('''
            SELECT AVG(volume_24h) FROM price_data 
            WHERE asset = ? AND timestamp > ? AND volume_24h IS NOT NULL
        ''', (asset, week_ago))
        
        result = cursor.fetchone()
        conn.close()
        
        if result and result[0] and current_volume:
            avg_volume = result[0]
            return current_volume > (avg_volume * self.volume_threshold)
        
        return False
    
    def generate_volatility_signal(self, asset: str, 
                                   sentiment_score: float,
                                   current_price: float,
                                   previous_price: float,
                                   current_volume: float) -> Optional[VolatilitySignal]:
        """Generate a volatility signal if thresholds are met"""
        
        price_change_pct = ((current_price - previous_price) / previous_price) * 100
        volume_spike = self.check_volume_spike(asset, current_volume)
        
        # Determine alert level
        alert_level = 'LOW'
        trigger_reasons = []
        
        if sentiment_score > 0.8:
            alert_level = 'HIGH'
            trigger_reasons.append(f"Extreme sentiment deviation ({sentiment_score:.2f})")
        elif sentiment_score > self.sentiment_threshold:
            alert_level = 'MEDIUM'
            trigger_reasons.append(f"Sentiment spike detected ({sentiment_score:.2f})")
        
        if abs(price_change_pct) > 2.0:
            alert_level = 'CRITICAL'
            trigger_reasons.append(f"Major price move ({price_change_pct:+.2f}%)")
        elif abs(price_change_pct) > self.price_threshold:
            if alert_level == 'LOW':
                alert_level = 'MEDIUM'
            trigger_reasons.append(f"Price movement ({price_change_pct:+.2f}%)")
        
        if volume_spike:
            if alert_level in ['LOW', 'MEDIUM']:
                alert_level = 'HIGH'
            trigger_reasons.append("Volume spike detected")
        
        if alert_level == 'LOW':
            return None
        
        # Calculate trade levels based on direction
        trade_levels = self._calculate_trade_levels(
            asset=asset,
            current_price=current_price,
            price_change_pct=price_change_pct,
            sentiment_score=sentiment_score
        )
        
        signal = VolatilitySignal(
            asset=asset,
            sentiment_score=sentiment_score,
            price_change_pct=price_change_pct,
            volume_spike=volume_spike,
            alert_level=alert_level,
            timestamp=datetime.now().timestamp(),
            trigger_reason=" | ".join(trigger_reasons),
            entry_price=trade_levels['entry'],
            stop_loss=trade_levels['stop_loss'],
            target_price=trade_levels['target'],
            risk_reward_ratio=trade_levels['rr'],
            direction=trade_levels['direction']
        )
        
        # Store the alert
        self._store_alert(signal)
        
        return signal
    
    def _calculate_trade_levels(self, asset: str, current_price: float, 
                               price_change_pct: float, sentiment_score: float) -> Dict:
        """
        Calculate entry, stop loss, and target levels based on market conditions.
        Uses ATR-style calculation for stop loss placement.
        """
        # Determine direction based on sentiment and price action
        if sentiment_score > 0.6 and price_change_pct < 0:
            # Strong bearish sentiment + price drop = SHORT opportunity
            direction = 'SHORT'
            # For shorts: entry is current, target is lower
            atr_factor = 0.02 * current_price  # 2% ATR approximation
            target = current_price * 0.97  # 3% target
            stop_loss = current_price * 1.025  # 2.5% stop
        elif sentiment_score > 0.6 and price_change_pct > 0:
            # Strong bullish sentiment + price rise = LONG opportunity
            direction = 'LONG'
            atr_factor = 0.02 * current_price
            target = current_price * 1.03  # 3% target
            stop_loss = current_price * 0.975  # 2.5% stop
        elif price_change_pct < -1.5:
            # Major drop but unclear sentiment = LONG (dip buy)
            direction = 'LONG'
            target = current_price * 1.025
            stop_loss = current_price * 0.97
        elif price_change_pct > 1.5:
            # Major rise = SHORT (take profit)
            direction = 'SHORT'
            target = current_price * 0.975
            stop_loss = current_price * 1.03
        else:
            # Neutral - no clear direction
            direction = 'WAIT'
            target = None
            stop_loss = None
        
        # Calculate Risk:Reward ratio
        if direction != 'WAIT' and target and stop_loss:
            if direction == 'LONG':
                risk = current_price - stop_loss
                reward = target - current_price
            else:
                risk = stop_loss - current_price
                reward = current_price - target
            
            rr = reward / risk if risk > 0 else 0
            rr_ratio = f"1:{rr:.1f}"
        else:
            rr_ratio = "N/A"
        
        return {
            'entry': current_price,
            'stop_loss': stop_loss,
            'target': target,
            'rr': rr_ratio,
            'direction': direction
        }
    
    def _store_alert(self, signal: VolatilitySignal):
        """Store volatility alert to database"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        cursor.execute('''
            INSERT INTO volatility_alerts 
            (asset, sentiment_score, price_change_pct, volume_spike, alert_level, 
             timestamp, trigger_reason)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        ''', (signal.asset, signal.sentiment_score, signal.price_change_pct,
              int(signal.volume_spike), signal.alert_level, 
              signal.timestamp, signal.trigger_reason))
        
        conn.commit()
        conn.close()
    
    def get_recent_alerts(self, hours: int = 24) -> List[Dict]:
        """Get recent volatility alerts"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        cutoff_time = datetime.now().timestamp() - (hours * 3600)
        
        cursor.execute('''
            SELECT asset, sentiment_score, price_change_pct, volume_spike,
                   alert_level, timestamp, trigger_reason
            FROM volatility_alerts
            WHERE timestamp > ?
            ORDER BY timestamp DESC
        ''', (cutoff_time,))
        
        alerts = []
        for row in cursor.fetchall():
            alerts.append({
                'asset': row[0],
                'sentiment_score': row[1],
                'price_change_pct': row[2],
                'volume_spike': bool(row[3]),
                'alert_level': row[4],
                'timestamp': row[5],
                'trigger_reason': row[6]
            })
        
        conn.close()
        return alerts
    
    def format_alert_message(self, signal: VolatilitySignal) -> str:
        """Format a volatility signal for Telegram/Discord delivery"""
        emoji_map = {
            'LOW': 'ℹ️',
            'MEDIUM': '⚠️',
            'HIGH': '🚨',
            'CRITICAL': '🔥'
        }
        
        emoji = emoji_map.get(signal.alert_level, '📊')
        timestamp_str = datetime.fromtimestamp(signal.timestamp).strftime('%H:%M UTC')
        
        # Trade levels section
        direction_emoji = "🟢" if signal.direction == "LONG" else "🔴" if signal.direction == "SHORT" else "⏸️"
        
        trade_levels = ""
        if signal.direction and signal.direction != 'WAIT':
            trade_levels = f"""
🎯 **TRADE SETUP**
{direction_emoji} **Direction:** {signal.direction}
📊 **Entry:** ${signal.entry_price:.2f}
🛡️ **Stop Loss:** ${signal.stop_loss:.2f}
🎯 **Target:** ${signal.target_price:.2f}
📈 **Risk:Reward:** {signal.risk_reward_ratio}
"""
        else:
            trade_levels = f"""
🎯 **TRADE SETUP**
⏸️ **Direction:** WAIT (No clear setup)
"""
        
        message = f"""{emoji} **SHIVA ALERT: {signal.asset}**

**Level:** {signal.alert_level}
**Time:** {timestamp_str}

**Price Action:** {signal.price_change_pct:+.2f}%
**Sentiment Score:** {signal.sentiment_score:.2f}/1.0
**Volume Spike:** {'YES' if signal.volume_spike else 'No'}

**Triggers:**
{signal.trigger_reason}
{trade_levels}_The Shadow Trader has spoken. 🎃_"""
        
        return message


def demo_volatility_analysis():
    """Demonstrate the volatility mapping system"""
    print("🎃 Shadow Trader - Volatility Mapper Demo\n")
    
    mapper = VolatilityMapper()
    
    # Simulate today's EIA 16M barrel build scenario
    print("Simulating EIA 16M barrel build scenario...")
    
    signal = mapper.generate_volatility_signal(
        asset="WTI Oil",
        sentiment_score=0.95,  # Extreme bearish sentiment from EIA report
        current_price=70.50,
        previous_price=72.00,
        current_volume=2500000  # High volume
    )
    
    if signal:
        print(f"\n✅ VOLATILITY SIGNAL GENERATED!")
        print(f"Alert Level: {signal.alert_level}")
        print(f"Price Change: {signal.price_change_pct:+.2f}%")
        print(f"\nFormatted Alert:")
        print("-" * 50)
        print(mapper.format_alert_message(signal))
    else:
        print("No significant volatility signal detected.")
    
    # Show recent sentiment spikes from logs
    print("\n\nAnalyzing recent sentiment spikes from raw feeds...")
    spikes = mapper.get_recent_sentiment_spikes(hours=12)
    print(f"Found {len(spikes)} high-impact headlines in last 12 hours")


if __name__ == "__main__":
    demo_volatility_analysis()
