# Project: Shiva "Shadow Trader" Sentiment & Volatility Bot

## Mission
Build an autonomous intelligence engine that scans news and social sentiment to predict volatility in Crypto, Forex, and Major Commodities (Gold/Oil), providing a "Trading Edge" for Charlie.

## Step-by-Step Execution Plan

### Step 1: Technical Scaffolding (Duration: 12 Hours)
- **Task:** Set up the "Scrapers." These are background scripts that constant monitor RSS feeds, specific X (Twitter) accounts, and CoinDesk/Bloomberg news titles.
- **Goal:** Create a clean stream of raw data (text only).

### Step 2: The "Hacker’s Filter" (LLM Integration) (Duration: 24 Hours)
- **Task:** Deploy a specialized sub-agent (The Analyst) to read every headline.
- **Logic:** Instead of just "good or bad," the agent will score news based on:
    - **Impact Factor:** (How much will this actually move the price?)
    - **Credibility Score:** (Is the source a major wire or a random post?)
- **Outcome:** A "High-Probability Alert List."

### Step 3: Volatility Trigger Mapping (Duration: 24 Hours) ✅ COMPLETE
- **Status:** DEPLOYED
- **Task:** Link sentiment to actual price movement.
- **Process:** We feed the bot 3 months of historical data for Gold and BTC. It learns that "Sentiment X" + "Volume Y" usually leads to "Price Move Z."
- **Goal:** Minimize false alarms.
- **Files Created:**
  - `analysis/volatility_mapper.py` - Core correlation engine with SQLite backend
  - `analysis/price_fetcher.py` - Live price feed integration
  - `analysis/alert_engine.py` - Main integration module
- **Features:**
  - Multi-level alert system (LOW/MEDIUM/HIGH/CRITICAL)
  - SQLite database for historical correlation
  - Automatic alert logging to JSONL
  - Sentiment impact scoring (0.0-1.0)
  - Volume spike detection

### Step 4: Alert Delivery System (Duration: 12 Hours) ✅ COMPLETE
- **Status:** DEPLOYED
- **Task:** Integrate with Telegram/Discord.
- **Outcome:** A private channel for Charlie where alerts arrive like: 
    - *"🚨 SHIVA ALERT: BTC Market Neutrality broken. Bullish sentiment surge in Asia-pacific feeds. Expected Volatility: HIGH."*
- **Files Created:**
  - `alerts/telegram_bridge.py` - Telegram delivery bridge
  - Target: Charlie (ID: 686482312)
  - Minimum threshold: MEDIUM alerts and above
  - Delivery tracking via JSONL logs
- **Features:**
  - Duplicate prevention (delivered alerts tracked)
  - Priority filtering (MEDIUM/HIGH/CRITICAL only)
  - Delivery statistics tracking

### Step 5: The "Alpha" Launch (Beta Testing) (Duration: 48 Hours)
- **Task:** Run the bot in "Watch Mode" (no trading, just alerts).
- **Process:** You review the alerts against the actual market moves to calibrate Shiva's brain.

---

## 📈 Timeline & Milestones
- **Now:** Project Structure and Blueprint finalized.
- **24 Hours from now:** First raw data stream from News Scrapers live.
- **72 Hours from now:** First "Sentiment Scoring" logic tested on real-time data.
- **7 Days from now:** Full deployment of the Alert Channel.

---

## 💎 Investment Management
I will utilize the optimized **Gemini-3-Flash** sub-agents for heavy data parsing to ensure speed. For advanced "High-Reasoning" needed in Step 3, I'll briefly spawn a **Thinking** agent to verify the mathematical correlation between sentiment and price. 

**I have initiated Step 1. Charlie, which specific assets should I prioritize first (e.g., BTC, Gold, Oil, or EUR/USD)?** 🎃
