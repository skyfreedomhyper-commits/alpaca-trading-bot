"""
Trading View Back Test Bot
專注於均線策略、陰陽燭形態及 TradingView 評級的歷史回測引擎。
"""

import os
import sys
import logging
from datetime import datetime, timedelta, timezone

import pandas as pd
import numpy as np

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from alpaca.data.historical import StockHistoricalDataClient, CryptoHistoricalDataClient
from alpaca.data.requests import StockBarsRequest, CryptoBarsRequest
from alpaca.data.timeframe import TimeFrame
from alpaca.data.enums import DataFeed

from tradingview_ta import TA_Handler, Interval

# ============================================================
# 策略參數 (同步自 alpaca_trading_bot.py)
# ============================================================
SHORT_MA  = 5
LONG_MA   = 20
BUY_WINDOW = 30
TRAILING_STOP_PCT = 0.02   # 2%
CANDLE_LOOKBACK = 3

# 預設觀察清單
WATCHLIST = ["NFLX", "TSLA", "NVDA", "AAPL", "MSFT", "GOOGL", "META", "AMZN"]
CRYPTO_WATCHLIST = ["BTC/USD", "ETH/USD", "SOL/USD", "AVAX/USD", "LINK/USD"]

# ============================================================
# Log 設定
# ============================================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
log = logging.getLogger(__name__)

# ----------------------------------------------------------
# TradingView 技術分析評級
# ----------------------------------------------------------
def analyse_stock(symbol: str) -> dict:
    for exchange in ("NASDAQ", "NYSE", "AMEX"):
        try:
            handler = TA_Handler(
                symbol=symbol,
                screener="america",
                exchange=exchange,
                interval=Interval.INTERVAL_1_DAY,
            )
            analysis  = handler.get_analysis()
            rec       = analysis.summary["RECOMMENDATION"]
            return {
                "tv_rating": rec,
                "bullish":   rec in ("BUY", "STRONG_BUY"),
            }
        except Exception:
            continue
    return {"tv_rating": "UNKNOWN", "bullish": False}

def analyse_crypto(symbol: str) -> dict:
    tv_sym = symbol.replace("/", "")
    for exchange in ("COINBASE", "BINANCE"):
        try:
            handler = TA_Handler(
                symbol=tv_sym,
                screener="crypto",
                exchange=exchange,
                interval=Interval.INTERVAL_1_DAY,
            )
            analysis  = handler.get_analysis()
            rec       = analysis.summary["RECOMMENDATION"]
            return {
                "tv_rating": rec,
                "bullish":   rec in ("BUY", "STRONG_BUY"),
            }
        except Exception:
            continue
    return {"tv_rating": "UNKNOWN", "bullish": False}

# ----------------------------------------------------------
# 歷史數據獲取
# ----------------------------------------------------------
def get_historical_bars_df(data_client, symbol: str, days: int, is_crypto=False) -> pd.DataFrame:
    end   = datetime.now(timezone.utc)
    start = end - timedelta(days=days + 30)

    try:
        if is_crypto:
            req = CryptoBarsRequest(symbol_or_symbols=symbol, timeframe=TimeFrame.Day, start=start, end=end)
            bars = data_client.get_crypto_bars(req)
        else:
            req = StockBarsRequest(symbol_or_symbols=symbol, timeframe=TimeFrame.Day, start=start, end=end, feed=DataFeed.IEX)
            bars = data_client.get_stock_bars(req)

        df = bars.df
        if df.empty: return pd.DataFrame()
        if isinstance(df.index, pd.MultiIndex):
            df = df.xs(symbol, level="symbol")
        return df[["open", "high", "low", "close", "volume"]].sort_index().tail(days)
    except Exception as e:
        log.warning(f"無法取得 {symbol} 數據: {e}")
        return pd.DataFrame()

# ----------------------------------------------------------
# 陰陽燭形態識別
# ----------------------------------------------------------
def _candle_body(o, c): return abs(c - o)
def _upper_wick(o, c, h): return h - max(o, c)
def _lower_wick(o, c, lo): return min(o, c) - lo

def detect_candle_patterns(df, i) -> dict:
    if i < 0 or i >= len(df): return {"bullish": False, "patterns": []}
    o0, h0, l0, c0 = df.iloc[i][["open", "high", "low", "close"]]
    body0 = _candle_body(o0, c0)
    range0 = (h0 - l0) or 1e-9
    up0, lo0 = _upper_wick(o0, c0, h0), _lower_wick(o0, c0, l0)
    bull0 = c0 > o0
    patterns = []
    bullish = False

    # 簡單形態範例 (詳細可參考主機器人代碼)
    if bull0 and body0 > 0 and lo0 >= 2 * body0 and up0 <= 0.2 * range0:
        patterns.append("槌子線"); bullish = True

    if i >= 1:
        o1, c1 = df.iloc[i-1][["open", "close"]]
        bear1 = c1 < o1
        if bear1 and bull0 and o0 <= c1 and c0 >= o1:
            patterns.append("牛市吞沒"); bullish = True

    return {"bullish": bullish, "patterns": patterns}

def has_recent_candle_signal(df, i, lookback=CANDLE_LOOKBACK):
    all_patterns = []
    for j in range(max(0, i - lookback + 1), i + 1):
        res = detect_candle_patterns(df, j)
        if res["bullish"]:
            for p in res["patterns"]:
                if p not in all_patterns: all_patterns.append(p)
    return bool(all_patterns), all_patterns

# ----------------------------------------------------------
# 回測核心
# ----------------------------------------------------------
def backtest_strategy(closes, symbol, bars_df=None, use_candle_filter=False):
    ma_short = closes.rolling(SHORT_MA).mean()
    ma_long  = closes.rolling(LONG_MA).mean()
    trades = []
    in_trade = False
    entry_price, entry_date, peak = 0.0, None, 0.0
    dates = closes.index.tolist()

    for i in range(1, len(closes)):
        ps, cs = ma_short.iloc[i-1], ma_short.iloc[i]
        pl, cl = ma_long.iloc[i-1], ma_long.iloc[i]
        if any(pd.isna(x) for x in (ps, cs, pl, cl)): continue

        price = closes.iloc[i]
        if not in_trade:
            if ps <= pl and cs > cl: # 金叉
                if use_candle_filter and bars_df is not None:
                    found, _ = has_recent_candle_signal(bars_df, i)
                    if not found: continue
                in_trade, entry_price, entry_date, peak = True, price, dates[i], price
        else:
            peak = max(peak, price)
            drawdown = (peak - price) / peak
            # 止損或死叉
            if drawdown >= TRAILING_STOP_PCT or (ps >= pl and cs < cl):
                exit_type = "STOP" if drawdown >= TRAILING_STOP_PCT else "CROSS"
                trades.append({
                    "entry_date": entry_date, "exit_date": dates[i],
                    "pnl_pct": (price - entry_price) / entry_price * 100,
                    "exit_type": exit_type
                })
                in_trade = False

    wins = sum(1 for t in trades if t["pnl_pct"] > 0)
    pnls = [t["pnl_pct"] for t in trades]
    return {
        "symbol": symbol, "trades": trades, "total": len(trades),
        "win_rate": wins / len(trades) if trades else 0,
        "avg_pnl": np.mean(pnls) if pnls else 0
    }

# ----------------------------------------------------------
# 主執行邏輯
# ----------------------------------------------------------
def run_backtest(days=365):
    api_key = os.getenv("ALPACA_API_KEY")
    api_secret = os.getenv("ALPACA_API_SECRET")
    if not api_key or not api_secret:
        log.error("請設定 ALPACA_API_KEY 與 ALPACA_API_SECRET")
        return

    stock_client = StockHistoricalDataClient(api_key, api_secret)
    crypto_client = CryptoHistoricalDataClient(api_key, api_secret)

    results = []

    log.info(f"=== 開始回測 (過去 {days} 天) ===")

    for symbol in WATCHLIST:
        df = get_historical_bars_df(stock_client, symbol, days)
        if df.empty: continue
        res_ma = backtest_strategy(df["close"], symbol)
        res_candle = backtest_strategy(df["close"], symbol, bars_df=df, use_candle_filter=True)
        tv = analyse_stock(symbol)
        results.append({"sym": symbol, "ma": res_ma, "candle": res_candle, "tv": tv})
        log.info(f"{symbol:5} | MA勝率: {res_ma['win_rate']*100:4.1f}% | 燭形勝率: {res_candle['win_rate']*100:4.1f}% | TV: {tv['tv_rating']}")

    for symbol in CRYPTO_WATCHLIST:
        df = get_historical_bars_df(crypto_client, symbol, days, is_crypto=True)
        if df.empty: continue
        res_ma = backtest_strategy(df["close"], symbol)
        res_candle = backtest_strategy(df["close"], symbol, bars_df=df, use_candle_filter=True)
        tv = analyse_crypto(symbol)
        results.append({"sym": symbol, "ma": res_ma, "candle": res_candle, "tv": tv})
        log.info(f"{symbol:5} | MA勝率: {res_ma['win_rate']*100:4.1f}% | 燭形勝率: {res_candle['win_rate']*100:4.1f}% | TV: {tv['tv_rating']}")

    log.info("=== 回測完成 ===")

if __name__ == "__main__":
    days = 365
    if len(sys.argv) > 1:
        try: days = int(sys.argv[1])
        except: pass
    run_backtest(days)
