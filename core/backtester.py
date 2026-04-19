"""
Backtester v2 — Fixed & Professional
=====================================
Phase 1 Fixes:
  - Uses FULL dataset (not rolling 50-bar window) for sweep/zone detection
  - Proper bar-by-bar simulation with expanding window
  - Entry logic matches signal engine v2 (retracement between sweep and close)
  - Slippage applied correctly on limit fills

Phase 2 Additions:
  - HTF bias integration (uses 4h data to filter 1h/15m signals)
  - OB/FVG confluence tracked in trade results
  - Detailed per-trade metadata
"""
import pandas as pd
import numpy as np
from typing import List, Dict, Optional
from dataclasses import dataclass, asdict
import logging
from datetime import datetime

logger = logging.getLogger(__name__)


@dataclass
class TradeResult:
    entry_time:       pd.Timestamp
    exit_time:        pd.Timestamp
    direction:        str
    entry_price:      float
    exit_price:       float
    pnl:              float
    pnl_pct:          float
    exit_reason:      str       # 'target' | 'stop' | 'timeout'
    signal_confidence: float
    ob_confluence:    bool = False
    fvg_confluence:   bool = False
    htf_bias:         str  = 'neutral'
    risk_reward:      float = 0.0


class Backtester:
    def __init__(self,
                 initial_capital:       float = 10000.0,
                 commission_pct:        float = 0.001,
                 slippage_pct:          float = 0.0005,
                 max_concurrent_trades: int   = 3,
                 trade_timeout_bars:    int   = 48):
        self.initial_capital = initial_capital
        self.capital         = initial_capital
        self.commission      = commission_pct
        self.slippage        = slippage_pct
        self.max_concurrent  = max_concurrent_trades
        self.timeout_bars    = trade_timeout_bars

    def run(self,
            df: pd.DataFrame,
            atr_series: pd.Series,
            liquidity_mapper,
            sweep_detector,
            signal_engine,
            df_htf: Optional[pd.DataFrame] = None) -> Dict:
        """
        Full backtest with expanding window.
        df:       primary timeframe OHLCV (1h or 15m)
        df_htf:   higher timeframe OHLCV (4h) for bias — optional
        """
        self.capital = self.initial_capital
        trades       = []
        open_signals = []   # pending limit orders
        active_trades = []  # open positions

        # Minimum warmup bars for sweep detection
        min_warmup = max(sweep_detector.lookback_bars + 5, 30)

        for i in range(min_warmup, len(df)):
            current_bar   = df.iloc[i]
            current_time  = df.index[i]
            current_price = current_bar['close']

            # ── Detect sweeps using EXPANDING window (all data up to now) ──
            lookback_df  = df.iloc[:i + 1]  # full history up to current bar
            lookback_atr = atr_series.iloc[:i + 1]

            try:
                zones  = liquidity_mapper.map_liquidity(lookback_df)
                sweeps = sweep_detector.detect_sweeps(lookback_df, lookback_atr, zones)

                # Phase 2: OB/FVG/HTF
                obs  = sweep_detector.detect_order_blocks(lookback_df)
                fvgs = sweep_detector.detect_fvgs(lookback_df)

                htf_bias = 'neutral'
                if df_htf is not None:
                    # Find the HTF bars up to current time
                    htf_slice = df_htf[df_htf.index <= current_time]
                    if len(htf_slice) >= 30:
                        htf_bias = sweep_detector.get_htf_bias(htf_slice)

                # Only consider the most recent sweep (one per bar check)
                if sweeps:
                    latest_sweep = sweeps[-1]
                    # Only generate signal if sweep is from the last few bars
                    sweep_bar_idx = lookback_df.index.get_loc(latest_sweep.timestamp)
                    bars_ago = i - sweep_bar_idx
                    if bars_ago <= 3:  # sweep must be very recent (≤3 bars old)
                        signal = signal_engine.generate_signal(
                            latest_sweep, zones, current_price,
                            capital=self.capital,
                            htf_bias=htf_bias,
                            order_blocks=obs,
                            fvgs=fvgs
                        )
                        if signal and len(active_trades) < self.max_concurrent:
                            # Check not already trading this direction
                            existing_dirs = [t['direction'] for t in active_trades]
                            if existing_dirs.count(signal.direction) < 2:
                                open_signals.append({
                                    'signal':       signal,
                                    'generated_at': current_time,
                                    'bars_waiting': 0,
                                })
            except Exception as e:
                logger.debug(f"Bar {i}: sweep/signal error: {e}")

            # ── Check entry fills ──────────────────────────────────────────
            still_pending = []
            for pending in open_signals:
                signal     = pending['signal']
                filled     = False
                fill_price = None

                if signal.direction == 'long':
                    # Limit buy: fills if bar's low touches entry price
                    if current_bar['low'] <= signal.entry_price <= current_bar['high']:
                        fill_price = signal.entry_price * (1 + self.slippage)
                        filled = True
                elif signal.direction == 'short':
                    if current_bar['low'] <= signal.entry_price <= current_bar['high']:
                        fill_price = signal.entry_price * (1 - self.slippage)
                        filled = True

                if filled:
                    active_trades.append({
                        'entry_time':   current_time,
                        'entry_price':  fill_price,
                        'direction':    signal.direction,
                        'position_size': signal.position_size,
                        'stop_loss':    signal.stop_loss,
                        'target':       signal.target,
                        'initial_risk': (abs(signal.entry_price - signal.stop_loss)
                                         * signal.position_size),
                        'confidence':   signal.confidence,
                        'commission_paid': fill_price * signal.position_size * self.commission,
                        'ob_confluence': signal.ob_confluence,
                        'fvg_confluence': signal.fvg_confluence,
                        'htf_bias':     signal.htf_bias,
                        'risk_reward':  signal.risk_reward,
                        'bars_in_trade': 0,
                    })
                else:
                    pending['bars_waiting'] += 1
                    if pending['bars_waiting'] < self.timeout_bars:
                        still_pending.append(pending)
                    # else: signal expired — discard silently
            open_signals = still_pending

            # ── Check exits ────────────────────────────────────────────────
            still_active = []
            for trade in active_trades:
                trade['bars_in_trade'] += 1
                exit_price  = None
                exit_reason = None

                if trade['direction'] == 'long':
                    if current_bar['low'] <= trade['stop_loss']:
                        exit_price  = trade['stop_loss'] * (1 - self.slippage)
                        exit_reason = 'stop'
                    elif current_bar['high'] >= trade['target']:
                        exit_price  = trade['target'] * (1 - self.slippage)
                        exit_reason = 'target'
                else:
                    if current_bar['high'] >= trade['stop_loss']:
                        exit_price  = trade['stop_loss'] * (1 + self.slippage)
                        exit_reason = 'stop'
                    elif current_bar['low'] <= trade['target']:
                        exit_price  = trade['target'] * (1 + self.slippage)
                        exit_reason = 'target'

                # Timeout exit
                if exit_price is None and trade['bars_in_trade'] >= self.timeout_bars:
                    exit_price  = current_price
                    exit_reason = 'timeout'

                if exit_price is not None:
                    if trade['direction'] == 'long':
                        pnl = (exit_price - trade['entry_price']) * trade['position_size']
                    else:
                        pnl = (trade['entry_price'] - exit_price) * trade['position_size']

                    # Deduct commissions (entry + exit)
                    pnl -= trade['commission_paid']
                    pnl -= exit_price * trade['position_size'] * self.commission

                    initial_risk = trade['initial_risk']
                    result = TradeResult(
                        entry_time=trade['entry_time'],
                        exit_time=current_time,
                        direction=trade['direction'],
                        entry_price=trade['entry_price'],
                        exit_price=exit_price,
                        pnl=pnl,
                        pnl_pct=pnl / initial_risk if initial_risk != 0 else 0,
                        exit_reason=exit_reason,
                        signal_confidence=trade['confidence'],
                        ob_confluence=trade['ob_confluence'],
                        fvg_confluence=trade['fvg_confluence'],
                        htf_bias=trade['htf_bias'],
                        risk_reward=trade['risk_reward'],
                    )
                    trades.append(asdict(result))
                    self.capital += pnl
                else:
                    still_active.append(trade)

            active_trades = still_active

        # Close remaining open trades at last price
        final_price = df.iloc[-1]['close']
        for trade in active_trades:
            if trade['direction'] == 'long':
                pnl = (final_price - trade['entry_price']) * trade['position_size']
            else:
                pnl = (trade['entry_price'] - final_price) * trade['position_size']
            pnl -= trade['commission_paid']
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
                signal_confidence=trade['confidence'],
                ob_confluence=trade['ob_confluence'],
                fvg_confluence=trade['fvg_confluence'],
                htf_bias=trade['htf_bias'],
                risk_reward=trade['risk_reward'],
            )
            trades.append(asdict(result))
            self.capital += pnl

        metrics = self._calculate_metrics(trades, self.initial_capital)
        return {
            'final_capital':    self.capital,
            'total_return_pct': (self.capital - self.initial_capital) / self.initial_capital * 100,
            'trades':           trades,
            'metrics':          metrics
        }

    def _calculate_metrics(self, trades: List[Dict], initial_capital: float) -> Dict:
        if not trades:
            return {
                'total_trades': 0, 'win_rate': 0.0, 'avg_win_r': 0.0,
                'avg_loss_r': 0.0, 'avg_r': 0.0, 'profit_factor': 0.0,
                'sharpe': 0.0, 'sortino': 0.0, 'max_drawdown_pct': 0.0,
                'avg_rr': 0.0, 'ob_win_rate': 0.0, 'fvg_win_rate': 0.0,
                'htf_aligned_win_rate': 0.0, 'exit_reasons': {}
            }

        df_t = pd.DataFrame(trades)
        total   = len(df_t)
        wins    = df_t[df_t['pnl'] > 0]
        losses  = df_t[df_t['pnl'] < 0]

        win_rate = len(wins) / total
        pf = (abs(wins['pnl'].sum() / losses['pnl'].sum())
              if len(losses) > 0 and losses['pnl'].sum() != 0 else float('inf'))

        returns  = df_t['pnl_pct'].values
        sharpe   = (returns.mean() / returns.std() * np.sqrt(252)
                    if returns.std() != 0 else 0.0)
        downside = returns[returns < 0]
        sortino  = (returns.mean() / downside.std() * np.sqrt(252)
                    if len(downside) > 0 and downside.std() != 0 else 0.0)

        # Equity curve + drawdown
        equity = [initial_capital]
        for t in trades:
            equity.append(equity[-1] + t['pnl'])
        eq_series   = pd.Series(equity)
        running_max = eq_series.expanding().max()
        drawdown    = (eq_series - running_max) / running_max
        max_dd      = drawdown.min() * 100

        # Confluence stats
        ob_trades   = df_t[df_t['ob_confluence'] == True]
        fvg_trades  = df_t[df_t['fvg_confluence'] == True]
        htf_aligned = df_t[df_t['htf_bias'].isin(['bullish', 'bearish'])]

        ob_wr  = (len(ob_trades[ob_trades['pnl'] > 0]) / len(ob_trades)
                  if len(ob_trades) > 0 else 0.0)
        fvg_wr = (len(fvg_trades[fvg_trades['pnl'] > 0]) / len(fvg_trades)
                  if len(fvg_trades) > 0 else 0.0)
        htf_wr = (len(htf_aligned[htf_aligned['pnl'] > 0]) / len(htf_aligned)
                  if len(htf_aligned) > 0 else 0.0)

        return {
            'total_trades':          total,
            'win_rate':              round(win_rate * 100, 2),
            'avg_win_r':             round(wins['pnl_pct'].mean(), 2) if len(wins) > 0 else 0,
            'avg_loss_r':            round(losses['pnl_pct'].mean(), 2) if len(losses) > 0 else 0,
            'avg_r':                 round(df_t['pnl_pct'].mean(), 2),
            'avg_rr':                round(df_t['risk_reward'].mean(), 2),
            'profit_factor':         round(pf, 2) if pf != float('inf') else 999.0,
            'sharpe':                round(sharpe, 2),
            'sortino':               round(sortino, 2),
            'max_drawdown_pct':      round(max_dd, 2),
            'ob_win_rate':           round(ob_wr * 100, 2),
            'fvg_win_rate':          round(fvg_wr * 100, 2),
            'htf_aligned_win_rate':  round(htf_wr * 100, 2),
            'exit_reasons':          df_t['exit_reason'].value_counts().to_dict()
        }
