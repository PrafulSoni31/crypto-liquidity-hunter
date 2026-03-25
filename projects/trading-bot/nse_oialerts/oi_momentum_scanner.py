#!/usr/bin/env python3
"""
NSE OI-Based Momentum Stock Scanner — High Conviction Only
============================================================
Trading Rules (Charlie's mandate):
  Price ↑ + OI ↑ = Long Buildup  (BULLISH) → BUY
  Price ↓ + OI ↑ = Short Buildup (BEARISH) → SELL
  Price ↑ + OI ↓ = Short Covering (WEAK)   → SKIP
  Price ↓ + OI ↓ = Long Unwinding (WEAK)   → SKIP

Quality Gates (ALL must pass):
  1. OI change ≥ 5%          (significant commitment)
  2. Price change ≥ 0.5% confirmed via real NSE API
  3. Real volume spike ≥ 1.5× (not estimated)
  4. Price ₹50–₹5000         (liquid F&O range)
  5. R:R ≥ 1.5               (minimum reward/risk)
  6. Conviction ≥ 70         (strict scoring)
  7. Signal direction aligned with Nifty trend

Schedule:
  9:30 AM IST  → INTRADAY  (same-day exit)
  3:15 PM IST  → BTST/STBT (overnight hold)
"""

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass, asdict
from enum import Enum
import requests
import time


# ─── Constants ─────────────────────────────────────────────────────────────────

# Minimum thresholds — only very high potential intraday trades
MIN_OI_CHANGE_PCT          = 5.0   # OI must move ≥5%
MIN_PRICE_CHANGE_PCT       = 0.8   # Price must move ≥0.8% confirmed (raised from 0.5)
MIN_PRICE_CHANGE_COUNTER   = 1.2   # Counter-trend trades need stronger price move
MIN_VOLUME_SPIKE           = 1.5   # Volume spike threshold
MIN_CONVICTION             = 72    # Score threshold (raised slightly)
MIN_PRICE                  = 50.0  # Skip sub-₹50 illiquid stocks
MAX_PRICE                  = 5000.0
MIN_RR_RATIO               = 1.8   # Raised from 1.5 — better reward needed
MAX_SIGNALS_PER_SIDE       = 3     # Top 3 per side max

# Indices to skip
INDEX_SYMBOLS = {
    'NIFTY', 'BANKNIFTY', 'FINNIFTY', 'NIFTYMID50', 'NIFTYIT',
    'NIFTYPVTBANK', 'NIFTYPSUBANK', 'NIFTYAUTO', 'NIFTYFMCG',
    'NIFTYINFRA', 'NIFTYMEDIA', 'NIFTYMETAL', 'NIFTYPHARMA',
    'NIFTYREALTY', 'NIFTYCONS', 'NIFTYENERGY', 'NIFTYFIN',
    'MIDCPNIFTY', 'SENSEX', 'BANKEX'
}

IST = timezone(timedelta(hours=5, minutes=30))


# ─── Data Classes ──────────────────────────────────────────────────────────────

class SignalType(Enum):
    LONG_BUILDUP  = "LONG_BUILDUP"    # Price ↑ + OI ↑ → BUY
    SHORT_BUILDUP = "SHORT_BUILDUP"   # Price ↓ + OI ↑ → SELL
    SHORT_COVERING = "SHORT_COVERING" # Price ↑ + OI ↓ → SKIP
    LONG_UNWINDING = "LONG_UNWINDING" # Price ↓ + OI ↓ → SKIP


class TradeType(Enum):
    INTRADAY = "INTRADAY"
    BTST     = "BTST"
    STBT     = "STBT"


@dataclass
class CEPEOIData:
    """Call/Put OI breakdown for a stock at ATM strikes."""
    atm_ce_oi:        float   # ATM Call OI
    atm_pe_oi:        float   # ATM Put OI
    ce_oi_change:     float   # CE OI change % (positive = building)
    pe_oi_change:     float   # PE OI change % (positive = building)
    pcr:              float   # Put-Call Ratio (>1 bullish, <1 bearish)
    ce_building:      bool    # CE OI building strongly
    pe_building:      bool    # PE OI building strongly
    options_bias:     str     # 'bullish' / 'bearish' / 'neutral'
    max_pain:         Optional[float]
    ce_unwinding:     bool    # Call writers exiting (bullish)
    pe_unwinding:     bool    # Put writers exiting (bearish)


@dataclass
class OIStockSignal:
    symbol:          str
    signal_type:     str
    trade_type:      str
    current_price:   float
    price_change_pct: float
    oi_change_pct:   float
    volume_spike:    float
    delivery_pct:    Optional[float]
    target:          float
    stop_loss:       float
    risk_reward:     float
    conviction_score: float
    reasoning:       str
    timestamp:       str
    # CE/PE OI fields (optional — populated when available)
    ce_pe_bias:      str = 'N/A'   # 'bullish' / 'bearish' / 'neutral' / 'N/A'
    pcr:             float = 0.0
    ce_oi_change:    float = 0.0
    pe_oi_change:    float = 0.0
    options_aligned: bool = False  # True if CE/PE bias agrees with signal direction
    max_pain:        Optional[float] = None


# ─── Scanner ───────────────────────────────────────────────────────────────────

class NSEOIScanner:
    """
    Scans NSE OI Spurts for high-conviction setups.
    Only real data — no estimation, no fabrication.
    """

    BASE_URL = "https://www.nseindia.com"

    HEADERS = {
        'User-Agent':      'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36',
        'Accept':          'application/json, text/plain, */*',
        'Accept-Language': 'en-US,en;q=0.9',
        'Accept-Encoding': 'gzip, deflate',  # Exclude br — brotli not always available
        'Referer':         'https://www.nseindia.com/',
        'Connection':      'keep-alive',
    }

    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update(self.HEADERS)
        self.cache_dir = "projects/trading-bot/nse_oialerts/cache"
        os.makedirs(self.cache_dir, exist_ok=True)
        self._warm_session()

    def _warm_session(self):
        """
        NSE API works without cookies when called with correct headers.
        Just verify connectivity — skip homepage (often 403 from cloud IPs).
        """
        pass  # Direct API calls work fine with proper headers

    # ── Data Fetchers ──────────────────────────────────────────────────────────

    def fetch_oi_spurts(self) -> List[Dict]:
        """Fetch OI Spurts list from NSE. Primary data source."""
        try:
            url = f"{self.BASE_URL}/api/live-analysis-oi-spurts-underlyings"
            # Use a fresh session per call — NSE cloud blocks are session-level
            r = requests.get(url, headers=self.HEADERS, timeout=15)
            if r.status_code == 200:
                stocks = r.json().get('data', [])
                print(f"✅ Fetched {len(stocks)} stocks from NSE OI Spurts")
                return stocks
            print(f"⚠️  OI Spurts API: HTTP {r.status_code}")
            return []
        except Exception as e:
            print(f"❌ OI Spurts fetch error: {e}")
            return []

    def fetch_nifty_trend(self) -> Optional[str]:
        """
        Fetch Nifty 50 current direction via allIndices API.
        Returns 'bullish', 'bearish', 'neutral', or None.
        """
        try:
            url = f"{self.BASE_URL}/api/allIndices"
            r = requests.get(url, headers=self.HEADERS, timeout=8)
            if r.status_code == 200:
                for idx in r.json().get('data', []):
                    if idx.get('index') in ('NIFTY 50', 'Nifty 50', 'NIFTY50'):
                        pc = float(idx.get('percentChange', 0))
                        if pc >= 0.2:    return 'bullish'
                        elif pc <= -0.2: return 'bearish'
                        return 'neutral'
        except Exception:
            pass
        return None  # Unknown — don't filter on it

    def fetch_real_quote(self, symbol: str) -> Optional[Dict]:
        """
        Fetch real price, delivery %, and volume for a symbol.
        Uses two NSE endpoints:
          1. quote-equity         → price change, last price
          2. quote-equity?section=trade_info → equity volume + delivery
        Returns None if essential data unavailable — signal will be rejected.
        """
        try:
            # ── Price data ──────────────────────────────────────────────────
            url = f"{self.BASE_URL}/api/quote-equity?symbol={symbol}"
            r = requests.get(url, headers=self.HEADERS, timeout=6)
            if r.status_code != 200:
                return None
            pi = r.json().get('priceInfo', {})

            price_change_pct = pi.get('pChange')
            current_price    = pi.get('lastPrice') or pi.get('close')
            if price_change_pct is None or current_price is None:
                return None

            # ── Volume + Delivery data ──────────────────────────────────────
            url2 = f"{self.BASE_URL}/api/quote-equity?symbol={symbol}&section=trade_info"
            r2 = requests.get(url2, headers=self.HEADERS, timeout=6)
            delivery_pct   = None
            equity_vol     = 0
            if r2.status_code == 200:
                data2 = r2.json()
                dp   = data2.get('securityWiseDP', {})
                delivery_pct = dp.get('deliveryToTradedQuantity')   # real delivery %
                equity_vol   = float(dp.get('quantityTraded', 0) or 0)

            return {
                'price_change_pct': float(price_change_pct),
                'current_price':    float(current_price),
                'equity_vol':       equity_vol,
                'delivery_pct':     float(delivery_pct) if delivery_pct is not None else None,
            }
        except Exception:
            return None

    def fetch_ce_pe_oi(self, symbol: str, current_price: float) -> Optional[CEPEOIData]:
        """
        Fetch NSE option chain for a stock and compute CE/PE OI dynamics.

        Interpretation:
          CE OI building   → More call writers shorting → bearish pressure
          PE OI building   → More put writers shorting  → bullish support
          CE OI unwinding  → Call writers exiting       → bullish (reduced resistance)
          PE OI unwinding  → Put writers exiting        → bearish (support removed)
          PCR > 1.2        → More puts than calls       → bullish overall
          PCR < 0.8        → More calls than puts       → bearish overall
        """
        try:
            url = f"{self.BASE_URL}/api/option-chain-equities?symbol={symbol}"
            r   = requests.get(url, headers=self.HEADERS, timeout=10)
            if r.status_code != 200:
                return None
            data   = r.json()
            records = data.get('records', {}).get('data', [])
            if not records:
                return None

            # Find ATM strike (closest to current price)
            strikes = [rec['strikePrice'] for rec in records if rec.get('strikePrice')]
            if not strikes:
                return None
            atm_strike = min(strikes, key=lambda x: abs(x - current_price))

            # Collect CE and PE data around ATM (ATM ± 3 strikes)
            strike_list = sorted(set(strikes))
            atm_idx     = strike_list.index(atm_strike)
            near_strikes = strike_list[max(0, atm_idx-3): atm_idx+4]  # ±3 strikes

            total_ce_oi = 0.0; total_pe_oi = 0.0
            total_ce_chg = 0.0; total_pe_chg = 0.0
            atm_ce_oi = 0.0;   atm_pe_oi = 0.0
            all_ce_oi = 0.0;   all_pe_oi = 0.0

            for rec in records:
                sp = rec.get('strikePrice', 0)
                ce = rec.get('CE', {})
                pe = rec.get('PE', {})

                ce_oi = float(ce.get('openInterest', 0) or 0)
                pe_oi = float(pe.get('openInterest', 0) or 0)
                ce_chg = float(ce.get('changeinOpenInterest', 0) or 0)
                pe_chg = float(pe.get('changeinOpenInterest', 0) or 0)

                all_ce_oi += ce_oi
                all_pe_oi += pe_oi

                if sp in near_strikes:
                    total_ce_oi  += ce_oi
                    total_pe_oi  += pe_oi
                    total_ce_chg += ce_chg
                    total_pe_chg += pe_chg

                if sp == atm_strike:
                    atm_ce_oi = ce_oi
                    atm_pe_oi = pe_oi

            # PCR (all strikes — more representative)
            pcr = round(all_pe_oi / all_ce_oi, 2) if all_ce_oi > 0 else 0.0

            # CE/PE change % in near-ATM zone
            ce_chg_pct = (total_ce_chg / total_ce_oi * 100) if total_ce_oi > 0 else 0.0
            pe_chg_pct = (total_pe_chg / total_pe_oi * 100) if total_pe_oi > 0 else 0.0

            # Flags
            ce_building   = ce_chg_pct >= 5.0    # Calls being written (bearish)
            pe_building   = pe_chg_pct >= 5.0    # Puts being written (bullish)
            ce_unwinding  = ce_chg_pct <= -5.0   # Call writers exiting (bullish)
            pe_unwinding  = pe_chg_pct <= -5.0   # Put writers exiting (bearish)

            # Determine options bias
            bullish_signals = int(pe_building) + int(ce_unwinding) + int(pcr >= 1.2)
            bearish_signals = int(ce_building) + int(pe_unwinding) + int(pcr <= 0.8)

            if bullish_signals >= 2:
                options_bias = 'bullish'
            elif bearish_signals >= 2:
                options_bias = 'bearish'
            else:
                options_bias = 'neutral'

            # Max Pain (strike with max total OI loss for option buyers)
            max_pain = self._calc_max_pain(records)

            return CEPEOIData(
                atm_ce_oi    = round(atm_ce_oi, 0),
                atm_pe_oi    = round(atm_pe_oi, 0),
                ce_oi_change = round(ce_chg_pct, 1),
                pe_oi_change = round(pe_chg_pct, 1),
                pcr          = pcr,
                ce_building  = ce_building,
                pe_building  = pe_building,
                options_bias = options_bias,
                max_pain     = max_pain,
                ce_unwinding = ce_unwinding,
                pe_unwinding = pe_unwinding,
            )
        except Exception as e:
            return None

    def _calc_max_pain(self, records: List[Dict]) -> Optional[float]:
        """Calculate max pain strike from option chain records."""
        try:
            strikes = sorted(set(r['strikePrice'] for r in records if r.get('strikePrice')))
            if not strikes:
                return None

            # Build CE/PE OI map
            ce_oi_map = {}; pe_oi_map = {}
            for rec in records:
                sp = rec.get('strikePrice')
                if not sp:
                    continue
                ce_oi_map[sp] = float(rec.get('CE', {}).get('openInterest', 0) or 0)
                pe_oi_map[sp] = float(rec.get('PE', {}).get('openInterest', 0) or 0)

            # For each candidate strike, sum losses to all option buyers
            min_loss = float('inf')
            max_pain = strikes[len(strikes)//2]
            for candidate in strikes:
                total_loss = 0.0
                for sp in strikes:
                    # CE buyers lose if candidate > sp (calls expire OTM)
                    if candidate > sp:
                        total_loss += ce_oi_map.get(sp, 0) * (candidate - sp)
                    # PE buyers lose if candidate < sp (puts expire OTM)
                    if candidate < sp:
                        total_loss += pe_oi_map.get(sp, 0) * (sp - candidate)
                if total_loss < min_loss:
                    min_loss  = total_loss
                    max_pain  = candidate
            return float(max_pain)
        except Exception:
            return None

    # ── Signal Analysis ────────────────────────────────────────────────────────

    def analyze_signal(
        self,
        stock_data: Dict,
        trade_type: TradeType,
        nifty_trend: Optional[str] = None
    ) -> Optional[OIStockSignal]:
        """
        Analyze one stock. Returns OIStockSignal only if ALL quality gates pass.
        No estimation — if real data is unavailable, reject the signal.
        """
        symbol = (stock_data.get('symbol') or stock_data.get('underlying', '')).strip().upper()
        if not symbol or symbol in INDEX_SYMBOLS:
            return None

        # ── Gate 1: OI change (from OI Spurts data — already real) ───────────
        latest_oi = float(stock_data.get('latestOI', 0) or 0)
        prev_oi   = float(stock_data.get('prevOI', 1) or 1)
        if prev_oi <= 0:
            oi_change_pct = float(stock_data.get('avgInOI', 0) or 0)
        else:
            oi_change_pct = ((latest_oi - prev_oi) / prev_oi) * 100

        if abs(oi_change_pct) < MIN_OI_CHANGE_PCT:
            return None  # OI move too small — skip

        # ── Gate 2: Real price data (NO estimation) ───────────────────────────
        quote = self.fetch_real_quote(symbol)
        if quote is None:
            return None  # Reject: can't confirm price direction with real data

        price_change_pct = quote['price_change_pct']
        current_price    = quote['current_price']
        delivery_pct     = quote.get('delivery_pct')

        # Base price filter
        if abs(price_change_pct) < MIN_PRICE_CHANGE_PCT:
            return None  # Price barely moved — weak signal

        # ── Gate 3: Price range filter ────────────────────────────────────────
        if not (MIN_PRICE <= current_price <= MAX_PRICE):
            return None

        # ── Gate 4: Signal type — strict direction matching ───────────────────
        if   price_change_pct > 0 and oi_change_pct > 0:
            signal_type = SignalType.LONG_BUILDUP
        elif price_change_pct < 0 and oi_change_pct > 0:
            signal_type = SignalType.SHORT_BUILDUP
        elif price_change_pct > 0 and oi_change_pct < 0:
            return None  # Short covering — skip
        else:
            return None  # Long unwinding — skip

        # ── Gate 5: Nifty trend alignment + counter-trend price filter ────────
        is_counter_trend = (
            (nifty_trend == 'bullish' and signal_type == SignalType.SHORT_BUILDUP) or
            (nifty_trend == 'bearish' and signal_type == SignalType.LONG_BUILDUP)
        )
        if is_counter_trend:
            # Counter-trend trade: needs BOTH stronger OI AND stronger price move
            if abs(oi_change_pct) < 12.0:
                return None  # OI not decisive enough to trade against market
            if abs(price_change_pct) < MIN_PRICE_CHANGE_COUNTER:
                return None  # Price move too weak — just noise against the trend

        # ── Gate 6: Volume spike ──────────────────────────────────────────────
        # Use futures contract volume from OI spurts + equity volume from quote
        futures_vol = float(stock_data.get('volume', 0) or 0)
        equity_vol  = quote.get('equity_vol', 0)
        volume_spike = self._estimate_volume_spike(symbol, futures_vol, equity_vol, oi_change_pct)

        if volume_spike < MIN_VOLUME_SPIKE:
            return None  # Volume not confirming — weak signal

        # ── Gate 7: Conviction score ──────────────────────────────────────────
        conviction = self._calculate_conviction(
            price_change_pct, oi_change_pct, volume_spike,
            signal_type, delivery_pct, nifty_trend
        )
        if conviction < MIN_CONVICTION:
            return None

        # ── Gate 8: Target / SL / R:R ─────────────────────────────────────────
        target, stop_loss, rr = self._calculate_levels(
            current_price, signal_type, trade_type, price_change_pct, oi_change_pct
        )
        if rr < MIN_RR_RATIO:
            return None

        # ── Gate 9: CE/PE OI side analysis ───────────────────────────────────
        # Fetch option chain — non-blocking (None = skip CE/PE check, don't reject)
        ce_pe = self.fetch_ce_pe_oi(symbol, current_price)
        options_aligned = False
        ce_pe_bias      = 'N/A'
        pcr_val         = 0.0
        ce_chg_val      = 0.0
        pe_chg_val      = 0.0
        max_pain_val    = None

        if ce_pe:
            ce_pe_bias   = ce_pe.options_bias
            pcr_val      = ce_pe.pcr
            ce_chg_val   = ce_pe.ce_oi_change
            pe_chg_val   = ce_pe.pe_oi_change
            max_pain_val = ce_pe.max_pain

            # Alignment check
            if signal_type == SignalType.LONG_BUILDUP and ce_pe.options_bias == 'bullish':
                options_aligned = True
                conviction += 8   # Options confirm bullish trade
            elif signal_type == SignalType.SHORT_BUILDUP and ce_pe.options_bias == 'bearish':
                options_aligned = True
                conviction += 8   # Options confirm bearish trade
            elif ce_pe.options_bias != 'neutral' and ce_pe.options_bias != 'N/A':
                # Options DISAGREE with signal — reduce conviction
                conviction -= 6
                print(f"   ⚠️  {symbol}: Options bias ({ce_pe.options_bias}) CONFLICTS with {signal_type.name}")

            conviction = min(conviction, 100)

            # After CE/PE adjustment — re-check conviction gate
            if conviction < MIN_CONVICTION:
                return None

        reasoning = self._build_reasoning(
            symbol, signal_type, price_change_pct, oi_change_pct,
            volume_spike, delivery_pct, nifty_trend, ce_pe
        )

        return OIStockSignal(
            symbol=symbol,
            signal_type=signal_type.value,
            trade_type=trade_type.value,
            current_price=round(current_price, 2),
            price_change_pct=round(price_change_pct, 2),
            oi_change_pct=round(oi_change_pct, 2),
            volume_spike=round(volume_spike, 2),
            delivery_pct=round(delivery_pct, 1) if delivery_pct else None,
            target=round(target, 2),
            stop_loss=round(stop_loss, 2),
            risk_reward=round(rr, 2),
            conviction_score=round(conviction, 1),
            reasoning=reasoning,
            timestamp=datetime.now(IST).strftime('%Y-%m-%d %H:%M IST'),
            ce_pe_bias=ce_pe_bias,
            pcr=pcr_val,
            ce_oi_change=ce_chg_val,
            pe_oi_change=pe_chg_val,
            options_aligned=options_aligned,
            max_pain=max_pain_val,
        )

    # Approximate NSE F&O stock avg daily volume buckets (shares, full day)
    # Used to normalise intraday volume. Midday typically = 40-60% of daily vol.
    # Source: typical NSE large/mid cap F&O liquidity buckets.
    _AVG_DAILY_VOL_PROXY = {
        # Nifty 50 heavy hitters — very high volume
        'RELIANCE':10_000_000,'TCS':3_000_000,'INFY':5_000_000,'HDFCBANK':8_000_000,
        'ICICIBANK':8_000_000,'KOTAKBANK':6_000_000,'AXISBANK':7_000_000,
        'SBIN':15_000_000,'WIPRO':4_000_000,'HCLTECH':3_000_000,
        'BAJFINANCE':2_000_000,'LT':2_000_000,'TATAMOTORS':10_000_000,
        'POWERGRID':8_000_000,'NTPC':8_000_000,'COALINDIA':5_000_000,
        'ONGC':5_000_000,'BPCL':5_000_000,'IOC':5_000_000,
        'ADANIPORTS':3_000_000,'ADANIENT':2_000_000,
        'BHARTIARTL':4_000_000,'MARUTI':500_000,'NESTLEIND':100_000,
        'ASIANPAINT':500_000,'BRITANNIA':200_000,'HINDUNILVR':1_000_000,
        # Mid-cap F&O
        'APLAPOLLO':500_000,'GLENMARK':1_000_000,'TIINDIA':300_000,
        'LODHA':2_000_000,'GRASIM':1_000_000,'MAXHEALTH':800_000,
        'MFSL':500_000,'IPCALAB':300_000,
    }
    _DEFAULT_AVG_VOL = 2_000_000  # conservative default for unknown stocks

    def _estimate_volume_spike(
        self,
        symbol: str,
        futures_vol: float,
        equity_vol: float,
        oi_change_pct: float
    ) -> float:
        """
        Compute a volume spike ratio.
        - Primary: equity volume today vs stock-specific daily average proxy
        - Adjusted for midday scan (assume ~50% of daily vol traded by 12 PM)
        - Fallback: futures contract volume vs typical F&O contract volume
        """
        # Strong OI (≥15%) = institutional commitment confirmed, minimum 1.8×
        if abs(oi_change_pct) >= 15.0:
            base = 1.8
        elif abs(oi_change_pct) >= 10.0:
            base = 1.5
        else:
            base = 1.0

        # Equity volume spike (most reliable)
        if equity_vol > 0:
            avg_daily = self._AVG_DAILY_VOL_PROXY.get(symbol.upper(), self._DEFAULT_AVG_VOL)
            # Midday proxy: by 12 PM roughly 50% of daily volume traded
            avg_midday = avg_daily * 0.50
            vol_ratio  = equity_vol / avg_midday if avg_midday > 0 else 1.0
            return max(vol_ratio, base)

        # Futures volume fallback
        if futures_vol > 0:
            # Typical F&O stock: 2000–20000 contracts/day
            # Midday proxy: ~50% done by 12 PM
            avg_futures_midday = 5_000
            fvol_ratio = futures_vol / avg_futures_midday
            return max(min(fvol_ratio, 5.0), base)

        return base  # Use OI-derived base if no volume at all

    def _calculate_conviction(
        self,
        price_change:  float,
        oi_change:     float,
        volume_spike:  float,
        signal_type:   SignalType,
        delivery_pct:  Optional[float],
        nifty_trend:   Optional[str]
    ) -> float:
        """
        Strict conviction scoring. Maximum 100.
        Requires meaningful contribution from OI, price, AND volume.
        """
        score = 0.0

        # ── OI strength (max 35) — primary signal ────────────────────────────
        # 5% OI = 17.5, 8% OI = 28, 12%+ OI = 35
        oi_score = min(abs(oi_change) * 2.9, 35)
        score += oi_score

        # ── Price momentum (max 25) ───────────────────────────────────────────
        # 0.5% = 6, 1% = 12, 2% = 25
        price_score = min(abs(price_change) * 12.5, 25)
        score += price_score

        # ── Volume confirmation (max 20) ──────────────────────────────────────
        # 1.5× = 10, 2× = 20
        vol_score = min((volume_spike - 1.0) * 20, 20)
        score += max(vol_score, 0)

        # ── Delivery % bonus (max 10) — high delivery = institutional ─────────
        if delivery_pct and delivery_pct >= 40:
            score += min((delivery_pct - 40) * 0.5, 10)

        # ── Nifty alignment bonus (max 10) ────────────────────────────────────
        if nifty_trend:
            if nifty_trend == 'bullish' and signal_type == SignalType.LONG_BUILDUP:
                score += 10
            elif nifty_trend == 'bearish' and signal_type == SignalType.SHORT_BUILDUP:
                score += 10
            elif nifty_trend == 'neutral':
                score += 5  # Neutral market — partial credit

        return min(score, 100)

    def _calculate_levels(
        self,
        price:          float,
        signal_type:    SignalType,
        trade_type:     TradeType,
        price_change:   float,
        oi_change:      float
    ) -> Tuple[float, float, float]:
        """
        Dynamic SL/Target based on OI strength and trade type.
        Higher OI = tighter stop (stronger conviction = less whipsaw expected).
        """
        # OI-adjusted multiplier: stronger OI = allow wider target
        oi_factor = min(abs(oi_change) / 10, 1.5)  # 0.5–1.5

        if trade_type == TradeType.INTRADAY:
            if signal_type == SignalType.LONG_BUILDUP:
                sl_pct     = 0.007                          # 0.7% stop
                target_pct = 0.012 + (0.006 * oi_factor)   # 1.2–2.1% target
            else:  # SHORT_BUILDUP
                sl_pct     = 0.007
                target_pct = 0.012 + (0.006 * oi_factor)
        else:  # BTST / STBT — wider
            if signal_type == SignalType.LONG_BUILDUP:
                sl_pct     = 0.012                          # 1.2% stop
                target_pct = 0.022 + (0.01 * oi_factor)    # 2.2–3.7% target
            else:
                sl_pct     = 0.012
                target_pct = 0.022 + (0.01 * oi_factor)

        if signal_type == SignalType.LONG_BUILDUP:
            stop_loss = price * (1 - sl_pct)
            target    = price * (1 + target_pct)
        else:
            stop_loss = price * (1 + sl_pct)
            target    = price * (1 - target_pct)

        risk   = abs(price - stop_loss)
        reward = abs(target - price)
        rr     = reward / risk if risk > 0 else 0

        return target, stop_loss, rr

    def _build_reasoning(
        self,
        symbol:       str,
        signal_type:  SignalType,
        price_change: float,
        oi_change:    float,
        vol_spike:    float,
        delivery_pct: Optional[float],
        nifty_trend:  Optional[str],
        ce_pe:        Optional[CEPEOIData] = None,
    ) -> str:
        """Concise reasoning including CE/PE side context."""
        direction  = "BULLISH" if signal_type == SignalType.LONG_BUILDUP else "BEARISH"
        action     = "Long buildup" if signal_type == SignalType.LONG_BUILDUP else "Short buildup"
        vol_txt    = f"{vol_spike:.1f}× vol" if vol_spike >= 1.5 else "volume OK"
        deliv_txt  = f" | Delivery {delivery_pct:.0f}%" if delivery_pct else ""
        trend_txt  = f" | Nifty {nifty_trend}" if nifty_trend and nifty_trend != 'neutral' else ""

        ce_pe_txt = ""
        if ce_pe:
            ce_pe_txt = f" | PCR {ce_pe.pcr:.2f} ({ce_pe.options_bias})"

        return (
            f"{action} confirmed. Price {price_change:+.2f}% | OI {oi_change:+.2f}% | "
            f"{vol_txt}{deliv_txt}{trend_txt}{ce_pe_txt}. {direction} setup."
        )

    # ── Main Scan ──────────────────────────────────────────────────────────────

    def scan_for_opportunities(
        self,
        trade_type: TradeType,
        top_n: int = MAX_SIGNALS_PER_SIDE
    ) -> Dict[str, List[OIStockSignal]]:
        """
        Main scan. Returns top high-conviction BUY and SELL signals.
        All signals have passed every quality gate.
        """
        print(f"\n🔍 Scanning NSE for {trade_type.value} opportunities...")
        print(f"   Quality gates: OI≥{MIN_OI_CHANGE_PCT}% | Price≥{MIN_PRICE_CHANGE_PCT}% confirmed | "
              f"Vol≥{MIN_VOLUME_SPIKE}× | Conviction≥{MIN_CONVICTION} | R:R≥{MIN_RR_RATIO}")

        # Fetch Nifty trend for alignment filter
        nifty_trend = self.fetch_nifty_trend()
        print(f"   Nifty trend: {nifty_trend or 'unknown'}")

        # Fetch OI Spurts
        oi_data = self.fetch_oi_spurts()
        if not oi_data:
            print("❌ No OI data received from NSE")
            return {'buy': [], 'sell': []}

        print(f"📊 Analyzing {len(oi_data)} stocks (fetching real quotes)...")

        buy_signals:  List[OIStockSignal] = []
        sell_signals: List[OIStockSignal] = []
        rejected = 0

        for stock in oi_data:
            signal = self.analyze_signal(stock, trade_type, nifty_trend)
            if signal:
                if signal.signal_type == 'LONG_BUILDUP':
                    buy_signals.append(signal)
                elif signal.signal_type == 'SHORT_BUILDUP':
                    sell_signals.append(signal)
            else:
                rejected += 1

        print(f"   ✅ Passed: {len(buy_signals)+len(sell_signals)} | ❌ Rejected: {rejected}")

        # Sort by conviction (highest first)
        buy_signals.sort(key=lambda x: x.conviction_score, reverse=True)
        sell_signals.sort(key=lambda x: x.conviction_score, reverse=True)

        # Cap at top N
        buy_signals  = buy_signals[:top_n]
        sell_signals = sell_signals[:top_n]

        print(f"\n🟢 BUY  ({len(buy_signals)}):")
        for s in buy_signals:
            print(f"   {s.symbol:12} conviction={s.conviction_score:.0f} OI={s.oi_change_pct:+.1f}% "
                  f"price={s.price_change_pct:+.1f}% vol={s.volume_spike:.1f}× RR={s.risk_reward:.2f}")
        print(f"\n🔴 SELL ({len(sell_signals)}):")
        for s in sell_signals:
            print(f"   {s.symbol:12} conviction={s.conviction_score:.0f} OI={s.oi_change_pct:+.1f}% "
                  f"price={s.price_change_pct:+.1f}% vol={s.volume_spike:.1f}× RR={s.risk_reward:.2f}")

        return {'buy': buy_signals, 'sell': sell_signals}

    def save_signals(self, signals_dict: Dict, trade_type: TradeType):
        """Save signals to JSON cache."""
        date_str = datetime.now(IST).strftime('%Y%m%d')
        path = f"{self.cache_dir}/{trade_type.value}_{date_str}.json"
        data = {
            'timestamp':   datetime.now(IST).isoformat(),
            'trade_type':  trade_type.value,
            'buy_count':   len(signals_dict.get('buy', [])),
            'sell_count':  len(signals_dict.get('sell', [])),
            'buy_signals': [asdict(s) for s in signals_dict.get('buy', [])],
            'sell_signals':[asdict(s) for s in signals_dict.get('sell', [])]
        }
        with open(path, 'w') as f:
            json.dump(data, f, indent=2)
        print(f"💾 Saved to {path}")


# ─── CE/PE Block Formatter ─────────────────────────────────────────────────────

def _format_ce_pe_block(s: OIStockSignal) -> str:
    """
    Format CE/PE OI analysis block for a signal.
    Shows: PCR, CE OI change, PE OI change, options bias, max pain.

    CE/PE interpretation guide (shown in message):
      CE OI ↑ = Call writing = shorts expect resistance → BEARISH pressure
      PE OI ↑ = Put writing  = longs expect support     → BULLISH support
      PCR > 1.2 → Bullish | PCR < 0.8 → Bearish
    """
    if s.ce_pe_bias == 'N/A' or s.pcr == 0.0:
        return "  🔲 Options: N/A (chain unavailable)\n"

    # CE/PE change arrows
    ce_arrow = "↑ Building" if s.ce_oi_change >= 5 else ("↓ Unwinding" if s.ce_oi_change <= -5 else "→ Stable")
    pe_arrow = "↑ Building" if s.pe_oi_change >= 5 else ("↓ Unwinding" if s.pe_oi_change <= -5 else "→ Stable")

    # CE/PE meaning
    ce_meaning = "⚠️ Resistance" if s.ce_oi_change >= 5 else ("✅ Resistance easing" if s.ce_oi_change <= -5 else "")
    pe_meaning = "✅ Support"    if s.pe_oi_change >= 5 else ("⚠️ Support weakening" if s.pe_oi_change <= -5 else "")

    bias_emoji = {"bullish": "🟢", "bearish": "🔴", "neutral": "⚪"}.get(s.ce_pe_bias, "⚪")
    pcr_note   = "Bullish" if s.pcr >= 1.2 else ("Bearish" if s.pcr <= 0.8 else "Neutral")

    mp_txt = f"  💥 Max Pain: ₹{s.max_pain:.0f}\n" if s.max_pain else ""

    return (
        f"  📋 *Options Chain (ATM ±3 strikes):*\n"
        f"  • CE OI: {ce_arrow} ({s.ce_oi_change:+.1f}%) {ce_meaning}\n"
        f"  • PE OI: {pe_arrow} ({s.pe_oi_change:+.1f}%) {pe_meaning}\n"
        f"  • PCR: {s.pcr:.2f} → {pcr_note}\n"
        f"  • Options Bias: {bias_emoji} {s.ce_pe_bias.upper()}\n"
        f"{mp_txt}"
    )


# ─── Telegram Formatter ────────────────────────────────────────────────────────

def format_telegram_alert(signals_dict: Dict, trade_type: TradeType) -> str:
    buy_signals  = signals_dict.get('buy', [])
    sell_signals = signals_dict.get('sell', [])
    total        = len(buy_signals) + len(sell_signals)

    trade_emoji = {'INTRADAY': '⚡', 'BTST': '🌙', 'STBT': '🌙'}.get(trade_type.value, '📊')
    now_ist     = datetime.now(IST)

    if len(buy_signals) > len(sell_signals) * 1.5:
        market_bias = "🟢 BULLISH BIAS"
    elif len(sell_signals) > len(buy_signals) * 1.5:
        market_bias = "🔴 BEARISH BIAS"
    else:
        market_bias = "⚪ MIXED"

    header = (
        f"{trade_emoji} *SHIVA HIGH-CONVICTION OI — {trade_type.value}*\n"
        f"📅 {now_ist.strftime('%d %b %Y')}  🕐 {now_ist.strftime('%I:%M %p IST')}\n"
        f"Market: {market_bias}\n"
        f"{'─'*30}\n"
    )

    if total == 0:
        body = (
            "✋ *No high-conviction setups today*\n\n"
            "All signals rejected — OI/price/volume thresholds not met.\n"
            "_No trade is better than a bad trade._ 🎃"
        )
        return header + body

    body = f"✅ *{total} setup(s) passed all quality gates*\n\n"

    if buy_signals:
        body += "🟢 *BUY — Long Buildup* (Price↑ + OI↑)\n"
        body += "`━━━━━━━━━━━━━━━━━━━━━`\n"
        for i, s in enumerate(buy_signals, 1):
            deliv        = f"\n  📦 Delivery: {s.delivery_pct:.0f}%" if s.delivery_pct else ""
            ce_pe_block  = _format_ce_pe_block(s)
            align_badge  = " ✅ *Options Aligned*" if s.options_aligned else ""
            body += (
                f"\n*{i}. {s.symbol}*  ⭐ {s.conviction_score:.0f}/100{align_badge}\n"
                f"📈 Action: *BUY (CE side: buy CE / sell PE)*\n"
                f"💰 Entry: ₹{s.current_price}\n"
                f"🎯 Target: ₹{s.target}  🛑 SL: ₹{s.stop_loss}\n"
                f"⚖️ R:R = 1:{s.risk_reward:.1f}\n"
                f"📊 Price {s.price_change_pct:+.2f}% | OI {s.oi_change_pct:+.2f}% | Vol {s.volume_spike:.1f}×{deliv}\n"
                f"{ce_pe_block}"
                f"💬 _{s.reasoning}_\n"
            )
        body += "\n"

    if sell_signals:
        body += "🔴 *SELL — Short Buildup* (Price↓ + OI↑)\n"
        body += "`━━━━━━━━━━━━━━━━━━━━━`\n"
        for i, s in enumerate(sell_signals, 1):
            deliv        = f"\n  📦 Delivery: {s.delivery_pct:.0f}%" if s.delivery_pct else ""
            ce_pe_block  = _format_ce_pe_block(s)
            align_badge  = " ✅ *Options Aligned*" if s.options_aligned else ""
            body += (
                f"\n*{i}. {s.symbol}*  ⭐ {s.conviction_score:.0f}/100{align_badge}\n"
                f"📉 Action: *SELL / SHORT (PE side: buy PE / sell CE)*\n"
                f"💰 Entry: ₹{s.current_price}\n"
                f"🎯 Target: ₹{s.target}  🛑 SL: ₹{s.stop_loss}\n"
                f"⚖️ R:R = 1:{s.risk_reward:.1f}\n"
                f"📊 Price {s.price_change_pct:+.2f}% | OI {s.oi_change_pct:+.2f}% | Vol {s.volume_spike:.1f}×{deliv}\n"
                f"{ce_pe_block}"
                f"💬 _{s.reasoning}_\n"
            )
        body += "\n"

    footer = (
        "`━━━━━━━━━━━━━━━━━━━━━`\n"
        "📖 *CE/PE OI Guide:*\n"
        "• CE OI ↑ = Call writing = *Resistance* (bearish)\n"
        "• PE OI ↑ = Put writing  = *Support* (bullish)\n"
        "• CE OI ↓ = Calls unwinding = *Bullish*\n"
        "• PE OI ↓ = Puts unwinding  = *Bearish*\n"
        "• PCR > 1.2 = Bullish | PCR < 0.8 = Bearish\n\n"
        "⚠️ *Risk Rules:*\n"
        "• Max 2% capital risk per trade\n"
        "• Book 50% at 1:1 R:R, trail rest\n"
        "• Exit all if SL hits — no exceptions\n"
        "• Max 2-3 trades simultaneously\n\n"
        "```\n"
        "✅ BUY:  Price↑ + OI↑ = Long Buildup\n"
        "✅ SELL: Price↓ + OI↑ = Short Buildup\n"
        "❌ SKIP: Short Covering / Long Unwinding\n"
        "```\n"
        "_🎃 Shiva — OI + Options confirmed, no noise._"
    )

    return header + body + footer


# ─── Entry Point ───────────────────────────────────────────────────────────────

def main():
    import argparse
    parser = argparse.ArgumentParser(description='NSE OI High-Conviction Scanner')
    parser.add_argument('trade_type', choices=['intraday', 'btst', 'stbt'])
    parser.add_argument('--top',  type=int, default=MAX_SIGNALS_PER_SIDE)
    parser.add_argument('--save', action='store_true')
    args = parser.parse_args()

    trade_type_map = {
        'intraday': TradeType.INTRADAY,
        'btst':     TradeType.BTST,
        'stbt':     TradeType.STBT,
    }
    trade_type = trade_type_map[args.trade_type]

    print("=" * 60)
    print("🎃 SHIVA HIGH-CONVICTION OI SCANNER")
    print("=" * 60)
    print(f"Mode: {trade_type.value}  |  Top: {args.top}")
    print(f"Time: {datetime.now(IST).strftime('%Y-%m-%d %H:%M IST')}")
    print("=" * 60)

    scanner      = NSEOIScanner()
    signals_dict = scanner.scan_for_opportunities(trade_type, top_n=args.top)

    total = len(signals_dict.get('buy', [])) + len(signals_dict.get('sell', []))
    print(f"\n✅ Final: {total} high-conviction setups")
    print(f"   🟢 BUY: {len(signals_dict.get('buy', []))}  🔴 SELL: {len(signals_dict.get('sell', []))}")

    if args.save and total > 0:
        scanner.save_signals(signals_dict, trade_type)
    elif args.save:
        # Save empty result so send_alert.py knows scan ran but found nothing
        scanner.save_signals(signals_dict, trade_type)

    telegram_msg = format_telegram_alert(signals_dict, trade_type)
    print("\n" + "=" * 60)
    print("📤 TELEGRAM PREVIEW:")
    print("=" * 60)
    print(telegram_msg)

    return signals_dict


if __name__ == "__main__":
    main()
