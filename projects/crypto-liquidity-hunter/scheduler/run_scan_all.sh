#!/bin/bash
# Cron job script: run hourly scan of all pairs and timeframes
cd /root/.openclaw/workspace/projects/crypto-liquidity-hunter
source venv/bin/activate
python main.py scan-all --capital 10000 --alert 2>&1 | tee -a logs/cron_$(date +\%Y\%m\%d\%H).log