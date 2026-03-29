# MeanRev AutoTrader v2.0.0

**Post-Pump Mean Reversion Auto-Trader**
Multi-broker · Paper + Live · Performance Analytics · Auto-execution

---

## What's New in v2

| Feature | Description |
|---------|-------------|
| **4 Brokers** | Binance Futures, Delta Exchange India, Coinbase, CoinDCX |
| **Performance Tab** | Equity curve, drawdown chart, Sharpe ratio, profit factor, monthly PnL |
| **Trailing Stop Loss** | Locks in profit as price falls — configurable % |
| **Daily Loss Limit** | Pauses auto-trading if daily loss exceeds threshold |
| **Max Drawdown Protection** | Halts all trading if account drawdown exceeds limit |
| **RSI Signal Confirmation** | RSI(14) calculated from kline data, shown on chart + scanner |
| **Signal Confidence Score** | 0-100% confidence based on pump %, reversal depth, volume |
| **Browser Notifications** | Push alerts for signals, entries, exits |
| **Activity Log Tab** | Full event history with tagged log entries |
| **Detailed Stats** | Best/worst trade, avg duration, win/loss streaks, estimated fees |

---

## Package Contents

```
meanrev-autotrader-v2/
├── public/
│   └── index.html          ← Complete trading app (no build step)
├── config/
│   └── nginx.conf          ← Nginx reverse proxy config
├── scripts/
│   ├── deploy.sh           ← One-command Ubuntu VPS setup
│   └── manage.sh           ← Start / stop / restart / logs
├── server.js               ← Express server + all broker proxies
├── package.json
├── ecosystem.config.js     ← PM2 config
├── .env.example
└── README.md
```

---

## Quick Deploy (Ubuntu 20.04 / 22.04)

```bash
# 1. Upload to VPS
scp -r meanrev-autotrader-v2/ root@YOUR_VPS_IP:/opt/

# 2. SSH and deploy
ssh root@YOUR_VPS_IP
cd /opt/meanrev-autotrader-v2
chmod +x scripts/deploy.sh scripts/manage.sh
./scripts/deploy.sh

# 3. Setup Nginx (edit server_name first)
nano config/nginx.conf   # change YOUR_DOMAIN_OR_IP
sudo cp config/nginx.conf /etc/nginx/sites-available/meanrev
sudo ln -s /etc/nginx/sites-available/meanrev /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx
```

App runs at: `http://YOUR_VPS_IP:3000`
With Nginx: `http://YOUR_VPS_IP` (port 80)

---

## Broker Setup Guide

### 🔶 Binance Futures
- **Testnet**: testnet.binancefuture.com → API Management → Generate keys
- **Mainnet**: binance.com → Account → API Management → Enable Futures
- Supports: Perpetual futures, short selling, leveraged positions
- Recommended for most users

### 🔷 Delta Exchange India
- **Keys**: india.delta.exchange → Settings → API Keys
- Enable: Read + Trade permissions
- Supports: Crypto futures & perpetuals, India-based KYC
- Note: Symbol format may differ — verify pairs after connecting

### 🔵 Coinbase Advanced Trade
- **Keys**: coinbase.com → Settings → API → Advanced Trade API
- Supports: Spot trading only (no native shorts)
- In paper mode: Short simulation works normally
- In live mode: Uses sell-side orders (spot)
- Pairs format: BTC-USD instead of BTCUSDT

### 🟦 CoinDCX
- **Keys**: coindcx.com → Profile → API Keys → Enable trading
- Supports: Spot + futures (India)
- Note: Market pairs use underscore format (BTC_USDT)

---

## Strategy Configuration

| Parameter | Default | Description |
|-----------|---------|-------------|
| Pump Threshold | 20% | Min 24h rise to qualify for scan |
| Reversal Threshold | 5% | % drop from 24h high to trigger signal |
| Position Size | $200 | USDT per paper trade |
| Take-Profit | 4% | Auto-close at this profit level |
| Stop-Loss | 12% | Hard stop loss per position |
| **Trailing Stop** | 2% | Locks profit as price falls (0=off) |
| Max Averaging | 3 | Max add-ons per position |
| **Daily Loss Limit** | $300 | Pauses auto if daily loss exceeds this (0=off) |
| **Max Account DD** | 20% | Halts trading if account DD exceeds this (0=off) |
| Min Volume | $500K | Filter low-liquidity coins |

---

## Performance Metrics Explained

| Metric | Formula | What it means |
|--------|---------|---------------|
| Total Return | (balance - 10000) / 10000 × 100 | Overall % gain/loss |
| Win Rate | wins / total trades × 100 | % of profitable trades |
| Profit Factor | gross profit / gross loss | >1 = profitable system |
| Sharpe Ratio | mean(returns) / std(returns) × √n | Risk-adjusted returns |
| Max Drawdown | max(peak - trough) / peak × 100 | Worst peak-to-trough loss |
| Avg R:R | avg win % / avg loss % | Risk to reward ratio |

---

## API Key Security

- API keys are stored **only in your browser session** — never sent to any server except the exchange itself
- The Node.js server acts as a CORS proxy — it forwards requests but cannot read your keys unless you use the /proxy routes
- **Recommended**: IP-whitelist your API keys to your VPS IP only
- **Never** enable withdrawal permissions on trading API keys

---

## PM2 Commands

```bash
pm2 status                # Check running
pm2 logs meanrev-v2       # Live logs
pm2 restart meanrev-v2    # Restart
./scripts/manage.sh logs  # View last 200 log lines
```

---

## Firewall

```bash
sudo ufw allow 22    # SSH
sudo ufw allow 80    # HTTP
sudo ufw allow 443   # HTTPS
sudo ufw enable
```

---

## VPS Requirements

| | Minimum | Recommended |
|-|---------|-------------|
| OS | Ubuntu 20.04 | Ubuntu 22.04 LTS |
| RAM | 512 MB | 1 GB |
| CPU | 1 vCPU | 1-2 vCPU |
| Storage | 5 GB | 10 GB |

Compatible: DigitalOcean, Vultr, Hetzner, Linode, AWS EC2, Google Cloud

---

## Test Your Broker Connection

1. Open app → click **🔌 Broker** tab
2. Select broker from left sidebar
3. Paste API Key + Secret
4. Click **🔍 Test Connection** (read-only, no orders placed)
5. Check Activity Log tab for result details
6. If passing → click **⚡ Connect & Use This Broker**
7. Switch to **🔴 LIVE** mode in topbar to enable real orders

---

## Disclaimer

This software is for educational purposes only. Cryptocurrency trading carries significant financial risk. Past performance does not guarantee future results. Use at your own risk. Always test on testnet/paper mode before using real funds.
