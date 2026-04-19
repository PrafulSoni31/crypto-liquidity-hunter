# Crypto Liquidity Hunter - Makefile

.PHONY: install run-scan run-backtest status clean

install:
	pip install -r requirements.txt

run-scan:
	python main.py scan --pair binance:BTC/USDT --tf 15m --limit 1000 --alert

run-backtest:
	python main.py backtest --pair binance:BTC/USDT --tf 15m --periods 2000 --output results/trades.csv

status:
	python main.py status

clean:
	rm -rf __pycache__ */__pycache__ */*/__pycache__ results/*.csv
