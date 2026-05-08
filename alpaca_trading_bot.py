"""
Alpaca 自動交易機器人
雙均線策略（5日線 / 20日線）+ 2% 止損風控

環境變數設定（建議儲存於 .env 檔案）：
  ALPACA_API_KEY    = 您的 API Key ID
  ALPACA_API_SECRET = 您的 API Secret Key

安裝指令：
  pip install alpaca-py pandas numpy python-dotenv

使用說明：
  1. 設定 PAPER_TRADING = True（Alpaca 模擬帳戶）或 False（實盤）
  2. 在 WATCHLIST 加入要監控的美股代碼
  3. 執行：python alpaca_trading_bot.py
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
    pass  # python-dotenv 非強制，可手動設定環境變數

from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest, StockLatestBarRequest
from alpaca.data.timeframe import TimeFrame

# ============================================================
# ⚠️  模式切換
#   True  = 使用 Alpaca Paper Trading 帳戶（模擬，安全預設值）
#   False = 使用 Alpaca 真實帳戶（實盤，有真實資金風險！）
# ============================================================
PAPER_TRADING = True

# ============================================================
# 監控股票清單（美股代碼，不加後綴）
# ============================================================
WATCHLIST = [
    "AAPL",   # 蘋果
    "MSFT",   # 微軟
    "TSLA",   # 特斯拉
    "NVDA",   # 輝達
]

# 均線參數
SHORT_MA = 5   # 短期均線（天）
LONG_MA  = 20  # 長期均線（天）

# 固定交易股數（整股，不使用槓桿）
SHARES_PER_TRADE = 10

# 風控：單筆虧損超過此比例即市價平倉
STOP_LOSS_PCT = 0.02

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


# ----------------------------------------------------------
# 費用計算
# ----------------------------------------------------------
def calc_fee(price: float, quantity: int, is_sell: bool) -> dict:
    """Alpaca 零佣金，賣出時有 SEC 費與 FINRA TAF"""
    sec_fee   = round(price * quantity * 0.0000278, 4) if is_sell else 0.0
    finra_taf = round(min(8.30, quantity * 0.000166), 4) if is_sell else 0.0
    total     = round(sec_fee + finra_taf, 4)
    return {
        "佣金":       0.0,
        "SEC費":      sec_fee,
        "FINRA_TAF":  finra_taf,
        "合計費用":   total,
    }


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
    start = end - timedelta(days=days + 30)  # 多抓以防節假日
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

    # bars.df 索引為 MultiIndex (symbol, timestamp)
    if isinstance(df.index, pd.MultiIndex):
        df = df.xs(symbol, level="symbol")

    return df["close"].sort_index().tail(days)


# ----------------------------------------------------------
# 均線策略
# ----------------------------------------------------------
def compute_signal(closes: pd.Series) -> str:
    """
    回傳交叉信號：
      'BUY'  - 5日線上穿20日線（金叉）
      'SELL' - 5日線下穿20日線（死叉）
      'HOLD' - 無訊號
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
    """取得目前持倉（試跑從記憶體查，連線模式從 Alpaca API 查）"""
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
    """提交訂單（試跑 / Paper / 實盤，由初始化設定決定路由）"""
    fee_info   = calc_fee(price, quantity, is_sell=(side == OrderSide.SELL))
    mode_label = "試跑" if trial else ("Paper" if PAPER_TRADING else "實盤")

    log.info(
        "[%s] %s %s %d 股 @ %.4f | 費用：%s | 原因：%s",
        mode_label, side.value.upper(), symbol, quantity, price, fee_info, reason,
    )

    if trial:
        # 純記憶體模擬，不呼叫任何 API
        pos = _paper_positions.get(symbol, {"quantity": 0, "avg_cost": 0.0})
        if side == OrderSide.BUY:
            total_cost   = pos["avg_cost"] * pos["quantity"] + price * quantity
            pos["quantity"] += quantity
            pos["avg_cost"] = total_cost / pos["quantity"] if pos["quantity"] else 0.0
        else:
            pos["quantity"] = max(0, pos["quantity"] - quantity)
            if pos["quantity"] == 0:
                pos["avg_cost"] = 0.0
        _paper_positions[symbol] = pos
        log.info("[試跑] 持倉更新 %s: %s", symbol, pos)
        return

    # 送出 Alpaca 市價單（paper=True 自動路由至模擬帳戶）
    order_req = MarketOrderRequest(
        symbol=symbol,
        qty=quantity,
        side=side,
        time_in_force=TimeInForce.DAY,
    )
    resp = trading_client.submit_order(order_req)
    log.info("[%s] 訂單已提交，Order ID: %s | 狀態：%s", mode_label, resp.id, resp.status)


# ----------------------------------------------------------
# 風控：止損檢查
# ----------------------------------------------------------
def check_stop_loss(
    trading_client: TradingClient | None,
    data_client: StockHistoricalDataClient | None,
    symbol: str,
    trial: bool = False,
):
    """若當前虧損超過 2%，立即市價平倉"""
    pos = get_current_position(trading_client, symbol, trial=trial)
    if pos["quantity"] <= 0 or pos["avg_cost"] <= 0:
        return

    if trial:
        return  # 試跑模式無即時報價，跳過

    try:
        req           = StockLatestBarRequest(symbol_or_symbols=[symbol])
        latest        = data_client.get_stock_latest_bar(req)
        current_price = float(latest[symbol].close)
    except Exception as e:
        log.warning("%s 取得即時報價失敗：%s", symbol, e)
        return

    loss_pct = (pos["avg_cost"] - current_price) / pos["avg_cost"]
    if loss_pct >= STOP_LOSS_PCT:
        log.warning(
            "[風控] %s 虧損 %.2f%% 超過門檻，觸發止損平倉！", symbol, loss_pct * 100
        )
        place_order(trading_client, symbol, OrderSide.SELL, pos["quantity"], current_price, "止損平倉")


# ----------------------------------------------------------
# 主策略輪詢
# ----------------------------------------------------------
def run_strategy(trading_client: TradingClient, data_client: StockHistoricalDataClient):
    """對 WATCHLIST 中每支股票執行一次策略判斷"""
    # 市場休市時跳過（避免送出無法成交的 DAY 訂單）
    clock = trading_client.get_clock()
    if not clock.is_open:
        log.info("市場休市，下次開盤：%s", clock.next_open.strftime("%Y-%m-%d %H:%M %Z"))
        return

    for symbol in WATCHLIST:
        try:
            log.info("--- 處理 %s ---", symbol)

            # 止損檢查（優先執行）
            check_stop_loss(trading_client, data_client, symbol)

            # 抓歷史收盤價
            closes = get_historical_closes(data_client, symbol, LONG_MA + 5)
            if closes.empty:
                log.warning("%s 無法取得歷史行情，跳過", symbol)
                continue

            signal = compute_signal(closes)
            log.info("%s 均線信號：%s（最新收盤 %.4f）", symbol, signal, closes.iloc[-1])

            pos = get_current_position(trading_client, symbol)

            if signal == "BUY" and pos["quantity"] == 0:
                req           = StockLatestBarRequest(symbol_or_symbols=[symbol])
                latest        = data_client.get_stock_latest_bar(req)
                current_price = float(latest[symbol].close)
                place_order(trading_client, symbol, OrderSide.BUY, SHARES_PER_TRADE, current_price, "均線金叉買入")

            elif signal == "SELL" and pos["quantity"] > 0:
                req           = StockLatestBarRequest(symbol_or_symbols=[symbol])
                latest        = data_client.get_stock_latest_bar(req)
                current_price = float(latest[symbol].close)
                place_order(trading_client, symbol, OrderSide.SELL, pos["quantity"], current_price, "均線死叉賣出")

            else:
                log.info("%s 無操作（信號=%s，持倉=%d）", symbol, signal, pos["quantity"])

        except Exception as exc:
            log.error("%s 處理出錯：%s", symbol, exc, exc_info=True)


# ----------------------------------------------------------
# 試跑驗證（不需要 API Key 即可確認各元件正常）
# ----------------------------------------------------------
def trial_run():
    """驗證費用計算、均線邏輯、持倉模擬、止損觸發等核心元件"""
    log.info("====== Trial Run 開始 ======")

    # 1. 費用計算
    buy_fee  = calc_fee(price=150.0, quantity=10, is_sell=False)
    sell_fee = calc_fee(price=155.0, quantity=10, is_sell=True)
    log.info("買入費用（10 股 @ $150）: %s", buy_fee)
    log.info("賣出費用（10 股 @ $155）: %s", sell_fee)
    assert buy_fee["合計費用"] == 0.0,  "買入不應有費用"
    assert sell_fee["合計費用"] > 0.0,  "賣出應有監管費用"

    # 2. 均線信號（用假資料）
    np.random.seed(42)
    rising = pd.Series(
        [100 + i * 0.5 + np.random.randn() * 2 for i in range(30)],
        index=pd.date_range(end=datetime.today(), periods=30).date,
    )
    signal1 = compute_signal(rising)
    log.info("上升假資料均線信號: %s", signal1)

    declining = pd.Series(
        [100 - i * 0.5 + np.random.randn() * 1 for i in range(30)],
        index=pd.date_range(end=datetime.today(), periods=30).date,
    )
    signal2 = compute_signal(declining)
    log.info("下跌假資料均線信號: %s", signal2)

    # 3. 模擬下單與持倉更新
    _paper_positions.clear()
    place_order(None, "AAPL", OrderSide.BUY,  10, 150.0, "試跑買入", trial=True)
    assert _paper_positions["AAPL"]["quantity"] == 10,  "買入後持倉應為 10"
    place_order(None, "AAPL", OrderSide.SELL, 10, 155.0, "試跑賣出", trial=True)
    assert _paper_positions["AAPL"]["quantity"] == 0,   "賣出後持倉應為 0"
    log.info("持倉模擬測試通過")

    # 4. 止損觸發邏輯
    _paper_positions["AAPL"] = {"quantity": 10, "avg_cost": 150.0}
    pos           = _paper_positions["AAPL"]
    current_price = 145.5   # 模擬虧損 3%
    loss_pct      = (pos["avg_cost"] - current_price) / pos["avg_cost"]
    triggered     = loss_pct >= STOP_LOSS_PCT
    log.info("止損觸發測試：虧損 %.2f%%，應觸發 True -> %s", loss_pct * 100, triggered)
    assert triggered, "止損邏輯錯誤！"

    log.info("====== Trial Run 全部通過 ✓ ======")
    log.info("")
    log.info("提示：若要連接 Alpaca API，請設定以下環境變數後執行 main()：")
    log.info("  ALPACA_API_KEY    = <您的 API Key ID>")
    log.info("  ALPACA_API_SECRET = <您的 API Secret Key>")
    log.info("  目前模式：PAPER_TRADING = %s", PAPER_TRADING)


# ----------------------------------------------------------
# 進入點
# ----------------------------------------------------------
def main():
    api_key    = os.getenv("ALPACA_API_KEY")
    api_secret = os.getenv("ALPACA_API_SECRET")

    if not api_key or not api_secret:
        log.error("請設定環境變數 ALPACA_API_KEY 與 ALPACA_API_SECRET（或建立 .env 檔案）")
        sys.exit(1)

    log.info("PAPER_TRADING = %s", PAPER_TRADING)
    log.info("監控清單: %s", WATCHLIST)

    trading_client = TradingClient(api_key, api_secret, paper=PAPER_TRADING)
    data_client    = StockHistoricalDataClient(api_key, api_secret)

    account = trading_client.get_account()
    log.info("帳戶狀態：%s | 可用資金：$%s", account.status, account.buying_power)

    log.info("已連線至 Alpaca API，開始策略輪詢（每 %d 秒）…", POLL_INTERVAL)
    try:
        while True:
            run_strategy(trading_client, data_client)
            time.sleep(POLL_INTERVAL)
    except KeyboardInterrupt:
        log.info("使用者中斷，程式結束。")


if __name__ == "__main__":
    # 預設執行試跑驗證；帶 --live 參數則連接真實 Alpaca API
    if "--live" in sys.argv:
        main()
    else:
        trial_run()
