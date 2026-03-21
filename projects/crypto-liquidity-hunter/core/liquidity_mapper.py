"""
Liquidity Mapper: Identifies liquidity zones in price data.
Detects equal highs/lows, swing fractals, round numbers.
"""
import pandas as pd
import numpy as np
from typing import List, Dict, Tuple
from dataclasses import dataclass
import logging

logger = logging.getLogger(__name__)

@dataclass
class LiquidityZone:
    """Represents a detected liquidity zone."""
    price: float
    zone_type: str  # 'equal_high', 'equal_low', 'swing_high', 'swing_low', 'round'
    strength: int   # number of touches or confluence score
    touches: List[float]  # prices that hit this zone
    last_touch: pd.Timestamp
    notes: str = ''

class LiquidityMapper:
    def __init__(self,
                 equal_touch_tolerance: float = 0.001,  # 0.1% tolerance for equal highs/lows
                 swing_lookback: int = 5,               # fractal lookback periods
                 round_tolerance: float = 0.005,        # 0.5% tolerance for round numbers
                 min_swing_strength: int = 3):          # min touches for equal highs/lows
        """
        Initialize mapper with detection parameters.
        - equal_touch_tolerance: how close prices must be to be considered equal (e.g., 0.001 = 0.1%)
        - swing_lookback: periods left/right for fractal detection
        - round_tolerance: round number buckets (e.g., 0.005 groups prices into 0.5% buckets)
        - min_swing_strength: minimum touches to form an equal zone
        """
        self.equal_touch_tolerance = equal_touch_tolerance
        self.swing_lookback = swing_lookback
        self.round_tolerance = round_tolerance
        self.min_swing_strength = min_swing_strength

    def map_liquidity(self, df: pd.DataFrame) -> List[LiquidityZone]:
        """
        Main entry: analyze OHLCV DataFrame and return list of liquidity zones.
        Expects df with columns: high, low, close, volume, indexed by timestamp.
        """
        zones = []
        # Detect equal highs and lows
        eq_highs = self._detect_equal_highs(df)
        eq_lows = self._detect_equal_lows(df)
        # Detect swing highs/lows
        swing_highs = self._detect_swing_highs(df)
        swing_lows = self._detect_swing_lows(df)
        # Detect round numbers
        round_zones = self._detect_round_numbers(df)

        zones.extend(eq_highs)
        zones.extend(eq_lows)
        zones.extend(swing_highs)
        zones.extend(swing_lows)
        zones.extend(round_zones)

        # Merge nearby zones (same price level within tolerance)
        zones = self._merge_nearby_zones(zones)
        # Sort by strength desc, price asc
        zones.sort(key=lambda z: (z.strength, -abs(z.price)), reverse=True)
        return zones

    def _detect_equal_highs(self, df: pd.DataFrame) -> List[LiquidityZone]:
        """Find price levels where high occurs repeatedly within tolerance."""
        highs = df['high'].values
        timestamps = df.index
        clusters = self._cluster_touches(highs, timestamps, is_high=True)
        zones = []
        for price, touches in clusters.items():
            if len(touches) >= self.min_swing_strength:
                zone = LiquidityZone(
                    price=price,
                    zone_type='equal_high',
                    strength=len(touches),
                    touches=touches,
                    last_touch=max(t[1] for t in touches),
                    notes=f'Equal high cluster: {len(touches)} touches'
                )
                zones.append(zone)
        return zones

    def _detect_equal_lows(self, df: pd.DataFrame) -> List[LiquidityZone]:
        """Find price levels where low occurs repeatedly within tolerance."""
        lows = df['low'].values
        timestamps = df.index
        clusters = self._cluster_touches(lows, timestamps, is_high=False)
        zones = []
        for price, touches in clusters.items():
            if len(touches) >= self.min_swing_strength:
                zone = LiquidityZone(
                    price=price,
                    zone_type='equal_low',
                    strength=len(touches),
                    touches=touches,
                    last_touch=max(t[1] for t in touches),
                    notes=f'Equal low cluster: {len(touches)} touches'
                )
                zones.append(zone)
        return zones

    def _cluster_touches(self,
                         prices: np.ndarray,
                         timestamps: pd.DatetimeIndex,
                         is_high: bool) -> Dict[float, List[Tuple[float, pd.Timestamp]]]:
        """Cluster price touches into buckets within tolerance."""
        # Sort prices to cluster sequentially
        sorted_indices = np.argsort(prices)
        sorted_prices = prices[sorted_indices]
        sorted_timestamps = [timestamps[i] for i in sorted_indices]

        clusters = {}
        current_cluster_price = None
        current_cluster_touches = []

        for price, ts in zip(sorted_prices, sorted_timestamps):
            if current_cluster_price is None:
                current_cluster_price = price
                current_cluster_touches.append((price, ts))
            else:
                # Check if price is within tolerance of cluster center
                rel_diff = abs(price - current_cluster_price) / current_cluster_price
                if rel_diff <= self.equal_touch_tolerance:
                    current_cluster_touches.append((price, ts))
                    # Update cluster center as mean
                    prices_only = [t[0] for t in current_cluster_touches]
                    current_cluster_price = np.mean(prices_only)
                else:
                    # Save previous cluster
                    cluster_price = np.mean([t[0] for t in current_cluster_touches])
                    clusters[cluster_price] = current_cluster_touches
                    # Start new cluster
                    current_cluster_price = price
                    current_cluster_touches = [(price, ts)]

        # Save last cluster
        if current_cluster_touches:
            cluster_price = np.mean([t[0] for t in current_cluster_touches])
            clusters[cluster_price] = current_cluster_touches

        return clusters

    def _detect_swing_highs(self, df: pd.DataFrame) -> List[LiquidityZone]:
        """Fractal swing highs: high > left N and > right N."""
        n = self.swing_lookback
        highs = df['high'].values
        timestamps = df.index
        swing_indices = []

        for i in range(n, len(highs) - n):
            window_left = highs[i-n:i]
            window_right = highs[i+1:i+n+1]
            if highs[i] > window_left.max() and highs[i] > window_right.max():
                swing_indices.append(i)

        zones = []
        for idx in swing_indices:
            price = highs[idx]
            ts = timestamps[idx]
            zone = LiquidityZone(
                price=price,
                zone_type='swing_high',
                strength=1,
                touches=[(price, ts)],
                last_touch=ts,
                notes=f'Swing high (fractal {n})'
            )
            zones.append(zone)
        return zones

    def _detect_swing_lows(self, df: pd.DataFrame) -> List[LiquidityZone]:
        """Fractal swing lows: low < left N and < right N."""
        n = self.swing_lookback
        lows = df['low'].values
        timestamps = df.index
        swing_indices = []

        for i in range(n, len(lows) - n):
            window_left = lows[i-n:i]
            window_right = lows[i+1:i+n+1]
            if lows[i] < window_left.min() and lows[i] < window_right.min():
                swing_indices.append(i)

        zones = []
        for idx in swing_indices:
            price = lows[idx]
            ts = timestamps[idx]
            zone = LiquidityZone(
                price=price,
                zone_type='swing_low',
                strength=1,
                touches=[(price, ts)],
                last_touch=ts,
                notes=f'Swing low (fractal {n})'
            )
            zones.append(zone)
        return zones

    def _detect_round_numbers(self, df: pd.DataFrame) -> List[LiquidityZone]:
        """
        Detect round number clusters within tolerance.
        Round numbers: multiples of 10, 100, 1000 etc. (depending on price)
        We'll bucket prices into 0.5% buckets and find clusters.
        """
        # Get all high and low touches as (timestamp, price)
        all_touches = [(ts, price) for ts, price in df['high'].items()] + \
                      [(ts, price) for ts, price in df['low'].items()]
        # Group by round bucket
        buckets = {}
        for ts, price in all_touches:
            bucket = self._round_to_bucket(price, self.round_tolerance)
            if bucket not in buckets:
                buckets[bucket] = []
            buckets[bucket].append((price, ts))

        zones = []
        for bucket_price, touches in buckets.items():
            if len(touches) >= 3:  # min 3 touches to be a round liquidity cluster
                zone = LiquidityZone(
                    price=bucket_price,
                    zone_type='round',
                    strength=len(touches),
                    touches=touches,
                    last_touch=max(t[1] for t in touches),
                    notes=f'Round number cluster: {len(touches)} touches'
                )
                zones.append(zone)
        return zones

    def _round_to_bucket(self, price: float, tolerance: float) -> float:
        """Round price to nearest bucket (tolerance-based)."""
        bucket_size = price * tolerance * 2  # bucket width
        bucket_size = max(bucket_size, 1e-8)  # avoid zero
        bucket = round(price / bucket_size) * bucket_size
        return bucket

    def _merge_nearby_zones(self, zones: List[LiquidityZone]) -> List[LiquidityZone]:
        """Merge zones that are very close (within tolerance) by averaging price and summing strength."""
        merged = []
        zones.sort(key=lambda z: z.price)
        i = 0
        while i < len(zones):
            current = zones[i]
            j = i + 1
            group = [current]
            while j < len(zones):
                next_zone = zones[j]
                rel_diff = abs(next_zone.price - current.price) / current.price
                if rel_diff <= self.equal_touch_tolerance:
                    group.append(next_zone)
                    j += 1
                else:
                    break
            if len(group) > 1:
                # Merge group
                total_strength = sum(z.strength for z in group)
                all_touches = []
                last_touch = None
                types = set(z.zone_type for z in group)
                for z in group:
                    all_touches.extend(z.touches)
                    if last_touch is None or z.last_touch > last_touch:
                        last_touch = z.last_touch
                avg_price = np.mean([z.price for z in group])
                merged_type = '/'.join(sorted(types))
                merged_zone = LiquidityZone(
                    price=avg_price,
                    zone_type=merged_type,
                    strength=total_strength,
                    touches=all_touches,
                    last_touch=last_touch,
                    notes=f'Merged {len(group)} zones: {merged_type}'
                )
                merged.append(merged_zone)
                i = j
            else:
                merged.append(current)
                i += 1
        return merged

if __name__ == '__main__':
    import sys
    from data_fetcher import MarketDataFetcher

    fetcher = MarketDataFetcher('binance')
    df = fetcher.fetch_ohlcv('BTC/USDT', '15m', 500)

    mapper = LiquidityMapper(equal_touch_tolerance=0.001, swing_lookback=5, round_tolerance=0.005)
    zones = mapper.map_liquidity(df)

    print(f"Found {len(zones)} liquidity zones:")
    for z in zones[:15]:
        print(f"{z.zone_type:15} price={z.price:,.2f} strength={z.strength} last={z.last_touch.date()}")
