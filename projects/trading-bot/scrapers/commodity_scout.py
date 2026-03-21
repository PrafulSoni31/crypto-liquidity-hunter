#!/usr/bin/env python3
"""
Shiva's Commodity Scraper
Targets: Gold, Oil, Natural Gas
Sources: OilPrice RSS
"""

import os
import json
import time
import sys
import urllib.request

SOURCES = {
    "oilprice": "https://oilprice.com/rss/main",
    "kitco": "https://www.kitco.com/news"
}

LOG_FILE = "logs/raw_feeds.jsonl"

def scrape_oilprice():
    """Scrape OilPrice RSS for headlines"""
    import xml.etree.ElementTree as ET
    
    print("🛰️ Shiva Scraper: Accessing OilPrice RSS...")
    
    # Use urllib.request to get the RSS
    req = urllib.request.Request('https://oilprice.com/rss/main', headers={'User-Agent': 'Mozilla/5.0'})
    with urllib.request.urlopen(req, timeout=10) as response:
        content = response.read().decode('utf-8')
    
    # Parse XML
    root = ET.fromstring(content)
    
    headlines = []
    for item in root.findall('.//item')[:10]:  # Get latest 10
        title = item.find('title')
        if title is not None:
            headlines.append(title.text)
    
    return headlines

def scrape_kitco():
    """Scrape Kitco for gold news"""
    print("🛰️ Shiva Scraper: Accessing Kitco...")
    # Kitco doesn't have working RSS, would need HTML parsing
    return []

def log_headlines(headlines, source):
    """Log headlines to raw_feeds.jsonl"""
    import hashlib
    
    # Create entry with timestamp as primary key
    entry = {
        "timestamp": time.time(),
        "source": source,
        "headlines": headlines,
        "asset_keywords": extract_asset_keywords(headlines),
        "status": "Active"
    }
    
    # Always log fresh - we want to track sentiment over time
    with open(LOG_FILE, "a") as f:
        f.write(json.dumps(entry) + "\n")
    
    return True

def extract_asset_keywords(headlines):
    """Extract relevant asset keywords from headlines"""
    keywords = {
        "Oil": ["oil", "crude", "wti", "brent", "opec", "inventory", "production"],
        "Gold": ["gold", "xau", "precious", "silver", "bullion"],
        "Gas": ["gas", "lng", "natural gas", "turbine", "electricity"],
        "Coal": ["coal", "power plant"],
        "Nuclear": ["nuclear", "uranium", "centrus"],
        "Trade": ["tariff", "trade", "china", "india", "iran", "sanctions"]
    }
    
    found = {}
    headlines_text = " ".join(headlines).lower()
    
    for asset, words in keywords.items():
        matches = [w for w in words if w in headlines_text]
        if matches:
            found[asset] = matches
    
    return found

def main():
    print("🛰️ Shiva Scraper: Starting commodity data collection...")
    print("=" * 50)
    
    all_headlines = {}
    
    # Scrape OilPrice
    try:
        headlines = scrape_oilprice()
        if headlines:
            all_headlines['oilprice'] = headlines
            print(f"  ✓ OilPrice: {len(headlines)} headlines")
    except Exception as e:
        print(f"  ⚠ OilPrice scrape failed: {e}")
    
    # Log the data
    for source, headlines in all_headlines.items():
        logged = log_headlines(headlines, source)
        if logged:
            print(f"  ✓ Logged {len(headlines)} headlines from {source}")
    
    print(f"✅ Raw feeds flowing to {LOG_FILE}")
    print("=" * 50)
    
    # Print sample headlines
    if all_headlines:
        print("\n📰 Latest Headlines:")
        for source, headlines in all_headlines.items():
            print(f"\n  [{source.upper()}]")
            for h in headlines[:5]:
                print(f"    • {h[:80]}...")

if __name__ == "__main__":
    main()
