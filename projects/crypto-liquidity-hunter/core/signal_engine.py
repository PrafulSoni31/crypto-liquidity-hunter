"""
Signal Engine: Generates trade signals from sweeps + liquidity zones.
Calculates entries, stop losses, targets, and position sizes.
"""
import pandas as pd
import numpy as np
from typing import List, Dict, Optional
from dataclasses import dataclass, asdict
import logging

logger = logging.getLogger(__name__)

@dataclass
class TradeSignal:
    """Complete trade signal with entry, stop, target, sizing."""
    timestamp: pd.Timestamp
    pair: str  # trading pair (e.g., 'binance:BTC/USDT')
    direction: str  # 'long' or 'short'
    entry_price: float
    stop_loss: float
    target: float
    target_type: str  # 'liquidity_zone' or 'risk_multiple'
    risk_reward: float
    position_size: float
    risk_amount: float
    confidence: float
    zone_strength: int
    sweep: Dict
    notes: str = ''

class SignalEngine:
    def __init__(self,
                 risk_per_trade: float = 0.01,        # 1% of capital per trade
                 retracement_levels: List[float] = None,  # fib levels for entry
                 stop_buffer_pct: float = 0.001,      # 0.1% beyond sweep extreme
                 min_risk_reward: float = 1.5,
                 target_buffer_pct: float = 0.001):   # buffer before zone
        """
        Parameters:
        - risk_per_trade: fraction of capital to risk per trade
        - retracement_levels: Fib retracement levels to consider for entry (default [0.5, 0.618, 0.786])
        - stop_buffer_pct: extra buffer beyond sweep extreme for stop placement
        - min_risk_reward: minimum R:R to take trade
        - target_buffer_pct: buffer before hitting zone exactly (helps with takers)
        """
        self.risk_per_trade = risk_per_trade
        self.retracement_levels = retracement_levels or [0.5, 0.618, 0.786]
        self.stop_buffer = stop_buffer_pct
        self.min_risk_reward = min_risk_reward
        self.target_buffer = target_buffer_pct

    def generate_signal(self,
                        sweep,
                        liquidity_zones: List,
                        current_price: float,
                        capital: float = 10000,
                        pair: str = None) -> Optional[TradeSignal]:
        """
        Create a trade signal from a sweep event and available liquidity zones.
        sweep: SweepEvent object or dict
        liquidity_zones: list of LiquidityZone objects
        current_price: latest market price
        capital: total account balance in quote currency (USDT)
        pair: trading pair string (e.g., 'binance:BTC/USDT')
        Returns TradeSignal or None.
        """
        if not sweep.confirmed:
            return None

        # ... existing logic ...

        signal = TradeSignal(
            timestamp=sweep.timestamp,
            pair=pair or 'UNKNOWN',
            direction=direction,
            entry_price=entry_price,
            stop_loss=stop_loss,
            target=target_price,
            target_type=target_type,
            risk_reward=risk_reward,
            position_size=position_size,
            risk_amount=risk_amount,
            confidence=confidence,
            zone_strength=zone_strength,
            sweep=asdict(sweep) if hasattr(sweep, '__dict__') else sweep,
            notes=f"Entry={entry_price:.2f}, SL={stop_loss:.2f}, TP={target_price:.2f}, R:R={risk_reward:.2f}"
        )
        return signal

    def _calculate_confidence(self, sweep, zone_strength: int, total_zones: int) -> float:
        """
        Score 0-1 based on:
        - Sweep volume ratio (higher = more institutional)
        - Sweep depth (deeper = stronger)
        - Zone strength (more touches = better)
        - Zone confluence (multiple zones near target)
        """
        score = 0.5  # base

        # Volume: ratio > 5 = excellent, > 3 = good
        if sweep.volume_ratio >= 5:
            score += 0.2
        elif sweep.volume_ratio >= 3:
            score += 0.1

        # Sweep depth
        if abs(sweep.sweep_depth_pct) >= 1.0:
            score += 0.1
        if abs(sweep.sweep_depth_pct) >= 2.0:
            score += 0.1

        # Zone strength
        if zone_strength >= 4:
            score += 0.2
        elif zone_strength >= 2:
            score += 0.1

        # Zone confluence: if multiple zone types near target, bonus
        if total_zones >= 10:  # rich liquidity environment
            score += 0.1

        return min(score, 1.0)

    def calculate_position_size(self, entry: float, stop: float, capital: float, risk_pct: float = 0.01) -> float:
        """Calculate position size in units."""
        risk_per_unit = abs(entry - stop)
        if risk_per_unit == 0:
            return 0
        risk_amount = capital * risk_pct
        position = risk_amount / risk_per_unit
        return position

if __name__ == '__main__':
    # Mock test
    from sweep_detector import SweepEvent

    sweep = SweepEvent(
        timestamp=pd.Timestamp('2024-01-15 10:00:00'),
        direction='long',
        sweep_price=42000.0,
        close_price=42200.0,
        volume=1000,
        volume_ratio=4.5,
        sweep_depth_pct=0.5,
        confirmed=True
    )

    zones = [
        type('Zone', (), {'price': 43000.0, 'zone_type': 'equal_high', 'strength': 4})(),
        type('Zone', (), {'price': 43500.0, 'zone_type': 'swing_high', 'strength': 2})(),
    ]

    engine = SignalEngine(risk_per_trade=0.01)
    signal = engine.generate_signal(sweep, zones, 42250.0, capital=10000)
    if signal:
        print(signal)
