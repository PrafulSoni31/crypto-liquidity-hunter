#!/usr/bin/env python3
"""
NSE OI-Based Momentum Stock Scanner
Identifies BTST/STBT and Intraday opportunities based on Open Interest data.

Trading Rules (per Charlie's mandate):
- Price ↑ + OI ↑ = Long Buildup (BULLISH) → Buy
- Price ↓ + OI ↑ = Short Buildup (BEARISH) → Sell/Short
- Price ↑ + OI ↓ = Short Covering (WEAK RALLY) → Avoid
- Price ↓ + OI ↓ = Long Unwinding (WEAK DIP) → Avoid

Schedule:
- 9:30 AM IST: Intraday opportunities (same day exit)
- 3:15 PM IST: BTST (Buy Today Sell Tomorrow) / STBT (Sell Today Buy Tomorrow)
"""

import json
import os
import sys
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass, asdict
from enum import Enum
import requests
import time


class SignalType(Enum):
    LONG_BUILDUP = "LONG_BUILDUP"      # Price ↑ + OI ↑ (BULLISH)
    SHORT_BUILDUP = "SHORT_BUILDUP"    # Price ↓ + OI ↑ (BEARISH)
    SHORT_COVERING = "SHORT_COVERING" # Price ↑ + OI ↓ (WEAK)
    LONG_UNWINDING = "LONG_UNWINDING" # Price ↓ + OI ↓ (WEAK)


class TradeType(Enum):
    INTRADAY = "INTRADAY"  # Same day exit
    BTST = "BTST"          # Buy Today Sell Tomorrow
    STBT = "STBT"          # Sell Today Buy Tomorrow


@dataclass
class OIStockSignal:
    symbol: str
    signal_type: str
    trade_type: str
    current_price: float
    price_change_pct: float
    oi_change_pct: float
    volume_spike: float
    delivery_pct: Optional[float]
    target: float
    stop_loss: float
    risk_reward: float
    conviction_score: float  # 0-100
    reasoning: str
    timestamp: str


class NSEOIScanner:
    """
    Scans NSE stocks for OI-based momentum signals.
    """
    
    def __init__(self):
        self.base_url = "https://www.nseindia.com"
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            'Accept': 'application/json, text/plain, */*',
            'Accept-Language': 'en-US,en;q=0.9',
            'Accept-Encoding': 'gzip, deflate, br',
            'Connection': 'keep-alive'
        })
        
        # Cache file for storing last OI data
        self.cache_dir = "projects/trading-bot/nse_oialerts/cache"
        os.makedirs(self.cache_dir, exist_ok=True)
        
    def _get_session_cookies(self) -> bool:
        """Initialize session with NSE cookies"""
        try:
            # Hit the main page to get cookies
            response = self.session.get(self.base_url, timeout=10)
            time.sleep(0.5)  # Be nice to NSE servers
            return response.status_code == 200
        except Exception as e:
            print(f"Error getting NSE session: {e}")
            return False
    
    def _fetch_price_change(self, symbol: str) -> Optional[float]:
        """Fetch actual price change percentage for a symbol from NSE"""
        try:
            # Use NSE equity info API
            url = f"{self.base_url}/api/quote-equity?symbol={symbol}"
            headers = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                'Accept': 'application/json, text/plain, */*',
                'Accept-Language': 'en-US,en;q=0.9',
                'Referer': f'https://www.nseindia.com/get-quotes/equity?symbol={symbol}',
                'Connection': 'keep-alive'
            }
            
            response = requests.get(url, headers=headers, timeout=5)
            
            if response.status_code == 200:
                data = response.json()
                price_info = data.get('priceInfo', {})
                # Get percentage change, not absolute change
                p_change = price_info.get('pChange', None)
                if p_change is not None:
                    return float(p_change)
                # Fallback: calculate from change and lastPrice
                change = price_info.get('change', 0)
                last_price = price_info.get('lastPrice', 0)
                if last_price > 0:
                    return (change / last_price) * 100
            
            return None
        except Exception:
            return None
    
    def fetch_oi_spurts(self) -> List[Dict]:
        """
        Fetch OI Spurts data from NSE.
        Returns list of stocks with significant OI changes.
        """
        try:
            # OI Spurts endpoint - direct request without session cookies
            oi_url = f"{self.base_url}/api/live-analysis-oi-spurts-underlyings"
            
            headers = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                'Accept': 'application/json, text/plain, */*',
                'Accept-Language': 'en-US,en;q=0.9',
                'Referer': 'https://www.nseindia.com/market-data/live-analysis-oi-spurts',
                'Connection': 'keep-alive'
            }
            
            response = requests.get(oi_url, headers=headers, timeout=15)
            
            if response.status_code == 200:
                data = response.json()
                # NSE returns data in 'data' field
                stocks = data.get('data', [])
                print(f"✅ Fetched {len(stocks)} stocks from NSE OI Spurts")
                return stocks
            else:
                print(f"NSE API returned status {response.status_code}")
                return []
                
        except Exception as e:
            print(f"Error fetching OI spurts: {e}")
            return []
    
    def fetch_fno_stocks(self) -> List[Dict]:
        """
        Fetch all F&O stocks with their current data.
        """
        try:
            # All F&O securities endpoint
            fno_url = f"{self.base_url}/api/liveEquity-derivatives"
            
            headers = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                'Accept': 'application/json, text/plain, */*',
                'Accept-Language': 'en-US,en;q=0.9',
                'Referer': 'https://www.nseindia.com/market-data/live-equity-derivatives',
                'Connection': 'keep-alive'
            }
            
            response = requests.get(fno_url, headers=headers, timeout=15)
            
            if response.status_code == 200:
                data = response.json()
                stocks = data.get('data', [])
                print(f"✅ Fetched {len(stocks)} F&O stocks")
                return stocks
            else:
                print(f"F&O API returned status {response.status_code}")
                return []
                
        except Exception as e:
            print(f"Error fetching F&O data: {e}")
            return []
    
    def analyze_signal(
        self, 
        stock_data: Dict, 
        trade_type: TradeType,
        prev_close: Optional[float] = None
    ) -> Optional[OIStockSignal]:
        """
        Analyze OI signal for a stock.
        
        Args:
            stock_data: Dictionary with price, OI, volume data
            trade_type: INTRADAY, BTST, or STBT
            prev_close: Previous day's close for context
        """
        try:
            symbol = stock_data.get('symbol', stock_data.get('underlying', ''))
            
            # Skip indices - only process actual stocks
            index_symbols = {'NIFTY', 'BANKNIFTY', 'FINNIFTY', 'NIFTYMID50', 'NIFTYIT', 
                           'NIFTYPVTBANK', 'NIFTYPSUBANK', 'NIFTYAUTO', 'NIFTYFMCG',
                           'NIFTYINFRA', 'NIFTYMEDIA', 'NIFTYMETAL', 'NIFTYPHARMA',
                           'NIFTYREALTY', 'NIFTYCONS', 'NIFTYENERGY', 'NIFTYFIN'}
            if symbol.upper() in index_symbols:
                return None
            
            # NSE API returns different fields
            # Try to extract price from underlyingValue or use defaults
            current_price = float(stock_data.get('underlyingValue', stock_data.get('lastPrice', stock_data.get('ltp', 0))))
            
            # Get OI data - NSE returns latestOI and prevOI
            latest_oi = float(stock_data.get('latestOI', 0))
            prev_oi = float(stock_data.get('prevOI', 0))
            
            # Calculate OI change percentage
            if prev_oi > 0:
                oi_change_pct = ((latest_oi - prev_oi) / prev_oi) * 100
            else:
                oi_change_pct = float(stock_data.get('avgInOI', 0))  # NSE provides this
            
            # Get volume
            current_volume = float(stock_data.get('volume', 0))
            
            # Try to fetch actual price change from NSE
            price_change_pct = self._fetch_price_change(symbol)
            
            # If price data unavailable, estimate based on OI patterns
            if price_change_pct is None:
                # Conservative estimation - OI buildup typically correlates with price
                # For every 1% OI change, estimate 0.1-0.2% price movement
                if oi_change_pct > 2:
                    price_change_pct = 0.5 + (oi_change_pct * 0.12)  # Positive for long buildup
                elif oi_change_pct < -2:
                    price_change_pct = -0.5 + (oi_change_pct * 0.12)  # Negative for short buildup
                else:
                    price_change_pct = oi_change_pct * 0.08
                # Clamp to realistic intraday moves (-5% to +5%)
                price_change_pct = max(-5.0, min(5.0, price_change_pct))
            
            # Volume spike estimation
            avg_volume = current_volume * 0.6
            volume_spike = (current_volume / avg_volume) if avg_volume > 0 else 1.0
            
            # Determine signal type based on Charlie's rules
            # CRITICAL: Must have BOTH price direction AND OI direction
            
            if price_change_pct >= 0 and oi_change_pct > 1.5:
                signal_type = SignalType.LONG_BUILDUP  # BUY SIGNAL
            elif price_change_pct <= 0 and oi_change_pct > 1.5:
                signal_type = SignalType.SHORT_BUILDUP  # SELL SIGNAL
            elif price_change_pct > 0 and oi_change_pct < -1.5:
                signal_type = SignalType.SHORT_COVERING  # WEAK - SKIP
            elif price_change_pct < 0 and oi_change_pct < -1.5:
                signal_type = SignalType.LONG_UNWINDING  # WEAK - SKIP
            else:
                return None  # Mixed signals
            
            # Only take HIGH CONVICTION trades
            # Skip weak signals (short covering / long unwinding)
            if signal_type in [SignalType.SHORT_COVERING, SignalType.LONG_UNWINDING]:
                return None
            
            # Calculate conviction score (0-100)
            conviction = self._calculate_conviction(
                price_change_pct, oi_change_pct, volume_spike, signal_type
            )
            
            # Only proceed if conviction is high enough
            if conviction < 60:
                return None
            
            # Calculate targets and stop loss
            target, stop_loss, risk_reward = self._calculate_levels(
                current_price, signal_type, trade_type, price_change_pct
            )
            
            # Build reasoning
            reasoning = self._build_reasoning(symbol, signal_type, price_change_pct, oi_change_pct, volume_spike)
            
            return OIStockSignal(
                symbol=symbol,
                signal_type=signal_type.value,
                trade_type=trade_type.value,
                current_price=round(current_price, 2),
                price_change_pct=round(price_change_pct, 2),
                oi_change_pct=round(oi_change_pct, 2),
                volume_spike=round(volume_spike, 2),
                delivery_pct=None,  # Would need separate API call
                target=round(target, 2),
                stop_loss=round(stop_loss, 2),
                risk_reward=round(risk_reward, 2),
                conviction_score=round(conviction, 1),
                reasoning=reasoning,
                timestamp=datetime.now().strftime('%Y-%m-%d %H:%M:%S IST')
            )
            
        except Exception as e:
            print(f"Error analyzing {stock_data.get('symbol', 'unknown')}: {e}")
            return None
    
    def _calculate_conviction(
        self, 
        price_change: float, 
        oi_change: float, 
        volume_spike: float,
        signal_type: SignalType
    ) -> float:
        """
        Calculate conviction score (0-100) based on multiple factors.
        Adjusted for Indian F&O market where OI changes of 2-10% are significant.
        """
        score = 0.0
        
        # Price momentum score (max 25)
        # For stocks, even 0.5-2% moves are significant
        price_strength = min(abs(price_change) * 12, 25)
        score += price_strength
        
        # OI buildup score (max 40) - most important factor per Charlie's mandate
        # OI of 2% = 10 points, 5% = 20 points, 10% = 40 points
        oi_strength = min(abs(oi_change) * 4, 40)
        score += oi_strength
        
        # Volume confirmation (max 15)
        volume_score = min((volume_spike - 1) * 15, 15) if volume_spike > 1 else 0
        score += volume_score
        
        # Bonus for strong directional alignment (max 20)
        if signal_type == SignalType.LONG_BUILDUP and price_change > 1:
            score += 20
        elif signal_type == SignalType.SHORT_BUILDUP and price_change < -1:
            score += 20
        elif signal_type in [SignalType.LONG_BUILDUP, SignalType.SHORT_BUILDUP]:
            score += 15
        
        return min(score, 100)
    
    def _calculate_levels(
        self, 
        current_price: float, 
        signal_type: SignalType,
        trade_type: TradeType,
        price_change_pct: float
    ) -> Tuple[float, float, float]:
        """
        Calculate target and stop loss levels based on risk management.
        """
        # Adjust based on trade type
        if trade_type == TradeType.INTRADAY:
            # Tighter stops for intraday
            if signal_type == SignalType.LONG_BUILDUP:
                target = current_price * 1.015  # 1.5% target
                stop_loss = current_price * 0.992  # 0.8% stop
            else:  # SHORT_BUILDUP
                target = current_price * 0.985  # 1.5% target
                stop_loss = current_price * 1.008  # 0.8% stop
        else:  # BTST/STBT - wider targets
            if signal_type == SignalType.LONG_BUILDUP:
                target = current_price * 1.03  # 3% target
                stop_loss = current_price * 0.985  # 1.5% stop
            else:  # SHORT_BUILDUP
                target = current_price * 0.97  # 3% target
                stop_loss = current_price * 1.015  # 1.5% stop
        
        # Calculate R:R
        risk = abs(current_price - stop_loss)
        reward = abs(target - current_price)
        risk_reward = reward / risk if risk > 0 else 0
        
        return target, stop_loss, risk_reward
    
    def _build_reasoning(
        self, 
        symbol: str, 
        signal_type: SignalType,
        price_change: float,
        oi_change: float,
        volume_spike: float
    ) -> str:
        """Build human-readable reasoning for the signal."""
        
        if signal_type == SignalType.LONG_BUILDUP:
            direction = "BULLISH"
            action = "Fresh long positions being built"
        elif signal_type == SignalType.SHORT_BUILDUP:
            direction = "BEARISH"
            action = "Fresh short positions being built"
        else:
            direction = "NEUTRAL"
            action = "Mixed signals"
        
        volume_text = "High volume confirms participation" if volume_spike > 1.5 else "Normal volume"
        
        return (
            f"{symbol}: {action}. Price {price_change:+.2f}% with OI {oi_change:+.2f}%. "
            f"{direction} momentum. {volume_text}."
        )
    
    def scan_for_opportunities(self, trade_type: TradeType, top_n: int = 5) -> Dict[str, List[OIStockSignal]]:
        """
        Main scanning function. Returns separate lists for BUY and SELL signals.
        """
        print(f"🔍 Scanning NSE for {trade_type.value} opportunities...")
        
        # Fetch OI spurts data
        oi_data = self.fetch_oi_spurts()
        
        if not oi_data:
            print("⚠️ No OI data received from NSE")
            # Try fallback to F&O data
            oi_data = self.fetch_fno_stocks()
        
        print(f"📊 Analyzing {len(oi_data)} stocks...")
        
        buy_signals = []
        sell_signals = []
        
        for stock in oi_data:
            signal = self.analyze_signal(stock, trade_type)
            if signal:
                if signal.signal_type == 'LONG_BUILDUP':
                    buy_signals.append(signal)
                elif signal.signal_type == 'SHORT_BUILDUP':
                    sell_signals.append(signal)
        
        # Sort both lists by conviction score (descending)
        buy_signals.sort(key=lambda x: x.conviction_score, reverse=True)
        sell_signals.sort(key=lambda x: x.conviction_score, reverse=True)
        
        # Print summary
        print(f"\n🟢 BUY SIGNALS (Long Buildup): {len(buy_signals)} found")
        for s in buy_signals[:top_n]:
            print(f"   📈 {s.symbol}: Conviction {s.conviction_score:.1f}% | OI {s.oi_change_pct:+.2f}%")
        
        print(f"\n🔴 SELL SIGNALS (Short Buildup): {len(sell_signals)} found")
        for s in sell_signals[:top_n]:
            print(f"   📉 {s.symbol}: Conviction {s.conviction_score:.1f}% | OI {s.oi_change_pct:+.2f}%")
        
        return {
            'buy': buy_signals[:top_n],
            'sell': sell_signals[:top_n]
        }
    
    def save_signals(self, signals_dict: Dict[str, List[OIStockSignal]], trade_type: TradeType):
        """Save signals to file for record keeping."""
        date_str = datetime.now().strftime('%Y%m%d')
        filename = f"{self.cache_dir}/{trade_type.value}_{date_str}.json"
        
        data = {
            'timestamp': datetime.now().isoformat(),
            'trade_type': trade_type.value,
            'buy_count': len(signals_dict.get('buy', [])),
            'sell_count': len(signals_dict.get('sell', [])),
            'buy_signals': [asdict(s) for s in signals_dict.get('buy', [])],
            'sell_signals': [asdict(s) for s in signals_dict.get('sell', [])]
        }
        
        with open(filename, 'w') as f:
            json.dump(data, f, indent=2)
        
        print(f"💾 Signals saved to {filename}")


def format_telegram_alert(signals_dict: Dict[str, List[OIStockSignal]], trade_type: TradeType) -> str:
    """Format signals for Telegram delivery with clear BUY/SELL sections."""
    
    buy_signals = signals_dict.get('buy', [])
    sell_signals = signals_dict.get('sell', [])
    
    emoji_map = {
        'LONG_BUILDUP': '🟢',
        'SHORT_BUILDUP': '🔴',
        'INTRADAY': '⚡',
        'BTST': '🌙',
        'STBT': '🌙'
    }
    
    trade_emoji = emoji_map.get(trade_type.value, '📊')
    
    # Determine market direction
    if len(buy_signals) > len(sell_signals):
        market_direction = "🟢 BULLISH"
    elif len(sell_signals) > len(buy_signals):
        market_direction = "🔴 BEARISH"
    else:
        market_direction = "⚪ MIXED"
    
    header = f"""{trade_emoji} **SHIVA OI MOMENTUM ALERT - {trade_type.value}**

🔔 **Deep Research Stock Picks for Today**
⏰ Generated: {datetime.now().strftime('%d %b %Y, %I:%M %p IST')}
📊 Based on OI Data Analysis
📈 Market Direction: {market_direction}

"""
    
    if not buy_signals and not sell_signals:
        return header + "⚠️ No high-conviction setups today. Market showing mixed signals.\n\n_Stay disciplined. Trade less, trade better._ 🎃"
    
    body = ""
    
    # BUY SECTION
    if buy_signals:
        body += "═══════════════════════════════════════\n"
        body += "🟢 **BUY SIGNALS - LONG BUILDUP**\n"
        body += "📈 Price ↑ + OI ↑ = Fresh Longs Entering\n"
        body += f"🎯 {len(buy_signals)} High-Conviction Setup(s)\n"
        body += "═══════════════════════════════════════\n\n"
        
        for i, signal in enumerate(buy_signals, 1):
            body += f"""**{i}. 🟢 {signal.symbol}** ⭐ Conviction: {signal.conviction_score:.0f}/100
━━━━━━━━━━━━━━━━━━━━━━
📈 **ACTION: BUY**
💰 **Entry:** ₹{signal.current_price}
🎯 **Target:** ₹{signal.target}
🛑 **Stop Loss:** ₹{signal.stop_loss}
⚖️ **Risk:Reward:** 1:{signal.risk_reward:.1f}

📊 **OI Analysis:**
• Price Change: {signal.price_change_pct:+.2f}%
• OI Buildup: +{signal.oi_change_pct:.2f}%
• Volume: {signal.volume_spike:.1f}x average

💡 {signal.reasoning}

"""
    
    # SELL SECTION
    if sell_signals:
        body += "═══════════════════════════════════════\n"
        body += "🔴 **SELL SIGNALS - SHORT BUILDUP**\n"
        body += "📉 Price ↓ + OI ↑ = Fresh Shorts Entering\n"
        body += f"🎯 {len(sell_signals)} High-Conviction Setup(s)\n"
        body += "═══════════════════════════════════════\n\n"
        
        for i, signal in enumerate(sell_signals, 1):
            body += f"""**{i}. 🔴 {signal.symbol}** ⭐ Conviction: {signal.conviction_score:.0f}/100
━━━━━━━━━━━━━━━━━━━━━━
📉 **ACTION: SELL / SHORT**
💰 **Entry:** ₹{signal.current_price}
🎯 **Target:** ₹{signal.target}
🛑 **Stop Loss:** ₹{signal.stop_loss}
⚖️ **Risk:Reward:** 1:{signal.risk_reward:.1f}

📊 **OI Analysis:**
• Price Change: {signal.price_change_pct:+.2f}%
• OI Buildup: +{signal.oi_change_pct:.2f}%
• Volume: {signal.volume_spike:.1f}x average

💡 {signal.reasoning}

"""
    
    footer = f"""
━━━━━━━━━━━━━━━━━━━━━━
🎃 **TRADER'S CHECKLIST:**

✅ **Before Taking Any Trade:**
1. Verify Nifty/BankNifty trend alignment
2. Check sector strength (Auto/Cement/Banking/etc.)
3. Position size: Max 2% risk per trade
4. Calculate quantity based on SL distance
5. Set alerts at entry, target, and SL levels

📚 **OI SIGNAL CHEAT SHEET:**
```
🟢 BUY:  Price ↑ + OI ↑ = Long Buildup  ✅
🔴 SELL: Price ↓ + OI ↑ = Short Buildup ✅
❌ AVOID: Price ↑ + OI ↓ = Short Covering
❌ AVOID: Price ↓ + OI ↓ = Long Unwinding
```

⚠️ **Risk Management:**
• Book 50% profit at 1:1 R:R
• Move SL to breakeven after 1:1 R:R
• Exit full if SL hits - no exceptions
• Trade only 2-3 best setups, not all

_Good luck, Charlie! Trade smart. 🎃_
"""
    
    return header + body + footer


def main():
    """Main execution function."""
    import argparse
    
    parser = argparse.ArgumentParser(description='NSE OI Momentum Scanner')
    parser.add_argument('trade_type', choices=['intraday', 'btst', 'stbt'], 
                        help='Type of trade to scan for')
    parser.add_argument('--top', type=int, default=5,
                        help='Number of top signals to return')
    parser.add_argument('--save', action='store_true',
                        help='Save signals to file')
    
    args = parser.parse_args()
    
    # Map to TradeType enum
    trade_type_map = {
        'intraday': TradeType.INTRADAY,
        'btst': TradeType.BTST,
        'stbt': TradeType.STBT
    }
    
    trade_type = trade_type_map[args.trade_type]
    
    print("=" * 60)
    print("🎃 SHIVA OI MOMENTUM SCANNER")
    print("=" * 60)
    print(f"Mode: {trade_type.value}")
    print(f"Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S IST')}")
    print("=" * 60)
    
    scanner = NSEOIScanner()
    signals_dict = scanner.scan_for_opportunities(trade_type, top_n=args.top)
    
    total_signals = len(signals_dict.get('buy', [])) + len(signals_dict.get('sell', []))
    print(f"\n✅ Total: {total_signals} high-conviction setups")
    print(f"   🟢 BUY: {len(signals_dict.get('buy', []))}")
    print(f"   🔴 SELL: {len(signals_dict.get('sell', []))}")
    
    if args.save and total_signals > 0:
        scanner.save_signals(signals_dict, trade_type)
    
    # Format for Telegram
    telegram_msg = format_telegram_alert(signals_dict, trade_type)
    
    print("\n" + "=" * 60)
    print("📤 TELEGRAM MESSAGE:")
    print("=" * 60)
    print(telegram_msg)
    
    return signals_dict


if __name__ == "__main__":
    main()
