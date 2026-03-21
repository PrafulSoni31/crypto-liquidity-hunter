"""
Sweep Detector: Identifies liquidity sweep events.
A sweep = price spikes into a liquidity zone and quickly reverses.
"""
import pandas as pd
import numpy as np
from typing import List, Dict, Optional
from dataclasses import dataclass
import logging

from .liquidity_mapper import LiquidityZone

logger = logging.getLogger(__name__)

@dataclass
class SweepEvent:
    """Represents a liquidity sweep event."""
    timestamp: pd.Timestamp
    direction: str  # 'long' (sweep lows) or 'short' (sweep highs)
    sweep_price: float  # extreme price reached (wick)
    close_price: float  # closing price of sweep candle
    volume: float
    volume_ratio: float  # volume / avg_volume
    sweep_depth_pct: float  # % move beyond prior level
    confirmed: bool = False
    confirmation_time: Optional[pd.Timestamp] = None
    notes: str = ''

class SweepDetector:
    def __init__(self,
                 sweep_multiplier: float = 1.5,      # ATR multiple for sweep threshold
                 volume_multiplier: float = 3.0,     # volume spike threshold
                 confirmation_bars: int = 3,         # bars to wait for confirmation
                 wick_ratio: float = 0.67,          # wick must be at least X% of candle body
                 min_sweep_pct: float = 0.2):       # minimum sweep depth (%)
        """
        Parameters:
        - sweep_multiplier: how many ATRs beyond recent range counts as a sweep
        - volume_multiplier: volume must exceed average by this factor
        - confirmation_bars: how many bars to wait for price to reject sweep extreme
        - wick_ratio: minimum wick-to-candle ratio (wick length / (high-low))
        - min_sweep_pct: minimum % move beyond previous high/low
        """
        self.sweep_multiplier = sweep_multiplier
        self.volume_multiplier = volume_multiplier
        self.confirmation_bars = confirmation_bars
        self.wick_ratio = wick_ratio
        self.min_sweep_pct = min_sweep_pct / 100.0

    def detect_sweeps(self,
                      df: pd.DataFrame,
                      atr_series: pd.Series,
                      liquidity_zones: Optional[List[Dict]] = None) -> List[SweepEvent]:
        """
        Scan DataFrame for sweep events.
        df must have: high, low, close, volume
        atr_series: ATR values aligned with df index
        liquidity_zones: optional list of known liquidity zones to reference
        Returns list of SweepEvent.
        """
        sweeps = []
        avg_volume = df['volume'].rolling(20).mean()

        for i in range(1, len(df)):
            row = df.iloc[i]
            prev_row = df.iloc[i-1]
            atr = atr_series.iloc[i] if atr_series.iloc[i] else row['close'] * 0.01

            # Long sweep: price spikes down (wick below recent lows), then closes higher
            # Condition: low significantly below previous low
            # Typical: wick > min_sweep_pct of price AND low < (prev_low - sweep_threshold)
            sweep_threshold = self.sweep_multiplier * atr
            long_sweep_condition = (
                row['low'] < prev_row['low'] - sweep_threshold and
                (prev_row['low'] - row['low']) / prev_row['low'] >= self.min_sweep_pct
            )
            # Short sweep: price spikes up (wick above recent highs), then closes lower
            short_sweep_condition = (
                row['high'] > prev_row['high'] + sweep_threshold and
                (row['high'] - prev_row['high']) / prev_row['high'] >= self.min_sweep_pct
            )

            # Volume check
            volume_ok = row['volume'] > avg_volume.iloc[i] * self.volume_multiplier

            # Wick ratio: body vs wick
            candle_range = row['high'] - row['low']
            if candle_range <= 0:
                continue
            is_long_wick = (row['high'] - max(row['open'], row['close'])) / candle_range >= self.wick_ratio
            is_short_wick = (min(row['open'], row['close']) - row['low']) / candle_range >= self.wick_ratio

            if long_sweep_condition and volume_ok and is_long_wick:
                # Long sweep (liquidity grab of stops below support)
                sweep = SweepEvent(
                    timestamp=row.name,
                    direction='long',
                    sweep_price=row['low'],
                    close_price=row['close'],
                    volume=row['volume'],
                    volume_ratio=row['volume'] / avg_volume.iloc[i],
                    sweep_depth_pct=(prev_row['low'] - row['low']) / prev_row['low'] * 100,
                    notes='Long sweep: wicks below prior low, volume spike, long wick'
                )
                sweeps.append(sweep)

            if short_sweep_condition and volume_ok and is_short_wick:
                # Short sweep (liquidity grab of stops above resistance)
                sweep = SweepEvent(
                    timestamp=row.name,
                    direction='short',
                    sweep_price=row['high'],
                    close_price=row['close'],
                    volume=row['volume'],
                    volume_ratio=row['volume'] / avg_volume.iloc[i],
                    sweep_depth_pct=(row['high'] - prev_row['high']) / prev_row['high'] * 100,
                    notes='Short sweep: wicks above prior high, volume spike, short wick'
                )
                sweeps.append(sweep)

        # Confirmation: price should not revisit the sweep extreme within confirmation_bars
        confirmed_sweeps = []
        for sweep in sweeps:
            idx = df.index.get_loc(sweep.timestamp)
            end_idx = min(idx + self.confirmation_bars, len(df))
            future_slice = df.iloc[idx+1:end_idx]
            if sweep.direction == 'long':
                # For long sweep, low should not be exceeded again
                if not (future_slice['low'] < sweep.sweep_price).any():
                    sweep.confirmed = True
                    sweep.confirmation_time = future_slice.index[-1] if len(future_slice) > 0 else sweep.timestamp
            else:  # short
                if not (future_slice['high'] > sweep.sweep_price).any():
                    sweep.confirmed = True
                    sweep.confirmation_time = future_slice.index[-1] if len(future_slice) > 0 else sweep.timestamp
            if sweep.confirmed:
                confirmed_sweeps.append(sweep)

        return confirmed_sweeps

    def find_nearest_liquidity(self,
                               current_price: float,
                               zones: List[LiquidityZone],
                               direction: str,
                               max_zones: int = 3) -> List[LiquidityZone]:
        """
        Given a sweep direction, find the nearest opposing liquidity zones that could be targets.
        For long sweep (lows taken), target resistance zones (equal highs, swing highs).
        For short sweep (highs taken), target support zones (equal lows, swing lows).
        """
        if direction == 'long':
            candidate_types = {'equal_high', 'swing_high', 'round'}
            # Zones above current price
            above_zones = [z for z in zones if z.price > current_price and z.zone_type in candidate_types]
            above_zones.sort(key=lambda z: z.price)  # nearest first
            return above_zones[:max_zones]
        else:
            candidate_types = {'equal_low', 'swing_low', 'round'}
            below_zones = [z for z in zones if z.price < current_price and z.zone_type in candidate_types]
            below_zones.sort(key=lambda z: z.price, reverse=True)  # nearest first
            return below_zones[:max_zones]

if __name__ == '__main__':
    from data_fetcher import MarketDataFetcher
    from liquidity_mapper import LiquidityMapper

    fetcher = MarketDataFetcher('binance')
    df = fetcher.fetch_ohlcv('BTC/USDT', '15m', 1000)
    atr = fetcher.calculate_atr(df)

    mapper = LiquidityMapper()
    zones = mapper.map_liquidity(df)
    print(f"Liquidity zones: {len(zones)}")

    detector = SweepDetector(sweep_multiplier=1.5, volume_multiplier=2.5, confirmation_bars=3)
    sweeps = detector.detect_sweeps(df, atr, zones)
    print(f"Sweeps detected: {len(sweeps)}")
    for s in sweeps[-5:]:
        print(f"{s.timestamp} {s.direction} sweep_price={s.sweep_price:.2f} close={s.close_price:.2f} confirmed={s.confirmed}")
