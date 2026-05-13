"""
Alpaca 自動交易機器人 — V5
雙均線策略（5日線 / 20日線）
+ TradingView 技術評級雙重確認
+ 2% 滾動止損（Trailing Stop Loss）
+ 開市前自動選股（5年回測評分）
+ 24/7 加密貨幣策略（BTC/ETH/SOL/AVAX/LINK）
+ 持倉股持續監察直至平倉
+ 瞬斷錯誤分類優化
+ 投資組合回測（--backtest）：雙重勝率報告 + TradingView 即時評級

環境變數設定（建議儲存於 .env 檔案）：
  ALPACA_API_KEY    = 您的 API Key ID
  ALPACA_API_SECRET = 您的 API Secret Key

安裝指令：
  pip install alpaca-py pandas numpy python-dotenv tradingview_ta

使用說明：
  python -X utf8 alpaca_trading_bot.py                    # 試跑驗證
  python -X utf8 alpaca_trading_bot.py --live             # 連線 Alpaca 實盤/Paper
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
# ⚠️  模式切換
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
POLL_INTERVAL = 15

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
CRYPTO_POLL_INTERVAL     = 300     # 加密策略輪詢間隔（秒，5 分鐘）

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
# 各持倉的歷史最高價（用於滾動止損）
_position_peaks: dict[str, float] = {}
# 本次 session 內已無法取得數據的符號（避免每輪詢重複警告）
_data_failed_symbols: set[str] = set()


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
# 歷史行情
# ----------------------------------------------------------
def get_historical_closes(
    data_client: StockHistoricalDataClient,
    symbol: str,
    days: int,
) -> pd.Series:
    """抓取最近 N 個交易日的收盤價"""
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
        return pd.Series(dtype=float)
    if isinstance(df.index, pd.MultiIndex):
        df = df.xs(symbol, level="symbol")
    return df["close"].sort_index().tail(days)


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
# 單一標的回測引擎
# ----------------------------------------------------------
def _backtest_symbol(
    closes: pd.Series,
    symbol: str,
    use_rsi_filter: bool = False,
    trailing_stop_pct: float = TRAILING_STOP_PCT,
) -> dict:
    """
    模擬 5/20 均線金叉/死叉策略（含滾動止損）。
    use_rsi_filter=True 時，金叉進場前須確認 RSI-14 > 50（TV BUY 評級的歷史代理指標）。

    回傳 dict：
      symbol, trades[], total, wins, losses, win_rate,
      avg_pnl, best_trade, worst_trade, stop_exits, cross_exits, open_trade
    """
    ma_short = closes.rolling(SHORT_MA).mean()
    ma_long  = closes.rolling(LONG_MA).mean()
    rsi      = calc_rsi(closes) if use_rsi_filter else None

    trades: list[dict] = []
    in_trade    = False
    entry_price = 0.0
    entry_date  = None
    peak        = 0.0
    dates       = closes.index.tolist()

    for i in range(1, len(closes)):
        ps, cs = ma_short.iloc[i - 1], ma_short.iloc[i]
        pl, cl = ma_long.iloc[i - 1],  ma_long.iloc[i]
        if any(pd.isna(x) for x in (ps, cs, pl, cl)):
            continue

        price = closes.iloc[i]

        if not in_trade:
            if ps <= pl and cs > cl:   # 金叉進場
                if use_rsi_filter and (pd.isna(rsi.iloc[i]) or rsi.iloc[i] <= 50):
                    continue           # RSI 未達 50，跳過本次進場
                in_trade    = True
                entry_price = price
                entry_date  = dates[i]
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
                    "hold_days":   int((dates[i] - entry_date).days),
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
                    "hold_days":   int((dates[i] - entry_date).days),
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
        _position_peaks.pop(symbol, None)

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
        _position_peaks.pop(symbol, None)
        return

    if trial:
        return

    current_price = get_latest_price(data_client, symbol)
    if current_price is None:
        return

    # 更新峰值
    peak = _position_peaks.get(symbol, current_price)
    if current_price > peak:
        peak = current_price
        _position_peaks[symbol] = peak
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
                WATCHLIST = [s.upper().strip() for s in _loaded if s.strip()]
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
    _crypto_pos_syms = {_crypto_pos_symbol(s) for s in CRYPTO_WATCHLIST}
    held_extra: list[str] = []
    try:
        for pos in trading_client.get_all_positions():
            sym = pos.symbol
            if sym not in _crypto_pos_syms and sym not in watchlist_set and float(pos.qty) > 0:
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

            # 2. 抓歷史收盤價 + 均線信號
            closes = get_historical_closes(data_client, symbol, LONG_MA + BUY_WINDOW + 5)
            if closes.empty:
                if symbol not in _data_failed_symbols:
                    log.warning("%s 無法取得歷史行情（IEX 未覆蓋），本 session 後續靜默跳過", symbol)
                    _data_failed_symbols.add(symbol)
                continue

            ma_signal = compute_signal(closes)
            pos       = get_current_position(trading_client, symbol)

            # 3. 買入邏輯：只在觀察清單內的股票允許新進場
            if ma_signal == "BUY" and pos["quantity"] == 0:
                if not in_watchlist:
                    log.info("%s [持倉監察] MA金叉但已移出觀察清單，不新增部位", symbol)
                else:
                    tv = analyse_stock(symbol)

                    if tv["tv_rating"] == "UNKNOWN":
                        allow_entry = True
                        decision    = "⚠️ TV不可用，僅憑MA信號入市"
                    else:
                        allow_entry = tv["bullish"]
                        decision    = "✅ 允許入市" if allow_entry else "❌ TV評級不足，跳過"

                    log.info(
                        "[分析] %s | TV評級: %s (買:%d 賣:%d) | MA信號: %s | 決策: %s",
                        symbol, tv["tv_rating"], tv["tv_buy_count"], tv["tv_sell_count"],
                        ma_signal, decision,
                    )
                    if allow_entry:
                        current_price = get_latest_price(data_client, symbol) or closes.iloc[-1]
                        qty    = max(1, int(CAPITAL_PER_TRADE / current_price))
                        reason = "TV+MA雙確認買入" if tv["tv_rating"] != "UNKNOWN" else "MA金叉買入（TV不可用）"
                        place_order(trading_client, symbol, OrderSide.BUY, qty, current_price, reason)
                        _position_peaks[symbol] = current_price

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


def get_crypto_historical_closes(
    crypto_client: CryptoHistoricalDataClient,
    symbol: str,
    days: int,
) -> pd.Series:
    """抓取加密貨幣最近 N 天日線收盤價（24/7，無需 DataFeed）"""
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
        return pd.Series(dtype=float)
    if isinstance(df.index, pd.MultiIndex):
        df = df.xs(symbol, level="symbol")
    return df["close"].sort_index().tail(days)


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
        _position_peaks.pop(symbol, None)

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
        return

    current_price = get_crypto_latest_price(crypto_client, symbol)
    if current_price is None:
        return

    peak = _position_peaks.get(symbol, current_price)
    if current_price > peak:
        peak = current_price
        _position_peaks[symbol] = peak
        log.info("%s 加密峰值更新：$%.4f", symbol, peak)

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
            if pos_sym in _pos_to_watch:
                watch_sym = _pos_to_watch[pos_sym]
                if watch_sym not in watchlist_set and float(pos.qty) > 1e-8:
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
        f" | {'RSI筆數':>7} | {'RSI勝率':>8} | {'RSI均損益':>9} | {'止損':>5} | TV即時評級"
    )
    sep = "  " + "-" * 90
    log.info("%s  (%d 個標的)", label, len(results))
    log.info(hdr)
    log.info(sep)
    for row in results:
        ma, rsi, tv = row["ma"], row["rsi"], row["tv"]
        stop_str = f"{ma['stop_exits']}/{ma['total']}" if ma["total"] else "0/0"
        log.info(
            "  %-10s | %6d  | %6.1f%%  | %+7.2f%% | %7d  | %7.1f%%  | %+8.2f%% | %5s | %s",
            row["symbol"],
            ma["total"],   ma["win_rate"]  * 100, ma["avg_pnl"],
            rsi["total"],  rsi["win_rate"] * 100, rsi["avg_pnl"],
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

    ma_total  = sum(r["ma"]["total"]  for r in results)
    rsi_total = sum(r["rsi"]["total"] for r in results)
    ma_wins   = sum(r["ma"]["wins"]   for r in results)
    rsi_wins  = sum(r["rsi"]["wins"]  for r in results)
    ma_stops  = sum(r["ma"]["stop_exits"]  for r in results)
    rsi_stops = sum(r["rsi"]["stop_exits"] for r in results)
    ma_pnls   = [t["pnl_pct"] for r in results for t in r["ma"]["trades"]]
    rsi_pnls  = [t["pnl_pct"] for r in results for t in r["rsi"]["trades"]]

    ma_wr  = ma_wins  / ma_total  if ma_total  else 0.0
    rsi_wr = rsi_wins / rsi_total if rsi_total else 0.0
    ma_avg  = float(np.mean(ma_pnls))  if ma_pnls  else 0.0
    rsi_avg = float(np.mean(rsi_pnls)) if rsi_pnls else 0.0

    log.info("%-24s | %-14s | %-14s", "", "MA-Only", "MA + RSI>50")
    log.info("%-24s | %-14s | %-14s", "-" * 24, "-" * 14, "-" * 14)
    log.info("%-24s | %-14s | %-14s", "Completed trades",
             str(ma_total), str(rsi_total))
    log.info("%-24s | %-14s | %-14s", "Win rate",
             f"{ma_wr * 100:.1f}%", f"{rsi_wr * 100:.1f}%")
    log.info("%-24s | %-14s | %-14s", "Avg trade P&L",
             f"{ma_avg:+.2f}%", f"{rsi_avg:+.2f}%")
    log.info("%-24s | %-14s | %-14s", "Stop-loss exits",
             f"{ma_stops}/{ma_total}", f"{rsi_stops}/{rsi_total}")
    log.info(div)
    log.info("NOTE: TV ratings are CURRENT (live) — not historical.")
    log.info("      RSI-14 > 50 is used as a historical proxy for TV BUY signals.")
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
                closes = get_historical_closes(data_client, symbol, lookback_days + 30)
                if closes.empty or len(closes) < LONG_MA + 2:
                    log.warning("  %s 數據不足，跳過", symbol)
                    continue
                ma_res  = _backtest_symbol(closes, symbol, use_rsi_filter=False)
                rsi_res = _backtest_symbol(closes, symbol, use_rsi_filter=True)
                tv      = analyse_stock(symbol)
                stock_results.append({"symbol": symbol, "ma": ma_res, "rsi": rsi_res, "tv": tv})
                log.info(
                    "  %s OK — MA: %d 筆 (勝率 %.1f%%) | RSI: %d 筆 (勝率 %.1f%%) | TV: %s",
                    symbol,
                    ma_res["total"],  ma_res["win_rate"]  * 100,
                    rsi_res["total"], rsi_res["win_rate"] * 100,
                    tv["tv_rating"],
                )
            except Exception as exc:
                log.warning("  %s 回測失敗：%s", symbol, exc)

    # ── 加密貨幣回測 ───────────────────────────────────────────────
    if crypto_watchlist:
        log.info("正在回測加密貨幣（%d 種）…", len(crypto_watchlist))
        for symbol in crypto_watchlist:
            try:
                closes = get_crypto_historical_closes(crypto_client, symbol, lookback_days + 30)
                if closes.empty or len(closes) < LONG_MA + 2:
                    log.warning("  %s 數據不足，跳過", symbol)
                    continue
                ma_res  = _backtest_symbol(closes, symbol, use_rsi_filter=False)
                rsi_res = _backtest_symbol(closes, symbol, use_rsi_filter=True)
                tv      = analyse_crypto(symbol)
                crypto_results.append({"symbol": symbol, "ma": ma_res, "rsi": rsi_res, "tv": tv})
                log.info(
                    "  %s OK — MA: %d 筆 (勝率 %.1f%%) | RSI: %d 筆 (勝率 %.1f%%) | TV: %s",
                    symbol,
                    ma_res["total"],  ma_res["win_rate"]  * 100,
                    rsi_res["total"], rsi_res["win_rate"] * 100,
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
    all_ma_trades = sum(r["ma"]["total"] for r in stock_results + crypto_results)
    if 0 < all_ma_trades <= 120:
        log.info("")
        log.info("=== 逐筆交易記錄（MA-only）===")
        for row in stock_results + crypto_results:
            _print_trade_log(row["symbol"], row["ma"]["trades"])
    elif all_ma_trades > 120:
        log.info("（MA 交易筆數共 %d，略過逐筆顯示）", all_ma_trades)


# ----------------------------------------------------------
# 試跑驗證
# ----------------------------------------------------------
def trial_run():
    log.info("====== Trial Run 開始（V5）======")

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

    # 5. TradingView 評級（live 網路測試）
    log.info("測試 TradingView 評級（需要網路）…")
    tv = analyse_stock("NFLX")
    log.info("NFLX TV評級: %s (買:%d 賣:%d) 交易所:%s",
             tv["tv_rating"], tv["tv_buy_count"], tv["tv_sell_count"], tv["exchange"])

    log.info("====== Trial Run 全部通過 ✓（V5）======")
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

    log.info("=== Alpaca Trading Bot V5 ===")
    log.info("PAPER_TRADING = %s | 滾動止損 = %.0f%% | 每筆本金 = $%d",
             PAPER_TRADING, TRAILING_STOP_PCT * 100, CAPITAL_PER_TRADE)

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
