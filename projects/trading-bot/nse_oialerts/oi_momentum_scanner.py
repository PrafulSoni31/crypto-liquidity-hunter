#!/usr/bin/env python3
"""
NSE OI-Based Momentum Stock Scanner — High Conviction Only  v3.0
=================================================================
Trading Rules (Charlie's mandate):
  Price ↑ + OI ↑ = Long Buildup  (BULLISH) → BUY
  Price ↓ + OI ↑ = Short Buildup (BEARISH) → SELL
  Price ↑ + OI ↓ = Short Covering (WEAK)   → SKIP
  Price ↓ + OI ↓ = Long Unwinding (WEAK)   → SKIP

v3.0 Fixes applied:
  1. Price exhaustion filter — avoid entering at top of spike
  2. Dynamic SL based on price level + move extent (no more flat 0.7%)
  3. Bad data filter — reject >15% moves (ex-date / data errors)
  4. VWAP gate — price must be above VWAP for longs, below for shorts
  5. Time-of-day session context — OPENING / MIDDAY / CLOSING modes
  6. Delivery % gating — low delivery (<25%) rejects long buildup
  7. Duplicate signal filter — flags repeat stocks, higher conviction required
  8. Volume spike tiers by price level (no more hardcoded fallback)
  9. Cleaner alert format — VWAP, entry quality, session badge shown

Schedule:
  9:30 AM IST  → INTRADAY  (same-day exit)
  3:15 PM IST  → BTST/STBT (overnight hold)
"""

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass, asdict, field
from enum import Enum
import requests
import time


# ─── Constants ─────────────────────────────────────────────────────────────────

MIN_OI_CHANGE_PCT          = 5.0    # OI must move ≥ 5%
MIN_PRICE_CHANGE_PCT       = 0.8    # Price must move ≥ 0.8% confirmed
MIN_PRICE_CHANGE_COUNTER   = 1.2    # Counter-trend trades need stronger price
MIN_VOLUME_SPIKE           = 1.5    # Volume spike threshold
MIN_CONVICTION             = 72     # Base conviction threshold
MIN_RR_RATIO               = 1.8    # Minimum reward/risk
MAX_SIGNALS_PER_SIDE       = 3      # Top 3 per side max
MAX_PRICE_CHANGE_PCT       = 15.0   # Reject if >15% — likely ex-date/bad data

MIN_PRICE                  = 50.0
MAX_PRICE                  = 5000.0

# Delivery % thresholds
MIN_DELIVERY_LONG          = 25.0   # Long buildup requires ≥ 25% delivery
DELIVERY_BONUS_THRESHOLD   = 40.0   # Bonus delivery starts at 40%

# VWAP tolerance — price must be within this band of VWAP for alignment
VWAP_TOLERANCE             = 0.002  # 0.2% either side counts as "at VWAP"

# Repeat signal thresholds
REPEAT_CONVICTION_PENALTY  = 8      # Extra conviction needed for repeat signals
MAX_REPEAT_DAYS            = 2      # Skip if same signal appeared ≥ this many consecutive days

# Indices to skip
INDEX_SYMBOLS = {
    'NIFTY', 'BANKNIFTY', 'FINNIFTY', 'NIFTYMID50', 'NIFTYIT',
    'NIFTYPVTBANK', 'NIFTYPSUBANK', 'NIFTYAUTO', 'NIFTYFMCG',
    'NIFTYINFRA', 'NIFTYMEDIA', 'NIFTYMETAL', 'NIFTYPHARMA',
    'NIFTYREALTY', 'NIFTYCONS', 'NIFTYENERGY', 'NIFTYFIN',
    'MIDCPNIFTY', 'SENSEX', 'BANKEX'
}

IST = timezone(timedelta(hours=5, minutes=30))


# ─── Enums ─────────────────────────────────────────────────────────────────────

class SignalType(Enum):
    LONG_BUILDUP   = "LONG_BUILDUP"
    SHORT_BUILDUP  = "SHORT_BUILDUP"
    SHORT_COVERING = "SHORT_COVERING"
    LONG_UNWINDING = "LONG_UNWINDING"


class TradeType(Enum):
    INTRADAY = "INTRADAY"
    BTST     = "BTST"
    STBT     = "STBT"


class EntryQuality(Enum):
    FRESH    = "FRESH"     # Price moved < 1.5% — ideal entry point
    MODERATE = "MODERATE"  # Price moved 1.5-2.5% — acceptable
    EXTENDED = "EXTENDED"  # Price moved > 2.5% — risky, wider SL needed


class SessionContext(Enum):
    OPENING = "OPENING"   # 9:30-10:30 AM IST
    MIDDAY  = "MIDDAY"    # 10:30 AM-1:30 PM IST
    CLOSING = "CLOSING"   # 1:30-3:15 PM IST
    BTST    = "BTST"      # 3:15 PM+ IST


# ─── Data Classes ──────────────────────────────────────────────────────────────

@dataclass
class CEPEOIData:
    atm_ce_oi:    float
    atm_pe_oi:    float
    ce_oi_change: float
    pe_oi_change: float
    pcr:          float
    ce_building:  bool
    pe_building:  bool
    options_bias: str
    max_pain:     Optional[float]
    ce_unwinding: bool
    pe_unwinding: bool


@dataclass
class OIStockSignal:
    symbol:           str
    signal_type:      str
    trade_type:       str
    current_price:    float
    price_change_pct: float
    oi_change_pct:    float
    volume_spike:     float
    delivery_pct:     Optional[float]
    target:           float
    stop_loss:        float
    risk_reward:      float
    conviction_score: float
    reasoning:        str
    timestamp:        str
    vwap:             Optional[float] = None
    vwap_aligned:     bool = False
    entry_quality:    str = "FRESH"
    session_context:  str = "OPENING"
    is_repeat:        bool = False
    repeat_days:      int = 0
    ce_pe_bias:       str = "N/A"
    pcr:              float = 0.0
    ce_oi_change:     float = 0.0
    pe_oi_change:     float = 0.0
    options_aligned:  bool = False
    max_pain:         Optional[float] = None


# ─── Scanner ───────────────────────────────────────────────────────────────────

class NSEOIScanner:

    BASE_URL = "https://www.nseindia.com"

    HEADERS = {
        'User-Agent':      'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36',
        'Accept':          'application/json, text/plain, */*',
        'Accept-Language': 'en-US,en;q=0.9',
        'Accept-Encoding': 'gzip, deflate',
        'Referer':         'https://www.nseindia.com/',
        'Connection':      'keep-alive',
    }

    def __init__(self):
        self.session   = requests.Session()
        self.session.headers.update(self.HEADERS)
        self.cache_dir = "projects/trading-bot/nse_oialerts/cache"
        os.makedirs(self.cache_dir, exist_ok=True)

    # ── Helpers ────────────────────────────────────────────────────────────────

    def _get_session_context(self) -> SessionContext:
        """Determine current market session based on IST time."""
        now = datetime.now(IST)
        hour, minute = now.hour, now.minute
        total_min = hour * 60 + minute

        if total_min < 9 * 60 + 30:
            return SessionContext.OPENING
        elif total_min < 10 * 60 + 30:
            return SessionContext.OPENING
        elif total_min < 13 * 60 + 30:
            return SessionContext.MIDDAY
        elif total_min < 15 * 60 + 15:
            return SessionContext.CLOSING
        else:
            return SessionContext.BTST

    def _get_entry_quality(self, price_change_pct: float) -> EntryQuality:
        """Classify entry quality based on how much price already moved."""
        abs_chg = abs(price_change_pct)
        if abs_chg < 1.5:
            return EntryQuality.FRESH
        elif abs_chg < 2.5:
            return EntryQuality.MODERATE
        else:
            return EntryQuality.EXTENDED

    def _get_default_avg_vol(self, price: float) -> int:
        """Tiered daily volume estimate based on stock price (more realistic)."""
        if price > 5000:
            return 300_000
        elif price > 2000:
            return 800_000
        elif price > 1000:
            return 1_500_000
        elif price > 500:
            return 3_000_000
        elif price > 100:
            return 6_000_000
        else:
            return 12_000_000

    # ── Data Fetchers ──────────────────────────────────────────────────────────

    def fetch_oi_spurts(self) -> List[Dict]:
        """Fetch OI Spurts list from NSE."""
        try:
            url = f"{self.BASE_URL}/api/live-analysis-oi-spurts-underlyings"
            r   = requests.get(url, headers=self.HEADERS, timeout=15)
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
        """Fetch Nifty 50 direction. Returns 'bullish', 'bearish', 'neutral', or None."""
        try:
            url = f"{self.BASE_URL}/api/allIndices"
            r   = requests.get(url, headers=self.HEADERS, timeout=8)
            if r.status_code == 200:
                for idx in r.json().get('data', []):
                    if idx.get('index') in ('NIFTY 50', 'Nifty 50', 'NIFTY50'):
                        pc = float(idx.get('percentChange', 0))
                        if pc >= 0.2:    return 'bullish'
                        elif pc <= -0.2: return 'bearish'
                        return 'neutral'
        except Exception:
            pass
        return None

    def fetch_real_quote(self, symbol: str) -> Optional[Dict]:
        """
        Fetch real price, VWAP, delivery%, and volume for a symbol.
        Returns None if essential data unavailable.
        """
        try:
            url = f"{self.BASE_URL}/api/quote-equity?symbol={symbol}"
            r   = requests.get(url, headers=self.HEADERS, timeout=6)
            if r.status_code != 200:
                return None
            pi = r.json().get('priceInfo', {})

            price_change_pct = pi.get('pChange')
            current_price    = pi.get('lastPrice') or pi.get('close')
            vwap             = pi.get('vwap')

            if price_change_pct is None or current_price is None:
                return None

            # Volume + delivery
            url2 = f"{self.BASE_URL}/api/quote-equity?symbol={symbol}&section=trade_info"
            r2   = requests.get(url2, headers=self.HEADERS, timeout=6)
            delivery_pct = None
            equity_vol   = 0
            if r2.status_code == 200:
                data2 = r2.json()
                dp    = data2.get('securityWiseDP', {})
                delivery_pct = dp.get('deliveryToTradedQuantity')
                equity_vol   = float(dp.get('quantityTraded', 0) or 0)

            return {
                'price_change_pct': float(price_change_pct),
                'current_price':    float(current_price),
                'vwap':             float(vwap) if vwap else None,
                'equity_vol':       equity_vol,
                'delivery_pct':     float(delivery_pct) if delivery_pct is not None else None,
            }
        except Exception:
            return None

    def fetch_ce_pe_oi(self, symbol: str, current_price: float) -> Optional[CEPEOIData]:
        """Fetch NSE option chain and compute CE/PE OI dynamics."""
        try:
            url  = f"{self.BASE_URL}/api/option-chain-equities?symbol={symbol}"
            r    = requests.get(url, headers=self.HEADERS, timeout=10)
            if r.status_code != 200:
                return None
            data    = r.json()
            records = data.get('records', {}).get('data', [])
            if not records:
                return None

            strikes = [rec['strikePrice'] for rec in records if rec.get('strikePrice')]
            if not strikes:
                return None
            atm_strike  = min(strikes, key=lambda x: abs(x - current_price))
            strike_list = sorted(set(strikes))
            atm_idx     = strike_list.index(atm_strike)
            near_strikes = strike_list[max(0, atm_idx - 3): atm_idx + 4]

            total_ce_oi=0.0; total_pe_oi=0.0
            total_ce_chg=0.0; total_pe_chg=0.0
            atm_ce_oi=0.0;   atm_pe_oi=0.0
            all_ce_oi=0.0;   all_pe_oi=0.0

            for rec in records:
                sp  = rec.get('strikePrice', 0)
                ce  = rec.get('CE', {})
                pe  = rec.get('PE', {})
                ce_oi  = float(ce.get('openInterest', 0) or 0)
                pe_oi  = float(pe.get('openInterest', 0) or 0)
                ce_chg = float(ce.get('changeinOpenInterest', 0) or 0)
                pe_chg = float(pe.get('changeinOpenInterest', 0) or 0)
                all_ce_oi += ce_oi
                all_pe_oi += pe_oi
                if sp in near_strikes:
                    total_ce_oi  += ce_oi;  total_pe_oi  += pe_oi
                    total_ce_chg += ce_chg; total_pe_chg += pe_chg
                if sp == atm_strike:
                    atm_ce_oi = ce_oi; atm_pe_oi = pe_oi

            pcr         = round(all_pe_oi / all_ce_oi, 2) if all_ce_oi > 0 else 0.0
            ce_chg_pct  = (total_ce_chg / total_ce_oi * 100) if total_ce_oi > 0 else 0.0
            pe_chg_pct  = (total_pe_chg / total_pe_oi * 100) if total_pe_oi > 0 else 0.0

            ce_building  = ce_chg_pct >= 5.0
            pe_building  = pe_chg_pct >= 5.0
            ce_unwinding = ce_chg_pct <= -5.0
            pe_unwinding = pe_chg_pct <= -5.0

            bullish_signals = int(pe_building) + int(ce_unwinding) + int(pcr >= 1.2)
            bearish_signals = int(ce_building) + int(pe_unwinding) + int(pcr <= 0.8)

            if bullish_signals >= 2:   options_bias = 'bullish'
            elif bearish_signals >= 2: options_bias = 'bearish'
            else:                      options_bias = 'neutral'

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
        except Exception:
            return None

    def _calc_max_pain(self, records: List[Dict]) -> Optional[float]:
        try:
            strikes = sorted(set(r['strikePrice'] for r in records if r.get('strikePrice')))
            if not strikes:
                return None
            ce_oi_map = {}; pe_oi_map = {}
            for rec in records:
                sp = rec.get('strikePrice')
                if not sp: continue
                ce_oi_map[sp] = float(rec.get('CE', {}).get('openInterest', 0) or 0)
                pe_oi_map[sp] = float(rec.get('PE', {}).get('openInterest', 0) or 0)
            min_loss = float('inf')
            max_pain = strikes[len(strikes) // 2]
            for candidate in strikes:
                total_loss = 0.0
                for sp in strikes:
                    if candidate > sp: total_loss += ce_oi_map.get(sp, 0) * (candidate - sp)
                    if candidate < sp: total_loss += pe_oi_map.get(sp, 0) * (sp - candidate)
                if total_loss < min_loss:
                    min_loss = total_loss; max_pain = candidate
            return float(max_pain)
        except Exception:
            return None

    # ── Repeat Signal Detection ────────────────────────────────────────────────

    def check_repeat_signal(self, symbol: str, direction: str) -> Tuple[bool, int]:
        """
        Check if the same symbol+direction appeared in previous days' cache.
        Returns (is_repeat, consecutive_days).
        """
        consecutive = 0
        date_check  = datetime.now(IST)
        for _ in range(4):  # Check last 4 days
            date_check = date_check - timedelta(days=1)
            # Skip weekends
            if date_check.weekday() >= 5:
                continue
            path = os.path.join("projects/trading-bot/nse_oialerts/cache",
                                f"INTRADAY_{date_check.strftime('%Y%m%d')}.json")
            if not os.path.exists(path):
                break
            try:
                data = json.load(open(path))
                all_prev = data.get('buy_signals', []) + data.get('sell_signals', [])
                found = any(
                    s.get('symbol') == symbol and s.get('signal_type') == direction
                    for s in all_prev
                )
                if found:
                    consecutive += 1
                else:
                    break
            except Exception:
                break

        return (consecutive > 0), consecutive

    # ── Signal Analysis ────────────────────────────────────────────────────────

    def analyze_signal(
        self,
        stock_data:   Dict,
        trade_type:   TradeType,
        nifty_trend:  Optional[str] = None,
        session_ctx:  SessionContext = SessionContext.MIDDAY,
    ) -> Optional[OIStockSignal]:
        """
        Analyze one stock through all quality gates.
        Returns OIStockSignal only if ALL gates pass.
        """
        symbol = (stock_data.get('symbol') or stock_data.get('underlying', '')).strip().upper()
        if not symbol or symbol in INDEX_SYMBOLS:
            return None

        # ── Gate 1: OI change ────────────────────────────────────────────────
        latest_oi = float(stock_data.get('latestOI', 0) or 0)
        prev_oi   = float(stock_data.get('prevOI', 1)   or 1)
        if prev_oi <= 0:
            oi_change_pct = float(stock_data.get('avgInOI', 0) or 0)
        else:
            oi_change_pct = ((latest_oi - prev_oi) / prev_oi) * 100

        if abs(oi_change_pct) < MIN_OI_CHANGE_PCT:
            return None

        # ── Gate 2: Real price data ──────────────────────────────────────────
        quote = self.fetch_real_quote(symbol)
        if quote is None:
            return None

        price_change_pct = quote['price_change_pct']
        current_price    = quote['current_price']
        delivery_pct     = quote.get('delivery_pct')
        vwap             = quote.get('vwap')

        # ── Gate 3: Bad data filter (ex-date/data errors) ────────────────────
        if abs(price_change_pct) > MAX_PRICE_CHANGE_PCT:
            print(f"   ⛔ {symbol}: price change {price_change_pct:.1f}% > {MAX_PRICE_CHANGE_PCT}% — likely ex-date/bad data, skipped")
            return None

        if abs(price_change_pct) < MIN_PRICE_CHANGE_PCT:
            return None

        # ── Gate 4: Price range filter ───────────────────────────────────────
        if not (MIN_PRICE <= current_price <= MAX_PRICE):
            return None

        # ── Gate 5: Signal type ──────────────────────────────────────────────
        if   price_change_pct > 0 and oi_change_pct > 0:
            signal_type = SignalType.LONG_BUILDUP
        elif price_change_pct < 0 and oi_change_pct > 0:
            signal_type = SignalType.SHORT_BUILDUP
        else:
            return None  # Short covering or long unwinding — skip

        # ── Gate 6: Delivery gating for LONG_BUILDUP ─────────────────────────
        if signal_type == SignalType.LONG_BUILDUP and delivery_pct is not None:
            if delivery_pct < MIN_DELIVERY_LONG:
                print(f"   ⛔ {symbol}: delivery {delivery_pct:.1f}% < {MIN_DELIVERY_LONG}% — low institutional conviction for long, skipped")
                return None

        # ── Gate 7: Nifty trend alignment ────────────────────────────────────
        is_counter_trend = (
            (nifty_trend == 'bullish' and signal_type == SignalType.SHORT_BUILDUP) or
            (nifty_trend == 'bearish' and signal_type == SignalType.LONG_BUILDUP)
        )
        if is_counter_trend:
            if abs(oi_change_pct) < 12.0:
                return None
            if abs(price_change_pct) < MIN_PRICE_CHANGE_COUNTER:
                return None

        # ── Gate 8: Volume spike ─────────────────────────────────────────────
        futures_vol = float(stock_data.get('volume', 0) or 0)
        equity_vol  = quote.get('equity_vol', 0)
        volume_spike = self._estimate_volume_spike(symbol, current_price, futures_vol, equity_vol, oi_change_pct)

        if volume_spike < MIN_VOLUME_SPIKE:
            return None

        # ── Gate 9: Entry quality & VWAP check ──────────────────────────────
        entry_quality  = self._get_entry_quality(price_change_pct)
        vwap_aligned   = False

        if vwap and vwap > 0:
            if signal_type == SignalType.LONG_BUILDUP:
                # Price should be at or above VWAP
                vwap_aligned = current_price >= vwap * (1 - VWAP_TOLERANCE)
            else:
                # Price should be at or below VWAP
                vwap_aligned = current_price <= vwap * (1 + VWAP_TOLERANCE)
        else:
            vwap_aligned = True  # Can't check — assume aligned

        # ── Gate 10: Closing session tightening ─────────────────────────────
        closing_min_conviction = MIN_CONVICTION
        if session_ctx == SessionContext.CLOSING:
            closing_min_conviction = MIN_CONVICTION + 10  # ≥82 in closing session

        # ── Gate 11: Conviction score ────────────────────────────────────────
        conviction = self._calculate_conviction(
            price_change_pct, oi_change_pct, volume_spike,
            signal_type, delivery_pct, nifty_trend,
            vwap_aligned, entry_quality
        )

        # ── Gate 12: Repeat signal check ─────────────────────────────────────
        is_repeat, repeat_days = self.check_repeat_signal(symbol, signal_type.value)
        if is_repeat:
            if repeat_days >= MAX_REPEAT_DAYS:
                print(f"   ⛔ {symbol}: repeat signal {repeat_days} consecutive days — structural move, not intraday, skipped")
                return None
            # Require higher conviction for day-1 repeats
            closing_min_conviction = max(closing_min_conviction, MIN_CONVICTION + REPEAT_CONVICTION_PENALTY)
            print(f"   🔁 {symbol}: repeat signal (day {repeat_days}) — requiring higher conviction")

        if conviction < closing_min_conviction:
            return None

        # ── Gate 13: Target / SL / R:R ───────────────────────────────────────
        target, stop_loss, rr = self._calculate_levels(
            current_price, signal_type, trade_type,
            price_change_pct, oi_change_pct, entry_quality, session_ctx
        )
        if rr < MIN_RR_RATIO:
            return None

        # ── Gate 14: CE/PE OI side analysis ──────────────────────────────────
        ce_pe           = self.fetch_ce_pe_oi(symbol, current_price)
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

            if signal_type == SignalType.LONG_BUILDUP and ce_pe.options_bias == 'bullish':
                options_aligned = True
                conviction += 8
            elif signal_type == SignalType.SHORT_BUILDUP and ce_pe.options_bias == 'bearish':
                options_aligned = True
                conviction += 8
            elif ce_pe.options_bias not in ('neutral', 'N/A'):
                conviction -= 6
                print(f"   ⚠️  {symbol}: Options bias ({ce_pe.options_bias}) conflicts with {signal_type.name}")

            conviction = min(conviction, 100)
            if conviction < closing_min_conviction:
                return None

        reasoning = self._build_reasoning(
            symbol, signal_type, price_change_pct, oi_change_pct,
            volume_spike, delivery_pct, nifty_trend, vwap, vwap_aligned,
            entry_quality, session_ctx, ce_pe
        )

        return OIStockSignal(
            symbol           = symbol,
            signal_type      = signal_type.value,
            trade_type       = trade_type.value,
            current_price    = round(current_price, 2),
            price_change_pct = round(price_change_pct, 2),
            oi_change_pct    = round(oi_change_pct, 2),
            volume_spike     = round(volume_spike, 2),
            delivery_pct     = round(delivery_pct, 1) if delivery_pct is not None else None,
            target           = round(target, 2),
            stop_loss        = round(stop_loss, 2),
            risk_reward      = round(rr, 2),
            conviction_score = round(conviction, 1),
            reasoning        = reasoning,
            timestamp        = datetime.now(IST).strftime('%Y-%m-%d %H:%M IST'),
            vwap             = round(vwap, 2) if vwap else None,
            vwap_aligned     = vwap_aligned,
            entry_quality    = entry_quality.value,
            session_context  = session_ctx.value,
            is_repeat        = is_repeat,
            repeat_days      = repeat_days,
            ce_pe_bias       = ce_pe_bias,
            pcr              = pcr_val,
            ce_oi_change     = ce_chg_val,
            pe_oi_change     = pe_chg_val,
            options_aligned  = options_aligned,
            max_pain         = max_pain_val,
        )

    # ── Volume Spike Estimation ────────────────────────────────────────────────

    _AVG_DAILY_VOL_PROXY = {
        'RELIANCE':8_000_000,'TCS':2_500_000,'INFY':4_500_000,'HDFCBANK':7_000_000,
        'ICICIBANK':7_000_000,'KOTAKBANK':5_000_000,'AXISBANK':6_000_000,
        'SBIN':12_000_000,'WIPRO':4_000_000,'HCLTECH':3_000_000,
        'BAJFINANCE':2_000_000,'LT':2_000_000,'TATAMOTORS':9_000_000,
        'POWERGRID':7_000_000,'NTPC':7_000_000,'COALINDIA':5_000_000,
        'ONGC':5_000_000,'BPCL':5_000_000,'IOC':5_000_000,
        'ADANIPORTS':3_000_000,'ADANIENT':2_000_000,'BHARTIARTL':4_000_000,
        'MARUTI':400_000,'NESTLEIND':80_000,'ASIANPAINT':400_000,
        'BRITANNIA':150_000,'HINDUNILVR':900_000,'INDIGO':500_000,
        'ULTRACEMCO':200_000,'SUNPHARMA':2_500_000,'DRREDDY':400_000,
        'CIPLA':1_500_000,'DIVISLAB':300_000,'TECHM':2_500_000,
        'BAJAJFINSV':800_000,'HDFCLIFE':3_000_000,'SBILIFE':2_000_000,
        'ICICIPRULI':2_000_000,'CHOLAFIN':1_500_000,'LODHA':2_000_000,
        'NYKAA':2_500_000,'ZOMATO':15_000_000,'PAYTM':5_000_000,
        'VBL':1_500_000,'HDFCAMC':500_000,'AMBUJACEM':4_000_000,
        'INDHOTEL':3_000_000,'BANKINDIA':8_000_000,'MCX':600_000,
        'VEDL':5_000_000,'ADANIENSOL':2_000_000,'BANDHANBNK':6_000_000,
    }

    def _estimate_volume_spike(
        self,
        symbol:      str,
        price:       float,
        futures_vol: float,
        equity_vol:  float,
        oi_change_pct: float
    ) -> float:
        """
        Volume spike ratio — now uses price-tiered defaults when symbol not in proxy dict.
        Midday proxy: ~50% of daily volume traded by 12 PM.
        """
        # OI-strength minimum floor
        if abs(oi_change_pct) >= 15.0:  base = 1.8
        elif abs(oi_change_pct) >= 10.0: base = 1.5
        else:                            base = 1.0

        if equity_vol > 0:
            avg_daily  = self._AVG_DAILY_VOL_PROXY.get(symbol.upper(), self._get_default_avg_vol(price))
            avg_midday = avg_daily * 0.50   # ~50% of daily vol by noon
            vol_ratio  = equity_vol / avg_midday if avg_midday > 0 else 1.0
            return max(vol_ratio, base)

        if futures_vol > 0:
            avg_futures_midday = 5_000
            fvol_ratio = futures_vol / avg_futures_midday
            return max(min(fvol_ratio, 5.0), base)

        return base

    # ── Conviction Scoring ─────────────────────────────────────────────────────

    def _calculate_conviction(
        self,
        price_change:  float,
        oi_change:     float,
        volume_spike:  float,
        signal_type:   SignalType,
        delivery_pct:  Optional[float],
        nifty_trend:   Optional[str],
        vwap_aligned:  bool,
        entry_quality: EntryQuality,
    ) -> float:
        """Conviction scoring with VWAP and entry quality factors. Max 100."""
        score = 0.0

        # OI strength (max 32)
        oi_score = min(abs(oi_change) * 2.65, 32)
        score += oi_score

        # Price momentum (max 23)
        price_score = min(abs(price_change) * 11.5, 23)
        score += price_score

        # Volume confirmation (max 18)
        vol_score = min((volume_spike - 1.0) * 18, 18)
        score += max(vol_score, 0)

        # Delivery % (max 10) — now properly gated upstream for longs
        if delivery_pct is not None:
            if delivery_pct >= DELIVERY_BONUS_THRESHOLD:
                score += min((delivery_pct - DELIVERY_BONUS_THRESHOLD) * 0.5, 10)

        # Nifty alignment (max 10)
        if nifty_trend:
            if nifty_trend == 'bullish' and signal_type == SignalType.LONG_BUILDUP:
                score += 10
            elif nifty_trend == 'bearish' and signal_type == SignalType.SHORT_BUILDUP:
                score += 10
            elif nifty_trend == 'neutral':
                score += 5

        # VWAP alignment bonus/penalty (max ±8)
        if vwap_aligned:
            score += 5
        else:
            score -= 8   # Price on wrong side of VWAP — penalise

        # Entry quality adjustment
        if entry_quality == EntryQuality.FRESH:
            score += 2
        elif entry_quality == EntryQuality.EXTENDED:
            score -= 5   # Extended move — risk of reversal higher

        return min(score, 100)

    # ── Level Calculation ──────────────────────────────────────────────────────

    def _calculate_levels(
        self,
        price:         float,
        signal_type:   SignalType,
        trade_type:    TradeType,
        price_change:  float,
        oi_change:     float,
        entry_quality: EntryQuality,
        session_ctx:   SessionContext,
    ) -> Tuple[float, float, float]:
        """
        Dynamic SL / Target based on:
          - Trade type (intraday vs BTST)
          - Stock price tier (higher price = wider SL)
          - Entry quality (extended move = wider SL)
          - Session context (closing = tighter target)
        """
        oi_factor = min(abs(oi_change) / 10, 1.5)

        # Base SL by price tier
        if price > 2000:   base_sl_pct = 0.010   # 1.0%
        elif price > 500:  base_sl_pct = 0.008   # 0.8%
        else:              base_sl_pct = 0.007   # 0.7%

        # Widen SL for extended entries
        if entry_quality == EntryQuality.EXTENDED:
            base_sl_pct += 0.005   # +0.5%
        elif entry_quality == EntryQuality.MODERATE:
            base_sl_pct += 0.002   # +0.2%

        if trade_type == TradeType.INTRADAY:
            sl_pct = base_sl_pct

            # Tight scalp mode during closing session
            if session_ctx == SessionContext.CLOSING:
                target_pct = 0.008 + (0.004 * oi_factor)   # 0.8–1.4% target
            else:
                target_pct = 0.012 + (0.008 * oi_factor)   # 1.2–2.4% target

        else:  # BTST / STBT
            sl_pct     = base_sl_pct + 0.005              # wider for overnight
            target_pct = 0.022 + (0.012 * oi_factor)     # 2.2–4.0% target

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

    # ── Reasoning Builder ─────────────────────────────────────────────────────

    def _build_reasoning(
        self,
        symbol:        str,
        signal_type:   SignalType,
        price_change:  float,
        oi_change:     float,
        vol_spike:     float,
        delivery_pct:  Optional[float],
        nifty_trend:   Optional[str],
        vwap:          Optional[float],
        vwap_aligned:  bool,
        entry_quality: EntryQuality,
        session_ctx:   SessionContext,
        ce_pe:         Optional[CEPEOIData] = None,
    ) -> str:
        direction  = "BULLISH" if signal_type == SignalType.LONG_BUILDUP else "BEARISH"
        action     = "Long buildup" if signal_type == SignalType.LONG_BUILDUP else "Short buildup"
        vol_txt    = f"{vol_spike:.1f}× vol" if vol_spike >= 1.5 else "volume OK"
        deliv_txt  = f" | Delivery {delivery_pct:.0f}%" if delivery_pct else ""
        trend_txt  = f" | Nifty {nifty_trend}" if nifty_trend and nifty_trend != 'neutral' else ""
        vwap_txt   = f" | VWAP ₹{vwap:.2f} ({'✅ aligned' if vwap_aligned else '⚠️ below VWAP'})" if vwap else ""
        ce_pe_txt  = f" | PCR {ce_pe.pcr:.2f} ({ce_pe.options_bias})" if ce_pe else ""
        return (
            f"{action} confirmed. Price {price_change:+.2f}% | OI {oi_change:+.2f}% | "
            f"{vol_txt}{deliv_txt}{trend_txt}{vwap_txt}{ce_pe_txt}. {direction} setup."
        )

    # ── Main Scan ──────────────────────────────────────────────────────────────

    def scan_for_opportunities(
        self,
        trade_type: TradeType,
        top_n:      int = MAX_SIGNALS_PER_SIDE
    ) -> Dict[str, List[OIStockSignal]]:
        """Main scan. Returns top high-conviction BUY/SELL signals."""
        session_ctx = self._get_session_context()

        print(f"\n🔍 Scanning NSE for {trade_type.value} opportunities...")
        print(f"   Session: {session_ctx.value}")
        print(f"   Gates: OI≥{MIN_OI_CHANGE_PCT}% | Price={MIN_PRICE_CHANGE_PCT}-{MAX_PRICE_CHANGE_PCT}% | "
              f"Vol≥{MIN_VOLUME_SPIKE}× | Conv≥{MIN_CONVICTION} | R:R≥{MIN_RR_RATIO}")

        nifty_trend = self.fetch_nifty_trend()
        print(f"   Nifty trend: {nifty_trend or 'unknown'}")

        oi_data = self.fetch_oi_spurts()
        if not oi_data:
            print("❌ No OI data received from NSE")
            return {'buy': [], 'sell': []}

        print(f"📊 Analyzing {len(oi_data)} stocks...")

        buy_signals:  List[OIStockSignal] = []
        sell_signals: List[OIStockSignal] = []
        rejected = 0

        for stock in oi_data:
            signal = self.analyze_signal(stock, trade_type, nifty_trend, session_ctx)
            if signal:
                if signal.signal_type == 'LONG_BUILDUP':
                    buy_signals.append(signal)
                elif signal.signal_type == 'SHORT_BUILDUP':
                    sell_signals.append(signal)
            else:
                rejected += 1

        print(f"   ✅ Passed: {len(buy_signals)+len(sell_signals)} | ❌ Rejected: {rejected}")

        buy_signals.sort(key=lambda x: x.conviction_score, reverse=True)
        sell_signals.sort(key=lambda x: x.conviction_score, reverse=True)

        # In closing session, cap at 2 signals per side
        max_n = min(top_n, 2) if session_ctx == SessionContext.CLOSING else top_n
        buy_signals  = buy_signals[:max_n]
        sell_signals = sell_signals[:max_n]

        print(f"\n🟢 BUY  ({len(buy_signals)}):")
        for s in buy_signals:
            print(f"   {s.symbol:12} conv={s.conviction_score:.0f} OI={s.oi_change_pct:+.1f}% "
                  f"price={s.price_change_pct:+.1f}% vol={s.volume_spike:.1f}× RR={s.risk_reward:.2f} "
                  f"quality={s.entry_quality} vwap={'✅' if s.vwap_aligned else '⚠️'}")
        print(f"\n🔴 SELL ({len(sell_signals)}):")
        for s in sell_signals:
            print(f"   {s.symbol:12} conv={s.conviction_score:.0f} OI={s.oi_change_pct:+.1f}% "
                  f"price={s.price_change_pct:+.1f}% vol={s.volume_spike:.1f}× RR={s.risk_reward:.2f} "
                  f"quality={s.entry_quality} vwap={'✅' if s.vwap_aligned else '⚠️'}")

        return {'buy': buy_signals, 'sell': sell_signals}

    def save_signals(self, signals_dict: Dict, trade_type: TradeType):
        date_str = datetime.now(IST).strftime('%Y%m%d')
        path     = f"{self.cache_dir}/{trade_type.value}_{date_str}.json"
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


# ─── CE/PE Block Formatter ────────────────────────────────────────────────────

def _format_ce_pe_block(s) -> str:
    if isinstance(s, dict):
        ce_pe_bias   = s.get('ce_pe_bias', 'N/A')
        pcr          = s.get('pcr', 0.0)
        ce_oi_change = s.get('ce_oi_change', 0.0)
        pe_oi_change = s.get('pe_oi_change', 0.0)
        max_pain     = s.get('max_pain')
    else:
        ce_pe_bias   = s.ce_pe_bias
        pcr          = s.pcr
        ce_oi_change = s.ce_oi_change
        pe_oi_change = s.pe_oi_change
        max_pain     = s.max_pain

    if ce_pe_bias == 'N/A' or pcr == 0.0:
        return "  🔲 Options: N/A\n"

    ce_arrow   = "↑ Building" if ce_oi_change >= 5 else ("↓ Unwinding" if ce_oi_change <= -5 else "→ Stable")
    pe_arrow   = "↑ Building" if pe_oi_change >= 5 else ("↓ Unwinding" if pe_oi_change <= -5 else "→ Stable")
    ce_meaning = "⚠️ Resistance" if ce_oi_change >= 5 else ("✅ Easing"    if ce_oi_change <= -5 else "")
    pe_meaning = "✅ Support"    if pe_oi_change >= 5 else ("⚠️ Weakening" if pe_oi_change <= -5 else "")
    bias_emoji = {"bullish":"🟢","bearish":"🔴","neutral":"⚪"}.get(ce_pe_bias, "⚪")
    pcr_note   = "Bullish" if pcr >= 1.2 else ("Bearish" if pcr <= 0.8 else "Neutral")
    mp_txt     = f"  💥 Max Pain: ₹{max_pain:.0f}\n" if max_pain else ""

    return (
        f"  📋 *Options (ATM ±3):*\n"
        f"  • CE OI: {ce_arrow} ({ce_oi_change:+.1f}%) {ce_meaning}\n"
        f"  • PE OI: {pe_arrow} ({pe_oi_change:+.1f}%) {pe_meaning}\n"
        f"  • PCR: {pcr:.2f} → {pcr_note}  {bias_emoji}\n"
        f"{mp_txt}"
    )


# ─── Telegram Formatter ───────────────────────────────────────────────────────

def _entry_quality_badge(eq: str) -> str:
    return {"FRESH":"🟢 FRESH","MODERATE":"🟡 MODERATE","EXTENDED":"🔴 EXTENDED"}.get(eq, eq)

def _session_badge(sc: str) -> str:
    return {"OPENING":"⏰ OPENING","MIDDAY":"📊 MIDDAY","CLOSING":"🏁 CLOSING","BTST":"🌙 BTST"}.get(sc, sc)

def format_telegram_alert(signals_dict: Dict, trade_type: TradeType) -> str:
    buy_signals  = signals_dict.get('buy', [])
    sell_signals = signals_dict.get('sell', [])
    total        = len(buy_signals) + len(sell_signals)
    now_ist      = datetime.now(IST)

    # Determine session context from first signal or current time
    sc = None
    if buy_signals or sell_signals:
        first = buy_signals[0] if buy_signals else sell_signals[0]
        sc    = first.session_context if hasattr(first, 'session_context') else (first.get('session_context') if isinstance(first, dict) else None)

    session_lbl = _session_badge(sc) if sc else ""

    trade_emoji = {'INTRADAY':'⚡','BTST':'🌙','STBT':'🌙'}.get(trade_type.value, '📊')

    if len(buy_signals) > len(sell_signals) * 1.5:
        market_bias = "🟢 BULLISH BIAS"
    elif len(sell_signals) > len(buy_signals) * 1.5:
        market_bias = "🔴 BEARISH BIAS"
    else:
        market_bias = "⚪ MIXED"

    header = (
        f"{trade_emoji} *SHIVA OI MOMENTUM — {trade_type.value}*\n"
        f"📅 {now_ist.strftime('%d %b %Y')}  🕐 {now_ist.strftime('%I:%M %p IST')}"
        f"  {session_lbl}\n"
        f"Market: {market_bias}\n"
        f"{'─'*30}\n"
    )

    if total == 0:
        return header + (
            "✋ *No high-conviction setups today*\n\n"
            "_All signals rejected — thresholds not met._\n"
            "_No trade is better than a bad trade._ 🎃"
        )

    body = f"✅ *{total} setup(s) passed all gates*\n\n"

    def _render_signal(i: int, s, side: str) -> str:
        # Support both dataclass and dict
        def g(attr, default=None):
            return getattr(s, attr, None) if not isinstance(s, dict) else s.get(attr, default)

        symbol          = g('symbol')
        conv            = g('conviction_score', 0)
        price           = g('current_price', 0)
        target_p        = g('target', 0)
        sl_p            = g('stop_loss', 0)
        rr              = g('risk_reward', 0)
        price_chg       = g('price_change_pct', 0)
        oi_chg          = g('oi_change_pct', 0)
        vol             = g('volume_spike', 0)
        delivery        = g('delivery_pct')
        vwap            = g('vwap')
        vwap_aligned    = g('vwap_aligned', True)
        entry_quality   = g('entry_quality', 'FRESH')
        is_repeat       = g('is_repeat', False)
        repeat_days     = g('repeat_days', 0)
        options_aligned = g('options_aligned', False)

        eq_badge    = _entry_quality_badge(entry_quality)
        align_badge = " ✅ *Options Aligned*" if options_aligned else ""
        repeat_txt  = f" 🔁 _Repeat Day {repeat_days}_" if is_repeat else ""
        vwap_txt    = f"\n  📍 VWAP: ₹{vwap:.2f} {'✅' if vwap_aligned else '⚠️ price below VWAP'}" if vwap else ""
        deliv_txt   = f"\n  📦 Delivery: {delivery:.0f}%" if delivery else ""
        ce_pe_block = _format_ce_pe_block(s)

        if side == 'buy':
            action_txt = "*BUY (CE side: buy CE / sell PE)*"
        else:
            action_txt = "*SELL / SHORT (PE side: buy PE / sell CE)*"

        return (
            f"\n*{i}. {symbol}*  ⭐ {conv:.0f}/100{align_badge}{repeat_txt}\n"
            f"🏷️ {eq_badge}\n"
            f"📌 Action: {action_txt}\n"
            f"💰 Entry: ₹{price}{vwap_txt}{deliv_txt}\n"
            f"🎯 Target: ₹{target_p}  🛑 SL: ₹{sl_p}\n"
            f"⚖️ R:R = 1:{rr:.1f}\n"
            f"📊 Price {price_chg:+.2f}% | OI {oi_chg:+.2f}% | Vol {vol:.1f}×\n"
            f"{ce_pe_block}"
            f"💬 _{g('reasoning')}_\n"
        )

    if buy_signals:
        body += "🟢 *BUY — Long Buildup* (Price↑ + OI↑)\n`━━━━━━━━━━━━━━━━━━━━━`\n"
        for i, s in enumerate(buy_signals, 1):
            body += _render_signal(i, s, 'buy')
        body += "\n"

    if sell_signals:
        body += "🔴 *SELL — Short Buildup* (Price↓ + OI↑)\n`━━━━━━━━━━━━━━━━━━━━━`\n"
        for i, s in enumerate(sell_signals, 1):
            body += _render_signal(i, s, 'sell')
        body += "\n"

    footer = (
        "`━━━━━━━━━━━━━━━━━━━━━`\n"
        "⚠️ *Risk Rules:*\n"
        "• Max 2% capital per trade\n"
        "• Book 50% at 1:1 R:R, trail rest\n"
        "• 🟢 FRESH entry = best | 🔴 EXTENDED = wide SL, smaller size\n"
        "• Exit ALL if SL hits — no exceptions\n\n"
        "```\n"
        "✅ BUY:  Price↑ + OI↑ = Long Buildup\n"
        "✅ SELL: Price↓ + OI↑ = Short Buildup\n"
        "❌ SKIP: Short Covering / Long Unwinding\n"
        "```\n"
        "_🎃 Shiva — OI + VWAP + Options confirmed._"
    )

    return header + body + footer


# ─── Entry Point ──────────────────────────────────────────────────────────────

def main():
    import argparse
    parser = argparse.ArgumentParser(description='NSE OI High-Conviction Scanner v3.0')
    parser.add_argument('trade_type', choices=['intraday', 'btst', 'stbt'])
    parser.add_argument('--top',  type=int, default=MAX_SIGNALS_PER_SIDE)
    parser.add_argument('--save', action='store_true')
    args = parser.parse_args()

    trade_type_map = {'intraday': TradeType.INTRADAY, 'btst': TradeType.BTST, 'stbt': TradeType.STBT}
    trade_type     = trade_type_map[args.trade_type]

    print("=" * 60)
    print("🎃 SHIVA OI SCANNER v3.0")
    print("=" * 60)
    print(f"Mode: {trade_type.value}  |  Top: {args.top}")
    print(f"Time: {datetime.now(IST).strftime('%Y-%m-%d %H:%M IST')}")
    print("=" * 60)

    scanner      = NSEOIScanner()
    signals_dict = scanner.scan_for_opportunities(trade_type, top_n=args.top)

    total = len(signals_dict.get('buy', [])) + len(signals_dict.get('sell', []))
    print(f"\n✅ Final: {total} high-conviction setups")
    print(f"   🟢 BUY: {len(signals_dict.get('buy', []))}  🔴 SELL: {len(signals_dict.get('sell', []))}")

    if args.save:
        scanner.save_signals(signals_dict, trade_type)

    telegram_msg = format_telegram_alert(signals_dict, trade_type)
    print("\n" + "=" * 60)
    print("📤 TELEGRAM PREVIEW:")
    print("=" * 60)
    print(telegram_msg)

    return signals_dict


if __name__ == "__main__":
    main()
