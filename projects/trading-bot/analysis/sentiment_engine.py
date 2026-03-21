#!/usr/bin/env python3
import sys
import json

def analyze_sentiment(headline):
    # This is the 'Skeleton' logic. 
    # High-Velocity Phase: This will be piped through a Flash LLM.
    indicators = {
        "bullish": ["surge", "rise", "low supply", "cut", "demand spike", "rally"],
        "bearish": ["drop", "oversupply", "low demand", "plunge", "surplus", "recession"]
    }
    
    headline_lower = headline.lower()
    score = 0
    for word in indicators["bullish"]:
        if word in headline_lower: score += 1
    for word in indicators["bearish"]:
        if word in headline_lower: score -= 1
        
    return score

if __name__ == "__main__":
    test_headline = "OPEC announces supply cuts as global demand for Oil surges"
    result = analyze_sentiment(test_headline)
    print(f"Test Headline: {test_headline}")
    print(f"Base Sentiment Score: {result}")
