#!/bin/bash
# OI Momentum Scanner Runner
# Called by cron at scheduled times

cd /root/.openclaw/workspace

# Activate virtual environment if it exists
if [ -f "projects/trading-bot/venv/bin/activate" ]; then
    source projects/trading-bot/venv/bin/activate
fi

# Get trade type from argument
TRADE_TYPE=$1

if [ -z "$TRADE_TYPE" ]; then
    echo "Usage: $0 <intraday|btst|stbt>"
    exit 1
fi

# Run the scanner and capture output
python3 projects/trading-bot/nse_oialerts/oi_momentum_scanner.py "$TRADE_TYPE" --top 5 --save > /tmp/oi_scanner_output.txt 2>&1

# Check if scanner succeeded
if [ $? -eq 0 ]; then
    echo "Scanner completed successfully"
    cat /tmp/oi_scanner_output.txt
else
    echo "Scanner failed:"
    cat /tmp/oi_scanner_output.txt
fi
