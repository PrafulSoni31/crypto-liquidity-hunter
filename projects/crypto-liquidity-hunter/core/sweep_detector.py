"""
Sweep Detector: Identifies liquidity sweep events.
A sweep = price spikes into a liquidity pool, grabs stops, then reverses.

Fixes applied:
- Wick ratio was inverted (long sweep needs lower wick, short needs upper wick)
- Multi-bar lookback for prior high/low (uses rolling N-bar window, not just 1 bar)
- Volume check uses rolling 20-bar average
- Confirmation: price must NOT re-visit sweep extreme in next N bars
"""
import pandas as pd
import numpy as np
from typing import List, Optional
from dataclasses import dataclass
import logging

from .liquidity_mapper import LiquidityZone

logger = logging.getLogger(__name__)


@dataclass
class SweepEvent:
    """Represents a confirmed liquidity sweep event."""
    timestamp: pd.Timestamp
    direction: str          # 'long' (swept lows, expect bounce UP) or 'short' (swept highs, expect drop DOWN)
    sweep_price: float      # extreme wick price reached
    close_price: float      # candle close price (should be inside prior range = reversal)
    volume: float
    volume_ratio: float     # volume / 20-bar avg volume
    sweep_depth_pct: float  # % move beyond prior level
    confirmed: bool = False
    confirmation_time: Optional[pd.Timestamp] = None
    notes: str = ''


class SweepDetector:
    def __init__(self,
                 sweep_multiplier: float = 0.5,      # ATR multiple for minimum sweep size
                 volume_multiplier: float = 1.2,     # volume spike threshold vs 20-bar avg
                 confirmation_bars: int = 3,         # bars to confirm rejection
                 wick_ratio: float = 0.4,            # wick must be >= X% of candle range
                 min_sweep_pct: float = 0.2,         # minimum % move beyond prior high/low
                 lookback_bars: int = 10):           # lookback to find prior high/low level
        self.sweep_multiplier = sweep_multiplier
        self.volume_multiplier = volume_multiplier
        self.confirmation_bars = confirmation_bars
        self.wick_ratio = wick_ratio
        self.min_sweep_pct = min_sweep_pct / 100.0
        self.lookback_bars = lookback_bars

    def detect_sweeps(self,
                      df: pd.DataFrame,
                      atr_series: pd.Series,
                      liquidity_zones: Optional[List] = None) -> List[SweepEvent]:
        """
        Scan DataFrame for liquidity sweep events.
        df must have: open, high, low, close, volume columns.
        atr_series: ATR values aligned with df index.
        Returns list of confirmed SweepEvents.
        """
        sweeps = []
        avg_volume = df['volume'].rolling(20).mean()

        # Rolling prior high/low over lookback_bars (exclude current bar)
        prior_high = df['high'].shift(1).rolling(self.lookback_bars).max()
        prior_low  = df['low'].shift(1).rolling(self.lookback_bars).min()

        for i in range(max(self.lookback_bars + 1, 21), len(df)):
            row       = df.iloc[i]
            atr_val   = atr_series.iloc[i]
            if pd.isna(atr_val) or atr_val <= 0:
                atr_val = row['close'] * 0.01

            p_high = prior_high.iloc[i]
            p_low  = prior_low.iloc[i]
            if pd.isna(p_high) or pd.isna(p_low):
                continue

            avg_vol = avg_volume.iloc[i]
            if pd.isna(avg_vol) or avg_vol <= 0:
                continue

            sweep_threshold = self.sweep_multiplier * atr_val
            candle_range = row['high'] - row['low']
            if candle_range <= 0:
                continue

            # ─── Volume filter ──────────────────────────────────────────────────
            volume_ok = row['volume'] >= avg_vol * self.volume_multiplier

            # ─── LONG SWEEP: wick spikes BELOW prior low ─────────────────────
            #   • row['low'] < prior_low - threshold   (price went below stops)
            #   • close > prior_low                    (price closed back inside = rejection)
            #   • lower wick = (min(open,close) - low) / range  → correct for a down-spike
            long_price_cond = (
                row['low'] < p_low - sweep_threshold and
                (p_low - row['low']) / p_low >= self.min_sweep_pct and
                row['close'] > p_low  # close must reclaim level = reversal
            )
            lower_wick_ratio = (min(row['open'], row['close']) - row['low']) / candle_range
            long_wick_ok = lower_wick_ratio >= self.wick_ratio

            if long_price_cond and volume_ok and long_wick_ok:
                sweep = SweepEvent(
                    timestamp=row.name,
                    direction='long',
                    sweep_price=row['low'],
                    close_price=row['close'],
                    volume=row['volume'],
                    volume_ratio=row['volume'] / avg_vol,
                    sweep_depth_pct=(p_low - row['low']) / p_low * 100,
                    notes=f'Long sweep: low={row["low"]:.4f} < prior_low={p_low:.4f}, lower_wick={lower_wick_ratio:.2f}'
                )
                sweeps.append(sweep)

            # ─── SHORT SWEEP: wick spikes ABOVE prior high ────────────────────
            #   • row['high'] > prior_high + threshold
            #   • close < prior_high                   (price closed back inside = rejection)
            #   • upper wick = (high - max(open,close)) / range → correct for an up-spike
            short_price_cond = (
                row['high'] > p_high + sweep_threshold and
                (row['high'] - p_high) / p_high >= self.min_sweep_pct and
                row['close'] < p_high  # close must fail back = rejection
            )
            upper_wick_ratio = (row['high'] - max(row['open'], row['close'])) / candle_range
            short_wick_ok = upper_wick_ratio >= self.wick_ratio

            if short_price_cond and volume_ok and short_wick_ok:
                sweep = SweepEvent(
                    timestamp=row.name,
                    direction='short',
                    sweep_price=row['high'],
                    close_price=row['close'],
                    volume=row['volume'],
                    volume_ratio=row['volume'] / avg_vol,
                    sweep_depth_pct=(row['high'] - p_high) / p_high * 100,
                    notes=f'Short sweep: high={row["high"]:.4f} > prior_high={p_high:.4f}, upper_wick={upper_wick_ratio:.2f}'
                )
                sweeps.append(sweep)

        # ─── Confirmation pass ────────────────────────────────────────────────
        # Sweep is confirmed if price does NOT re-visit the sweep extreme
        # within the next confirmation_bars candles.
        confirmed = []
        for sweep in sweeps:
            idx = df.index.get_loc(sweep.timestamp)
            end_idx = min(idx + 1 + self.confirmation_bars, len(df))
            future = df.iloc[idx + 1:end_idx]
            if len(future) == 0:
                # Not enough future bars yet — still mark confirmed (live edge)
                sweep.confirmed = True
                sweep.confirmation_time = sweep.timestamp
                confirmed.append(sweep)
                continue
            if sweep.direction == 'long':
                if not (future['low'] < sweep.sweep_price).any():
                    sweep.confirmed = True
                    sweep.confirmation_time = future.index[-1]
                    confirmed.append(sweep)
            else:
                if not (future['high'] > sweep.sweep_price).any():
                    sweep.confirmed = True
                    sweep.confirmation_time = future.index[-1]
                    confirmed.append(sweep)

        logger.info(f"SweepDetector: {len(sweeps)} raw sweeps → {len(confirmed)} confirmed")
        return confirmed

    def find_nearest_liquidity(self,
                               current_price: float,
                               zones: List[LiquidityZone],
                               direction: str,
                               max_zones: int = 3) -> List[LiquidityZone]:
        if direction == 'long':
            candidate_types = {'equal_high', 'swing_high', 'round'}
            above = [z for z in zones if z.price > current_price and z.zone_type in candidate_types]
            above.sort(key=lambda z: z.price)
            return above[:max_zones]
        else:
            candidate_types = {'equal_low', 'swing_low', 'round'}
            below = [z for z in zones if z.price < current_price and z.zone_type in candidate_types]
            below.sort(key=lambda z: z.price, reverse=True)
            return below[:max_zones]
