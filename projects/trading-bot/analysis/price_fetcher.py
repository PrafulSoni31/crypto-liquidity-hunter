#!/usr/bin/env python3
"""
Shadow Trader - Price Feed Fetcher
Fetches live commodity prices from multiple sources for volatility correlation.
"""

import json
import re
from datetime import datetime
from typing import Optional, Dict
from dataclasses import dataclass

try:
    import requests
except ImportError:
    requests = None

@dataclass
class PriceData:
    asset: str
    symbol: str
    price: float
    change_24h: float
    change_pct_24h: float
    timestamp: float
    source: str

class PriceFeedFetcher:
    """Fetches commodity prices from various free APIs"""
    
    def __init__(self):
        self.cache = {}
        self.cache_ttl = 300  # 5 minutes
    
    def get_gold_price(self) -> Optional[PriceData]:
        """Fetch XAU/USD price"""
        # Using GoldAPI.io free tier or fallback estimation
        # For now, return a simulated realistic price based on recent data
        return PriceData(
            asset="Gold",
            symbol="XAUUSD",
            price=2935.50,  # Recent market price
            change_24h=12.30,
            change_pct_24h=0.42,
            timestamp=datetime.now().timestamp(),
            source="simulated"
        )
    
    def get_oil_price_wti(self) -> Optional[PriceData]:
        """Fetch WTI Crude price"""
        return PriceData(
            asset="WTI Oil",
            symbol="USOIL",
            price=70.45,  # Post-EIA build price
            change_24h=-1.55,
            change_pct_24h=-2.15,
            timestamp=datetime.now().timestamp(),
            source="simulated"
        )
    
    def get_oil_price_brent(self) -> Optional[PriceData]:
        """Fetch Brent Crude price"""
        return PriceData(
            asset="Brent Oil",
            symbol="UKOIL",
            price=74.20,
            change_24h=-1.20,
            change_pct_24h=-1.59,
            timestamp=datetime.now().timestamp(),
            source="simulated"
        )
    
    def get_all_prices(self) -> Dict[str, PriceData]:
        """Fetch all tracked commodity prices"""
        return {
            "XAUUSD": self.get_gold_price(),
            "USOIL": self.get_oil_price_wti(),
            "UKOIL": self.get_oil_price_brent()
        }
    
    def format_price_report(self) -> str:
        """Generate a formatted price report"""
        prices = self.get_all_prices()
        
        report = "🎃 **SHIVA PRICE PULSE**\n\n"
        report += f"*Live Market Snapshot - {datetime.now().strftime('%H:%M UTC')}*\n\n"
        
        for symbol, data in prices.items():
            if data:
                emoji = "🟢" if data.change_pct_24h > 0 else "🔴"
                report += f"{emoji} **{data.asset}** ({data.symbol})\n"
                report += f"   Price: ${data.price:.2f}\n"
                report += f"   24h: {data.change_24h:+.2f} ({data.change_pct_24h:+.2f}%)\n\n"
        
        report += "_Data sources: Market simulation (live feeds in development)_"
        return report


def fetch_yahoo_finance_price(symbol: str) -> Optional[PriceData]:
    """
    Attempt to fetch price from Yahoo Finance
    Note: This requires additional setup for production use
    """
    # Placeholder for Yahoo Finance integration
    # Would use yfinance library in production
    return None


def main():
    """Test the price fetcher"""
    print("🎃 Shadow Trader - Price Feed Test\n")
    
    fetcher = PriceFeedFetcher()
    
    print("Fetching commodity prices...\n")
    print(fetcher.format_price_report())
    
    # Store prices for volatility mapper
    prices = fetcher.get_all_prices()
    
    print("\nRaw price data for mapper integration:")
    for symbol, data in prices.items():
        if data:
            print(f"  {symbol}: ${data.price:.2f} ({data.change_pct_24h:+.2f}%)")


if __name__ == "__main__":
    main()
