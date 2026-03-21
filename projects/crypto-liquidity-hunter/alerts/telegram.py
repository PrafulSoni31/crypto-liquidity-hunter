"""
Trade Alerts: Dispatch notifications (Telegram, Discord, etc.)
"""
import os
import requests
import logging
from typing import Dict, Optional
from dataclasses import asdict

logger = logging.getLogger(__name__)

class TelegramAlerter:
    def __init__(self, bot_token: str, chat_id: str):
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.base_url = f"https://api.telegram.org/bot{bot_token}"

    def send_message(self, text: str, parse_mode: str = 'HTML') -> bool:
        """Send a text message."""
        url = f"{self.base_url}/sendMessage"
        payload = {
            'chat_id': self.chat_id,
            'text': text,
            'parse_mode': parse_mode,
            'disable_web_page_preview': True
        }
        try:
            resp = requests.post(url, json=payload, timeout=10)
            if resp.status_code == 200:
                return True
            else:
                logger.error(f"Telegram send failed: {resp.text}")
                return False
        except Exception as e:
            logger.error(f"Telegram exception: {e}")
            return False

    def format_sweep_alert(self, sweep: Dict) -> str:
        """Format sweep detection message."""
        direction_emoji = '🔽' if sweep['direction'] == 'long' else '🔼'
        text = f"""<b>LIQUIDITY SWEEP DETECTED</b> {direction_emoji}

Pair: BTC/USDT
Timeframe: 15m
Direction: {sweep['direction'].upper()}
Sweep Price: ${sweep['sweep_price']:.2f}
Close: ${sweep['close_price']:.2f}
Volume: {sweep['volume']:,.0f} ({sweep['volume_ratio']:.1f}x avg)
Depth: {abs(sweep['sweep_depth_pct']):.2f}%

<i>{sweep.get('notes', '')}</i>"""
        return text

    def format_signal_alert(self, signal) -> str:
        """Format trade signal message."""
        direction_emoji = '🟢' if signal.direction == 'long' else '🔴'
        # Determine base currency from pair (e.g., 'binance:WIF/USDT' -> 'WIF')
        base_currency = signal.pair.split('/')[-1].split(':')[-1] if '/' in signal.pair else 'BTC'
        # Dynamic precision: more decimals for low-priced assets
        def fmt(price):
            if price < 1:
                return f"{price:.6f}"
            elif price < 10:
                return f"{price:.4f}"
            else:
                return f"{price:.2f}"
        text = f"""<b>TRADE SIGNAL</b> {direction_emoji}

Pair: {signal.pair}
Direction: {signal.direction.upper()}
Entry: ${fmt(signal.entry_price)}
Stop: ${fmt(signal.stop_loss)}
Target: ${fmt(signal.target)}
R:R: {signal.risk_reward:.2f}
Size: {signal.position_size:.4f} {base_currency}
Confidence: {signal.confidence*100:.0f}%
Reason: {signal.target_type}

<b>Notes:</b> {signal.notes}"""
        return text

    def format_backtest_report(self, metrics: Dict) -> str:
        """Format backtest summary."""
        text = f"""<b>BACKTEST RESULTS</b>

Total Trades: {metrics['total_trades']}
Win Rate: {metrics['win_rate']}%
Avg R:R: {metrics['avg_r']}
Profit Factor: {metrics['profit_factor']}
Sharpe: {metrics['sharpe']}
Max Drawdown: {metrics['max_drawdown_pct']}%

Exit Reasons:
{metrics['exit_reasons']}"""
        return text

class AlertDispatcher:
    def __init__(self, config: Dict):
        self.telegram = None
        if config.get('telegram', {}).get('enabled'):
            self.telegram = TelegramAlerter(
                bot_token=config['telegram']['bot_token'],
                chat_id=config['telegram']['chat_id']
            )

    def send_sweep(self, sweep: Dict):
        if self.telegram:
            text = self.telegram.format_sweep_alert(sweep)
            self.telegram.send_message(text)

    def send_signal(self, signal):
        if self.telegram:
            text = self.telegram.format_signal_alert(signal)
            self.telegram.send_message(text)

    def send_backtest(self, metrics: Dict):
        if self.telegram:
            text = self.telegram.format_backtest_report(metrics)
            self.telegram.send_message(text)
