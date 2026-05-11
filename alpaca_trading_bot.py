"""
Alpaca 自動交易機器人 — V2
雙均線策略（5日線 / 20日線）
+ TradingView 技術評級雙重確認
+ 2% 滾動止損（Trailing Stop Loss）

環境變數設定（建議儲存於 .env 檔案）：
  ALPACA_API_KEY    = 您的 API Key ID
  ALPACA_API_SECRET = 您的 API Secret Key

安裝指令：
  pip install alpaca-py pandas numpy python-dotenv tradingview_ta

使用說明：
  python -X utf8 alpaca_trading_bot.py          # 試跑驗證
  python -X utf8 alpaca_trading_bot.py --live   # 連線 Alpaca
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
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest, StockLatestBarRequest
from alpaca.data.timeframe import TimeFrame

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
    "X",      # U.S. Steel
]

# 均線參數
SHORT_MA = 5
LONG_MA  = 20

# 每筆交易本金上限（USD）— 買入股數 = int(CAPITAL_PER_TRADE / 當前股價)
CAPITAL_PER_TRADE = 10_000

# 滾動止損：從持倉峰值回落超過此比例即平倉
TRAILING_STOP_PCT = 0.02   # 2%

# 輪詢間隔（秒）
POLL_INTERVAL = 60

# ============================================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

# 試跑模式用的記憶體持倉
_paper_positions: dict[str, dict] = {}
# 各持倉的歷史最高價（用於滾動止損）
_position_peaks: dict[str, float] = {}


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
    BUY  — 5日線上穿20日線（金叉）
    SELL — 5日線下穿20日線（死叉）
    HOLD — 無訊號
    """
    if len(closes) < LONG_MA + 1:
        return "HOLD"
    ma_short = closes.rolling(SHORT_MA).mean()
    ma_long  = closes.rolling(LONG_MA).mean()
    prev_short, curr_short = ma_short.iloc[-2], ma_short.iloc[-1]
    prev_long,  curr_long  = ma_long.iloc[-2],  ma_long.iloc[-1]
    if prev_short <= prev_long and curr_short > curr_long:
        return "BUY"
    if prev_short >= prev_long and curr_short < curr_long:
        return "SELL"
    return "HOLD"


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
        req    = StockLatestBarRequest(symbol_or_symbols=[symbol])
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
    """對 WATCHLIST 中每支股票執行一次策略判斷"""
    try:
        clock = trading_client.get_clock()
    except Exception as e:
        log.warning("取得市場時鐘失敗，跳過本次輪詢：%s", e)
        return

    if not clock.is_open:
        log.info("市場休市，下次開盤：%s", clock.next_open.strftime("%Y-%m-%d %H:%M %Z"))
        return

    for symbol in WATCHLIST:
        try:
            log.info("--- 處理 %s ---", symbol)

            # 1. 滾動止損檢查（優先執行）
            update_trailing_stop(trading_client, data_client, symbol)

            # 2. 抓歷史收盤價 + 均線信號
            closes = get_historical_closes(data_client, symbol, LONG_MA + 5)
            if closes.empty:
                log.warning("%s 無法取得歷史行情，跳過", symbol)
                continue

            ma_signal = compute_signal(closes)
            pos       = get_current_position(trading_client, symbol)

            # 3. 買入邏輯：MA 金叉 + TV 評級雙重確認
            if ma_signal == "BUY" and pos["quantity"] == 0:
                tv      = analyse_stock(symbol)
                decision = "✅ 允許入市" if tv["bullish"] else "❌ 評級不足，跳過"
                log.info(
                    "[分析] %s | TV評級: %s (買:%d 賣:%d) | MA信號: %s | 決策: %s",
                    symbol, tv["tv_rating"], tv["tv_buy_count"], tv["tv_sell_count"],
                    ma_signal, decision,
                )
                if tv["bullish"]:
                    current_price = get_latest_price(data_client, symbol) or closes.iloc[-1]
                    qty = max(1, int(CAPITAL_PER_TRADE / current_price))
                    place_order(trading_client, symbol, OrderSide.BUY, qty, current_price, "TV+MA雙確認買入")
                    _position_peaks[symbol] = current_price  # 初始化峰值

            # 4. 賣出邏輯：MA 死叉
            elif ma_signal == "SELL" and pos["quantity"] > 0:
                log.info("[分析] %s | MA信號: SELL | 決策: ✅ 均線死叉平倉", symbol)
                current_price = get_latest_price(data_client, symbol) or closes.iloc[-1]
                place_order(trading_client, symbol, OrderSide.SELL, pos["quantity"], current_price, "均線死叉賣出")

            else:
                log.info("%s 無操作（MA信號=%s，持倉=%d）", symbol, ma_signal, pos["quantity"])

        except Exception as exc:
            log.error("%s 處理出錯：%s", symbol, exc, exc_info=True)


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
# 試跑驗證
# ----------------------------------------------------------
def trial_run():
    log.info("====== Trial Run 開始（V2）======")

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

    log.info("====== Trial Run 全部通過 ✓（V2）======")
    log.info("PAPER_TRADING = %s | TRAILING_STOP_PCT = %.0f%% | CAPITAL_PER_TRADE = $%d",
             PAPER_TRADING, TRAILING_STOP_PCT * 100, CAPITAL_PER_TRADE)


# ----------------------------------------------------------
# 進入點
# ----------------------------------------------------------
def main():
    api_key    = os.getenv("ALPACA_API_KEY")
    api_secret = os.getenv("ALPACA_API_SECRET")
    if not api_key or not api_secret:
        log.error("請設定環境變數 ALPACA_API_KEY 與 ALPACA_API_SECRET")
        sys.exit(1)

    log.info("=== Alpaca Trading Bot V2 ===")
    log.info("PAPER_TRADING = %s | 滾動止損 = %.0f%% | 每筆本金 = $%d",
             PAPER_TRADING, TRAILING_STOP_PCT * 100, CAPITAL_PER_TRADE)
    log.info("監控清單: %s", WATCHLIST)

    trading_client = TradingClient(api_key, api_secret, paper=PAPER_TRADING)
    data_client    = StockHistoricalDataClient(api_key, api_secret)

    account = trading_client.get_account()
    log.info("帳戶狀態：%s | 可用資金：$%s", account.status, account.buying_power)

    run_duration = 7200  # 2 小時
    end_time     = time.time() + run_duration
    log.info("開始策略輪詢（每 %d 秒），執行時間：2 小時…", POLL_INTERVAL)

    try:
        while time.time() < end_time:
            try:
                run_strategy(trading_client, data_client)
            except Exception as e:
                log.error("策略輪詢出錯（將在 %d 秒後重試）：%s", POLL_INTERVAL, e)
            remaining = end_time - time.time()
            if remaining <= 0:
                break
            time.sleep(min(POLL_INTERVAL, remaining))
    except KeyboardInterrupt:
        log.info("使用者中斷，程式結束。")

    log.info("2 小時交易結束，生成結算報告…")
    _print_summary(trading_client)


if __name__ == "__main__":
    if "--live" in sys.argv:
        main()
    else:
        trial_run()
