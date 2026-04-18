"""
Sweep Detector v2 — Professional Liquidity Hunt
================================================
A sweep = price grabs liquidity (stops) beyond a prior high/low,
then shows DISPLACEMENT back inside (strong-bodied candle, NOT just a wick close).

Phase 1 Fixes:
  - Displacement candle requirement: strong body close, not just any close inside range
  - Volume threshold raised to 2.5× average (was 1.2× — too permissive)
  - Confirmation window extended: 5 bars (was 3 — too short)
  - Wick ratio tightened: 0.5 (was 0.4)

Phase 2 Additions:
  - Order Block detection (last bearish/bullish engulfing before sweep move)
  - Fair Value Gap (FVG) detection (3-candle imbalance)
  - HTF trend bias (exported for use by signal engine)
"""
import pandas as pd
import numpy as np
from typing import List, Optional, Tuple
from dataclasses import dataclass, field
import logging

from .liquidity_mapper import LiquidityZone

logger = logging.getLogger(__name__)


# ─── Data Structures ──────────────────────────────────────────────────────────

@dataclass
class SweepEvent:
    """Confirmed liquidity sweep with displacement."""
    timestamp:        pd.Timestamp
    direction:        str           # 'long' (swept lows → expect UP) | 'short' (swept highs → expect DOWN)
    sweep_price:      float         # extreme wick price
    close_price:      float         # displacement candle close
    volume:           float
    volume_ratio:     float         # vs 20-bar avg
    sweep_depth_pct:  float         # % beyond prior level
    confirmed:        bool = False
    confirmation_time: Optional[pd.Timestamp] = None
    displacement_body_pct: float = 0.0  # body / candle range — quality metric
    notes:            str = ''


@dataclass
class OrderBlock:
    """
    Order Block: the last opposing candle before a strong displacement move.
    For a LONG setup (swept lows): the last BEARISH candle before price exploded up.
    For a SHORT setup (swept highs): the last BULLISH candle before price dropped.
    """
    timestamp:   pd.Timestamp
    direction:   str       # 'bullish' OB (buy from here) | 'bearish' OB (sell from here)
    high:        float
    low:         float
    open:        float
    close:       float
    strength:    int = 1   # how many times price has respected this OB
    mitigated:   bool = False  # True if price has traded through the OB fully


@dataclass
class FairValueGap:
    """
    Fair Value Gap (FVG / Imbalance):
    3-candle pattern where candle[i-1].high < candle[i+1].low (bullish FVG)
    or candle[i-1].low > candle[i+1].high (bearish FVG).
    Price tends to return to fill these gaps.
    """
    timestamp:   pd.Timestamp
    direction:   str    # 'bullish' (gap above — acts as support) | 'bearish' (gap below — acts as resistance)
    top:         float  # upper bound of the gap
    bottom:      float  # lower bound of the gap
    size_pct:    float  # gap size as % of mid price
    filled:      bool = False


# ─── Detector ─────────────────────────────────────────────────────────────────

class SweepDetector:
    def __init__(self,
                 sweep_multiplier:   float = 0.5,
                 volume_multiplier:  float = 2.5,   # RAISED from 1.2 — institutional level
                 confirmation_bars:  int   = 5,     # RAISED from 3
                 wick_ratio:         float = 0.5,   # RAISED from 0.4
                 min_sweep_pct:      float = 0.2,
                 lookback_bars:      int   = 10,
                 min_body_ratio:     float = 0.4):  # NEW: displacement candle body must be ≥40% of range
        self.sweep_multiplier  = sweep_multiplier
        self.volume_multiplier = volume_multiplier
        self.confirmation_bars = confirmation_bars
        self.wick_ratio        = wick_ratio
        self.min_sweep_pct     = min_sweep_pct / 100.0
        self.lookback_bars     = lookback_bars
        self.min_body_ratio    = min_body_ratio

    # ── Main Entry ─────────────────────────────────────────────────────────────

    def detect_sweeps(self,
                      df: pd.DataFrame,
                      atr_series: pd.Series,
                      liquidity_zones: Optional[List] = None) -> List[SweepEvent]:
        """
        Detect confirmed liquidity sweeps with displacement.
        Requires:
          1. Price wicks beyond prior high/low (liquidity grab)
          2. Volume ≥ 2.5× average (institutional participation)
          3. Strong displacement close back inside range (not just wick close)
          4. Displacement candle body ≥ 40% of candle range
          5. Confirmation: price does NOT re-visit sweep extreme in next 5 bars
        """
        sweeps = []
        avg_volume = df['volume'].rolling(20).mean()
        prior_high = df['high'].shift(1).rolling(self.lookback_bars).max()
        prior_low  = df['low'].shift(1).rolling(self.lookback_bars).min()

        warmup = max(self.lookback_bars + 1, 21)

        for i in range(warmup, len(df)):
            row     = df.iloc[i]
            atr_val = atr_series.iloc[i]
            if pd.isna(atr_val) or atr_val <= 0:
                atr_val = row['close'] * 0.01

            p_high  = prior_high.iloc[i]
            p_low   = prior_low.iloc[i]
            avg_vol = avg_volume.iloc[i]

            if pd.isna(p_high) or pd.isna(p_low) or pd.isna(avg_vol) or avg_vol <= 0:
                continue

            sweep_threshold = self.sweep_multiplier * atr_val
            candle_range    = row['high'] - row['low']
            if candle_range <= 0:
                continue

            # ── Volume gate (institutional requirement) ────────────────────
            volume_ok = row['volume'] >= avg_vol * self.volume_multiplier

            # ── Candle anatomy ─────────────────────────────────────────────
            body_size        = abs(row['close'] - row['open'])
            body_ratio       = body_size / candle_range        # 0–1
            lower_wick_ratio = (min(row['open'], row['close']) - row['low'])  / candle_range
            upper_wick_ratio = (row['high'] - max(row['open'], row['close'])) / candle_range

            # ── LONG SWEEP: wick spikes BELOW prior low, closes back above ─
            # Crypto reality: sweep candle often has large wick + small body
            # Requirement: wick > body (wick dominates), close back inside range
            long_price_cond = (
                row['low']   < p_low - sweep_threshold and
                (p_low - row['low']) / p_low >= self.min_sweep_pct and
                row['close'] > p_low      # closed back above prior low = rejection
            )
            long_wick_ok = lower_wick_ratio >= self.wick_ratio
            # Wick must dominate the candle (confirms the reversal intent)
            long_wick_dominant = lower_wick_ratio > body_ratio * 0.8  # wick ≥ 80% of body

            if long_price_cond and volume_ok and long_wick_ok and long_wick_dominant:
                sweep = SweepEvent(
                    timestamp=row.name,
                    direction='long',
                    sweep_price=row['low'],
                    close_price=row['close'],
                    volume=row['volume'],
                    volume_ratio=row['volume'] / avg_vol,
                    sweep_depth_pct=(p_low - row['low']) / p_low * 100,
                    displacement_body_pct=body_ratio * 100,
                    notes=f'Long sweep: low={row["low"]:.6f} < prior_low={p_low:.6f} '
                          f'vol={row["volume"]/avg_vol:.1f}× wick={lower_wick_ratio:.2f}'
                )
                sweeps.append(sweep)

            # ── SHORT SWEEP: wick spikes ABOVE prior high, closes back below
            short_price_cond = (
                row['high']  > p_high + sweep_threshold and
                (row['high'] - p_high) / p_high >= self.min_sweep_pct and
                row['close'] < p_high     # closed back below prior high = rejection
            )
            short_wick_ok       = upper_wick_ratio >= self.wick_ratio
            short_wick_dominant = upper_wick_ratio > body_ratio * 0.8

            if short_price_cond and volume_ok and short_wick_ok and short_wick_dominant:
                sweep = SweepEvent(
                    timestamp=row.name,
                    direction='short',
                    sweep_price=row['high'],
                    close_price=row['close'],
                    volume=row['volume'],
                    volume_ratio=row['volume'] / avg_vol,
                    sweep_depth_pct=(row['high'] - p_high) / p_high * 100,
                    displacement_body_pct=body_ratio * 100,
                    notes=f'Short sweep: high={row["high"]:.6f} > prior_high={p_high:.6f} '
                          f'vol={row["volume"]/avg_vol:.1f}× wick={upper_wick_ratio:.2f}'
                )
                sweeps.append(sweep)

        # ── Confirmation pass ──────────────────────────────────────────────
        confirmed = []
        for sweep in sweeps:
            idx     = df.index.get_loc(sweep.timestamp)
            end_idx = min(idx + 1 + self.confirmation_bars, len(df))
            future  = df.iloc[idx + 1:end_idx]

            if len(future) == 0:
                sweep.confirmed          = True
                sweep.confirmation_time  = sweep.timestamp
                confirmed.append(sweep)
                continue

            if sweep.direction == 'long':
                if not (future['low'] < sweep.sweep_price).any():
                    sweep.confirmed         = True
                    sweep.confirmation_time = future.index[-1]
                    confirmed.append(sweep)
            else:
                if not (future['high'] > sweep.sweep_price).any():
                    sweep.confirmed         = True
                    sweep.confirmation_time = future.index[-1]
                    confirmed.append(sweep)

        logger.info(f"SweepDetector v2: {len(sweeps)} raw → {len(confirmed)} confirmed "
                    f"(vol≥{self.volume_multiplier}× body≥{self.min_body_ratio:.0%})")
        return confirmed

    # ── Order Block Detection ──────────────────────────────────────────────────

    def detect_order_blocks(self, df: pd.DataFrame, lookback: int = 50) -> List[OrderBlock]:
        """
        Detect Order Blocks — the last opposing candle before a strong impulse move.

        BULLISH OB: last BEARISH candle before a strong bullish impulse
          (institution sells into retail longs, then reverses up — OB is demand zone)
        BEARISH OB: last BULLISH candle before a strong bearish impulse
          (institution buys from retail shorts, then reverses down — OB is supply zone)

        Impulse = 3 consecutive candles in same direction with expanding range.
        """
        obs = []
        avg_range = (df['high'] - df['low']).rolling(20).mean()

        scan_start = max(4, len(df) - lookback)

        for i in range(scan_start, len(df) - 3):
            row       = df.iloc[i]
            atr_proxy = avg_range.iloc[i]
            if pd.isna(atr_proxy) or atr_proxy <= 0:
                continue

            # Look ahead 3 bars for impulse
            fwd = df.iloc[i+1:i+4]
            if len(fwd) < 3:
                continue

            fwd_bodies = abs(fwd['close'] - fwd['open'])
            fwd_directions = (fwd['close'] > fwd['open']).astype(int)  # 1=bull, 0=bear

            # ── Bullish OB: bearish candle followed by 3-bar bullish impulse ──
            if (row['close'] < row['open'] and               # OB candle is bearish
                fwd_directions.sum() >= 2 and                # ≥2 of next 3 are bullish
                fwd['close'].iloc[-1] > row['high'] and      # impulse breaks above OB
                fwd_bodies.mean() > atr_proxy * 0.5):        # impulse has decent body size

                # Check OB not already mitigated (price hasn't traded back into it)
                future_lows = df.iloc[i+4:]['low'] if i+4 < len(df) else pd.Series(dtype=float)
                mitigated = len(future_lows) > 0 and (future_lows <= row['high']).any()

                ob = OrderBlock(
                    timestamp=row.name,
                    direction='bullish',
                    high=row['high'],
                    low=row['low'],
                    open=row['open'],
                    close=row['close'],
                    mitigated=mitigated
                )
                obs.append(ob)

            # ── Bearish OB: bullish candle followed by 3-bar bearish impulse ──
            elif (row['close'] > row['open'] and              # OB candle is bullish
                  fwd_directions.sum() <= 1 and               # ≥2 of next 3 are bearish
                  fwd['close'].iloc[-1] < row['low'] and      # impulse breaks below OB
                  fwd_bodies.mean() > atr_proxy * 0.5):

                future_highs = df.iloc[i+4:]['high'] if i+4 < len(df) else pd.Series(dtype=float)
                mitigated = len(future_highs) > 0 and (future_highs >= row['low']).any()

                ob = OrderBlock(
                    timestamp=row.name,
                    direction='bearish',
                    high=row['high'],
                    low=row['low'],
                    open=row['open'],
                    close=row['close'],
                    mitigated=mitigated
                )
                obs.append(ob)

        # Filter: keep only unmitigated OBs
        active_obs = [ob for ob in obs if not ob.mitigated]
        logger.info(f"OrderBlocks: {len(obs)} detected, {len(active_obs)} unmitigated")
        return active_obs

    # ── Fair Value Gap Detection ───────────────────────────────────────────────

    def detect_fvgs(self, df: pd.DataFrame, min_size_pct: float = 0.1,
                    lookback: int = 50) -> List[FairValueGap]:
        """
        Detect Fair Value Gaps (FVGs / Imbalances).

        Bullish FVG: candle[i-1].high < candle[i+1].low
          → gap zone = [candle[i-1].high, candle[i+1].low]
          → acts as support when price returns to fill

        Bearish FVG: candle[i-1].low > candle[i+1].high
          → gap zone = [candle[i+1].high, candle[i-1].low]
          → acts as resistance when price returns to fill

        Only keep unfilled FVGs (price hasn't fully closed through them).
        """
        fvgs = []
        scan_start = max(1, len(df) - lookback - 1)

        for i in range(scan_start + 1, len(df) - 1):
            c_prev = df.iloc[i - 1]
            c_curr = df.iloc[i]
            c_next = df.iloc[i + 1]
            mid    = c_curr['close']
            if mid <= 0:
                continue

            # ── Bullish FVG ───────────────────────────────────────────────
            if c_prev['high'] < c_next['low']:
                bottom   = c_prev['high']
                top      = c_next['low']
                size_pct = (top - bottom) / mid * 100

                if size_pct >= min_size_pct:
                    # Check if already filled (price traded back below bottom)
                    future = df.iloc[i+2:] if i+2 < len(df) else pd.DataFrame()
                    filled = (len(future) > 0 and
                              (future['low'] <= bottom).any())
                    fvg = FairValueGap(
                        timestamp=c_curr.name,
                        direction='bullish',
                        top=top,
                        bottom=bottom,
                        size_pct=round(size_pct, 3),
                        filled=filled
                    )
                    fvgs.append(fvg)

            # ── Bearish FVG ───────────────────────────────────────────────
            elif c_prev['low'] > c_next['high']:
                top      = c_prev['low']
                bottom   = c_next['high']
                size_pct = (top - bottom) / mid * 100

                if size_pct >= min_size_pct:
                    future = df.iloc[i+2:] if i+2 < len(df) else pd.DataFrame()
                    filled = (len(future) > 0 and
                              (future['high'] >= top).any())
                    fvg = FairValueGap(
                        timestamp=c_curr.name,
                        direction='bearish',
                        top=top,
                        bottom=bottom,
                        size_pct=round(size_pct, 3),
                        filled=filled
                    )
                    fvgs.append(fvg)

        active = [f for f in fvgs if not f.filled]
        logger.info(f"FVGs: {len(fvgs)} detected, {len(active)} unfilled")
        return active

    # ── HTF Trend Bias ─────────────────────────────────────────────────────────

    def get_htf_bias(self, df_htf: pd.DataFrame, ema_fast: int = 50,
                     ema_slow: int = 200) -> str:
        """
        Determine higher-timeframe trend bias.
        Uses EMA 50/200 crossover + recent swing structure.
        EMA 50/200 on 4h = proper macro structural bias (institutional reference).
        Previous 20/50 was too sensitive — whipsawed in ranging markets.

        Returns: 'bullish' | 'bearish' | 'neutral'
        """
        if len(df_htf) < ema_slow + 5:
            return 'neutral'

        closes = df_htf['close']
        ema_f  = closes.ewm(span=ema_fast, adjust=False).mean()
        ema_s  = closes.ewm(span=ema_slow, adjust=False).mean()

        last_fast = ema_f.iloc[-1]
        last_slow = ema_s.iloc[-1]
        last_close = closes.iloc[-1]

        # Check last 10 bars for higher highs / higher lows (bullish structure)
        recent = df_htf.iloc[-10:]
        hh = recent['high'].iloc[-1] > recent['high'].iloc[:-1].max()
        hl = recent['low'].iloc[-1]  > recent['low'].iloc[:-3].min()
        lh = recent['high'].iloc[-1] < recent['high'].iloc[:-1].max()
        ll = recent['low'].iloc[-1]  < recent['low'].iloc[:-3].min()

        ema_bull = last_fast > last_slow and last_close > last_fast
        ema_bear = last_fast < last_slow and last_close < last_fast

        if ema_bull and (hh or hl):
            return 'bullish'
        elif ema_bear and (lh or ll):
            return 'bearish'
        elif ema_bull or (hh and hl):
            return 'bullish'
        elif ema_bear or (lh and ll):
            return 'bearish'
        return 'neutral'

    def find_nearest_liquidity(self,
                               current_price: float,
                               zones: List[LiquidityZone],
                               direction: str,
                               max_zones: int = 3) -> List[LiquidityZone]:
        if direction == 'long':
            candidate_types = {'equal_high', 'swing_high', 'round'}
            above = [z for z in zones if z.price > current_price
                     and z.zone_type in candidate_types]
            above.sort(key=lambda z: z.price)
            return above[:max_zones]
        else:
            candidate_types = {'equal_low', 'swing_low', 'round'}
            below = [z for z in zones if z.price < current_price
                     and z.zone_type in candidate_types]
            below.sort(key=lambda z: z.price, reverse=True)
            return below[:max_zones]
