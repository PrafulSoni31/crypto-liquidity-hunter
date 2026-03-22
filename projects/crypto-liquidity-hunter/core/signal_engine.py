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
    position_size: float  # in base currency (e.g., BTC)
    risk_amount: float  # in quote currency (e.g., USDT) - potential loss
    notional_usd: float  # total position value at entry (position_size * entry_price)
    margin_required_usd: float  # notional / leverage
    commission_estimated_usd: float  # commission on entry (and maybe exit)
    confidence: float  # 0-1 score
    zone_strength: int  # strength of target zone
    sweep: Dict
    notes: str = ''

class SignalEngine:
    def __init__(self,
                 risk_per_trade: float = 0.01,        # 1% of capital per trade
                 retracement_levels: List[float] = None,  # fib levels for entry
                 stop_buffer_pct: float = 0.001,      # 0.1% beyond sweep extreme
                 min_risk_reward: float = 1.5,
                 target_buffer_pct: float = 0.001,    # buffer before zone
                 position_sizing: str = 'risk_percent',  # 'risk_percent' or 'fixed_notional'
                 fixed_notional_usd: float = 50.0,
                 margin_leverage: float = 1.0,
                 commission_pct: float = 0.001):
        """
        Parameters:
        - risk_per_trade: fraction of capital to risk per trade (used if position_sizing='risk_percent')
        - retracement_levels: Fib retracement levels for entry
        - stop_buffer_pct: extra buffer beyond sweep extreme for stop placement
        - min_risk_reward: minimum R:R to take trade
        - target_buffer_pct: buffer before hitting zone exactly (helps with takers)
        - position_sizing: 'risk_percent' (default) or 'fixed_notional'
        - fixed_notional_usd: if using fixed_notional, the target notional value in USD
        - margin_leverage: margin multiplier (e.g., 20 for 1:20)
        - commission_pct: commission rate per trade (for P&L calculation)
        """
        self.risk_per_trade = risk_per_trade
        self.retracement_levels = retracement_levels or [0.5, 0.618, 0.786]
        self.stop_buffer = stop_buffer_pct
        self.min_risk_reward = min_risk_reward
        self.target_buffer = target_buffer_pct
        self.position_sizing = position_sizing
        self.fixed_notional_usd = fixed_notional_usd
        self.margin_leverage = margin_leverage
        self.commission_pct = commission_pct

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

        direction = sweep.direction
        sweep_price = sweep.sweep_price
        sweep_range = abs(sweep.close_price - sweep_price)

        # 1. Determine entry zone (retracement)
        entry_price = None
        for level in self.retracement_levels:
            if direction == 'long':
                candidate = sweep.close_price + (sweep_price - sweep.close_price) * level
                if candidate > current_price:
                    entry_price = candidate
                    break
            else:
                candidate = sweep.close_price - (sweep_price - sweep.close_price) * level
                if candidate < current_price:
                    entry_price = candidate
                    break

        if entry_price is None:
            if direction == 'long':
                entry_price = sweep.close_price + 0.618 * (sweep_price - sweep.close_price)
            else:
                entry_price = sweep.close_price - 0.618 * (sweep_price - sweep.close_price)

        # 2. Stop loss placement
        if direction == 'long':
            stop_loss = sweep_price * (1 - self.stop_buffer)
            risk_per_unit = entry_price - stop_loss
        else:
            stop_loss = sweep_price * (1 + self.stop_buffer)
            risk_per_unit = stop_loss - entry_price

        # Reject if risk is too small (near-zero) — prevents huge positions and calculation errors
        min_risk = entry_price * 0.0001  # 0.01% of entry price
        if risk_per_unit < min_risk:
            logger.debug(f"Risk per unit too small: {risk_per_unit:.6f} < {min_risk:.6f}, rejecting")
            return None

        if risk_per_unit <= 0:
            logger.warning(f"Invalid risk calc: entry={entry_price} stop={stop_loss} direction={direction}")
            return None

        # 3. Find target zones
        if direction == 'long':
            target_zones = [z for z in liquidity_zones if z.price > entry_price and
                            z.zone_type in ('equal_high', 'swing_high', 'round')]
            target_zones.sort(key=lambda z: z.price)
        else:
            target_zones = [z for z in liquidity_zones if z.price < entry_price and
                            z.zone_type in ('equal_low', 'swing_low', 'round')]
            target_zones.sort(key=lambda z: z.price, reverse=True)

        target_price = None
        target_type = 'risk_multiple'
        zone_strength = 0

        if target_zones:
            nearest_zone = target_zones[0]
            target_price = nearest_zone.price
            target_type = 'liquidity_zone'
            zone_strength = nearest_zone.strength
        else:
            if direction == 'long':
                target_price = entry_price + 2 * risk_per_unit
            else:
                target_price = entry_price - 2 * risk_per_unit

        # 4. Validate target direction
        if direction == 'long' and target_price <= entry_price:
            logger.debug(f"Target {target_price} not above entry {entry_price}, rejecting")
            return None
        if direction == 'short' and target_price >= entry_price:
            logger.debug(f"Target {target_price} not below entry {entry_price}, rejecting")
            return None

        # 5. Risk-reward ratio
        if direction == 'long':
            reward = target_price - entry_price
        else:
            reward = entry_price - target_price
        risk_reward = reward / risk_per_unit

        if risk_reward < self.min_risk_reward:
            logger.info(f"Signal rejected: R:R too low {risk_reward:.2f} < {self.min_risk_reward}")
            return None

        # 6. Position sizing based on mode
        if self.position_sizing == 'fixed_notional':
            # Fixed notional value in USD (e.g., $50) regardless of risk
            notional_usd = self.fixed_notional_usd
            position_size = notional_usd / entry_price
            risk_amount = risk_per_unit * position_size
            margin_required_usd = notional_usd / self.margin_leverage
            commission_estimated_usd = notional_usd * self.commission_pct
        else:  # risk_percent (default)
            risk_amount = capital * self.risk_per_trade
            position_size = risk_amount / risk_per_unit
            notional_usd = position_size * entry_price
            margin_required_usd = notional_usd / self.margin_leverage if self.margin_leverage > 1 else notional_usd
            commission_estimated_usd = notional_usd * self.commission_pct

        # 7. Confidence score
        confidence = self._calculate_confidence(sweep, zone_strength, len(liquidity_zones))

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
            notional_usd=notional_usd,
            margin_required_usd=margin_required_usd,
            commission_estimated_usd=commission_estimated_usd,
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
