"""
Backtester: Simulate liquidity sweep strategy on historical data.
Evaluates performance metrics: win rate, Sharpe, max drawdown, profit factor.
"""
import pandas as pd
import numpy as np
from typing import List, Dict
from dataclasses import dataclass, asdict
import logging
from datetime import datetime

logger = logging.getLogger(__name__)

@dataclass
class TradeResult:
    """Outcome of a single trade."""
    entry_time: pd.Timestamp
    exit_time: pd.Timestamp
    direction: str
    entry_price: float
    exit_price: float
    pnl: float  # in quote currency (USDT)
    pnl_pct: float  # % of risk
    exit_reason: str  # 'target', 'stop', 'timeout'
    signal_confidence: float

class Backtester:
    def __init__(self,
                 initial_capital: float = 10000.0,
                 commission_pct: float = 0.001,  # 0.1% per trade (taker)
                 slippage_pct: float = 0.0005,   # 0.05% slippage on fill
                 max_concurrent_trades: int = 3,
                 trade_timeout_bars: int = 48):  # exit if not filled within N bars
        self.initial_capital = initial_capital
        self.capital = initial_capital
        self.commission = commission_pct
        self.slippage = slippage_pct
        self.max_concurrent = max_concurrent_trades
        self.timeout_bars = trade_timeout_bars

    def run(self,
            df: pd.DataFrame,
            atr_series: pd.Series,
            liquidity_mapper,
            sweep_detector,
            signal_engine) -> Dict:
        """
        Main backtest loop.
        Walk through df bar by bar, detect sweeps, generate signals, simulate fills.
        Returns dict with performance metrics and trade list.
        """
        trades = []
        open_signals = []  # signals waiting for entry limit fill
        active_trades = []  # currently open positions

        for i in range(len(df)):
            current_bar = df.iloc[i]
            current_time = df.index[i]
            current_price = current_bar['close']

            # 1. Check for new sweeps (after enough data)
            if i >= 50:
                lookback_df = df.iloc[i-50:i+1]
                zones = liquidity_mapper.map_liquidity(lookback_df)
                sweeps = sweep_detector.detect_sweeps(lookback_df, atr_series.iloc[i-50:i+1], zones)

                for sweep in sweeps:
                    if sweep.confirmed:
                        signal = signal_engine.generate_signal(
                            sweep, zones, current_price, self.capital
                        )
                        if signal:
                            open_signals.append({
                                'signal': signal,
                                'generated_at': current_time,
                                'bars_waiting': 0,
                                'status': 'pending'
                            })

            # 2. Check entry fills for pending signals (limit orders)
            still_pending = []
            for pending in open_signals:
                signal = pending['signal']
                # Limit fill if price crosses entry
                filled = False
                if signal.direction == 'long' and current_price >= signal.entry_price:
                    filled = True
                elif signal.direction == 'short' and current_price <= signal.entry_price:
                    filled = True

                if filled:
                    # Apply slippage: buy slightly higher, sell slightly lower
                    if signal.direction == 'long':
                        fill_price = signal.entry_price * (1 + self.slippage)
                    else:
                        fill_price = signal.entry_price * (1 - self.slippage)

                    # Enter trade
                    trade = {
                        'entry_time': current_time,
                        'entry_price': fill_price,
                        'direction': signal.direction,
                        'position_size': signal.position_size,
                        'stop_loss': signal.stop_loss,
                        'target': signal.target,
                        'initial_risk': abs(signal.entry_price - signal.stop_loss) * signal.position_size,
                        'confidence': signal.confidence,
                        'commission_paid': fill_price * signal.position_size * self.commission
                    }
                    active_trades.append(trade)
                else:
                    pending['bars_waiting'] += 1
                    if pending['bars_waiting'] >= self.timeout_bars:
                        # Signal expired, remove
                        logger.debug(f"Signal expired at {current_time}, waited {pending['bars_waiting']} bars")
                    else:
                        still_pending.append(pending)
            open_signals = still_pending

            # 3. Check exits for active trades
            still_active = []
            for trade in active_trades:
                exit_price = None
                exit_reason = None

                # Check stop loss
                if trade['direction'] == 'long' and current_bar['low'] <= trade['stop_loss']:
                    exit_price = trade['stop_loss']
                    exit_reason = 'stop'
                elif trade['direction'] == 'short' and current_bar['high'] >= trade['stop_loss']:
                    exit_price = trade['stop_loss']
                    exit_reason = 'stop'

                # Check target
                if exit_price is None:
                    if trade['direction'] == 'long' and current_bar['high'] >= trade['target']:
                        exit_price = trade['target']
                        exit_reason = 'target'
                    elif trade['direction'] == 'short' and current_bar['low'] <= trade['target']:
                        exit_price = trade['target']
                        exit_reason = 'target'

                if exit_price is not None:
                    # Apply slippage on exit
                    if trade['direction'] == 'long':
                        exit_price_adj = exit_price * (1 - self.slippage)
                    else:
                        exit_price_adj = exit_price * (1 + self.slippage)

                    # Calculate P&L
                    if trade['direction'] == 'long':
                        pnl = (exit_price_adj - trade['entry_price']) * trade['position_size']
                    else:
                        pnl = (trade['entry_price'] - exit_price_adj) * trade['position_size']

                    # Deduct commission on exit
                    pnl -= trade['entry_price'] * trade['position_size'] * self.commission
                    pnl -= exit_price_adj * trade['position_size'] * self.commission

                    result = TradeResult(
                        entry_time=trade['entry_time'],
                        exit_time=current_time,
                        direction=trade['direction'],
                        entry_price=trade['entry_price'],
                        exit_price=exit_price_adj,
                        pnl=pnl,
                        pnl_pct=pnl / trade['initial_risk'] if trade['initial_risk'] != 0 else 0,
                        exit_reason=exit_reason,
                        signal_confidence=trade['confidence']
                    )
                    trades.append(asdict(result))
                    self.capital += pnl
                else:
                    still_active.append(trade)

            active_trades = still_active

            # Optional: enforce max concurrent
            if len(active_trades) > self.max_concurrent:
                # Could close oldest? For now just log
                logger.warning(f"Exceeded max concurrent trades: {len(active_trades)}")

        # Close any remaining open trades at last price (timeout)
        final_price = df.iloc[-1]['close']
        for trade in active_trades:
            if trade['direction'] == 'long':
                pnl = (final_price - trade['entry_price']) * trade['position_size']
            else:
                pnl = (trade['entry_price'] - final_price) * trade['position_size']
            pnl -= trade['entry_price'] * trade['position_size'] * self.commission
            pnl -= final_price * trade['position_size'] * self.commission
            result = TradeResult(
                entry_time=trade['entry_time'],
                exit_time=df.index[-1],
                direction=trade['direction'],
                entry_price=trade['entry_price'],
                exit_price=final_price,
                pnl=pnl,
                pnl_pct=pnl / trade['initial_risk'] if trade['initial_risk'] != 0 else 0,
                exit_reason='timeout',
                signal_confidence=trade['confidence']
            )
            trades.append(asdict(result))
            self.capital += pnl

        # Calculate metrics
        metrics = self._calculate_metrics(trades, initial_capital=self.initial_capital)
        return {
            'final_capital': self.capital,
            'total_return_pct': (self.capital - self.initial_capital) / self.initial_capital * 100,
            'trades': trades,
            'metrics': metrics
        }

    def _calculate_metrics(self, trades: List[Dict], initial_capital: float) -> Dict:
        df_trades = pd.DataFrame(trades)
        if df_trades.empty:
            # Return consistent keys for no-trade scenario
            return {
                'total_trades': 0,
                'win_rate': 0.0,
                'avg_win_r': 0.0,
                'avg_loss_r': 0.0,
                'avg_r': 0.0,
                'profit_factor': 0.0,
                'sharpe': 0.0,
                'max_drawdown_pct': 0.0,
                'exit_reasons': {}
            }

        total_trades = len(df_trades)
        wins = df_trades[df_trades['pnl'] > 0]
        losses = df_trades[df_trades['pnl'] < 0]

        win_rate = len(wins) / total_trades if total_trades > 0 else 0
        avg_win = wins['pnl_pct'].mean() if len(wins) > 0 else 0
        avg_loss = losses['pnl_pct'].mean() if len(losses) > 0 else 0
        avg_r = df_trades['pnl_pct'].mean()
        profit_factor = abs(wins['pnl'].sum() / losses['pnl'].sum()) if len(losses) > 0 and losses['pnl'].sum() != 0 else np.inf

        # Sharpe (using trade returns as daily approx)
        returns = df_trades['pnl_pct'].values
        sharpe = returns.mean() / returns.std() * np.sqrt(365) if returns.std() != 0 else 0.0

        # Max drawdown
        equity_curve = [initial_capital]
        for trade in trades:
            equity_curve.append(equity_curve[-1] + trade['pnl'])
        equity_series = pd.Series(equity_curve)
        running_max = equity_series.expanding().max()
        drawdown = (equity_series - running_max) / running_max
        max_dd = drawdown.min() * 100

        # Exit reasons
        reason_counts = df_trades['exit_reason'].value_counts().to_dict()

        return {
            'total_trades': total_trades,
            'win_rate': round(win_rate * 100, 2),
            'avg_win_r': round(avg_win, 2),
            'avg_loss_r': round(avg_loss, 2),
            'avg_r': round(avg_r, 2),
            'profit_factor': round(profit_factor, 2) if profit_factor != np.inf else float('inf'),
            'sharpe': round(sharpe, 2),
            'max_drawdown_pct': round(max_dd, 2),
            'exit_reasons': reason_counts
        }

if __name__ == '__main__':
    print("Backtester module loaded.")
