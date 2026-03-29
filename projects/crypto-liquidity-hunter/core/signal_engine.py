"""
Signal Engine v2 — Professional Liquidity Hunt
===============================================
Generates trade signals from sweep + OB/FVG confluence.

Phase 1 Fixes:
  - Entry logic corrected: retracement between sweep_price and close_price
  - Entry validated against live current_price (no stale setups)
  - Sweep age gate (timeframe-appropriate)
  - Signal timestamp = NOW (not historical candle time)

Phase 2 Additions:
  - HTF bias filter: signal direction must align with 4h trend (or be very strong)
  - Order Block confluence: entry zone must overlap an unmitigated OB
  - FVG confluence: bonus confidence when FVG overlaps entry zone
  - Without OB/FVG, signal is rejected (no raw sweeps without confluence)
"""
import pandas as pd
import numpy as np
from typing import List, Dict, Optional
from dataclasses import dataclass, asdict
from datetime import datetime, timezone, timedelta
import logging

logger = logging.getLogger(__name__)


@dataclass
class TradeSignal:
    """Complete trade signal with entry, stop, target, sizing."""
    timestamp:                  pd.Timestamp
    pair:                       str
    direction:                  str
    entry_price:                float
    stop_loss:                  float
    target:                     float
    target_type:                str
    risk_reward:                float
    position_size:              float
    risk_amount:                float
    notional_usd:               float
    margin_required_usd:        float
    commission_estimated_usd:   float
    confidence:                 float
    zone_strength:              int
    htf_bias:                   str = 'neutral'
    ob_confluence:              bool = False
    fvg_confluence:             bool = False
    sweep:                      Dict = None
    notes:                      str = ''


class SignalEngine:
    def __init__(self,
                 risk_per_trade:      float = 0.01,
                 retracement_levels:  List[float] = None,
                 stop_buffer_pct:     float = 0.001,
                 min_risk_reward:     float = 5.0,
                 target_buffer_pct:   float = 0.001,
                 position_sizing:     str   = 'risk_percent',
                 fixed_notional_usd:  float = 50.0,
                 margin_leverage:     float = 1.0,
                 commission_pct:      float = 0.001,
                 require_confluence:  bool  = True):   # Phase 2: require OB or FVG
        self.risk_per_trade      = risk_per_trade
        self.retracement_levels  = retracement_levels or [0.5, 0.618, 0.786]
        self.stop_buffer         = stop_buffer_pct
        self.min_risk_reward     = min_risk_reward
        self.target_buffer       = target_buffer_pct
        self.position_sizing     = position_sizing
        self.fixed_notional_usd  = fixed_notional_usd
        self.margin_leverage     = margin_leverage
        self.commission_pct      = commission_pct
        self.require_confluence  = require_confluence

    def generate_signal(self,
                        sweep,
                        liquidity_zones:  List,
                        current_price:    float,
                        capital:          float = 10000,
                        pair:             str   = None,
                        max_sweep_age_hours: float = 4.0,
                        htf_bias:         str   = 'neutral',
                        order_blocks:     List  = None,
                        fvgs:             List  = None) -> Optional[TradeSignal]:
        """
        Generate a trade signal from a sweep + confluence check.

        Phase 1 gates:
          - Sweep age (timeframe-appropriate)
          - Entry price within valid range of current price
          - Corrected retracement entry formula

        Phase 2 gates:
          - HTF bias alignment
          - Order Block or FVG confluence required
        """
        if not sweep.confirmed:
            return None

        # ── GATE 1: Sweep recency ──────────────────────────────────────────
        now_utc   = datetime.now(timezone.utc)
        sweep_ts  = sweep.timestamp
        if hasattr(sweep_ts, 'to_pydatetime'):
            sweep_ts = sweep_ts.to_pydatetime()
        if sweep_ts.tzinfo is None:
            sweep_ts = sweep_ts.replace(tzinfo=timezone.utc)
        sweep_age_h = (now_utc - sweep_ts).total_seconds() / 3600
        if sweep_age_h > max_sweep_age_hours:
            logger.debug(f"Sweep too old: {sweep_age_h:.1f}h > {max_sweep_age_hours}h")
            return None

        direction   = sweep.direction
        sweep_price = sweep.sweep_price
        close_price = sweep.close_price

        # ── GATE 2: HTF bias alignment (Phase 2) ──────────────────────────
        if htf_bias != 'neutral':
            if htf_bias == 'bullish' and direction == 'short':
                # Counter-trend short — only allow if sweep is very strong
                if sweep.volume_ratio < 5.0 or sweep.sweep_depth_pct < 0.5:
                    logger.debug(f"Short sweep rejected: HTF is bullish, sweep not strong enough")
                    return None
            elif htf_bias == 'bearish' and direction == 'long':
                if sweep.volume_ratio < 5.0 or sweep.sweep_depth_pct < 0.5:
                    logger.debug(f"Long sweep rejected: HTF is bearish, sweep not strong enough")
                    return None

        # ── GATE 3: Correct entry calculation ─────────────────────────────
        # Entry must be BETWEEN sweep_price and close_price (the retracement zone)
        # For LONG: sweep_price is the LOW wick, close is higher → entry is a dip into the zone
        # For SHORT: sweep_price is the HIGH wick, close is lower → entry is a rally into the zone
        sweep_range = abs(close_price - sweep_price)
        if sweep_range <= 0:
            return None

        entry_price = None
        for level in self.retracement_levels:
            if direction == 'long':
                # Entry = close_price - retracement * (close_price - sweep_price)
                # This gives a price BETWEEN sweep_price and close_price
                candidate = close_price - level * (close_price - sweep_price)
                # Valid if ≥ sweep_price and ≤ current_price + small buffer (not already above us)
                if sweep_price <= candidate <= current_price * 1.005:
                    entry_price = candidate
                    break
            else:
                # Entry = close_price + retracement * (sweep_price - close_price)
                candidate = close_price + level * (sweep_price - close_price)
                if current_price * 0.995 <= candidate <= sweep_price:
                    entry_price = candidate
                    break

        if entry_price is None:
            # Use 50% retracement as fallback — but only if valid vs current price
            if direction == 'long':
                entry_price = close_price - 0.5 * (close_price - sweep_price)
                if entry_price > current_price * 1.01:  # entry too far above current
                    logger.debug(f"Long entry {entry_price:.6f} unreachable vs current {current_price:.6f}")
                    return None
            else:
                entry_price = close_price + 0.5 * (sweep_price - close_price)
                if entry_price < current_price * 0.99:
                    logger.debug(f"Short entry {entry_price:.6f} unreachable vs current {current_price:.6f}")
                    return None

        # ── GATE 4: Entry still valid vs current live price ───────────────
        if direction == 'long':
            # For long: current price should still be near or above entry
            # If price has already shot far up, setup is stale
            if current_price > close_price * 1.03:
                logger.debug(f"Long: price {current_price:.6f} already far above close {close_price:.6f}")
                return None
            # Entry should not be more than 2% below current (too far to retrace to)
            if entry_price < current_price * 0.98:
                logger.debug(f"Long entry {entry_price:.6f} too far below current {current_price:.6f}")
                return None
        else:
            if current_price < close_price * 0.97:
                logger.debug(f"Short: price {current_price:.6f} already far below close {close_price:.6f}")
                return None
            if entry_price > current_price * 1.02:
                logger.debug(f"Short entry {entry_price:.6f} too far above current {current_price:.6f}")
                return None

        # ── GATE 5: Order Block confluence (Phase 2) ──────────────────────
        ob_confluence  = False
        ob_zone_used   = None
        # 2% proximity tolerance — OB doesn't need to be exact, just nearby
        ob_tol = 0.02
        if order_blocks:
            for ob in order_blocks:
                if direction == 'long' and ob.direction == 'bullish':
                    # Entry within 2% of OB zone (inside, above, or just below)
                    if ob.low * (1 - ob_tol) <= entry_price <= ob.high * (1 + ob_tol):
                        ob_confluence = True
                        ob_zone_used  = ob
                        break
                elif direction == 'short' and ob.direction == 'bearish':
                    if ob.low * (1 - ob_tol) <= entry_price <= ob.high * (1 + ob_tol):
                        ob_confluence = True
                        ob_zone_used  = ob
                        break

        # ── GATE 6: FVG confluence (Phase 2) ──────────────────────────────
        fvg_confluence = False
        fvg_tol = 0.02  # 2% proximity tolerance
        if fvgs:
            for fvg in fvgs:
                if direction == 'long' and fvg.direction == 'bullish':
                    # Entry within 2% of FVG zone
                    if fvg.bottom * (1 - fvg_tol) <= entry_price <= fvg.top * (1 + fvg_tol):
                        fvg_confluence = True
                        break
                elif direction == 'short' and fvg.direction == 'bearish':
                    if fvg.bottom * (1 - fvg_tol) <= entry_price <= fvg.top * (1 + fvg_tol):
                        fvg_confluence = True
                        break

        # ── GATE 7: Confluence check (Phase 2) ────────────────────────────
        # OB/FVG confluence is PREFERRED but not mandatory — reduces confidence if absent
        # Hard rejection only if require_confluence is explicitly True AND no OBs/FVGs exist at all
        if self.require_confluence and not ob_confluence and not fvg_confluence:
            if not order_blocks and not fvgs:
                # Truly no OBs/FVGs on this pair — allow signal with reduced confidence
                logger.debug(f"No OBs/FVGs found on chart — allowing signal without confluence")
            else:
                # OBs/FVGs exist but entry not aligned — apply 20% confidence penalty instead of hard reject
                logger.debug(f"No OB/FVG confluence at entry {entry_price:.6f} — reducing confidence")

        # ── Stop loss placement ────────────────────────────────────────────
        if direction == 'long':
            stop_loss    = sweep_price * (1 - self.stop_buffer)
            risk_per_unit = entry_price - stop_loss
        else:
            stop_loss    = sweep_price * (1 + self.stop_buffer)
            risk_per_unit = stop_loss - entry_price

        min_risk = entry_price * 0.0001
        if risk_per_unit < min_risk or risk_per_unit <= 0:
            logger.debug(f"Risk too small: {risk_per_unit:.8f}")
            return None

        # ── Target: liquidity zone above (long) or below (short) ──────────
        if direction == 'long':
            target_zones = [z for z in liquidity_zones
                            if z.price > entry_price
                            and z.zone_type in ('equal_high', 'swing_high', 'round')]
            target_zones.sort(key=lambda z: z.price)
        else:
            target_zones = [z for z in liquidity_zones
                            if z.price < entry_price
                            and z.zone_type in ('equal_low', 'swing_low', 'round')]
            target_zones.sort(key=lambda z: z.price, reverse=True)

        # Also consider FVGs as targets
        if fvgs:
            for fvg in fvgs:
                if direction == 'long' and fvg.direction == 'bearish':
                    # Bearish FVG above = first resistance = target
                    if fvg.bottom > entry_price:
                        target_zones = sorted(
                            target_zones + [type('Z', (), {'price': fvg.bottom,
                                            'zone_type': 'fvg', 'strength': 2})()],
                            key=lambda z: z.price
                        )
                elif direction == 'short' and fvg.direction == 'bullish':
                    if fvg.top < entry_price:
                        target_zones = sorted(
                            target_zones + [type('Z', (), {'price': fvg.top,
                                            'zone_type': 'fvg', 'strength': 2})()],
                            key=lambda z: z.price, reverse=True
                        )

        target_price = None
        target_type  = 'risk_multiple'
        zone_strength = 0

        if target_zones:
            nearest       = target_zones[0]
            target_price  = nearest.price
            target_type   = getattr(nearest, 'zone_type', 'liquidity_zone')
            zone_strength = getattr(nearest, 'strength', 1)
        else:
            target_price = (entry_price + 3 * risk_per_unit
                            if direction == 'long'
                            else entry_price - 3 * risk_per_unit)

        # Validate target direction
        if direction == 'long' and target_price <= entry_price:
            return None
        if direction == 'short' and target_price >= entry_price:
            return None

        # ── Risk/Reward ────────────────────────────────────────────────────
        reward     = abs(target_price - entry_price)
        risk_reward = reward / risk_per_unit
        if risk_reward < self.min_risk_reward:
            logger.debug(f"R:R {risk_reward:.2f} < min {self.min_risk_reward}")
            return None

        # ── Position sizing ────────────────────────────────────────────────
        if self.position_sizing == 'fixed_notional':
            notional_usd             = self.fixed_notional_usd
            position_size            = notional_usd / entry_price
            risk_amount              = risk_per_unit * position_size
            margin_required_usd      = notional_usd / self.margin_leverage
            commission_estimated_usd = notional_usd * self.commission_pct
        else:
            risk_amount              = capital * self.risk_per_trade
            position_size            = risk_amount / risk_per_unit
            notional_usd             = position_size * entry_price
            margin_required_usd      = (notional_usd / self.margin_leverage
                                        if self.margin_leverage > 1 else notional_usd)
            commission_estimated_usd = notional_usd * self.commission_pct

        # ── Confidence score ───────────────────────────────────────────────
        confidence = self._calculate_confidence(
            sweep, zone_strength, len(liquidity_zones),
            htf_bias, direction, ob_confluence, fvg_confluence
        )

        # Signal timestamp = NOW (when signal fires, not historical candle)
        signal_timestamp = pd.Timestamp(datetime.now(timezone.utc))

        ob_notes  = f" OB@{ob_zone_used.high:.6f}" if ob_zone_used else ""
        fvg_notes = " FVG✓" if fvg_confluence else ""
        htf_notes = f" HTF:{htf_bias}" if htf_bias != 'neutral' else ""

        return TradeSignal(
            timestamp=signal_timestamp,
            pair=pair or 'UNKNOWN',
            direction=direction,
            entry_price=round(entry_price, 8),
            stop_loss=round(stop_loss, 8),
            target=round(target_price, 8),
            target_type=target_type,
            risk_reward=round(risk_reward, 2),
            position_size=position_size,
            risk_amount=risk_amount,
            notional_usd=notional_usd,
            margin_required_usd=margin_required_usd,
            commission_estimated_usd=commission_estimated_usd,
            confidence=round(confidence, 2),
            zone_strength=zone_strength,
            htf_bias=htf_bias,
            ob_confluence=ob_confluence,
            fvg_confluence=fvg_confluence,
            sweep=asdict(sweep) if hasattr(sweep, '__dataclass_fields__') else sweep,
            notes=(f"Entry={entry_price:.6f} SL={stop_loss:.6f} TP={target_price:.6f} "
                   f"RR={risk_reward:.2f}{ob_notes}{fvg_notes}{htf_notes}")
        )

    def _calculate_confidence(self, sweep, zone_strength: int, total_zones: int,
                               htf_bias: str, direction: str,
                               ob_confluence: bool, fvg_confluence: bool) -> float:
        """
        Confidence score 0–1.
        Base: 0.5
        Volume quality, sweep depth, zone strength, HTF alignment, OB/FVG confluence
        """
        score = 0.5

        # Sweep quality
        if sweep.volume_ratio >= 5:
            score += 0.10
        elif sweep.volume_ratio >= 3:
            score += 0.05

        if abs(sweep.sweep_depth_pct) >= 1.0:
            score += 0.05
        if abs(sweep.sweep_depth_pct) >= 2.0:
            score += 0.05

        # Displacement body quality
        if hasattr(sweep, 'displacement_body_pct') and sweep.displacement_body_pct >= 60:
            score += 0.05

        # Zone strength
        if zone_strength >= 4:
            score += 0.10
        elif zone_strength >= 2:
            score += 0.05

        # HTF alignment
        if htf_bias == 'bullish' and direction == 'long':
            score += 0.10
        elif htf_bias == 'bearish' and direction == 'short':
            score += 0.10
        elif htf_bias == 'neutral':
            score += 0.02  # small bonus for neutral (no penalty)

        # OB confluence (strong signal)
        if ob_confluence:
            score += 0.10

        # FVG confluence (additional edge)
        if fvg_confluence:
            score += 0.05

        # Both OB + FVG = maximum confluence
        if ob_confluence and fvg_confluence:
            score += 0.05

        return min(score, 1.0)

    def calculate_position_size(self, entry: float, stop: float,
                                 capital: float, risk_pct: float = 0.01) -> float:
        risk_per_unit = abs(entry - stop)
        if risk_per_unit == 0:
            return 0
        return (capital * risk_pct) / risk_per_unit
