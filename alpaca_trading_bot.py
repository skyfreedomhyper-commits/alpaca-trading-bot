"""
Alpaca 自動交易機器人 — V7
雙均線策略（5日線 / 20日線）
+ TradingView 技術評級雙重確認
+ 陰陽燭形態三重確認（參考 CandleSticker.com）
+ 2% 滾動止損（Trailing Stop Loss，持久化 peaks.json）
+ 10 秒高頻監測（股票 & 加密貨幣同步）
+ 全自動加密監控：自動識別 SOLUSD/ETHUSD 格式並監管所有持倉
+ GUI 優化：二合一啟動開關、一鍵切換 Paper/Live 模式
+ 投資組合回測：按圖表陰陽燭形態計算勝率

環境變數設定（建議儲存於 .env 檔案）：
  ALPACA_API_KEY    = 您的 API Key ID
  ALPACA_API_SECRET = 您的 API Secret Key

安裝指令：
  pip install alpaca-py pandas numpy python-dotenv tradingview_ta

使用說明：
  python -X utf8 alpaca_trading_bot.py                    # 試跑驗證
  python -X utf8 alpaca_trading_bot.py --live             # 連線 Alpaca Paper (預設)
  python -X utf8 alpaca_trading_bot.py --live --real      # 連線 Alpaca 實盤 (⚠️ 真實資金)
  python -X utf8 alpaca_trading_bot.py --backtest         # 投資組合回測（預設 365 天）
  python -X utf8 alpaca_trading_bot.py --backtest --days 180  # 自訂回測天數
"""

import os
import sys
import time
import logging
from datetime import datetime, timedelta, timezone

import pandas as pd
import numpy as np

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce
from alpaca.data.historical import StockHistoricalDataClient, CryptoHistoricalDataClient
from alpaca.data.requests import (
    StockBarsRequest, StockLatestBarRequest,
    CryptoBarsRequest, CryptoLatestBarRequest,
)
from alpaca.data.timeframe import TimeFrame
from alpaca.data.enums import DataFeed

from tradingview_ta import TA_Handler, Interval

# ============================================================
# ⚠️  模式切換（可透過 CLI 參數 --real 切換）
#   True  = Alpaca Paper Trading（模擬，安全預設值）
#   False = Alpaca 真實帳戶（實盤，有真實資金風險！）
# ============================================================
PAPER_TRADING = True

# ============================================================
# 監控股票清單（美股代碼）
# ============================================================
WATCHLIST = [
    "NFLX",   # Netflix
    "TSLA",   # Tesla
    "NVDA",   # NVIDIA（原 X/U.S.Steel 不在 IEX 覆蓋範圍，已替換）
]

# 均線參數
SHORT_MA  = 5
LONG_MA   = 20
# 金叉/死叉有效視窗（天）：在此天數內發生的交叉均視為有效信號
BUY_WINDOW = 30

# 每筆交易本金上限（USD）— 買入股數 = int(CAPITAL_PER_TRADE / 當前股價)
CAPITAL_PER_TRADE = 10_000

# 滾動止損：從持倉峰值回落超過此比例即平倉
TRAILING_STOP_PCT = 0.02   # 2%

# 輪詢間隔（秒）
POLL_INTERVAL = 10

# ============================================================
# 開市前自動選股設定
# ============================================================
SCREEN_CANDIDATES = [
    # 科技
    "AAPL", "MSFT", "NVDA", "GOOGL", "META", "AMZN", "TSLA", "AMD", "INTC", "AVGO",
    # 金融
    "JPM", "BAC", "GS", "MS", "WFC", "C", "BLK", "AXP",
    # 醫療
    "UNH", "JNJ", "PFE", "ABBV", "MRK", "LLY",
    # 能源
    "XOM", "CVX", "COP", "OXY",
    # 消費 / 零售
    "WMT", "HD", "COST", "NKE", "MCD", "SBUX",
    # 高波動活躍股
    "NFLX", "COIN", "PLTR", "MSTR", "SQ", "ROKU", "SNAP", "RIVN",
    # 金融科技 / 其他
    "V", "MA", "PYPL", "UBER", "DIS",
]

SCREEN_TOP_N          = 5   # 最終選出股票數量
SCREEN_LOOKBACK_YEARS = 5   # 回溯年數

# GUI 手動觀察清單共享檔（bot_gui.py 寫入，bot 每次輪詢讀取）
WATCHLIST_FILE = os.path.join(os.path.dirname(__file__), "watchlist.json")
# 滾動止損峰值持久化檔案
PEAKS_FILE = os.path.join(os.path.dirname(__file__), "peaks.json")

# ============================================================
# 加密貨幣設定（24/7 運行，不受股票市場開收市影響）
# ============================================================
CRYPTO_WATCHLIST = [
    "BTC/USD",    # Bitcoin
    "ETH/USD",    # Ethereum
    "SOL/USD",    # Solana
    "AVAX/USD",   # Avalanche
    "LINK/USD",   # Chainlink
]
CAPITAL_PER_CRYPTO_TRADE = 1_000   # 每筆加密貨幣交易本金（USD）
CRYPTO_POLL_INTERVAL     = 10      # 加密策略輪詢間隔（秒）

# ============================================================
_log_fmt     = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
_console_hdl = logging.StreamHandler(sys.stdout)
_console_hdl.setFormatter(_log_fmt)

_log_path    = os.path.join(os.path.dirname(__file__), "bot_log.txt")
_file_hdl    = logging.FileHandler(_log_path, encoding="utf-8", mode="w")   # mode="w"：每次啟動覆寫，不累積
_file_hdl.setFormatter(_log_fmt)

logging.basicConfig(level=logging.INFO, handlers=[_console_hdl, _file_hdl])
log = logging.getLogger(__name__)

# 試跑模式用的記憶體持倉
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

# 各持倉的歷史最高價（用於滾動止損，從檔案載入）
_position_peaks: dict[str, float] = load_peaks()
# 本次 session 內已無法取得數據的符號（避免每輪詢重複警告）
_data_failed_symbols: set[str] = set()

def _normalize_crypto_symbol(s: str) -> str:
    """自動校正加密貨幣符號格式。例如 SOLUSD -> SOL/USD"""
    s = s.upper().strip()
    if "/" in s:
        return s
    # 如果是以 USD 結尾且長度大於 3，通常是加密貨幣（如 BTCUSD, ETHUSD, SOLUSD）
    if s.endswith("USD") and len(s) > 3:
        return f"{s[:-3]}/USD"
    return s


# ----------------------------------------------------------
# 費用計算
# ----------------------------------------------------------
def calc_fee(price: float, quantity: int, is_sell: bool) -> dict:
    """Alpaca 零佣金，賣出時有 SEC 費與 FINRA TAF"""
    sec_fee   = round(price * quantity * 0.0000278, 4) if is_sell else 0.0
    finra_taf = round(min(8.30, quantity * 0.000166), 4) if is_sell else 0.0
    return {
        "佣金":     0.0,
        "SEC費":    sec_fee,
        "FINRA_TAF": finra_taf,
        "合計費用": round(sec_fee + finra_taf, 4),
    }


# ----------------------------------------------------------
# TradingView 技術分析評級
# ----------------------------------------------------------
def analyse_stock(symbol: str) -> dict:
    """
    抓取 TradingView 日線技術分析評級。
    依序嘗試 NASDAQ → NYSE，兩者皆失敗則回傳 UNKNOWN。
    回傳：
      tv_rating    : 'STRONG_BUY' / 'BUY' / 'NEUTRAL' / 'SELL' / 'STRONG_SELL' / 'UNKNOWN'
      tv_buy_count : 買入指標數量
      tv_sell_count: 賣出指標數量
      bullish      : True = 通過評級篩選（BUY 或 STRONG_BUY）
    """
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
            buy_count = analysis.summary["BUY"]
            sell_count = analysis.summary["SELL"]
            return {
                "tv_rating":     rec,
                "tv_buy_count":  buy_count,
                "tv_sell_count": sell_count,
                "bullish":       rec in ("BUY", "STRONG_BUY"),
                "exchange":      exchange,
            }
        except Exception:
            continue

    log.warning("%s 無法取得 TradingView 評級，預設 UNKNOWN", symbol)
    return {"tv_rating": "UNKNOWN", "tv_buy_count": 0, "tv_sell_count": 0, "bullish": False, "exchange": "N/A"}


# ----------------------------------------------------------
# 歷史行情（OHLCV）
# ----------------------------------------------------------
def get_historical_bars_df(
    data_client: StockHistoricalDataClient,
    symbol: str,
    days: int,
) -> pd.DataFrame:
    """抓取最近 N 個交易日完整 OHLCV 資料（供均線計算及陰陽燭形態識別使用）"""
    end   = datetime.now(timezone.utc)
    start = end - timedelta(days=days + 30)
    req   = StockBarsRequest(
        symbol_or_symbols=symbol,
        timeframe=TimeFrame.Day,
        start=start,
        end=end,
        feed=DataFeed.IEX,
    )
    bars = data_client.get_stock_bars(req)
    df   = bars.df
    if df.empty:
        return pd.DataFrame()
    if isinstance(df.index, pd.MultiIndex):
        df = df.xs(symbol, level="symbol")
    return df[["open", "high", "low", "close", "volume"]].sort_index().tail(days)


def get_historical_closes(
    data_client: StockHistoricalDataClient,
    symbol: str,
    days: int,
) -> pd.Series:
    """抓取最近 N 個交易日的收盤價（向後相容保留）"""
    df = get_historical_bars_df(data_client, symbol, days)
    return df["close"] if not df.empty else pd.Series(dtype=float)


# ----------------------------------------------------------
# 均線策略
# ----------------------------------------------------------
def compute_signal(closes: pd.Series) -> str:
    """
    BUY  — 最近 BUY_WINDOW 天內出現金叉（5MA 上穿 20MA）
    SELL — 最近 BUY_WINDOW 天內出現死叉（5MA 下穿 20MA）
    HOLD — 視窗內無交叉事件

    原設計只檢查最後 1 根 K 棒，導致只有金叉當天才產生 BUY。
    改為向前掃描 BUY_WINDOW 天，以「最近一次交叉事件」決定信號。
    """
    if len(closes) < LONG_MA + 1:
        return "HOLD"

    ma_short = closes.rolling(SHORT_MA).mean()
    ma_long  = closes.rolling(LONG_MA).mean()
    diff     = (ma_short - ma_long).dropna()

    if len(diff) < 2:
        return "HOLD"

    # 從最新往回掃 BUY_WINDOW 天，找出最近一次交叉事件
    # +2：BUY_WINDOW 根 K 棒 + 1 根「交叉前一根」（兩者都必須在視窗內才能偵測交叉）
    scan = diff.iloc[-(BUY_WINDOW + 2):]
    for i in range(len(scan) - 1, 0, -1):
        prev_d = scan.iloc[i - 1]
        curr_d = scan.iloc[i]
        if pd.isna(prev_d) or pd.isna(curr_d):
            continue
        if prev_d <= 0 and curr_d > 0:
            return "BUY"   # 金叉：5MA 由下往上穿越 20MA
        if prev_d >= 0 and curr_d < 0:
            return "SELL"  # 死叉：5MA 由上往下穿越 20MA

    return "HOLD"


# ----------------------------------------------------------
# 歷史均線策略勝率回測
# ----------------------------------------------------------
def calc_ma_win_rate(closes: pd.Series) -> float:
    """模擬 5/20 均線金叉死叉，計算歷史勝率（完成交易中獲利比例）"""
    ma_short = closes.rolling(SHORT_MA).mean()
    ma_long  = closes.rolling(LONG_MA).mean()

    in_trade    = False
    entry_price = 0.0
    wins = total = 0

    for i in range(1, len(closes)):
        ps, cs = ma_short.iloc[i - 1], ma_short.iloc[i]
        pl, cl = ma_long.iloc[i - 1],  ma_long.iloc[i]
        if any(pd.isna(x) for x in (ps, cs, pl, cl)):
            continue
        if not in_trade and ps <= pl and cs > cl:   # 金叉進場
            in_trade    = True
            entry_price = closes.iloc[i]
        elif in_trade and ps >= pl and cs < cl:     # 死叉出場
            total += 1
            if closes.iloc[i] > entry_price:
                wins += 1
            in_trade = False

    return wins / total if total > 0 else 0.0


# ----------------------------------------------------------
# RSI 計算（供回測使用）
# ----------------------------------------------------------
def calc_rsi(closes: pd.Series, period: int = 14) -> pd.Series:
    """Wilder 平滑 RSI，純 pandas 實作（無需外部套件）"""
    delta    = closes.diff()
    gain     = delta.clip(lower=0)
    loss     = (-delta).clip(lower=0)
    avg_gain = gain.ewm(alpha=1 / period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False).mean()
    rs       = avg_gain / avg_loss.replace(0, float("nan"))
    return 100 - (100 / (1 + rs))


# ----------------------------------------------------------
# 陰陽燭形態識別（參考 CandleSticker.com 常見形態）
# 涵蓋：槌子線、倒槌線、蜻蜓十字、流星線、上吊線、
#       牛市吞沒、熊市吞沒、穿刺線、烏雲蓋頂、牛市孕線、熊市孕線、
#       晨星、夜星、三白兵、三黑鴉
# ----------------------------------------------------------

CANDLE_LOOKBACK = 3   # 入場前 N 根 K 棒內有看漲/看跌形態即視為確認


def _candle_body(o: float, c: float) -> float:
    return abs(c - o)


def _upper_wick(o: float, c: float, h: float) -> float:
    return h - max(o, c)


def _lower_wick(o: float, c: float, lo: float) -> float:
    return min(o, c) - lo


def detect_candle_patterns(df: pd.DataFrame, i: int) -> dict:
    """
    分析 df 第 i 根K棒（及前兩根）的陰陽燭形態。
    df 必須含 open / high / low / close 欄位。
    回傳 {"bullish": bool, "bearish": bool, "patterns": list[str]}
    """
    if i < 0 or i >= len(df):
        return {"bullish": False, "bearish": False, "patterns": []}

    o0 = float(df["open"].iloc[i])
    h0 = float(df["high"].iloc[i])
    l0 = float(df["low"].iloc[i])
    c0 = float(df["close"].iloc[i])

    body0  = _candle_body(o0, c0)
    range0 = (h0 - l0) or 1e-9
    up0    = _upper_wick(o0, c0, h0)
    lo0    = _lower_wick(o0, c0, l0)
    bull0  = c0 > o0
    bear0  = c0 < o0

    patterns: list[str] = []
    bullish = bearish = False

    # ── 單根形態 ────────────────────────────────────────────
    is_doji = body0 / range0 < 0.05

    # 槌子線 Hammer：牛，下影 ≥ 2×實體，上影 ≤ 20% 總幅
    if bull0 and body0 > 0 and lo0 >= 2 * body0 and up0 <= 0.2 * range0:
        patterns.append("槌子線"); bullish = True

    # 倒槌線 Inverted Hammer：上影 ≥ 2×實體，下影 ≤ 20%
    if bull0 and body0 > 0 and up0 >= 2 * body0 and lo0 <= 0.2 * range0:
        patterns.append("倒槌線"); bullish = True

    # 蜻蜓十字 Dragonfly Doji
    if is_doji and lo0 > up0 * 2:
        patterns.append("蜻蜓十字"); bullish = True

    # 流星線 Shooting Star：熊，上影 ≥ 2×實體，下影 ≤ 20%
    if bear0 and body0 > 0 and up0 >= 2 * body0 and lo0 <= 0.2 * range0:
        patterns.append("流星線"); bearish = True

    # 上吊線 Hanging Man：熊，下影 ≥ 2×實體，上影 ≤ 20%
    if bear0 and body0 > 0 and lo0 >= 2 * body0 and up0 <= 0.2 * range0:
        patterns.append("上吊線"); bearish = True

    # ── 兩根形態 ────────────────────────────────────────────
    if i >= 1:
        o1 = float(df["open"].iloc[i - 1])
        h1 = float(df["high"].iloc[i - 1])
        l1 = float(df["low"].iloc[i - 1])
        c1 = float(df["close"].iloc[i - 1])
        body1 = _candle_body(o1, c1)
        bull1 = c1 > o1
        bear1 = c1 < o1

        # 牛市吞沒 Bullish Engulfing
        if bear1 and bull0 and o0 <= c1 and c0 >= o1:
            patterns.append("牛市吞沒"); bullish = True

        # 熊市吞沒 Bearish Engulfing
        if bull1 and bear0 and o0 >= c1 and c0 <= o1:
            patterns.append("熊市吞沒"); bearish = True

        # 穿刺線 Piercing Line
        if (bear1 and bull0 and body1 > 0
                and o0 < l1 and c0 > (o1 + c1) / 2 and c0 < o1):
            patterns.append("穿刺線"); bullish = True

        # 烏雲蓋頂 Dark Cloud Cover
        if (bull1 and bear0 and body1 > 0
                and o0 > h1 and c0 < (o1 + c1) / 2 and c0 > o1):
            patterns.append("烏雲蓋頂"); bearish = True

        # 牛市孕線 Bullish Harami
        if (bear1 and bull0 and body1 > 0
                and o0 > c1 and c0 < o1 and body0 < body1 * 0.6):
            patterns.append("牛市孕線"); bullish = True

        # 熊市孕線 Bearish Harami
        if (bull1 and bear0 and body1 > 0
                and o0 < c1 and c0 > o1 and body0 < body1 * 0.6):
            patterns.append("熊市孕線"); bearish = True

    # ── 三根形態 ────────────────────────────────────────────
    if i >= 2:
        o2 = float(df["open"].iloc[i - 2])
        c2 = float(df["close"].iloc[i - 2])
        o1 = float(df["open"].iloc[i - 1])
        c1 = float(df["close"].iloc[i - 1])
        body2 = _candle_body(o2, c2)
        body1 = _candle_body(o1, c1)
        bull2 = c2 > o2
        bear2 = c2 < o2
        bull1 = c1 > o1
        bear1 = c1 < o1

        # 晨星 Morning Star
        if (bear2 and body2 > 0.01 * abs(c2)
                and body1 < body2 * 0.4
                and bull0 and c0 > (o2 + c2) / 2):
            patterns.append("晨星"); bullish = True

        # 夜星 Evening Star
        if (bull2 and body2 > 0.01 * abs(c2)
                and body1 < body2 * 0.4
                and bear0 and c0 < (o2 + c2) / 2):
            patterns.append("夜星"); bearish = True

        # 三白兵 Three White Soldiers
        if (bull2 and bull1 and bull0
                and body1 > body2 * 0.7 and body0 > body1 * 0.7):
            patterns.append("三白兵"); bullish = True

        # 三黑鴉 Three Black Crows
        if (bear2 and bear1 and bear0
                and body1 > body2 * 0.7 and body0 > body1 * 0.7):
            patterns.append("三黑鴉"); bearish = True

    return {"bullish": bullish, "bearish": bearish, "patterns": patterns}


def has_recent_candle_signal(
    df: pd.DataFrame, i: int, signal_type: str, lookback: int = CANDLE_LOOKBACK
) -> tuple[bool, list[str]]:
    """
    在第 i 根K棒往前 lookback 根範圍內，是否出現指定方向的陰陽燭形態。
    signal_type: "bullish" 或 "bearish"
    回傳 (found: bool, patterns: list[str])（去重、保序）
    """
    all_patterns: list[str] = []
    for j in range(max(0, i - lookback + 1), i + 1):
        result = detect_candle_patterns(df, j)
        if result[signal_type]:
            for p in result["patterns"]:
                if p not in all_patterns:
                    all_patterns.append(p)
    return bool(all_patterns), all_patterns


# ----------------------------------------------------------
# 單一標的回測引擎
# ----------------------------------------------------------
def _backtest_symbol(
    closes: pd.Series,
    symbol: str,
    bars_df: pd.DataFrame | None = None,
    use_candle_filter: bool = False,
    trailing_stop_pct: float = TRAILING_STOP_PCT,
) -> dict:
    """
    模擬 5/20 均線金叉/死叉策略（含滾動止損）。
    use_candle_filter=True 時，金叉進場前須確認過去 CANDLE_LOOKBACK 根K棒內
    出現看漲陰陽燭形態（晨星、牛市吞沒、槌子線等），作為圖表確認信號。
    bars_df 需含 open/high/low/close 欄位，索引需與 closes 對齊。

    回傳 dict：
      symbol, trades[], total, wins, losses, win_rate,
      avg_pnl, best_trade, worst_trade, stop_exits, cross_exits, open_trade
    """
    ma_short = closes.rolling(SHORT_MA).mean()
    ma_long  = closes.rolling(LONG_MA).mean()

    trades: list[dict] = []
    in_trade    = False
    entry_price = 0.0
    entry_date  = None
    entry_idx   = 0       # 記錄進場 bar 索引，用於計算持倉天數
    peak        = 0.0
    # 統一轉為 date 字串，避免 Timestamp 時區比較錯誤
    dates = [str(d)[:10] for d in closes.index.tolist()]

    for i in range(1, len(closes)):
        ps, cs = ma_short.iloc[i - 1], ma_short.iloc[i]
        pl, cl = ma_long.iloc[i - 1],  ma_long.iloc[i]
        if any(pd.isna(x) for x in (ps, cs, pl, cl)):
            continue

        price = closes.iloc[i]

        if not in_trade:
            if ps <= pl and cs > cl:   # 金叉進場
                if use_candle_filter and bars_df is not None and not bars_df.empty:
                    found, _ = has_recent_candle_signal(bars_df, i, "bullish")
                    if not found:
                        continue       # 無看漲燭形確認，跳過本次進場
                in_trade    = True
                entry_price = price
                entry_date  = dates[i]
                entry_idx   = i
                peak        = price
        else:
            if price > peak:
                peak = price

            # 滾動止損優先於死叉出場
            drawdown = (peak - price) / peak
            if drawdown >= trailing_stop_pct:
                trades.append({
                    "entry_date":  entry_date,
                    "exit_date":   dates[i],
                    "entry_price": entry_price,
                    "exit_price":  price,
                    "pnl_pct":     (price - entry_price) / entry_price * 100,
                    "hold_days":   i - entry_idx,
                    "exit_type":   "STOP",
                })
                in_trade = False
                continue

            # 死叉出場
            if ps >= pl and cs < cl:
                trades.append({
                    "entry_date":  entry_date,
                    "exit_date":   dates[i],
                    "entry_price": entry_price,
                    "exit_price":  price,
                    "pnl_pct":     (price - entry_price) / entry_price * 100,
                    "hold_days":   i - entry_idx,
                    "exit_type":   "CROSS",
                })
                in_trade = False

    open_trade = None
    if in_trade:
        last_price = closes.iloc[-1]
        open_trade = {
            "entry_date":         entry_date,
            "entry_price":        entry_price,
            "current_price":      last_price,
            "unrealized_pnl_pct": (last_price - entry_price) / entry_price * 100,
        }

    wins       = sum(1 for t in trades if t["pnl_pct"] > 0)
    pnls       = [t["pnl_pct"] for t in trades]
    stop_exits = sum(1 for t in trades if t["exit_type"] == "STOP")

    return {
        "symbol":      symbol,
        "trades":      trades,
        "total":       len(trades),
        "wins":        wins,
        "losses":      len(trades) - wins,
        "win_rate":    wins / len(trades) if trades else 0.0,
        "avg_pnl":     float(np.mean(pnls)) if pnls else 0.0,
        "best_trade":  float(max(pnls)) if pnls else 0.0,
        "worst_trade": float(min(pnls)) if pnls else 0.0,
        "stop_exits":  stop_exits,
        "cross_exits": len(trades) - stop_exits,
        "open_trade":  open_trade,
    }


# ----------------------------------------------------------
# 開市前自動選股
# ----------------------------------------------------------
def screen_stocks(data_client: StockHistoricalDataClient) -> list:
    """
    從 SCREEN_CANDIDATES 中，依過往 5 年日線數據評分，
    選出 SCREEN_TOP_N 隻最適合低買高賣的活躍股票。

    評分指標（加權）：
      平均日成交量（流動性）    25%
      平均日波幅 % (high-low)  35%
      均線策略歷史勝率          40%
    """
    log.info("=== 開市前選股開始（候選股：%d 隻，回溯：%d 年）===",
             len(SCREEN_CANDIDATES), SCREEN_LOOKBACK_YEARS)

    end   = datetime.now(timezone.utc)
    start = end - timedelta(days=SCREEN_LOOKBACK_YEARS * 365 + 60)

    results: dict[str, dict] = {}
    batch_size = 10

    for i in range(0, len(SCREEN_CANDIDATES), batch_size):
        batch = SCREEN_CANDIDATES[i: i + batch_size]
        try:
            req  = StockBarsRequest(
                symbol_or_symbols=batch,
                timeframe=TimeFrame.Day,
                start=start,
                end=end,
                feed=DataFeed.IEX,
            )
            raw_df = data_client.get_stock_bars(req).df
            if raw_df.empty:
                continue

            for symbol in batch:
                try:
                    sym_df = (
                        raw_df.xs(symbol, level="symbol")
                        if isinstance(raw_df.index, pd.MultiIndex)
                        else raw_df
                    )
                    if len(sym_df) < 252:   # 不足一年數據，略過
                        continue

                    closes  = sym_df["close"]
                    volumes = sym_df["volume"]
                    highs   = sym_df["high"]
                    lows    = sym_df["low"]

                    avg_vol     = float(volumes.mean())
                    daily_range = float(((highs - lows) / closes).mean() * 100)
                    win_rate    = calc_ma_win_rate(closes)

                    results[symbol] = {
                        "avg_vol":     avg_vol,
                        "daily_range": daily_range,
                        "win_rate":    win_rate,
                    }
                    log.info(
                        "  %s | 均量 %s | 日波幅 %.2f%% | MA勝率 %.1f%% | %d 交易日",
                        symbol, f"{avg_vol:,.0f}", daily_range, win_rate * 100, len(sym_df),
                    )
                except Exception as exc:
                    log.debug("%s 處理失敗：%s", symbol, exc)

        except Exception as exc:
            log.warning("批次抓取失敗 %s：%s", batch, exc)

        time.sleep(0.5)   # 避免觸發 API 限速

    if not results:
        log.warning("選股結果為空，保留原觀察清單：%s", WATCHLIST)
        return list(WATCHLIST)

    # 正規化各指標至 [0, 1] 後加權求總分
    score_df = pd.DataFrame(results).T.astype(float)
    for col in ("avg_vol", "daily_range", "win_rate"):
        lo, hi = score_df[col].min(), score_df[col].max()
        denom  = hi - lo if hi != lo else 1.0
        score_df[f"{col}_n"] = (score_df[col] - lo) / denom

    score_df["score"] = (
        score_df["avg_vol_n"]     * 0.25 +
        score_df["daily_range_n"] * 0.35 +
        score_df["win_rate_n"]    * 0.40
    )

    top = score_df.nlargest(SCREEN_TOP_N, "score")

    log.info("=== 選股結果（Top %d）===", SCREEN_TOP_N)
    for sym, row in top.iterrows():
        log.info(
            "  ✅ %s | 綜合分 %.3f | 均量 %s | 日波幅 %.2f%% | MA勝率 %.1f%%",
            sym, row["score"], f"{row['avg_vol']:,.0f}", row["daily_range"], row["win_rate"] * 100,
        )

    selected = top.index.tolist()
    log.info("新觀察清單：%s", selected)
    return selected


# ----------------------------------------------------------
# 持倉查詢
# ----------------------------------------------------------
def get_current_position(
    trading_client: TradingClient | None,
    symbol: str,
    trial: bool = False,
) -> dict:
    if trial:
        return _paper_positions.get(symbol, {"quantity": 0, "avg_cost": 0.0})
    try:
        pos = trading_client.get_open_position(symbol)
        return {
            "quantity": int(float(pos.qty)),
            "avg_cost": float(pos.avg_entry_price),
        }
    except Exception:
        return {"quantity": 0, "avg_cost": 0.0}


# ----------------------------------------------------------
# 即時報價
# ----------------------------------------------------------
def get_latest_price(data_client: StockHistoricalDataClient, symbol: str) -> float | None:
    try:
        req    = StockLatestBarRequest(symbol_or_symbols=[symbol], feed=DataFeed.IEX)
        latest = data_client.get_stock_latest_bar(req)
        return float(latest[symbol].close)
    except Exception as e:
        log.warning("%s 取得即時報價失敗：%s", symbol, e)
        return None


# ----------------------------------------------------------
# 下單邏輯
# ----------------------------------------------------------
def place_order(
    trading_client: TradingClient | None,
    symbol: str,
    side: OrderSide,
    quantity: int,
    price: float,
    reason: str = "",
    trial: bool = False,
):
    fee_info   = calc_fee(price, quantity, is_sell=(side == OrderSide.SELL))
    mode_label = "試跑" if trial else ("Paper" if PAPER_TRADING else "實盤")

    log.info(
        "[%s] %s %s %d 股 @ %.4f | 費用：%s | 原因：%s",
        mode_label, side.value.upper(), symbol, quantity, price, fee_info, reason,
    )

    # 賣出時清除峰值記錄
    if side == OrderSide.SELL:
        if symbol in _position_peaks:
            _position_peaks.pop(symbol, None)
            save_peaks(_position_peaks)

    if trial:
        pos = _paper_positions.get(symbol, {"quantity": 0, "avg_cost": 0.0})
        if side == OrderSide.BUY:
            total_cost  = pos["avg_cost"] * pos["quantity"] + price * quantity
            pos["quantity"] += quantity
            pos["avg_cost"] = total_cost / pos["quantity"] if pos["quantity"] else 0.0
        else:
            pos["quantity"] = max(0, pos["quantity"] - quantity)
            if pos["quantity"] == 0:
                pos["avg_cost"] = 0.0
        _paper_positions[symbol] = pos
        log.info("[試跑] 持倉更新 %s: %s", symbol, pos)
        return

    order_req = MarketOrderRequest(
        symbol=symbol,
        qty=quantity,
        side=side,
        time_in_force=TimeInForce.DAY,
    )
    resp = trading_client.submit_order(order_req)
    log.info("[%s] 訂單已提交，Order ID: %s | 狀態：%s", mode_label, resp.id, resp.status)


# ----------------------------------------------------------
# 滾動止損
# ----------------------------------------------------------
def update_trailing_stop(
    trading_client: TradingClient | None,
    data_client: StockHistoricalDataClient | None,
    symbol: str,
    trial: bool = False,
):
    """
    追蹤持倉峰值，若當前價從峰值回落 >= TRAILING_STOP_PCT 則平倉。
    試跑模式下跳過（無即時報價）。
    """
    pos = get_current_position(trading_client, symbol, trial=trial)
    if pos["quantity"] <= 0:
        if symbol in _position_peaks:
            _position_peaks.pop(symbol, None)
            save_peaks(_position_peaks)
        return

    if trial:
        return

    current_price = get_latest_price(data_client, symbol)
    if current_price is None:
        return

    # 更新峰值
    if symbol not in _position_peaks:
        _position_peaks[symbol] = current_price
        save_peaks(_position_peaks)
        log.info("%s 滾動止損監測已啟動，初始峰值：$%.4f", symbol, current_price)

    peak = _position_peaks[symbol]
    if current_price > peak:
        peak = current_price
        _position_peaks[symbol] = peak
        save_peaks(_position_peaks)
        log.info("%s 峰值更新：$%.4f", symbol, peak)

    # 計算回撤
    drawdown = (peak - current_price) / peak
    log.info(
        "%s 滾動止損監控：當前 $%.4f | 峰值 $%.4f | 回撤 %.2f%%（門檻 %.0f%%）",
        symbol, current_price, peak, drawdown * 100, TRAILING_STOP_PCT * 100,
    )

    if drawdown >= TRAILING_STOP_PCT:
        log.warning(
            "[滾動止損] %s 從峰值 $%.4f 回落 %.2f%%，觸發平倉！",
            symbol, peak, drawdown * 100,
        )
        place_order(trading_client, symbol, OrderSide.SELL, pos["quantity"], current_price, "滾動止損平倉")


# ----------------------------------------------------------
# 主策略輪詢
# ----------------------------------------------------------
def run_strategy(trading_client: TradingClient, data_client: StockHistoricalDataClient):
    """對 WATCHLIST ∪ 當前持倉股票執行策略（持倉股不在清單仍持續監察至平倉）"""
    global WATCHLIST
    # 讀取 GUI 手動設定的觀察清單（若存在）
    if os.path.exists(WATCHLIST_FILE):
        try:
            import json as _json
            with open(WATCHLIST_FILE, encoding="utf-8") as _f:
                _loaded = _json.load(_f)
            if isinstance(_loaded, list) and _loaded:
                # 自動校正清單中的加密貨幣格式
                raw_list = [s.upper().strip() for s in _loaded if s.strip()]
                WATCHLIST = []
                for s in raw_list:
                    norm = _normalize_crypto_symbol(s)
                    if "/" in norm:
                        if norm not in CRYPTO_WATCHLIST:
                            CRYPTO_WATCHLIST.append(norm)
                    else:
                        WATCHLIST.append(norm)
        except Exception:
            pass

    try:
        clock = trading_client.get_clock()
    except Exception as e:
        log.warning("取得市場時鐘失敗，跳過本次輪詢：%s", e)
        return

    if not clock.is_open:
        log.info("市場休市，下次開盤：%s", clock.next_open.strftime("%Y-%m-%d %H:%M %Z"))
        return

    # ── 擴大監察範圍：WATCHLIST ∪ 目前已持股票部位 ──
    watchlist_set  = set(WATCHLIST)
    held_extra: list[str] = []
    try:
        for pos in trading_client.get_all_positions():
            sym = pos.symbol
            # 使用標準化判斷是否為加密貨幣，避免股票邏輯誤抓加密持倉
            if "/" in _normalize_crypto_symbol(sym):
                continue
            
            if sym not in watchlist_set and float(pos.qty) > 0:
                held_extra.append(sym)
    except Exception as e:
        log.warning("無法取得持倉清單（僅監察 WATCHLIST）：%s", e)

    if held_extra:
        log.info("[持倉監察] 以下持倉不在觀察清單，加入本輪監察直至平倉：%s", held_extra)

    monitor_list = list(WATCHLIST) + held_extra

    for symbol in monitor_list:
        in_watchlist = symbol in watchlist_set
        try:
            label = symbol if in_watchlist else f"{symbol} [持倉監察]"
            log.info("--- 處理 %s ---", label)

            # 1. 滾動止損檢查（優先執行）
            update_trailing_stop(trading_client, data_client, symbol)

            # 2. 抓取完整 OHLCV + 均線信號
            bars_df = get_historical_bars_df(data_client, symbol, LONG_MA + BUY_WINDOW + 5)
            if bars_df.empty:
                if symbol not in _data_failed_symbols:
                    log.warning("%s 無法取得歷史行情（IEX 未覆蓋），本 session 後續靜默跳過", symbol)
                    _data_failed_symbols.add(symbol)
                continue
            closes = bars_df["close"]

            ma_signal = compute_signal(closes)
            pos       = get_current_position(trading_client, symbol)

            # 3. 買入邏輯：只在觀察清單內的股票允許新進場
            if ma_signal == "BUY" and pos["quantity"] == 0:
                if not in_watchlist:
                    log.info("%s [持倉監察] MA金叉但已移出觀察清單，不新增部位", symbol)
                else:
                    tv = analyse_stock(symbol)

                    if tv["tv_rating"] == "UNKNOWN":
                        tv_ok    = True
                        tv_label = "⚠️ TV不可用"
                    else:
                        tv_ok    = tv["bullish"]
                        tv_label = tv["tv_rating"]

                    # 陰陽燭圖形態三重確認
                    candle_found, candle_patterns = has_recent_candle_signal(
                        bars_df, len(bars_df) - 1, "bullish"
                    )
                    candle_str = "、".join(candle_patterns) if candle_found else "無看漲形態"

                    allow_entry = tv_ok and candle_found
                    if not tv_ok:
                        decision = f"❌ TV評級不足（{tv_label}），跳過"
                    elif not candle_found:
                        decision = "❌ 無看漲陰陽燭確認，跳過"
                    else:
                        decision = f"✅ 允許入市（燭形：{candle_str}）"

                    log.info(
                        "[分析] %s | TV評級: %s (買:%d 賣:%d) | MA信號: %s | 燭形: %s | 決策: %s",
                        symbol, tv["tv_rating"], tv["tv_buy_count"], tv["tv_sell_count"],
                        ma_signal, candle_str, decision,
                    )
                    if allow_entry:
                        current_price = get_latest_price(data_client, symbol) or closes.iloc[-1]
                        qty    = max(1, int(CAPITAL_PER_TRADE / current_price))
                        reason = f"TV+MA+燭形三重確認（{candle_str}）"
                        place_order(trading_client, symbol, OrderSide.BUY, qty, current_price, reason)
                        _position_peaks[symbol] = current_price
                        save_peaks(_position_peaks)

            # 4. 賣出邏輯：MA 死叉（持倉監察狀態同樣執行）
            elif ma_signal == "SELL" and pos["quantity"] > 0:
                log.info("[分析] %s | MA信號: SELL | 決策: ✅ 均線死叉平倉", symbol)
                current_price = get_latest_price(data_client, symbol) or closes.iloc[-1]
                place_order(trading_client, symbol, OrderSide.SELL, pos["quantity"], current_price, "均線死叉賣出")

            else:
                log.info("%s 無操作（MA信號=%s，持倉=%d）", symbol, ma_signal, pos["quantity"])

        except Exception as exc:
            log.error("%s 處理出錯：%s", symbol, exc, exc_info=True)


# ----------------------------------------------------------
# 加密貨幣工具函數
# ----------------------------------------------------------
def _crypto_tv_symbol(symbol: str) -> str:
    """BTC/USD → BTCUSD（TradingView 格式）"""
    return symbol.replace("/", "")


def _crypto_pos_symbol(symbol: str) -> str:
    """BTC/USD → BTCUSD（Alpaca 持倉查詢格式）"""
    return symbol.replace("/", "")


def analyse_crypto(symbol: str) -> dict:
    """
    TradingView 加密貨幣技術分析評級（日線）。
    依序嘗試 COINBASE → BINANCE，皆失敗則回傳 UNKNOWN。
    """
    tv_sym = _crypto_tv_symbol(symbol)
    for exchange in ("COINBASE", "BINANCE"):
        try:
            handler = TA_Handler(
                symbol=tv_sym,
                screener="crypto",
                exchange=exchange,
                interval=Interval.INTERVAL_1_DAY,
            )
            analysis  = handler.get_analysis()
            rec        = analysis.summary["RECOMMENDATION"]
            buy_count  = analysis.summary["BUY"]
            sell_count = analysis.summary["SELL"]
            return {
                "tv_rating":     rec,
                "tv_buy_count":  buy_count,
                "tv_sell_count": sell_count,
                "bullish":       rec in ("BUY", "STRONG_BUY"),
                "exchange":      exchange,
            }
        except Exception:
            continue

    log.warning("%s 無法取得 TradingView 加密評級，預設 UNKNOWN", symbol)
    return {"tv_rating": "UNKNOWN", "tv_buy_count": 0, "tv_sell_count": 0, "bullish": False, "exchange": "N/A"}


def get_crypto_historical_bars_df(
    crypto_client: CryptoHistoricalDataClient,
    symbol: str,
    days: int,
) -> pd.DataFrame:
    """抓取加密貨幣最近 N 天完整 OHLCV 資料（供陰陽燭形態識別使用）"""
    end   = datetime.now(timezone.utc)
    start = end - timedelta(days=days + 10)
    req   = CryptoBarsRequest(
        symbol_or_symbols=symbol,
        timeframe=TimeFrame.Day,
        start=start,
        end=end,
    )
    bars = crypto_client.get_crypto_bars(req)
    df   = bars.df
    if df.empty:
        return pd.DataFrame()
    if isinstance(df.index, pd.MultiIndex):
        df = df.xs(symbol, level="symbol")
    return df[["open", "high", "low", "close", "volume"]].sort_index().tail(days)


def get_crypto_historical_closes(
    crypto_client: CryptoHistoricalDataClient,
    symbol: str,
    days: int,
) -> pd.Series:
    """抓取加密貨幣最近 N 天日線收盤價（向後相容保留）"""
    df = get_crypto_historical_bars_df(crypto_client, symbol, days)
    return df["close"] if not df.empty else pd.Series(dtype=float)


def get_crypto_latest_price(
    crypto_client: CryptoHistoricalDataClient,
    symbol: str,
) -> float | None:
    try:
        req    = CryptoLatestBarRequest(symbol_or_symbols=[symbol])
        latest = crypto_client.get_crypto_latest_bar(req)
        return float(latest[symbol].close)
    except Exception as e:
        log.warning("%s 取得加密即時報價失敗：%s", symbol, e)
        return None


def get_crypto_position(trading_client: TradingClient, symbol: str) -> dict:
    """查詢加密貨幣持倉；Alpaca 用 BTCUSD 格式存倉位"""
    pos_symbol = _crypto_pos_symbol(symbol)
    try:
        pos = trading_client.get_open_position(pos_symbol)
        return {
            "quantity": float(pos.qty),
            "avg_cost": float(pos.avg_entry_price),
        }
    except Exception:
        return {"quantity": 0.0, "avg_cost": 0.0}


def place_crypto_order(
    trading_client: TradingClient,
    symbol: str,
    side: OrderSide,
    qty: float,
    price: float,
    reason: str = "",
):
    """
    提交加密貨幣市場訂單。
    qty  — 加密貨幣數量（浮點數，精度 8 位）
    使用 TimeInForce.GTC（加密貨幣 24/7 交易）
    """
    qty = round(qty, 8)
    mode_label = "Paper" if PAPER_TRADING else "實盤"
    log.info(
        "[%s][加密] %s %s %.8f @ $%.4f | 約 $%.2f | 原因：%s",
        mode_label, side.value.upper(), symbol, qty, price, qty * price, reason,
    )

    if side == OrderSide.SELL:
        if symbol in _position_peaks:
            _position_peaks.pop(symbol, None)
            save_peaks(_position_peaks)

    order_req = MarketOrderRequest(
        symbol=symbol,
        qty=qty,
        side=side,
        time_in_force=TimeInForce.GTC,
    )
    resp = trading_client.submit_order(order_req)
    log.info("[%s][加密] 訂單已提交，Order ID: %s | 狀態：%s", mode_label, resp.id, resp.status)


def update_crypto_trailing_stop(
    trading_client: TradingClient,
    crypto_client: CryptoHistoricalDataClient,
    symbol: str,
):
    """追蹤加密貨幣持倉峰值，回撤超過 TRAILING_STOP_PCT 則平倉"""
    pos = get_crypto_position(trading_client, symbol)
    if pos["quantity"] <= 1e-8:
        _position_peaks.pop(symbol, None)
        save_peaks(_position_peaks)
        return

    current_price = get_crypto_latest_price(crypto_client, symbol)
    if current_price is None:
        return

    # 更新峰值
    if symbol not in _position_peaks:
        _position_peaks[symbol] = current_price
        save_peaks(_position_peaks)
        log.info("[加密] %s 滾動止損監測已啟動，初始峰值：$%.4f", symbol, current_price)

    peak = _position_peaks[symbol]
    if current_price > peak:
        peak = current_price
        _position_peaks[symbol] = peak
        save_peaks(_position_peaks)
        log.info("[加密] %s 峰值更新：$%.4f", symbol, peak)

    drawdown = (peak - current_price) / peak
    log.info(
        "%s 加密滾動止損：當前 $%.4f | 峰值 $%.4f | 回撤 %.2f%%（門檻 %.0f%%）",
        symbol, current_price, peak, drawdown * 100, TRAILING_STOP_PCT * 100,
    )

    if drawdown >= TRAILING_STOP_PCT:
        log.warning(
            "[加密滾動止損] %s 從峰值 $%.4f 回落 %.2f%%，觸發平倉！",
            symbol, peak, drawdown * 100,
        )
        place_crypto_order(
            trading_client, symbol, OrderSide.SELL,
            pos["quantity"], current_price, "加密滾動止損平倉",
        )


def run_crypto_strategy(
    trading_client: TradingClient,
    crypto_client: CryptoHistoricalDataClient,
):
    """對 CRYPTO_WATCHLIST ∪ 當前加密持倉執行策略（持倉幣不在清單仍監察至平倉）"""
    # 持倉格式反查表：BTCUSD → BTC/USD
    _pos_to_watch: dict[str, str] = {_crypto_pos_symbol(s): s for s in CRYPTO_WATCHLIST}
    watchlist_set = set(CRYPTO_WATCHLIST)

    # ── 擴大監察範圍：CRYPTO_WATCHLIST ∪ 目前已持加密部位 ──
    held_extra: list[str] = []
    try:
        for pos in trading_client.get_all_positions():
            pos_sym = pos.symbol
            # 判斷是否為加密貨幣：包含 USD 且不是股票 (這裡簡單判斷，如果不在 WATCHLIST 且在 _pos_to_watch 或符合加密命名)
            # 其實更簡單的做法是：如果 pos_sym 在 _pos_to_watch，用 watch_sym；不在的話，嘗試轉化為帶斜線格式
            watch_sym = _pos_to_watch.get(pos_sym, _normalize_crypto_symbol(pos_sym))
            
            if "/" in watch_sym and watch_sym not in watchlist_set and float(pos.qty) > 1e-8:
                held_extra.append(watch_sym)
    except Exception as e:
        log.warning("無法取得加密持倉清單（僅監察 CRYPTO_WATCHLIST）：%s", e)

    if held_extra:
        log.info("[加密持倉監察] 以下持倉不在 CRYPTO_WATCHLIST，加入本輪監察直至平倉：%s", held_extra)

    monitor_list = list(CRYPTO_WATCHLIST) + held_extra
    log.info("=== 加密貨幣策略輪詢 (%d 種) ===", len(monitor_list))

    for symbol in monitor_list:
        in_watchlist = symbol in watchlist_set
        try:
            label = symbol if in_watchlist else f"{symbol} [持倉監察]"
            log.info("--- 加密 %s ---", label)

            # 1. 滾動止損優先檢查
            update_crypto_trailing_stop(trading_client, crypto_client, symbol)

            # 2. 歷史收盤 + 均線信號
            closes = get_crypto_historical_closes(
                crypto_client, symbol, LONG_MA + BUY_WINDOW + 5
            )
            if closes.empty:
                if symbol not in _data_failed_symbols:
                    log.warning("%s 無法取得加密歷史行情，本 session 後續靜默跳過", symbol)
                    _data_failed_symbols.add(symbol)
                continue

            ma_signal = compute_signal(closes)
            pos       = get_crypto_position(trading_client, symbol)

            # 3. 買入：只在 CRYPTO_WATCHLIST 內允許新進場
            if ma_signal == "BUY" and pos["quantity"] <= 1e-8:
                if not in_watchlist:
                    log.info("%s [持倉監察] MA金叉但已移出 CRYPTO_WATCHLIST，不新增部位", symbol)
                else:
                    tv = analyse_crypto(symbol)

                    if tv["tv_rating"] == "UNKNOWN":
                        allow_entry = True
                        decision    = "⚠️ TV不可用，僅憑MA信號入市"
                    else:
                        allow_entry = tv["bullish"]
                        decision    = "✅ 允許入市" if allow_entry else "❌ TV評級不足，跳過"

                    log.info(
                        "[加密分析] %s | TV評級: %s (買:%d 賣:%d) | MA信號: %s | 決策: %s",
                        symbol, tv["tv_rating"], tv["tv_buy_count"], tv["tv_sell_count"],
                        ma_signal, decision,
                    )

                    if allow_entry:
                        current_price = get_crypto_latest_price(crypto_client, symbol) or closes.iloc[-1]
                        buy_qty = CAPITAL_PER_CRYPTO_TRADE / current_price
                        reason  = "加密TV+MA雙確認買入" if tv["tv_rating"] != "UNKNOWN" else "加密MA金叉買入（TV不可用）"
                        place_crypto_order(
                            trading_client, symbol, OrderSide.BUY,
                            buy_qty, current_price, reason,
                        )
                        _position_peaks[symbol] = current_price
                        save_peaks(_position_peaks)

            # 4. 賣出：MA 死叉（持倉監察狀態同樣執行）
            elif ma_signal == "SELL" and pos["quantity"] > 1e-8:
                log.info("[加密分析] %s | MA信號: SELL | 決策: ✅ 均線死叉平倉", symbol)
                current_price = get_crypto_latest_price(crypto_client, symbol) or closes.iloc[-1]
                place_crypto_order(
                    trading_client, symbol, OrderSide.SELL,
                    pos["quantity"], current_price, "加密均線死叉賣出",
                )

            else:
                log.info(
                    "%s 無操作（MA信號=%s，持倉=%.8f）",
                    symbol, ma_signal, pos["quantity"],
                )

        except Exception as exc:
            log.error("%s 加密處理出錯：%s", symbol, exc, exc_info=True)


# ----------------------------------------------------------
# 結算報告
# ----------------------------------------------------------
def _print_summary(trading_client: TradingClient):
    log.info("=" * 60)
    log.info("TRADING SESSION SUMMARY")
    log.info("=" * 60)
    try:
        account = trading_client.get_account()
        log.info("帳戶狀態   : %s", account.status)
        log.info("淨值       : $%s", account.equity)
        log.info("可用資金   : $%s", account.buying_power)
    except Exception as e:
        log.warning("無法取得帳戶摘要：%s", e)
    log.info("-" * 60)
    for symbol in WATCHLIST:
        try:
            pos = trading_client.get_open_position(symbol)
            log.info(
                "%s  數量=%s  平均成本=$%s  現價=$%s  未實現損益=$%s",
                symbol, pos.qty, pos.avg_entry_price,
                pos.current_price, pos.unrealized_pl,
            )
        except Exception:
            log.info("%s  無持倉", symbol)
    log.info("=" * 60)


# ----------------------------------------------------------
# 回測輸出輔助函數
# ----------------------------------------------------------
def _print_backtest_table(label: str, results: list[dict]) -> None:
    if not results:
        log.info("  %s: 無有效數據", label)
        return
    hdr = (
        f"\n  {'Symbol':<10} | {'MA筆數':>6} | {'MA勝率':>7} | {'MA均損益':>8}"
        f" | {'燭形筆數':>8} | {'燭形勝率':>8} | {'燭形均損益':>10} | {'止損':>5} | TV即時評級"
    )
    sep = "  " + "-" * 95
    log.info("%s  (%d 個標的)", label, len(results))
    log.info(hdr)
    log.info(sep)
    for row in results:
        ma, candle, tv = row["ma"], row["candle"], row["tv"]
        stop_str = f"{ma['stop_exits']}/{ma['total']}" if ma["total"] else "0/0"
        log.info(
            "  %-10s | %6d  | %6.1f%%  | %+7.2f%% | %8d  | %7.1f%%   | %+9.2f%% | %5s | %s",
            row["symbol"],
            ma["total"],     ma["win_rate"]     * 100, ma["avg_pnl"],
            candle["total"], candle["win_rate"] * 100, candle["avg_pnl"],
            stop_str, tv["tv_rating"],
        )
    log.info(sep)


def _print_portfolio_aggregate(results: list[dict]) -> None:
    div = "=" * 80
    log.info(div)
    log.info("PORTFOLIO AGGREGATE")
    log.info(div)
    if not results:
        log.info("  無數據")
        log.info(div)
        return

    ma_total     = sum(r["ma"]["total"]         for r in results)
    candle_total = sum(r["candle"]["total"]      for r in results)
    ma_wins      = sum(r["ma"]["wins"]           for r in results)
    candle_wins  = sum(r["candle"]["wins"]       for r in results)
    ma_stops     = sum(r["ma"]["stop_exits"]     for r in results)
    candle_stops = sum(r["candle"]["stop_exits"] for r in results)
    ma_pnls      = [t["pnl_pct"] for r in results for t in r["ma"]["trades"]]
    candle_pnls  = [t["pnl_pct"] for r in results for t in r["candle"]["trades"]]

    ma_wr     = ma_wins     / ma_total     if ma_total     else 0.0
    candle_wr = candle_wins / candle_total if candle_total else 0.0
    ma_avg     = float(np.mean(ma_pnls))     if ma_pnls     else 0.0
    candle_avg = float(np.mean(candle_pnls)) if candle_pnls else 0.0

    log.info("%-24s | %-18s | %-18s", "", "MA-Only", "MA + 陰陽燭確認")
    log.info("%-24s | %-18s | %-18s", "-" * 24, "-" * 18, "-" * 18)
    log.info("%-24s | %-18s | %-18s", "Completed trades",
             str(ma_total), str(candle_total))
    log.info("%-24s | %-18s | %-18s", "Win rate",
             f"{ma_wr * 100:.1f}%", f"{candle_wr * 100:.1f}%")
    log.info("%-24s | %-18s | %-18s", "Avg trade P&L",
             f"{ma_avg:+.2f}%", f"{candle_avg:+.2f}%")
    log.info("%-24s | %-18s | %-18s", "Stop-loss exits",
             f"{ma_stops}/{ma_total}", f"{candle_stops}/{candle_total}")
    log.info(div)
    log.info("NOTE: TV ratings are CURRENT (live) — not historical.")
    log.info("      陰陽燭欄：金叉當日前 %d 根K棒內出現看漲形態（晨星/牛市吞沒等）方才進場。", CANDLE_LOOKBACK)
    log.info(div)


def _print_trade_log(symbol: str, trades: list[dict]) -> None:
    if not trades:
        return
    log.info("  TRADE LOG — %s", symbol)
    log.info("  %3s | %-10s | %-10s | %9s | %9s | %7s | %4s | %s",
             "#", "Entry", "Exit", "Entry $", "Exit $", "P&L%", "Days", "Exit")
    for i, t in enumerate(trades, 1):
        log.info("  %3d | %-10s | %-10s | %9.4f | %9.4f | %+6.2f%% | %4d | %s",
                 i,
                 str(t["entry_date"])[:10], str(t["exit_date"])[:10],
                 t["entry_price"], t["exit_price"],
                 t["pnl_pct"], t["hold_days"], t["exit_type"])


# ----------------------------------------------------------
# 投資組合回測（主入口）
# ----------------------------------------------------------
def run_portfolio_backtest(
    data_client: StockHistoricalDataClient,
    crypto_client: CryptoHistoricalDataClient,
    watchlist: list[str],
    crypto_watchlist: list[str],
    lookback_days: int = 365,
) -> None:
    """
    對當前投資組合（股票 + 加密貨幣）執行歷史回測，輸出雙重勝率報告：
      MA-only   — 純 5/20 均線策略（基準）
      MA+RSI>50 — 加入 RSI-14 > 50 確認（TradingView BUY 評級的歷史代理指標）

    同時拉取各標的的 TradingView 即時評級作為參考。

    CLI:
      python -X utf8 alpaca_trading_bot.py --backtest
      python -X utf8 alpaca_trading_bot.py --backtest --days 180
    """
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    div   = "=" * 80

    log.info(div)
    log.info("PORTFOLIO BACKTEST REPORT  |  Lookback: %d days  |  %s", lookback_days, today)
    log.info("Strategy: %dMA / %dMA crossover + %.0f%% trailing stop",
             SHORT_MA, LONG_MA, TRAILING_STOP_PCT * 100)
    log.info(div)

    stock_results:  list[dict] = []
    crypto_results: list[dict] = []

    # ── 股票回測 ──────────────────────────────────────────────────
    if watchlist:
        log.info("正在回測股票（%d 隻）…", len(watchlist))
        for symbol in watchlist:
            try:
                bars_df = get_historical_bars_df(data_client, symbol, lookback_days + 30)
                if bars_df.empty or len(bars_df) < LONG_MA + 2:
                    log.warning("  %s 數據不足，跳過", symbol)
                    continue
                closes     = bars_df["close"]
                ma_res     = _backtest_symbol(closes, symbol, use_candle_filter=False)
                candle_res = _backtest_symbol(closes, symbol, bars_df=bars_df, use_candle_filter=True)
                tv         = analyse_stock(symbol)
                stock_results.append({"symbol": symbol, "ma": ma_res, "candle": candle_res, "tv": tv})
                log.info(
                    "  %s OK — MA: %d 筆 (勝率 %.1f%%) | 燭形: %d 筆 (勝率 %.1f%%) | TV: %s",
                    symbol,
                    ma_res["total"],     ma_res["win_rate"]     * 100,
                    candle_res["total"], candle_res["win_rate"] * 100,
                    tv["tv_rating"],
                )
            except Exception as exc:
                log.warning("  %s 回測失敗：%s", symbol, exc)

    # ── 加密貨幣回測 ───────────────────────────────────────────────
    if crypto_watchlist:
        log.info("正在回測加密貨幣（%d 種）…", len(crypto_watchlist))
        for symbol in crypto_watchlist:
            try:
                bars_df = get_crypto_historical_bars_df(crypto_client, symbol, lookback_days + 30)
                if bars_df.empty or len(bars_df) < LONG_MA + 2:
                    log.warning("  %s 數據不足，跳過", symbol)
                    continue
                closes     = bars_df["close"]
                ma_res     = _backtest_symbol(closes, symbol, use_candle_filter=False)
                candle_res = _backtest_symbol(closes, symbol, bars_df=bars_df, use_candle_filter=True)
                tv         = analyse_crypto(symbol)
                crypto_results.append({"symbol": symbol, "ma": ma_res, "candle": candle_res, "tv": tv})
                log.info(
                    "  %s OK — MA: %d 筆 (勝率 %.1f%%) | 燭形: %d 筆 (勝率 %.1f%%) | TV: %s",
                    symbol,
                    ma_res["total"],     ma_res["win_rate"]     * 100,
                    candle_res["total"], candle_res["win_rate"] * 100,
                    tv["tv_rating"],
                )
            except Exception as exc:
                log.warning("  %s 加密回測失敗：%s", symbol, exc)

    # ── 輸出報告 ───────────────────────────────────────────────────
    log.info("")
    _print_backtest_table("STOCKS", stock_results)
    log.info("")
    _print_backtest_table("CRYPTO", crypto_results)
    log.info("")
    _print_portfolio_aggregate(stock_results + crypto_results)

    # ── 逐筆交易記錄（筆數少時才顯示）───────────────────────────────
    all_ma_trades     = sum(r["ma"]["total"]     for r in stock_results + crypto_results)
    all_candle_trades = sum(r["candle"]["total"] for r in stock_results + crypto_results)
    if 0 < all_candle_trades <= 120:
        log.info("")
        log.info("=== 逐筆交易記錄（MA + 陰陽燭確認）===")
        for row in stock_results + crypto_results:
            _print_trade_log(row["symbol"], row["candle"]["trades"])
    elif 0 < all_ma_trades <= 120:
        log.info("")
        log.info("=== 逐筆交易記錄（MA-only）===")
        for row in stock_results + crypto_results:
            _print_trade_log(row["symbol"], row["ma"]["trades"])
    elif all_ma_trades > 120:
        log.info("（MA 交易筆數共 %d，略過逐筆顯示）", all_ma_trades)


# ----------------------------------------------------------
# GUI 回測入口（回傳結構化資料 + bars_df，供圖表渲染使用）
# ----------------------------------------------------------
def backtest_for_gui(
    api_key: str,
    api_secret: str,
    watchlist: list[str],
    crypto_watchlist: list[str],
    lookback_days: int = 365,
    progress_callback=None,
) -> dict:
    """
    GUI 友好版回測：回傳結構化結果而非僅輸出 log。

    回傳格式：
      {
        "stocks":  [{"symbol", "ma", "candle", "tv", "bars_df"}, ...],
        "crypto":  [{"symbol", "ma", "candle", "tv", "bars_df"}, ...],
        "errors":  [{"symbol": str, "error": str}, ...],
      }

    progress_callback(str) 為可選的進度回呼（供 QThread 轉發至 UI Log 面板）。
    """
    _emit = progress_callback or log.info

    data_client   = StockHistoricalDataClient(api_key, api_secret)
    crypto_client = CryptoHistoricalDataClient(api_key, api_secret)

    stock_results:  list[dict] = []
    crypto_results: list[dict] = []
    errors:         list[dict] = []

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    _emit(f"[GUI 回測] 開始  |  回溯 {lookback_days} 天  |  {today}")

    # ── 股票 ──────────────────────────────────────────────
    for symbol in watchlist:
        try:
            # 取 lookback_days + 60 天小時，前 60 天供 MA 暖身，不裁切
            bars_df = get_historical_bars_df(data_client, symbol, lookback_days + 60)
            if bars_df.empty or len(bars_df) < LONG_MA + 2:
                _emit(f"  {symbol} 數據不足，跳過")
                continue
            closes     = bars_df["close"]
            ma_res     = _backtest_symbol(closes, symbol, use_candle_filter=False)
            candle_res = _backtest_symbol(closes, symbol, bars_df=bars_df, use_candle_filter=True)
            tv         = analyse_stock(symbol)
            # 圖表僅顯示最近 lookback_days 天，回測則用全部數據
            chart_df = bars_df.tail(lookback_days)
            stock_results.append({
                "symbol":  symbol,
                "ma":      ma_res,
                "candle":  candle_res,
                "tv":      tv,
                "bars_df": chart_df,
            })
            _emit(
                f"  {symbol} OK — MA: {ma_res['total']} 筆 ({ma_res['win_rate']*100:.1f}%)"
                f" | 燭形: {candle_res['total']} 筆 ({candle_res['win_rate']*100:.1f}%)"
                f" | TV: {tv['tv_rating']}"
            )
        except Exception as exc:
            errors.append({"symbol": symbol, "error": str(exc)})
            _emit(f"  {symbol} 回測失敗：{exc}")

    # ── 加密貨幣 ───────────────────────────────────────────
    for symbol in crypto_watchlist:
        try:
            bars_df = get_crypto_historical_bars_df(crypto_client, symbol, lookback_days + 60)
            if bars_df.empty or len(bars_df) < LONG_MA + 2:
                _emit(f"  {symbol} 加密數據不足，跳過")
                continue
            closes     = bars_df["close"]
            ma_res     = _backtest_symbol(closes, symbol, use_candle_filter=False)
            candle_res = _backtest_symbol(closes, symbol, bars_df=bars_df, use_candle_filter=True)
            tv         = analyse_crypto(symbol)
            chart_df = bars_df.tail(lookback_days)
            crypto_results.append({
                "symbol":  symbol,
                "ma":      ma_res,
                "candle":  candle_res,
                "tv":      tv,
                "bars_df": chart_df,
            })
            _emit(
                f"  {symbol} OK — MA: {ma_res['total']} 筆 ({ma_res['win_rate']*100:.1f}%)"
                f" | 燭形: {candle_res['total']} 筆 ({candle_res['win_rate']*100:.1f}%)"
                f" | TV: {tv['tv_rating']}"
            )
        except Exception as exc:
            errors.append({"symbol": symbol, "error": str(exc)})
            _emit(f"  {symbol} 加密回測失敗：{exc}")

    _emit(f"[GUI 回測] 完成  股票 {len(stock_results)} / 加密 {len(crypto_results)} / 錯誤 {len(errors)}")
    return {"stocks": stock_results, "crypto": crypto_results, "errors": errors}


# ----------------------------------------------------------
# 試跑驗證
# ----------------------------------------------------------
def trial_run():
    log.info("====== Trial Run 開始（V6）======")

    # 1. 費用計算
    buy_fee  = calc_fee(150.0, 10, is_sell=False)
    sell_fee = calc_fee(155.0, 10, is_sell=True)
    log.info("買入費用: %s", buy_fee)
    log.info("賣出費用: %s", sell_fee)
    assert buy_fee["合計費用"] == 0.0
    assert sell_fee["合計費用"] > 0.0

    # 2. 均線信號
    np.random.seed(42)
    rising = pd.Series(
        [100 + i * 0.5 + np.random.randn() * 2 for i in range(30)],
        index=pd.date_range(end=datetime.today(), periods=30).date,
    )
    log.info("上升假資料均線信號: %s", compute_signal(rising))

    # 3. 模擬下單與持倉
    _paper_positions.clear()
    test_price = 700.0
    test_qty   = max(1, int(CAPITAL_PER_TRADE / test_price))  # 10000/700 = 14
    place_order(None, "NFLX", OrderSide.BUY,  test_qty, test_price, "試跑買入", trial=True)
    assert _paper_positions["NFLX"]["quantity"] == test_qty
    place_order(None, "NFLX", OrderSide.SELL, test_qty, 720.0, "試跑賣出", trial=True)
    assert _paper_positions["NFLX"]["quantity"] == 0
    log.info("持倉模擬測試通過")

    # 4. 滾動止損邏輯
    _position_peaks["NFLX"] = 750.0
    peak          = _position_peaks["NFLX"]
    current_price = 732.0   # 模擬回撤 2.4%
    drawdown      = (peak - current_price) / peak
    triggered     = drawdown >= TRAILING_STOP_PCT
    log.info("滾動止損測試：回撤 %.2f%%，應觸發 True -> %s", drawdown * 100, triggered)
    assert triggered, "滾動止損邏輯錯誤！"

    # 5. 陰陽燭形態識別
    log.info("測試陰陽燭形態識別…")
    candle_test = pd.DataFrame({
        "open":  [100.0, 105.0, 103.0],
        "high":  [110.0, 106.0, 112.0],
        "low":   [ 90.0, 102.0, 102.0],
        "close": [104.0, 103.5, 111.0],
    })
    result = detect_candle_patterns(candle_test, 2)
    log.info("陰陽燭測試結果：牛=%s 熊=%s 形態=%s", result["bullish"], result["bearish"], result["patterns"])
    found, patterns = has_recent_candle_signal(candle_test, 2, "bullish")
    log.info("近期看漲形態掃描：找到=%s 形態=%s", found, patterns)

    # 6. TradingView 評級（live 網路測試）
    log.info("測試 TradingView 評級（需要網路）…")
    tv = analyse_stock("NFLX")
    log.info("NFLX TV評級: %s (買:%d 賣:%d) 交易所:%s",
             tv["tv_rating"], tv["tv_buy_count"], tv["tv_sell_count"], tv["exchange"])

    log.info("====== Trial Run 全部通過 ✓（V6）======")
    log.info("PAPER_TRADING = %s | TRAILING_STOP_PCT = %.0f%% | CAPITAL_PER_TRADE = $%d",
             PAPER_TRADING, TRAILING_STOP_PCT * 100, CAPITAL_PER_TRADE)


# ----------------------------------------------------------
# 選股結果同步至 watchlist.json（讓 GUI 即時反映最新選股）
# ----------------------------------------------------------
def _sync_watchlist_file(symbols: list[str]):
    import json as _json
    try:
        with open(WATCHLIST_FILE, "w", encoding="utf-8") as _f:
            _json.dump(symbols, _f, indent=2)
        log.info("選股結果已同步至 watchlist.json：%s", symbols)
    except Exception as exc:
        log.warning("watchlist.json 同步失敗（不影響策略）：%s", exc)


# ----------------------------------------------------------
# 進入點
# ----------------------------------------------------------
def main():
    api_key    = os.getenv("ALPACA_API_KEY")
    api_secret = os.getenv("ALPACA_API_SECRET")
    if not api_key or not api_secret:
        log.error("請設定環境變數 ALPACA_API_KEY 與 ALPACA_API_SECRET")
        sys.exit(1)

    global WATCHLIST

    log.info("=== Alpaca Trading Bot V6 ===")
    mode_str = "【實盤 LIVE】" if not PAPER_TRADING else "【模擬 PAPER】"
    log.info("交易模式：%s | 滾動止損 = %.0f%% | 每筆本金 = $%d",
             mode_str, TRAILING_STOP_PCT * 100, CAPITAL_PER_TRADE)

    trading_client = TradingClient(api_key, api_secret, paper=PAPER_TRADING)
    data_client    = StockHistoricalDataClient(api_key, api_secret)
    crypto_client  = CryptoHistoricalDataClient(api_key, api_secret)

    account = trading_client.get_account()
    log.info("帳戶狀態：%s | 可用資金：$%s", account.status, account.buying_power)

    log.info("觀察清單（預設）：%s", WATCHLIST)
    log.info("加密貨幣觀察清單：%s", CRYPTO_WATCHLIST)

    run_duration    = 86400  # 24 小時
    end_time        = time.time() + run_duration
    screened_date   = None   # 記錄最近一次選股的日期，避免同一天重複執行
    _last_crypto_ts = 0.0    # 記錄上次加密策略執行時間

    log.info("開始策略輪詢（股票每 %d 秒 / 加密每 %d 秒），執行時間：24 小時…",
             POLL_INTERVAL, CRYPTO_POLL_INTERVAL)

    try:
        while time.time() < end_time:
            try:
                clock = trading_client.get_clock()
                today = datetime.now(timezone.utc).date()

                if not clock.is_open:
                    # ── 收市：計算距離下次開市的秒數，暫停等待 ──
                    now_utc    = datetime.now(timezone.utc)
                    sleep_secs = max(0.0, (clock.next_open - now_utc).total_seconds())
                    log.info(
                        "市場收市，暫停至 %s（約 %.0f 分鐘後）…",
                        clock.next_open.strftime("%Y-%m-%d %H:%M %Z"),
                        sleep_secs / 60,
                    )

                    # 分段休眠（每段最多 5 分鐘），同時維持加密貨幣 24/7 策略
                    wake_at = time.time() + sleep_secs
                    while time.time() < wake_at and time.time() < end_time:
                        next_crypto_ts = _last_crypto_ts + CRYPTO_POLL_INTERVAL
                        sleep_until    = min(wake_at, end_time, next_crypto_ts)
                        sleep_dur      = max(1.0, sleep_until - time.time())
                        time.sleep(sleep_dur)
                        if time.time() >= next_crypto_ts:
                            try:
                                run_crypto_strategy(trading_client, crypto_client)
                            except Exception as _exc:
                                log.warning("加密策略出錯：%s", _exc)
                            _last_crypto_ts = time.time()

                    # ── 醒來：執行開市前選股（每交易日只做一次）──
                    today = datetime.now(timezone.utc).date()
                    if screened_date != today:
                        log.info("市場即將開市，執行開市前選股…")
                        try:
                            WATCHLIST     = screen_stocks(data_client)
                            screened_date = today
                            _sync_watchlist_file(WATCHLIST)
                            log.info("觀察清單更新：%s", WATCHLIST)
                        except Exception as exc:
                            log.warning("選股失敗，保留現有清單：%s", exc)

                else:
                    # ── 開市：若今日尚未選股（如程式在開市中啟動），補做一次 ──
                    if screened_date != today:
                        log.info("開市中啟動，執行開市前選股…")
                        try:
                            WATCHLIST     = screen_stocks(data_client)
                            screened_date = today
                            _sync_watchlist_file(WATCHLIST)
                            log.info("觀察清單更新：%s", WATCHLIST)
                        except Exception as exc:
                            log.warning("選股失敗，保留現有清單：%s", exc)

                    run_strategy(trading_client, data_client)

                    # 加密貨幣策略（開市期間也持續運行）
                    if time.time() - _last_crypto_ts >= CRYPTO_POLL_INTERVAL:
                        try:
                            run_crypto_strategy(trading_client, crypto_client)
                        except Exception as _exc:
                            log.warning("加密策略出錯：%s", _exc)
                        _last_crypto_ts = time.time()

                    remaining = end_time - time.time()
                    if remaining > 0:
                        time.sleep(min(POLL_INTERVAL, remaining))

            except Exception as e:
                _transient = ("connection aborted", "remotedisconnected",
                              "connectionerror", "timeout", "connectionreset",
                              "connection reset", "remote end closed")
                if any(t in str(e).lower() for t in _transient):
                    log.warning("網路瞬斷，將在 %d 秒後重試：%s", POLL_INTERVAL, e)
                else:
                    log.error("輪詢出錯（將在 %d 秒後重試）：%s", POLL_INTERVAL, e, exc_info=True)
                time.sleep(POLL_INTERVAL)

    except KeyboardInterrupt:
        log.info("使用者中斷，程式結束。")

    log.info("24 小時交易結束，生成結算報告…")
    _print_summary(trading_client)


if __name__ == "__main__":
    if "--live" in sys.argv:
        # 一鍵切換實盤：若參數含 --real 則設為實盤模式
        if "--real" in sys.argv:
            PAPER_TRADING = False
        main()
    elif "--backtest" in sys.argv:
        # 可選：--days N（預設 365 天）
        _bt_days = 365
        if "--days" in sys.argv:
            try:
                _bt_days = int(sys.argv[sys.argv.index("--days") + 1])
            except (ValueError, IndexError):
                pass

        _api_key    = os.getenv("ALPACA_API_KEY", "")
        _api_secret = os.getenv("ALPACA_API_SECRET", "")
        if not _api_key or not _api_secret:
            log.error("請設定環境變數 ALPACA_API_KEY 與 ALPACA_API_SECRET")
            sys.exit(1)

        _dc = StockHistoricalDataClient(_api_key, _api_secret)
        _cc = CryptoHistoricalDataClient(_api_key, _api_secret)

        # 載入 watchlist.json（若存在），否則沿用模組預設清單
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

        run_portfolio_backtest(_dc, _cc, _wl, CRYPTO_WATCHLIST, lookback_days=_bt_days)
    else:
        trial_run()
