"""
Data fetching layer for crypto markets.
Uses ccxt to get OHLCV, orderbook snapshots, funding rates.
"""
import ccxt
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
import time
from typing import Optional, Dict, List
import logging

logger = logging.getLogger(__name__)

class MarketDataFetcher:
    def __init__(self, exchange_id: str = 'binance', config: Dict = None):
        """
        Initialize exchange connection.
        config: {'apiKey': ..., 'secret': ...} for private endpoints (optional)
        """
        self.exchange_id = exchange_id
        self.exchange_class = getattr(ccxt, exchange_id)
        self.exchange = self.exchange_class(config or {})
        self.markets = None

    def load_markets(self):
        """Load available markets."""
        self.markets = self.exchange.load_markets()
        return self.markets

    def fetch_ohlcv(self,
                    symbol: str,
                    timeframe: str = '15m',
                    limit: int = 1000,
                    since: Optional[datetime] = None) -> pd.DataFrame:
        """
        Fetch OHLCV data and return as DataFrame.
        Columns: timestamp, open, high, low, close, volume, (quote_volume)
        """
        try:
            since_ts = None
            if since:
                since_ts = int(since.timestamp() * 1000)

            ohlcv = self.exchange.fetch_ohlcv(symbol, timeframe, since=since_ts, limit=limit)
            df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
            df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms', utc=True)
            df.set_index('timestamp', inplace=True)
            df.sort_index(inplace=True)
            return df
        except Exception as e:
            logger.error(f"Fetch OHLCV failed: {e}")
            raise

    def fetch_orderbook(self, symbol: str, limit: int = 20) -> Dict:
        """Fetch orderbook snapshot (bids/asks)."""
        try:
            orderbook = self.exchange.fetch_order_book(symbol, limit=limit)
            return orderbook
        except Exception as e:
            logger.error(f"Fetch orderbook failed: {e}")
            raise

    def fetch_funding_rate(self, symbol: str) -> Optional[float]:
        """Fetch funding rate for futures symbols."""
        try:
            if not hasattr(self.exchange, 'fetch_funding_rate'):
                return None
            funding = self.exchange.fetch_funding_rate(symbol)
            return funding.get('fundingRate')
        except Exception as e:
            logger.debug(f"Funding rate not available: {e}")
            return None

    def fetch_ticker(self, symbol: str) -> Dict:
        """Fetch 24h ticker stats."""
        try:
            ticker = self.exchange.fetch_ticker(symbol)
            return ticker
        except Exception as e:
            logger.error(f"Fetch ticker failed: {e}")
            raise

    def calculate_atr(self, df: pd.DataFrame, period: int = 14) -> pd.Series:
        """Calculate Average True Range."""
        high = df['high']
        low = df['low']
        close = df['close'].shift(1)

        tr1 = high - low
        tr2 = abs(high - close)
        tr3 = abs(low - close)
        tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
        atr = tr.rolling(period).mean()
        return atr

if __name__ == '__main__':
    # Quick test
    fetcher = MarketDataFetcher('binance')
    df = fetcher.fetch_ohlcv('BTC/USDT', '15m', 100)
    print(df.tail())
    print(f"ATR: {fetcher.calculate_atr(df).iloc[-1]:.2f}")
