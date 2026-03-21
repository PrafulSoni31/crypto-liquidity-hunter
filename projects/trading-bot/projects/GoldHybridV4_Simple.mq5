//+------------------------------------------------------------------+
//|                    GOLD HYBRID V4 - SIMPLE VERSION                |
//|                                                                  |
//|  Settings:                                                       |
//|  - Timeframe: H4 or Daily                                        |
//|  - Symbol: XAUUSD                                                |
//|  - Lot: 0.01-0.1 (adjust based on balance)                      |
//|                                                                  |
//|  BUY Rules:                                                      |
//|  1. Price > 200 MA                                               |
//|  2. Price > 50 MA                                                |
//|  3. Price > 20 MA                                                |
//|  4. MACD crosses above signal OR RSI < 35                       |
//|                                                                  |
//|  SELL Rules:                                                     |
//|  - Take Profit: +5% (or 4 ATR)                                   |
//|  - Stop Loss: -2% (or 2 ATR)                                     |
//|  - Trend Exit: Price < 50 MA                                     |
//|                                                                  |
//+------------------------------------------------------------------+

#include <Trade/Trade.mqh>

input double LotSize = 0.01;
input ENUM_TIMEFRAMES Timeframe = PERIOD_H4;
input int Magic = 20260226;

CTrade mtrade;

int OnInit()
{
   mtrade.SetExpertMagicNumber(Magic);
   Print("Gold Hybrid V4 EA Started");
   return INIT_SUCCEEDED;
}

void OnTick()
{
   static int ticket = 0;
   
   double ma20 = iMA(_Symbol, Timeframe, 20, 0, MODE_EMA, PRICE_CLOSE);
   double ma50 = iMA(_Symbol, Timeframe, 50, 0, MODE_EMA, PRICE_CLOSE);
   double ma200 = iMA(_Symbol, Timeframe, 200, 0, MODE_EMA, PRICE_CLOSE);
   double rsi = iRSI(_Symbol, Timeframe, 14, PRICE_CLOSE);
   double close = iClose(_Symbol, Timeframe, 0);
   double prevClose = iClose(_Symbol, Timeframe, 1);
   
   // MACD
   double macdMain = iMACD(_Symbol, Timeframe, 12, 26, 9, PRICE_CLOSE).Main;
   double macdPrev = iMACD(_Symbol, Timeframe, 12, 26, 9, PRICE_CLOSE).Main;
   double macdSig = iMACD(_Symbol, Timeframe, 12, 26, 9, PRICE_CLOSE).Signal;
   double macdSigPrev = iMACD(_Symbol, Timeframe, 12, 26, 9, PRICE_CLOSE).Signal;
   
   // ATR for stops
   double atr = iATR(_Symbol, Timeframe, 14, 0);
   
   // Check for open position
   if(ticket > 0)
   {
      if(!PositionSelectByTicket(ticket))
      {
         ticket = 0;
         return;
      }
      
      double posPrice = PositionGetDouble(POSITION_PRICE_OPEN);
      double sl = PositionGetDouble(POSITION_SL);
      double tp = PositionGetDouble(POSITION_TP);
      
      // Check TP
      if(close >= tp)
      {
         mtrade.PositionClose(ticket);
         Print("TP Hit!");
         ticket = 0;
      }
      // Check SL
      else if(close <= sl)
      {
         mtrade.PositionClose(ticket);
         Print("SL Hit!");
         ticket = 0;
      }
      // Trend exit
      else if(close < ma50)
      {
         mtrade.PositionClose(ticket);
         Print("Trend Exit!");
         ticket = 0;
      }
      return;
   }
   
   // Entry conditions
   bool buy1 = (close > ma200) && (close > ma50) && (close > ma20) 
            && (macdMain > macdSig) && (macdPrev <= macdSigPrev) && (rsi < 65);
   bool buy2 = (close > ma200) && (rsi < 35) && (close > prevClose);
   
   if(buy1 || buy2)
   {
      double entry = close;
      double stopLoss = close - atr * 2;
      double takeProfit = close + atr * 5;  // 1:2.5 ratio
      
      mtrade.Buy(LotSize, _Symbol, 0, stopLoss, takeProfit, "GoldV4");
      ticket = mtrade.ResultOrder();
      Print("BUY Opened at ", entry);
   }
}
