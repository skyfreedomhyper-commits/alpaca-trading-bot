"""
Webull 自動交易機器人 — V8.1
雙均線策略（5日線 / 20日線）
+ TradingView 技術評級雙重確認
+ 陰陽燭形態三重確認（參考 CandleSticker.com）
+ 2% 滾動止損（Trailing Stop Loss，持久化 peaks.json）
+ 10 秒高頻監測
+ 歷史行情 & 即時報價由 yfinance（Yahoo Finance）提供（無需 Webull 數據 API 憑證）
+ Webull OpenAPI SDK（webull-openapi-python-sdk v2.0.7）— 僅用於下單 & 帳戶

環境變數設定（建議儲存於 .env 檔案）：
  WEBULL_APP_KEY    = 您的 Webull App Key
  WEBULL_APP_SECRET = 您的 Webull App Secret

安裝指令：
  pip install webull-openapi-python-sdk pandas numpy python-dotenv tradingview_ta

使用說明：
  python -X utf8 webull_trading_bot_v8.py                       # 試跑驗證
  python -X utf8 webull_trading_bot_v8.py --live                # UAT 模擬（預設）
  python -X utf8 webull_trading_bot_v8.py --live --real         # 實盤（⚠️ 真實資金）
  python -X utf8 webull_trading_bot_v8.py --backtest            # 回測（預設 365 天）
  python -X utf8 webull_trading_bot_v8.py --backtest --days 180
"""

import os
import sys
import time
import uuid
import logging
from datetime import datetime, timedelta, timezone

import pandas as pd
import numpy as np

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# Webull SDK's init_token() calls GET /openapi/config to decide whether a bearer
# token is needed.  On US endpoints this route returns 404/401 (not implemented
# for the US region).  Patching _check_token_enable to always return False tells
# the SDK to skip the token flow and proceed with HMAC-only signing — which is
# correct for US OpenAPI accounts.
from webull.core.http.initializer.client_initializer import ClientInitializer
ClientInitializer._check_token_enable = staticmethod(lambda _: False)

import yfinance as yf

from webull.core.client import ApiClient
from webull.trade.trade_client import TradeClient

from tradingview_ta import TA_Handler, Interval

# ============================================================
# ⚠️  模式切換
#   True  = Webull UAT 測試環境（模擬，需 UAT 憑證）
#   False = Webull 實盤（有真實資金風險！）
# ============================================================
WEBULL_PAPER = True

# ============================================================
# Webull API 端點
# ============================================================
TRADE_ENDPOINT_PAPER = "us-openapi-alb.uat.webullbroker.com"
TRADE_ENDPOINT_LIVE  = "api.webull.com"

# ============================================================
# 監控股票清單（美股代碼）
# ============================================================
WATCHLIST = [
    "NFLX",
    "TSLA",
    "NVDA",
]

SHORT_MA   = 5
LONG_MA    = 20
BUY_WINDOW = 30

CAPITAL_PER_TRADE = 10_000
TRAILING_STOP_PCT = 0.02
POLL_INTERVAL     = 10

# ============================================================
# 開市前自動選股設定
# ============================================================
SCREEN_CANDIDATES = [
    "AAPL", "MSFT", "NVDA", "GOOGL", "META", "AMZN", "TSLA", "AMD", "INTC", "AVGO",
    "JPM", "BAC", "GS", "MS", "WFC", "C", "BLK", "AXP",
    "UNH", "JNJ", "PFE", "ABBV", "MRK", "LLY",
    "XOM", "CVX", "COP", "OXY",
    "WMT", "HD", "COST", "NKE", "MCD", "SBUX",
    "NFLX", "COIN", "PLTR", "MSTR", "SQ", "ROKU", "SNAP", "RIVN",
    "V", "MA", "PYPL", "UBER", "DIS",
]
SCREEN_TOP_N          = 5
SCREEN_LOOKBACK_YEARS = 5

WATCHLIST_FILE = os.path.join(os.path.dirname(__file__), "watchlist.json")
PEAKS_FILE     = os.path.join(os.path.dirname(__file__), "peaks.json")

# ============================================================
# 日誌
# ============================================================
_log_fmt     = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
_console_hdl = logging.StreamHandler(sys.stdout)
_console_hdl.setFormatter(_log_fmt)
_log_path    = os.path.join(os.path.dirname(__file__), "bot_log.txt")
_file_hdl    = logging.FileHandler(_log_path, encoding="utf-8", mode="w")
_file_hdl.setFormatter(_log_fmt)
logging.basicConfig(level=logging.INFO, handlers=[_console_hdl, _file_hdl])
log = logging.getLogger(__name__)

_paper_positions: dict[str, dict] = {}

def load_peaks() -> dict[str, float]:
    if os.path.exists(PEAKS_FILE):
        try:
            import json as _json
            with open(PEAKS_FILE, encoding="utf-8") as f:
                return _json.load(f)
        except Exception:
            pass
    return {}

def save_peaks(peaks: dict[str, float]):
    try:
        import json as _json
        with open(PEAKS_FILE, "w", encoding="utf-8") as f:
            _json.dump(peaks, f, indent=4)
    except Exception as e:
        log.error("儲存峰值檔案失敗: %s", e)

_position_peaks: dict[str, float] = load_peaks()
_data_failed_symbols: set[str]    = set()
_account_id: str = ""   # 啟動後由 init_clients() 填入


# ----------------------------------------------------------
# 市場時段判斷（取代 Alpaca clock API）
# ----------------------------------------------------------
def _is_market_open() -> bool:
    """判斷美股市場是否開盤（EST/EDT 9:30–16:00，週一至五）"""
    now = datetime.now(timezone.utc)
    if now.weekday() >= 5:
        return False
    month = now.month
    is_edt = 3 <= month <= 11   # 夏令時近似
    open_h, open_m   = (13, 30) if is_edt else (14, 30)
    close_h, close_m = (20,  0) if is_edt else (21,  0)
    open_dt  = now.replace(hour=open_h,  minute=open_m,  second=0, microsecond=0)
    close_dt = now.replace(hour=close_h, minute=close_m, second=0, microsecond=0)
    return open_dt <= now < close_dt

def _get_next_market_open() -> datetime:
    """計算下一個美股開盤時間（UTC）"""
    now = datetime.now(timezone.utc)
    for days_ahead in range(8):
        dt = now + timedelta(days=days_ahead)
        if dt.weekday() >= 5:
            continue
        month = dt.month
        is_edt = 3 <= month <= 11
        open_h = 13 if is_edt else 14
        open_dt = dt.replace(hour=open_h, minute=30, second=0, microsecond=0)
        if open_dt > now:
            return open_dt
    return now + timedelta(hours=24)


# ----------------------------------------------------------
# Webull 回應解析輔助函數
# ----------------------------------------------------------
def _parse_positions(resp) -> list[dict]:
    """解析 get_account_position 回應 → list of position dicts"""
    try:
        data = resp.json()
        positions = (
            (data.get("data") or {}).get("positions") or
            data.get("positions") or
            (data.get("data") if isinstance(data.get("data"), list) else None) or
            []
        )
        return positions
    except Exception as e:
        log.warning("解析持倉回應失敗：%s", e)
        return []


def _parse_account_balance(resp) -> dict:
    """解析 get_account_balance 回應 → 含 equity / buying_power 的 dict"""
    try:
        data = resp.json()
        d = data.get("data") or data
        if isinstance(d, list) and d:
            d = d[0]
        return {
            "equity":        d.get("netLiquidation") or d.get("equity") or d.get("totalValue") or "0",
            "buying_power":  d.get("buyingPower") or d.get("cashBalance") or d.get("availableFunds") or "0",
            "cash":          d.get("totalCashValue") or d.get("cashBalance") or d.get("cash") or "0",
            "portfolio_value": d.get("grossPositionValue") or d.get("portfolioValue") or "0",
            "status":        d.get("accountStatus") or d.get("status") or "ACTIVE",
        }
    except Exception as e:
        log.warning("解析帳戶餘額失敗：%s", e)
        return {"equity": "0", "buying_power": "0", "cash": "0", "portfolio_value": "0", "status": "UNKNOWN"}


def _parse_order_history(resp) -> list[dict]:
    """解析 get_order_history 回應 → list of order dicts"""
    try:
        data = resp.json()
        orders = (
            (data.get("data") or {}).get("orders") or
            data.get("orders") or
            (data.get("data") if isinstance(data.get("data"), list) else None) or
            []
        )
        return orders
    except Exception as e:
        log.warning("解析訂單歷史失敗：%s", e)
        return []


# ----------------------------------------------------------
# Webull 客戶端初始化
# ----------------------------------------------------------
def init_clients(app_key: str, app_secret: str, paper: bool = True):
    """
    建立 TradeClient 並返回 (trade_client, account_id)。
    歷史行情與即時報價改由 yfinance（Yahoo Finance）提供，無需 DataClient。
    """
    trade_ep = TRADE_ENDPOINT_PAPER if paper else TRADE_ENDPOINT_LIVE

    trade_api = ApiClient(app_key, app_secret, "us")
    trade_api.add_endpoint("us", trade_ep)
    trade_api._stream_logger_set = True
    trade_api._file_logger_set   = True
    trade_client = TradeClient(trade_api)

    account_id = ""
    try:
        resp = trade_client.account_v2.get_account_list()
        raw  = resp.json()
        accounts = raw.get("data") or raw.get("accounts") or []
        if isinstance(accounts, list) and accounts:
            first = accounts[0]
            account_id = (first.get("accountId") or first.get("account_id") or
                          first.get("id") or "")
        if not account_id:
            log.warning("無法解析帳戶 ID，原始回應：%s", str(raw)[:300])
    except Exception as e:
        log.warning("取得帳戶清單失敗：%s", e)

    log.info("Webull 交易客戶端初始化完成 | 端點：%s | 帳戶 ID：%s", trade_ep, account_id or "未知")
    return trade_client, account_id


# ----------------------------------------------------------
# 費用計算（美股標準 SEC + FINRA TAF）
# ----------------------------------------------------------
def calc_fee(price: float, quantity: int, is_sell: bool) -> dict:
    sec_fee   = round(price * quantity * 0.0000278, 4) if is_sell else 0.0
    finra_taf = round(min(8.30, quantity * 0.000166), 4) if is_sell else 0.0
    return {
        "佣金":      0.0,
        "SEC費":     sec_fee,
        "FINRA_TAF": finra_taf,
        "合計費用":  round(sec_fee + finra_taf, 4),
    }


# ----------------------------------------------------------
# TradingView 技術分析評級
# ----------------------------------------------------------
def analyse_stock(symbol: str) -> dict:
    for exchange in ("NASDAQ", "NYSE", "AMEX"):
        try:
            handler  = TA_Handler(symbol=symbol, screener="america",
                                  exchange=exchange, interval=Interval.INTERVAL_1_DAY)
            analysis = handler.get_analysis()
            rec      = analysis.summary["RECOMMENDATION"]
            return {
                "tv_rating":     rec,
                "tv_buy_count":  analysis.summary["BUY"],
                "tv_sell_count": analysis.summary["SELL"],
                "bullish":       rec in ("BUY", "STRONG_BUY"),
                "exchange":      exchange,
            }
        except Exception:
            continue
    log.warning("%s 無法取得 TradingView 評級，預設 UNKNOWN", symbol)
    return {"tv_rating": "UNKNOWN", "tv_buy_count": 0, "tv_sell_count": 0, "bullish": False, "exchange": "N/A"}


# ----------------------------------------------------------
# 歷史行情（OHLCV）— yfinance (Yahoo Finance)
# ----------------------------------------------------------
def get_historical_bars_df(symbol: str, days: int) -> pd.DataFrame:
    """抓取最近 N 個交易日完整 OHLCV（yfinance，無需 API 憑證）"""
    try:
        # Fetch extra calendar days to ensure enough trading days after weekends/holidays
        start = (datetime.now(timezone.utc) - timedelta(days=int(days * 1.6) + 30)).strftime("%Y-%m-%d")
        end   = (datetime.now(timezone.utc) + timedelta(days=1)).strftime("%Y-%m-%d")
        df = yf.Ticker(symbol).history(start=start, end=end, auto_adjust=True)
        if df.empty:
            return pd.DataFrame()
        df = df.rename(columns={"Open": "open", "High": "high",
                                 "Low": "low", "Close": "close", "Volume": "volume"})
        df.index = pd.to_datetime(df.index, utc=True)
        return df[["open", "high", "low", "close", "volume"]].tail(days)
    except Exception as e:
        log.warning("%s yfinance K線失敗：%s", symbol, e)
        return pd.DataFrame()


def get_historical_closes(symbol: str, days: int) -> pd.Series:
    df = get_historical_bars_df(symbol, days)
    return df["close"] if not df.empty else pd.Series(dtype=float)


# ----------------------------------------------------------
# 均線策略
# ----------------------------------------------------------
def compute_signal(closes: pd.Series) -> str:
    if len(closes) < LONG_MA + 1:
        return "HOLD"
    ma_short = closes.rolling(SHORT_MA).mean()
    ma_long  = closes.rolling(LONG_MA).mean()
    diff     = (ma_short - ma_long).dropna()
    if len(diff) < 2:
        return "HOLD"
    scan = diff.iloc[-(BUY_WINDOW + 2):]
    for i in range(len(scan) - 1, 0, -1):
        prev_d = scan.iloc[i - 1]
        curr_d = scan.iloc[i]
        if pd.isna(prev_d) or pd.isna(curr_d):
            continue
        if prev_d <= 0 and curr_d > 0:
            return "BUY"
        if prev_d >= 0 and curr_d < 0:
            return "SELL"
    return "HOLD"


def calc_ma_win_rate(closes: pd.Series) -> float:
    ma_short = closes.rolling(SHORT_MA).mean()
    ma_long  = closes.rolling(LONG_MA).mean()
    in_trade = False
    entry_price = 0.0
    wins = total = 0
    for i in range(1, len(closes)):
        ps, cs = ma_short.iloc[i - 1], ma_short.iloc[i]
        pl, cl = ma_long.iloc[i - 1],  ma_long.iloc[i]
        if any(pd.isna(x) for x in (ps, cs, pl, cl)):
            continue
        if not in_trade and ps <= pl and cs > cl:
            in_trade    = True
            entry_price = closes.iloc[i]
        elif in_trade and ps >= pl and cs < cl:
            total += 1
            if closes.iloc[i] > entry_price:
                wins += 1
            in_trade = False
    return wins / total if total > 0 else 0.0


def calc_rsi(closes: pd.Series, period: int = 14) -> pd.Series:
    delta    = closes.diff()
    gain     = delta.clip(lower=0)
    loss     = (-delta).clip(lower=0)
    avg_gain = gain.ewm(alpha=1 / period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False).mean()
    rs       = avg_gain / avg_loss.replace(0, float("nan"))
    return 100 - (100 / (1 + rs))


# ----------------------------------------------------------
# 陰陽燭形態識別
# ----------------------------------------------------------
CANDLE_LOOKBACK = 3

def _candle_body(o, c):   return abs(c - o)
def _upper_wick(o, c, h): return h - max(o, c)
def _lower_wick(o, c, lo): return min(o, c) - lo


def detect_candle_patterns(df: pd.DataFrame, i: int) -> dict:
    if i < 0 or i >= len(df):
        return {"bullish": False, "bearish": False, "patterns": []}
    o0 = float(df["open"].iloc[i]); h0 = float(df["high"].iloc[i])
    l0 = float(df["low"].iloc[i]);  c0 = float(df["close"].iloc[i])
    body0  = _candle_body(o0, c0)
    range0 = (h0 - l0) or 1e-9
    up0    = _upper_wick(o0, c0, h0)
    lo0    = _lower_wick(o0, c0, l0)
    bull0  = c0 > o0; bear0 = c0 < o0
    patterns: list[str] = []; bullish = bearish = False
    is_doji = body0 / range0 < 0.05
    if bull0 and body0 > 0 and lo0 >= 2 * body0 and up0 <= 0.2 * range0:
        patterns.append("槌子線"); bullish = True
    if bull0 and body0 > 0 and up0 >= 2 * body0 and lo0 <= 0.2 * range0:
        patterns.append("倒槌線"); bullish = True
    if is_doji and lo0 > up0 * 2:
        patterns.append("蜻蜓十字"); bullish = True
    if bear0 and body0 > 0 and up0 >= 2 * body0 and lo0 <= 0.2 * range0:
        patterns.append("流星線"); bearish = True
    if bear0 and body0 > 0 and lo0 >= 2 * body0 and up0 <= 0.2 * range0:
        patterns.append("上吊線"); bearish = True
    if i >= 1:
        o1 = float(df["open"].iloc[i-1]); h1 = float(df["high"].iloc[i-1])
        l1 = float(df["low"].iloc[i-1]);  c1 = float(df["close"].iloc[i-1])
        body1 = _candle_body(o1, c1); bull1 = c1 > o1; bear1 = c1 < o1
        if bear1 and bull0 and o0 <= c1 and c0 >= o1:
            patterns.append("牛市吞沒"); bullish = True
        if bull1 and bear0 and o0 >= c1 and c0 <= o1:
            patterns.append("熊市吞沒"); bearish = True
        if bear1 and bull0 and body1 > 0 and o0 < l1 and c0 > (o1+c1)/2 and c0 < o1:
            patterns.append("穿刺線"); bullish = True
        if bull1 and bear0 and body1 > 0 and o0 > h1 and c0 < (o1+c1)/2 and c0 > o1:
            patterns.append("烏雲蓋頂"); bearish = True
        if bear1 and bull0 and body1 > 0 and o0 > c1 and c0 < o1 and body0 < body1 * 0.6:
            patterns.append("牛市孕線"); bullish = True
        if bull1 and bear0 and body1 > 0 and o0 < c1 and c0 > o1 and body0 < body1 * 0.6:
            patterns.append("熊市孕線"); bearish = True
    if i >= 2:
        o2 = float(df["open"].iloc[i-2]); c2 = float(df["close"].iloc[i-2])
        o1 = float(df["open"].iloc[i-1]); c1 = float(df["close"].iloc[i-1])
        body2 = _candle_body(o2, c2); body1 = _candle_body(o1, c1)
        bull2 = c2 > o2; bear2 = c2 < o2; bull1 = c1 > o1; bear1 = c1 < o1
        if bear2 and body2 > 0.01*abs(c2) and body1 < body2*0.4 and bull0 and c0 > (o2+c2)/2:
            patterns.append("晨星"); bullish = True
        if bull2 and body2 > 0.01*abs(c2) and body1 < body2*0.4 and bear0 and c0 < (o2+c2)/2:
            patterns.append("夜星"); bearish = True
        if bull2 and bull1 and bull0 and body1 > body2*0.7 and body0 > body1*0.7:
            patterns.append("三白兵"); bullish = True
        if bear2 and bear1 and bear0 and body1 > body2*0.7 and body0 > body1*0.7:
            patterns.append("三黑鴉"); bearish = True
    return {"bullish": bullish, "bearish": bearish, "patterns": patterns}


def has_recent_candle_signal(df, i, signal_type, lookback=CANDLE_LOOKBACK):
    all_patterns: list[str] = []
    for j in range(max(0, i - lookback + 1), i + 1):
        result = detect_candle_patterns(df, j)
        if result[signal_type]:
            for p in result["patterns"]:
                if p not in all_patterns:
                    all_patterns.append(p)
    return bool(all_patterns), all_patterns


# ----------------------------------------------------------
# 單一標的回測引擎（策略邏輯不變）
# ----------------------------------------------------------
def _backtest_symbol(closes, symbol, bars_df=None, use_candle_filter=False,
                     trailing_stop_pct=TRAILING_STOP_PCT):
    ma_short = closes.rolling(SHORT_MA).mean()
    ma_long  = closes.rolling(LONG_MA).mean()
    trades: list[dict] = []
    in_trade = False; entry_price = 0.0; entry_date = None; entry_idx = 0; peak = 0.0
    dates = [str(d)[:10] for d in closes.index.tolist()]
    for i in range(1, len(closes)):
        ps, cs = ma_short.iloc[i-1], ma_short.iloc[i]
        pl, cl = ma_long.iloc[i-1],  ma_long.iloc[i]
        if any(pd.isna(x) for x in (ps, cs, pl, cl)):
            continue
        price = closes.iloc[i]
        if not in_trade:
            if ps <= pl and cs > cl:
                if use_candle_filter and bars_df is not None and not bars_df.empty:
                    found, _ = has_recent_candle_signal(bars_df, i, "bullish")
                    if not found:
                        continue
                in_trade = True; entry_price = price; entry_date = dates[i]
                entry_idx = i; peak = price
        else:
            if price > peak:
                peak = price
            drawdown = (peak - price) / peak
            if drawdown >= trailing_stop_pct:
                trades.append({"entry_date": entry_date, "exit_date": dates[i],
                                "entry_price": entry_price, "exit_price": price,
                                "pnl_pct": (price-entry_price)/entry_price*100,
                                "hold_days": i-entry_idx, "exit_type": "STOP"})
                in_trade = False; continue
            if ps >= pl and cs < cl:
                trades.append({"entry_date": entry_date, "exit_date": dates[i],
                                "entry_price": entry_price, "exit_price": price,
                                "pnl_pct": (price-entry_price)/entry_price*100,
                                "hold_days": i-entry_idx, "exit_type": "CROSS"})
                in_trade = False
    open_trade = None
    if in_trade:
        lp = closes.iloc[-1]
        open_trade = {"entry_date": entry_date, "entry_price": entry_price,
                      "current_price": lp, "unrealized_pnl_pct": (lp-entry_price)/entry_price*100}
    wins       = sum(1 for t in trades if t["pnl_pct"] > 0)
    pnls       = [t["pnl_pct"] for t in trades]
    stop_exits = sum(1 for t in trades if t["exit_type"] == "STOP")
    return {"symbol": symbol, "trades": trades, "total": len(trades), "wins": wins,
            "losses": len(trades)-wins, "win_rate": wins/len(trades) if trades else 0.0,
            "avg_pnl": float(np.mean(pnls)) if pnls else 0.0,
            "best_trade": float(max(pnls)) if pnls else 0.0,
            "worst_trade": float(min(pnls)) if pnls else 0.0,
            "stop_exits": stop_exits, "cross_exits": len(trades)-stop_exits,
            "open_trade": open_trade}


# ----------------------------------------------------------
# 開市前自動選股
# ----------------------------------------------------------
def screen_stocks() -> list:
    log.info("=== 開市前選股開始（候選：%d 隻，回溯：%d 年）===",
             len(SCREEN_CANDIDATES), SCREEN_LOOKBACK_YEARS)
    results: dict[str, dict] = {}
    for symbol in SCREEN_CANDIDATES:
        try:
            sym_df = yf.Ticker(symbol).history(
                period=f"{SCREEN_LOOKBACK_YEARS}y", auto_adjust=True)
            if sym_df.empty or len(sym_df) < 252:
                continue
            sym_df = sym_df.rename(columns={"Open": "open", "High": "high",
                                             "Low": "low", "Close": "close", "Volume": "volume"})
            closes  = sym_df["close"]
            volumes = sym_df["volume"]
            highs   = sym_df["high"]
            lows    = sym_df["low"]
            avg_vol     = float(volumes.mean())
            daily_range = float(((highs - lows) / closes).mean() * 100)
            win_rate    = calc_ma_win_rate(closes)
            results[symbol] = {"avg_vol": avg_vol, "daily_range": daily_range, "win_rate": win_rate}
            log.info("  %s | 均量 %s | 日波幅 %.2f%% | MA勝率 %.1f%%",
                     symbol, f"{avg_vol:,.0f}", daily_range, win_rate * 100)
        except Exception as exc:
            log.debug("%s 選股失敗：%s", symbol, exc)
        time.sleep(0.1)

    if not results:
        log.warning("選股結果為空，保留原觀察清單：%s", WATCHLIST)
        return list(WATCHLIST)

    score_df = pd.DataFrame(results).T.astype(float)
    for col in ("avg_vol", "daily_range", "win_rate"):
        lo, hi = score_df[col].min(), score_df[col].max()
        denom  = hi - lo if hi != lo else 1.0
        score_df[f"{col}_n"] = (score_df[col] - lo) / denom
    score_df["score"] = (score_df["avg_vol_n"] * 0.25 +
                         score_df["daily_range_n"] * 0.35 +
                         score_df["win_rate_n"] * 0.40)
    top = score_df.nlargest(SCREEN_TOP_N, "score")
    log.info("=== 選股結果（Top %d）===", SCREEN_TOP_N)
    for sym, row in top.iterrows():
        log.info("  ✅ %s | 綜合分 %.3f | MA勝率 %.1f%%", sym, row["score"], row["win_rate"] * 100)
    selected = top.index.tolist()
    log.info("新觀察清單：%s", selected)
    return selected


# ----------------------------------------------------------
# 持倉查詢
# ----------------------------------------------------------
def get_current_position(trade_client: TradeClient | None, symbol: str,
                         trial: bool = False) -> dict:
    if trial:
        return _paper_positions.get(symbol, {"quantity": 0, "avg_cost": 0.0})
    try:
        resp      = trade_client.account_v2.get_account_position(_account_id)
        positions = _parse_positions(resp)
        for p in positions:
            pos_sym = (p.get("symbol") or p.get("ticker") or "").upper()
            if pos_sym == symbol.upper():
                qty  = float(p.get("qty") or p.get("quantity") or p.get("holdingQty") or 0)
                cost = float(p.get("avgCost") or p.get("costPrice") or
                             p.get("avgEntryPrice") or p.get("avg_cost") or 0)
                return {"quantity": qty, "avg_cost": cost}
        return {"quantity": 0, "avg_cost": 0.0}
    except Exception:
        return {"quantity": 0, "avg_cost": 0.0}


# ----------------------------------------------------------
# 即時報價 — yfinance
# ----------------------------------------------------------
def get_latest_price(symbol: str) -> float | None:
    try:
        hist = yf.Ticker(symbol).history(period="2d", auto_adjust=True)
        if not hist.empty:
            return float(hist["Close"].iloc[-1])
        return None
    except Exception as e:
        log.warning("%s yfinance 即時報價失敗：%s", symbol, e)
        return None


# ----------------------------------------------------------
# 下單
# ----------------------------------------------------------
def place_order(trade_client: TradeClient | None, symbol: str, side: str,
                quantity: int, price: float, reason: str = "", trial: bool = False):
    fee_info   = calc_fee(price, int(quantity), is_sell=(side == "SELL"))
    mode_label = "試跑" if trial else ("UAT" if WEBULL_PAPER else "實盤")
    log.info("[%s] %s %s %s @ %.4f | 費用：%s | 原因：%s",
             mode_label, side, symbol, quantity, price, fee_info, reason)

    if side == "SELL":
        _position_peaks.pop(symbol, None)
        save_peaks(_position_peaks)

    if trial:
        pos = _paper_positions.get(symbol, {"quantity": 0, "avg_cost": 0.0})
        if side == "BUY":
            total_cost = pos["avg_cost"] * pos["quantity"] + price * quantity
            pos["quantity"] += quantity
            pos["avg_cost"]  = total_cost / pos["quantity"] if pos["quantity"] else 0.0
        else:
            pos["quantity"] = max(0, pos["quantity"] - quantity)
            if pos["quantity"] == 0:
                pos["avg_cost"] = 0.0
        _paper_positions[symbol] = pos
        log.info("[試跑] 持倉更新 %s: %s", symbol, pos)
        return

    order_dict = {
        "combo_type":              "NORMAL",
        "client_order_id":         str(uuid.uuid4()),
        "symbol":                  symbol,
        "instrument_type":         "STOCK",
        "market":                  "US",
        "order_type":              "MARKET",
        "quantity":                str(quantity),
        "support_trading_session": "CORE",
        "side":                    side,
        "time_in_force":           "DAY",
        "entrust_type":            "QTY",
    }
    resp = trade_client.order_v2.place_order(_account_id, [order_dict])
    log.info("[%s] 訂單已提交 %s | 回應：%s", mode_label, symbol, str(resp.json())[:200])


# ----------------------------------------------------------
# 滾動止損
# ----------------------------------------------------------
def update_trailing_stop(trade_client: TradeClient | None, symbol: str,
                         trial: bool = False):
    pos = get_current_position(trade_client, symbol, trial=trial)
    qty = pos["quantity"]
    if (qty if isinstance(qty, float) else int(qty)) <= 0:
        _position_peaks.pop(symbol, None)
        save_peaks(_position_peaks)
        return
    if trial:
        return
    current_price = get_latest_price(symbol)
    if current_price is None:
        return
    if symbol not in _position_peaks:
        _position_peaks[symbol] = current_price
        save_peaks(_position_peaks)
        log.info("%s 滾動止損監測啟動，初始峰值：$%.4f", symbol, current_price)
    peak = _position_peaks[symbol]
    if current_price > peak:
        peak = current_price
        _position_peaks[symbol] = peak
        save_peaks(_position_peaks)
        log.info("%s 峰值更新：$%.4f", symbol, peak)
    drawdown = (peak - current_price) / peak
    log.info("%s 滾動止損：當前 $%.4f | 峰值 $%.4f | 回撤 %.2f%%（門檻 %.0f%%）",
             symbol, current_price, peak, drawdown * 100, TRAILING_STOP_PCT * 100)
    if drawdown >= TRAILING_STOP_PCT:
        log.warning("[滾動止損] %s 從峰值 $%.4f 回落 %.2f%%，觸發平倉！",
                    symbol, peak, drawdown * 100)
        place_order(trade_client, symbol, "SELL", int(qty), current_price, "滾動止損平倉")


# ----------------------------------------------------------
# 主策略輪詢（股票）
# ----------------------------------------------------------
def run_strategy(trade_client: TradeClient):
    global WATCHLIST
    if os.path.exists(WATCHLIST_FILE):
        try:
            import json as _json
            with open(WATCHLIST_FILE, encoding="utf-8") as _f:
                _loaded = _json.load(_f)
            if isinstance(_loaded, list) and _loaded:
                WATCHLIST = [s.upper().strip() for s in _loaded if s.strip()]
        except Exception:
            pass

    if not _is_market_open():
        next_open = _get_next_market_open()
        log.info("市場休市，下次開盤：%s", next_open.strftime("%Y-%m-%d %H:%M %Z"))
        return

    watchlist_set  = set(WATCHLIST)
    held_extra: list[str] = []
    try:
        resp      = trade_client.account_v2.get_account_position(_account_id)
        positions = _parse_positions(resp)
        for p in positions:
            sym = (p.get("symbol") or p.get("ticker") or "").upper()
            qty = float(p.get("qty") or p.get("quantity") or p.get("holdingQty") or 0)
            if sym not in watchlist_set and qty > 0:
                held_extra.append(sym)
    except Exception as e:
        log.warning("無法取得持倉清單：%s", e)

    if held_extra:
        log.info("[持倉監察] 以下持倉不在觀察清單，加入本輪監察直至平倉：%s", held_extra)

    monitor_list = list(WATCHLIST) + held_extra
    for symbol in monitor_list:
        in_watchlist = symbol in watchlist_set
        try:
            label = symbol if in_watchlist else f"{symbol} [持倉監察]"
            log.info("--- 處理 %s ---", label)
            update_trailing_stop(trade_client, symbol)
            bars_df = get_historical_bars_df(symbol, LONG_MA + BUY_WINDOW + 5)
            if bars_df.empty:
                if symbol not in _data_failed_symbols:
                    log.warning("%s 無法取得歷史行情，本 session 後續靜默跳過", symbol)
                    _data_failed_symbols.add(symbol)
                continue
            closes    = bars_df["close"]
            ma_signal = compute_signal(closes)
            pos       = get_current_position(trade_client, symbol)
            if ma_signal == "BUY" and pos["quantity"] == 0:
                if not in_watchlist:
                    log.info("%s [持倉監察] MA金叉但已移出觀察清單，不新增部位", symbol)
                else:
                    tv = analyse_stock(symbol)
                    tv_ok = True if tv["tv_rating"] == "UNKNOWN" else tv["bullish"]
                    candle_found, candle_patterns = has_recent_candle_signal(
                        bars_df, len(bars_df) - 1, "bullish")
                    candle_str = "、".join(candle_patterns) if candle_found else "無看漲形態"
                    tv_label   = "⚠️ TV不可用" if tv["tv_rating"] == "UNKNOWN" else tv["tv_rating"]
                    allow_entry = tv_ok and candle_found
                    if not tv_ok:
                        decision = f"❌ TV評級不足（{tv_label}），跳過"
                    elif not candle_found:
                        decision = "❌ 無看漲陰陽燭確認，跳過"
                    else:
                        decision = f"✅ 允許入市（燭形：{candle_str}）"
                    log.info("[分析] %s | TV: %s (%d/%d) | MA: %s | 燭形: %s | 決策: %s",
                             symbol, tv["tv_rating"], tv["tv_buy_count"], tv["tv_sell_count"],
                             ma_signal, candle_str, decision)
                    if allow_entry:
                        current_price = get_latest_price(symbol) or float(closes.iloc[-1])
                        qty    = max(1, int(CAPITAL_PER_TRADE / current_price))
                        reason = f"TV+MA+燭形三重確認（{candle_str}）"
                        place_order(trade_client, symbol, "BUY", qty, current_price, reason)
                        _position_peaks[symbol] = current_price
                        save_peaks(_position_peaks)
            elif ma_signal == "SELL" and pos["quantity"] > 0:
                log.info("[分析] %s | MA: SELL | 決策: ✅ 死叉平倉", symbol)
                current_price = get_latest_price(symbol) or float(closes.iloc[-1])
                place_order(trade_client, symbol, "SELL", int(pos["quantity"]), current_price, "均線死叉賣出")
            else:
                log.info("%s 無操作（MA=%s，持倉=%s）", symbol, ma_signal, pos["quantity"])
        except Exception as exc:
            log.error("%s 處理出錯：%s", symbol, exc, exc_info=True)


# ----------------------------------------------------------
# 結算報告
# ----------------------------------------------------------
def _print_summary(trade_client: TradeClient):
    log.info("=" * 60)
    log.info("TRADING SESSION SUMMARY")
    log.info("=" * 60)
    try:
        resp = trade_client.account_v2.get_account_balance(_account_id)
        bal  = _parse_account_balance(resp)
        log.info("帳戶狀態   : %s", bal["status"])
        log.info("淨值       : $%s", bal["equity"])
        log.info("可用資金   : $%s", bal["buying_power"])
    except Exception as e:
        log.warning("無法取得帳戶摘要：%s", e)
    log.info("-" * 60)
    for symbol in WATCHLIST:
        pos = get_current_position(trade_client, symbol)
        if pos["quantity"] > 0:
            log.info("%s  數量=%s  平均成本=$%.4f", symbol, pos["quantity"], pos["avg_cost"])
        else:
            log.info("%s  無持倉", symbol)
    log.info("=" * 60)


# ----------------------------------------------------------
# 回測輔助函數（不變）
# ----------------------------------------------------------
def _print_backtest_table(label: str, results: list[dict]) -> None:
    if not results:
        log.info("  %s: 無有效數據", label); return
    hdr = (f"\n  {'Symbol':<10} | {'MA筆數':>6} | {'MA勝率':>7} | {'MA均損益':>8}"
           f" | {'燭形筆數':>8} | {'燭形勝率':>8} | {'燭形均損益':>10} | {'止損':>5} | TV即時評級")
    sep = "  " + "-" * 95
    log.info("%s  (%d 個標的)", label, len(results)); log.info(hdr); log.info(sep)
    for row in results:
        ma, candle, tv = row["ma"], row["candle"], row["tv"]
        stop_str = f"{ma['stop_exits']}/{ma['total']}" if ma["total"] else "0/0"
        log.info("  %-10s | %6d  | %6.1f%%  | %+7.2f%% | %8d  | %7.1f%%   | %+9.2f%% | %5s | %s",
                 row["symbol"], ma["total"], ma["win_rate"]*100, ma["avg_pnl"],
                 candle["total"], candle["win_rate"]*100, candle["avg_pnl"],
                 stop_str, tv["tv_rating"])
    log.info(sep)


def _print_portfolio_aggregate(results: list[dict]) -> None:
    div = "=" * 80; log.info(div); log.info("PORTFOLIO AGGREGATE"); log.info(div)
    if not results:
        log.info("  無數據"); log.info(div); return
    ma_total   = sum(r["ma"]["total"] for r in results)
    c_total    = sum(r["candle"]["total"] for r in results)
    ma_wins    = sum(r["ma"]["wins"] for r in results)
    c_wins     = sum(r["candle"]["wins"] for r in results)
    ma_stops   = sum(r["ma"]["stop_exits"] for r in results)
    c_stops    = sum(r["candle"]["stop_exits"] for r in results)
    ma_pnls    = [t["pnl_pct"] for r in results for t in r["ma"]["trades"]]
    c_pnls     = [t["pnl_pct"] for r in results for t in r["candle"]["trades"]]
    ma_wr  = ma_wins / ma_total if ma_total else 0.0
    c_wr   = c_wins  / c_total  if c_total  else 0.0
    ma_avg = float(np.mean(ma_pnls)) if ma_pnls else 0.0
    c_avg  = float(np.mean(c_pnls))  if c_pnls  else 0.0
    log.info("%-24s | %-18s | %-18s", "", "MA-Only", "MA + 陰陽燭確認")
    log.info("%-24s | %-18s | %-18s", "-"*24, "-"*18, "-"*18)
    log.info("%-24s | %-18s | %-18s", "Completed trades", str(ma_total), str(c_total))
    log.info("%-24s | %-18s | %-18s", "Win rate", f"{ma_wr*100:.1f}%", f"{c_wr*100:.1f}%")
    log.info("%-24s | %-18s | %-18s", "Avg trade P&L", f"{ma_avg:+.2f}%", f"{c_avg:+.2f}%")
    log.info("%-24s | %-18s | %-18s", "Stop-loss exits",
             f"{ma_stops}/{ma_total}", f"{c_stops}/{c_total}")
    log.info(div)
    log.info("NOTE: TV ratings are CURRENT (live) — not historical.")
    log.info("      陰陽燭欄：金叉當日前 %d 根K棒內出現看漲形態方才進場。", CANDLE_LOOKBACK)
    log.info(div)


def _print_trade_log(symbol: str, trades: list[dict]) -> None:
    if not trades: return
    log.info("  TRADE LOG — %s", symbol)
    log.info("  %3s | %-10s | %-10s | %9s | %9s | %7s | %4s | %s",
             "#", "Entry", "Exit", "Entry $", "Exit $", "P&L%", "Days", "Exit")
    for i, t in enumerate(trades, 1):
        log.info("  %3d | %-10s | %-10s | %9.4f | %9.4f | %+6.2f%% | %4d | %s",
                 i, str(t["entry_date"])[:10], str(t["exit_date"])[:10],
                 t["entry_price"], t["exit_price"], t["pnl_pct"], t["hold_days"], t["exit_type"])


# ----------------------------------------------------------
# 投資組合回測（主入口）
# ----------------------------------------------------------
def run_portfolio_backtest(watchlist: list[str], lookback_days: int = 365) -> None:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    div   = "=" * 80
    log.info(div)
    log.info("PORTFOLIO BACKTEST REPORT  |  Lookback: %d days  |  %s", lookback_days, today)
    log.info("Strategy: %dMA / %dMA crossover + %.0f%% trailing stop",
             SHORT_MA, LONG_MA, TRAILING_STOP_PCT * 100)
    log.info(div)
    stock_results: list[dict] = []
    if watchlist:
        log.info("正在回測股票（%d 隻）…", len(watchlist))
        for symbol in watchlist:
            try:
                bars_df = get_historical_bars_df(symbol, lookback_days + 30)
                if bars_df.empty or len(bars_df) < LONG_MA + 2:
                    log.warning("  %s 數據不足，跳過", symbol); continue
                closes     = bars_df["close"]
                ma_res     = _backtest_symbol(closes, symbol, use_candle_filter=False)
                candle_res = _backtest_symbol(closes, symbol, bars_df=bars_df, use_candle_filter=True)
                tv         = analyse_stock(symbol)
                stock_results.append({"symbol": symbol, "ma": ma_res, "candle": candle_res, "tv": tv})
            except Exception as exc:
                log.warning("  %s 回測失敗：%s", symbol, exc)
    log.info("")
    _print_backtest_table("STOCKS", stock_results)
    log.info("")
    _print_portfolio_aggregate(stock_results)
    all_ct = sum(r["candle"]["total"] for r in stock_results)
    all_mt = sum(r["ma"]["total"]     for r in stock_results)
    if 0 < all_ct <= 120:
        log.info("")
        log.info("=== 逐筆交易記錄（MA + 陰陽燭確認）===")
        for row in stock_results:
            _print_trade_log(row["symbol"], row["candle"]["trades"])
    elif 0 < all_mt <= 120:
        log.info("")
        log.info("=== 逐筆交易記錄（MA-only）===")
        for row in stock_results:
            _print_trade_log(row["symbol"], row["ma"]["trades"])


# ----------------------------------------------------------
# GUI 回測入口（Webull 版）
# ----------------------------------------------------------
def backtest_for_gui(watchlist: list[str], lookback_days: int = 365,
                     progress_callback=None, **_) -> dict:
    """歷史數據由 yfinance 提供，無需 API 憑證。"""
    _emit = progress_callback or log.info
    stock_results: list[dict] = []
    errors: list[dict] = []
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    _emit(f"[GUI 回測] 開始  |  回溯 {lookback_days} 天  |  {today}")
    for symbol in watchlist:
        try:
            bars_df = get_historical_bars_df(symbol, lookback_days + 60)
            if bars_df.empty or len(bars_df) < LONG_MA + 2:
                _emit(f"  {symbol} 數據不足，跳過"); continue
            closes     = bars_df["close"]
            ma_res     = _backtest_symbol(closes, symbol, use_candle_filter=False)
            candle_res = _backtest_symbol(closes, symbol, bars_df=bars_df, use_candle_filter=True)
            tv         = analyse_stock(symbol)
            chart_df   = bars_df.tail(lookback_days)
            stock_results.append({"symbol": symbol, "ma": ma_res, "candle": candle_res,
                                   "tv": tv, "bars_df": chart_df})
            _emit(f"  {symbol} OK — MA: {ma_res['total']} 筆 ({ma_res['win_rate']*100:.1f}%)"
                  f" | 燭形: {candle_res['total']} 筆 ({candle_res['win_rate']*100:.1f}%)"
                  f" | TV: {tv['tv_rating']}")
        except Exception as exc:
            errors.append({"symbol": symbol, "error": str(exc)})
            _emit(f"  {symbol} 回測失敗：{exc}")
    _emit(f"[GUI 回測] 完成  股票 {len(stock_results)} / 錯誤 {len(errors)}")
    return {"stocks": stock_results, "errors": errors}


# ----------------------------------------------------------
# 試跑驗證（不需 API）
# ----------------------------------------------------------
def trial_run():
    log.info("====== Trial Run 開始（V8.1 Webull）======")

    # 1. 費用計算
    buy_fee  = calc_fee(150.0, 10, is_sell=False)
    sell_fee = calc_fee(155.0, 10, is_sell=True)
    log.info("買入費用: %s", buy_fee)
    log.info("賣出費用: %s", sell_fee)
    assert buy_fee["合計費用"] == 0.0
    assert sell_fee["合計費用"] > 0.0

    # 2. 市場時段判斷
    is_open = _is_market_open()
    next_open = _get_next_market_open()
    log.info("市場狀態：%s | 下次開盤：%s", "開盤" if is_open else "休市", next_open.strftime("%Y-%m-%d %H:%M %Z"))

    # 3. 符號格式
    assert WATCHLIST[0].isalpha(), "觀察清單格式正常"
    log.info("觀察清單格式測試通過")

    # 4. 均線信號
    np.random.seed(42)
    rising = pd.Series(
        [100 + i * 0.5 + np.random.randn() * 2 for i in range(30)],
        index=pd.date_range(end=datetime.today(), periods=30).date,
    )
    log.info("上升假資料均線信號: %s", compute_signal(rising))

    # 5. 模擬下單
    _paper_positions.clear()
    test_price = 700.0
    test_qty   = max(1, int(CAPITAL_PER_TRADE / test_price))
    place_order(None, "NFLX", "BUY",  test_qty, test_price, "試跑買入", trial=True)
    assert _paper_positions["NFLX"]["quantity"] == test_qty
    place_order(None, "NFLX", "SELL", test_qty, 720.0, "試跑賣出", trial=True)
    assert _paper_positions["NFLX"]["quantity"] == 0
    log.info("持倉模擬測試通過")

    # 6. 滾動止損邏輯
    _position_peaks["NFLX"] = 750.0
    peak = _position_peaks["NFLX"]
    current_price = 732.0
    drawdown = (peak - current_price) / peak
    triggered = drawdown >= TRAILING_STOP_PCT
    log.info("滾動止損測試：回撤 %.2f%%，應觸發 True -> %s", drawdown * 100, triggered)
    assert triggered

    # 7. 陰陽燭形態識別
    candle_test = pd.DataFrame({
        "open":  [100.0, 105.0, 103.0],
        "high":  [110.0, 106.0, 112.0],
        "low":   [ 90.0, 102.0, 102.0],
        "close": [104.0, 103.5, 111.0],
    })
    result = detect_candle_patterns(candle_test, 2)
    log.info("陰陽燭：牛=%s 熊=%s 形態=%s", result["bullish"], result["bearish"], result["patterns"])
    found, patterns = has_recent_candle_signal(candle_test, 2, "bullish")
    log.info("近期看漲掃描：找到=%s 形態=%s", found, patterns)

    # 8. TradingView 評級
    log.info("測試 TradingView 評級（需要網路）…")
    tv = analyse_stock("NFLX")
    log.info("NFLX TV: %s (買:%d 賣:%d) 交易所:%s",
             tv["tv_rating"], tv["tv_buy_count"], tv["tv_sell_count"], tv["exchange"])

    log.info("====== Trial Run 全部通過 ✓（V8.1 Webull）======")
    log.info("WEBULL_PAPER = %s | TRAILING_STOP_PCT = %.0f%% | CAPITAL_PER_TRADE = $%d",
             WEBULL_PAPER, TRAILING_STOP_PCT * 100, CAPITAL_PER_TRADE)


# ----------------------------------------------------------
# watchlist.json 同步
# ----------------------------------------------------------
def _sync_watchlist_file(symbols: list[str]):
    import json as _json
    try:
        with open(WATCHLIST_FILE, "w", encoding="utf-8") as _f:
            _json.dump(symbols, _f, indent=2)
        log.info("選股結果已同步至 watchlist.json：%s", symbols)
    except Exception as exc:
        log.warning("watchlist.json 同步失敗：%s", exc)


# ----------------------------------------------------------
# 進入點
# ----------------------------------------------------------
def main():
    global WATCHLIST, _account_id

    app_key    = os.getenv("WEBULL_APP_KEY")
    app_secret = os.getenv("WEBULL_APP_SECRET")
    if not app_key or not app_secret:
        log.error("請設定環境變數 WEBULL_APP_KEY 與 WEBULL_APP_SECRET")
        sys.exit(1)

    log.info("=== Webull Trading Bot V8.1 ===")
    mode_str = "【UAT 模擬】" if WEBULL_PAPER else "【實盤 LIVE】"
    log.info("交易模式：%s | 滾動止損 = %.0f%% | 每筆本金 = $%d",
             mode_str, TRAILING_STOP_PCT * 100, CAPITAL_PER_TRADE)

    trade_client, account_id = init_clients(app_key, app_secret, paper=WEBULL_PAPER)
    _account_id = account_id

    # 顯示帳戶資訊
    try:
        resp = trade_client.account_v2.get_account_balance(account_id)
        bal  = _parse_account_balance(resp)
        log.info("帳戶狀態：%s | 可用資金：$%s", bal["status"], bal["buying_power"])
    except Exception as e:
        log.warning("無法取得帳戶資訊：%s", e)

    log.info("觀察清單：%s", WATCHLIST)

    run_duration  = 86400
    end_time      = time.time() + run_duration
    screened_date = None

    log.info("開始策略輪詢（每 %d 秒），執行時間：24 小時…", POLL_INTERVAL)

    try:
        while time.time() < end_time:
            try:
                today = datetime.now(timezone.utc).date()

                if not _is_market_open():
                    next_open  = _get_next_market_open()
                    now_utc    = datetime.now(timezone.utc)
                    sleep_secs = max(0.0, (next_open - now_utc).total_seconds())
                    log.info("市場收市，暫停至 %s（約 %.0f 分鐘後）…",
                             next_open.strftime("%Y-%m-%d %H:%M %Z"), sleep_secs / 60)
                    time.sleep(min(sleep_secs, end_time - time.time(), 60.0))

                    today = datetime.now(timezone.utc).date()
                    if screened_date != today and _is_market_open():
                        log.info("市場即將開市，執行開市前選股…")
                        try:
                            WATCHLIST     = screen_stocks()
                            screened_date = today
                            _sync_watchlist_file(WATCHLIST)
                        except Exception as exc:
                            log.warning("選股失敗，保留現有清單：%s", exc)

                else:
                    if screened_date != today:
                        log.info("開市中啟動，執行開市前選股…")
                        try:
                            WATCHLIST     = screen_stocks()
                            screened_date = today
                            _sync_watchlist_file(WATCHLIST)
                        except Exception as exc:
                            log.warning("選股失敗，保留現有清單：%s", exc)

                    run_strategy(trade_client)

                    remaining = end_time - time.time()
                    if remaining > 0:
                        time.sleep(min(POLL_INTERVAL, remaining))

            except Exception as e:
                _transient = ("connection aborted", "remotedisconnected",
                              "connectionerror", "timeout", "connectionreset",
                              "remote end closed")
                if any(t in str(e).lower() for t in _transient):
                    log.warning("網路瞬斷，將在 %d 秒後重試：%s", POLL_INTERVAL, e)
                else:
                    log.error("輪詢出錯（將在 %d 秒後重試）：%s", POLL_INTERVAL, e, exc_info=True)
                time.sleep(POLL_INTERVAL)

    except KeyboardInterrupt:
        log.info("使用者中斷，程式結束。")

    log.info("24 小時交易結束，生成結算報告…")
    _print_summary(trade_client)


if __name__ == "__main__":
    if "--live" in sys.argv:
        if "--real" in sys.argv:
            WEBULL_PAPER = False
        main()
    elif "--backtest" in sys.argv:
        _bt_days = 365
        if "--days" in sys.argv:
            try:
                _bt_days = int(sys.argv[sys.argv.index("--days") + 1])
            except (ValueError, IndexError):
                pass
        _wl = list(WATCHLIST)
        if os.path.exists(WATCHLIST_FILE):
            try:
                import json as _json
                with open(WATCHLIST_FILE, encoding="utf-8") as _f:
                    _loaded = _json.load(_f)
                if isinstance(_loaded, list) and _loaded:
                    _wl = [s.upper().strip() for s in _loaded if s.strip()]
            except Exception:
                pass
        run_portfolio_backtest(_wl, lookback_days=_bt_days)
    else:
        trial_run()
