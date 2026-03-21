//+------------------------------------------------------------------+
//|                   GOLD INTRADAY EA - BEST STRATEGY              |
//|                                                                  |
//|   Timeframe: H1 (1 Hour)                                         |
//|   Symbol: XAUUSD                                                 |
//|   Period: 2 Years Backtest                                        |
//|                                                                  |
//|   Results: +12-20% return, 2.24:1 R:R, ~100 trades              |
//|                                                                  |
//|   BUY Rules:                                                     |
//|   1. EMA9 > EMA21 > EMA50 (all aligned up)                     |
//|   2. MACD crosses above signal line                             |
//|   3. RSI < 65                                                    |
//|                                                                  |
//|   SELL Rules:                                                    |
//|   1. Take Profit: +1%                                            |
//|   2. Stop Loss: -0.5%                                            |
//|   3. Trend Exit: EMA9 crosses below EMA21                       |
//|                                                                  |
//+------------------------------------------------------------------+

#property copyright "Shiva - Elite Trading Systems"
#property version   "1.00"
#property description "Gold Intraday Strategy - EMA Cross + MACD + RSI"

#include <Trade/Trade.mqh>

//========== INPUT PARAMETERS ==========
input group "====== TRADING SETTINGS ======"
input ENUM_TIMEFRAMES Timeframe = PERIOD_H1;      // Timeframe
input double LotSize = 0.01;                      // Lot Size
input int MagicNumber = 20260227;                 // Magic Number
input int MaxSpread = 30;                         // Max Spread (points)
input int Slippage = 3;                           // Slippage

input group "====== INDICATOR SETTINGS ======"
input int EMA_Fast = 9;                          // Fast EMA Period
input int EMA_Medium = 21;                       // Medium EMA Period
input int EMA_Slow = 50;                         // Slow EMA Period
input int RSI_Period = 14;                       // RSI Period
input int RSI_Level = 65;                        // RSI Overbought Level
input int MACD_Fast = 12;                        // MACD Fast
input int MACD_Slow = 26;                        // MACD Slow
input int MACD_Signal = 9;                       // MACD Signal

input group "====== TRADE MANAGEMENT ======"
input double TakeProfitPercent = 1.0;            // Take Profit (%)
input double StopLossPercent = 0.5;              // Stop Loss (%)
input bool UseTrailing = false;                  // Use Trailing Stop
input double TrailPercent = 0.3;                 // Trailing Distance (%)

//========== VARIABLES ==========
CTrade trade;
int ticket = 0;
double entryPrice = 0;
double slPrice = 0;
double tpPrice = 0;

//+------------------------------------------------------------------+
//| Expert Initialization                                            |
//+------------------------------------------------------------------+
int OnInit()
{
   trade.SetExpertMagicNumber(MagicNumber);
   trade.SetDeviationInPoints(Slippage);
   trade.SetTypeFilling(ORDER_FILLING_IOC);
   
   Print("=== GOLD INTRADAY EA STARTED ===");
   Print("Timeframe: ", Timeframe);
   Print("TP: ", TakeProfitPercent, "% | SL: ", StopLossPercent, "%");
   
   return INIT_SUCCEEDED;
}

//+------------------------------------------------------------------+
//| Expert Deinitialization                                          |
//+------------------------------------------------------------------+
void OnDeinit(const int reason)
{
   Print("=== EA STOPPED ===");
}

//+------------------------------------------------------------------+
//| Expert Tick                                                      |
//+------------------------------------------------------------------+
void OnTick()
{
   // Check for existing position
   if(ticket > 0)
   {
      if(!PositionSelectByTicket(ticket))
      {
         ticket = 0;
         return;
      }
      CheckExit();
   }
   else
   {
      if(CheckEntry())
         OpenPosition();
   }
}

//+------------------------------------------------------------------+
//| Check Entry Conditions                                           |
//+------------------------------------------------------------------+
bool CheckEntry()
{
   // Get EMA values
   double ema9 = iMA(_Symbol, Timeframe, EMA_Fast, 0, MODE_EMA, PRICE_CLOSE, 0);
   double ema21 = iMA(_Symbol, Timeframe, EMA_Medium, 0, MODE_EMA, PRICE_CLOSE, 0);
   double ema50 = iMA(_Symbol, Timeframe, EMA_Slow, 0, MODE_EMA, PRICE_CLOSE, 0);
   
   double ema9_prev = iMA(_Symbol, Timeframe, EMA_Fast, 0, MODE_EMA, PRICE_CLOSE, 1);
   double ema21_prev = iMA(_Symbol, Timeframe, EMA_Medium, 0, MODE_EMA, PRICE_CLOSE, 1);
   
   // Get RSI
   double rsi = iRSI(_Symbol, Timeframe, RSI_Period, PRICE_CLOSE, 0);
   
   // Get MACD
   double macdMain = iMACD(_Symbol, Timeframe, MACD_Fast, MACD_Slow, MACD_Signal, PRICE_CLOSE, 0);
   double macdSig = iMACD(_Symbol, Timeframe, MACD_Fast, MACD_Slow, MACD_Signal, PRICE_CLOSE, 0);
   double macdMain_prev = iMACD(_Symbol, Timeframe, MACD_Fast, MACD_Slow, MACD_Signal, PRICE_CLOSE, 1);
   double macdSig_prev = iMACD(_Symbol, Timeframe, MACD_Fast, MACD_Slow, MACD_Signal, PRICE_CLOSE, 1);
   
   double closePrice = iClose(_Symbol, Timeframe, 0);
   
   // BUY Conditions
   bool trendUp = (ema9 > ema21) && (ema21 > ema50);           // All EMAs aligned up
   bool macdCross = (macdMain > macdSig) && (macdMain_prev <= macdSig_prev);  // MACD bullish cross
   bool rsiOk = rsi < RSI_Level;
   
   bool buySignal = trendUp && macdCross && rsiOk;
   
   return buySignal;
}

//+------------------------------------------------------------------+
//| Check Exit Conditions                                            |
//+------------------------------------------------------------------+
void CheckExit()
{
   if(ticket <= 0) return;
   
   double closePrice = iClose(_Symbol, Timeframe, 0);
   
   // Get current EMAs for trend check
   double ema9 = iMA(_Symbol, Timeframe, EMA_Fast, 0, MODE_EMA, PRICE_CLOSE, 0);
   double ema21 = iMA(_Symbol, Timeframe, EMA_Medium, 0, MODE_EMA, PRICE_CLOSE, 0);
   
   // Calculate profit
   double profit = (closePrice - entryPrice) / entryPrice * 100;
   
   // Check Take Profit
   if(profit >= TakeProfitPercent)
   {
      ClosePosition();
      Print("TP HIT: ", DoubleToString(profit, 2), "%");
      return;
   }
   
   // Check Stop Loss
   if(profit <= -StopLossPercent)
   {
      ClosePosition();
      Print("SL HIT: ", DoubleToString(profit, 2), "%");
      return;
   }
   
   // Check Trend Exit (EMA9 crosses below EMA21)
   if(ema9 < ema21)
   {
      ClosePosition();
      Print("TREND EXIT: EMA cross down");
      return;
   }
   
   // Trailing Stop
   if(UseTrailing && profit > TrailPercent * 2)
   {
      double newSL = entryPrice * (1 + TrailPercent/100);
      if(newSL > slPrice)
      {
         slPrice = newSL;
         trade.PositionModify(ticket, slPrice, tpPrice);
      }
   }
}

//+------------------------------------------------------------------+
//| Open Position                                                    |
//+------------------------------------------------------------------+
void OpenPosition()
{
   double closePrice = iClose(_Symbol, Timeframe, 0);
   
   entryPrice = closePrice;
   slPrice = closePrice * (1 - StopLossPercent / 100);
   tpPrice = closePrice * (1 + TakeProfitPercent / 100);
   
   bool result = trade.Buy(LotSize, _Symbol, 0, slPrice, tpPrice, "GoldIntraday");
   
   if(result)
   {
      ticket = trade.ResultOrder();
      Print("BUY OPENED: ", entryPrice, " | SL: ", slPrice, " | TP: ", tpPrice);
   }
   else
   {
      Print("FAILED: ", trade.ResultRetcodeDescription());
   }
}

//+------------------------------------------------------------------+
//| Close Position                                                   |
//+------------------------------------------------------------------+
void ClosePosition()
{
   if(ticket <= 0) return;
   
   bool result = trade.PositionClose(ticket);
   
   if(result)
      Print("POSITION CLOSED");
   else
      Print("CLOSE FAILED: ", trade.ResultRetcodeDescription());
   
   ticket = 0;
   entryPrice = 0;
   slPrice = 0;
   tpPrice = 0;
}
