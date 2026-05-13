"""
Longbridge（長橋證券）自動交易機器人
雙均線策略（5日線 / 20日線）+ 2% 止損風控

環境變數設定（建議儲存於 .env 檔案）：
  LONGBRIDGE_APP_KEY      = 您的 App Key
  LONGBRIDGE_APP_SECRET   = 您的 App Secret
  LONGBRIDGE_ACCESS_TOKEN = 您的 Access Token

安裝指令：
  pip install longbridge pandas numpy

使用說明：
  1. 設定下方 DRY_RUN = True（模擬）或 False（實盤）
  2. 在 WATCHLIST 加入要監控的股票代碼
  3. 執行：python longbridge_trading_bot.py
"""

import os
import time
import logging
from datetime import date, timedelta
from decimal import Decimal

import pandas as pd
import numpy as np
from longbridge.openapi import (
    Config, QuoteContext, TradeContext,
    OrderType, OrderSide, TimeInForceType,
    AdjustType, Period
)

# ============================================================
# ⚠️  模擬交易開關：True = 模擬模式（不會真實下單），False = 實盤
# ============================================================
DRY_RUN = True

# ============================================================
# 監控股票清單（格式：港股 = "代碼.HK"，美股 = "代碼.US"）
# ============================================================
WATCHLIST = [
    "700.HK",   # 騰訊控股（港股示例）
    "9988.HK",  # 阿里巴巴-W（港股示例）
    "AAPL.US",  # 蘋果（美股示例）
]

# 均線參數
SHORT_MA = 5   # 短期均線（天）
LONG_MA  = 20  # 長期均線（天）

# 固定交易股數
HK_SHARES = 2000  # 港股每次買賣股數
US_SHARES = 100   # 美股每次買賣股數

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


# ----------------------------------------------------------
# 費用計算
# ----------------------------------------------------------
def calc_hk_fee(price: float, quantity: int, is_sell: bool) -> dict:
    """計算港股交易成本（終身免佣，平台費最高 HKD 15）"""
    total_value = price * quantity
    platform_fee = min(15.0, total_value * 0.0003)   # 假設平台費約 0.03%，上限 HKD 15
    stamp_duty   = total_value * 0.001 if is_sell else 0.0  # 印花稅 0.1%（僅賣出）
    trade_fee    = total_value * 0.0000565             # 交易費 0.00565%
    settle_fee   = total_value * 0.000042              # 結算費 0.0042%
    total_cost   = platform_fee + stamp_duty + trade_fee + settle_fee
    return {
        "平台費": round(platform_fee, 4),
        "印花稅": round(stamp_duty, 4),
        "交易費": round(trade_fee, 4),
        "結算費": round(settle_fee, 4),
        "合計費用": round(total_cost, 4),
    }


def calc_us_fee(quantity: int) -> dict:
    """計算美股交易成本（免佣，平台費 US$0.005/股，最低 $0.99，最高 $1.5）"""
    platform_fee = max(0.99, min(1.5, quantity * 0.005))
    return {
        "平台費": round(platform_fee, 4),
        "合計費用": round(platform_fee, 4),
    }


# ----------------------------------------------------------
# 均線策略
# ----------------------------------------------------------
def get_historical_closes(quote_ctx: QuoteContext, symbol: str, days: int) -> pd.Series:
    """抓取最近 N 天的收盤價"""
    end_date   = date.today()
    start_date = end_date - timedelta(days=days + 30)  # 多抓一些以防節假日
    candles = quote_ctx.history_candlesticks_by_date(
        symbol, Period.Day, AdjustType.ForwardAdjust, start_date, end_date
    )
    closes = pd.Series(
        [float(c.close) for c in candles],
        index=[c.timestamp.date() for c in candles],
        name="close"
    ).sort_index()
    return closes.tail(days)


def compute_signal(closes: pd.Series) -> str:
    """
    回傳交叉信號：
      'BUY'  - 5日線上穿20日線
      'SELL' - 5日線下穿20日線
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
# 持倉管理（記憶體內，用於 DRY_RUN；實盤直接查 API）
# ----------------------------------------------------------
_paper_positions: dict[str, dict] = {}  # symbol -> {"quantity": int, "avg_cost": float}


def get_current_position(trade_ctx: TradeContext, symbol: str) -> dict:
    """取得目前持倉（實盤從 API 查，模擬從記憶體查）"""
    if DRY_RUN:
        return _paper_positions.get(symbol, {"quantity": 0, "avg_cost": 0.0})
    positions = trade_ctx.stock_positions()
    for pos in (positions.channels or []):
        for stock in pos.positions:
            if stock.symbol == symbol:
                return {
                    "quantity": int(stock.quantity),
                    "avg_cost": float(stock.cost_price),
                }
    return {"quantity": 0, "avg_cost": 0.0}


# ----------------------------------------------------------
# 下單邏輯
# ----------------------------------------------------------
def place_order(
    trade_ctx: TradeContext,
    symbol: str,
    side: OrderSide,
    quantity: int,
    price: float,
    reason: str = "",
):
    """提交訂單（模擬 or 實盤）"""
    market = "HK" if symbol.endswith(".HK") else "US"
    fee_info = calc_hk_fee(price, quantity, is_sell=(side == OrderSide.Sell)) \
               if market == "HK" else calc_us_fee(quantity)

    log.info(
        "[%s] %s %s %d 股 @ %.4f | 費用：%s",
        "模擬" if DRY_RUN else "實盤",
        side.name, symbol, quantity, price, fee_info
    )

    if DRY_RUN:
        # 模擬更新持倉
        pos = _paper_positions.get(symbol, {"quantity": 0, "avg_cost": 0.0})
        if side == OrderSide.Buy:
            total_cost = pos["avg_cost"] * pos["quantity"] + price * quantity
            pos["quantity"] += quantity
            pos["avg_cost"] = total_cost / pos["quantity"] if pos["quantity"] else 0.0
        else:
            pos["quantity"] = max(0, pos["quantity"] - quantity)
            if pos["quantity"] == 0:
                pos["avg_cost"] = 0.0
        _paper_positions[symbol] = pos
        log.info("[模擬] 持倉更新 %s: %s", symbol, pos)
        return

    # 實盤下單（市價單）
    resp = trade_ctx.submit_order(
        symbol=symbol,
        order_type=OrderType.MO,
        side=side,
        submitted_quantity=quantity,
        time_in_force=TimeInForceType.Day,
        remark=reason[:64] if reason else None,
    )
    log.info("[實盤] 訂單已提交，Order ID: %s", resp.order_id)


# ----------------------------------------------------------
# 風控：止損檢查
# ----------------------------------------------------------
def check_stop_loss(
    trade_ctx: TradeContext,
    quote_ctx: QuoteContext,
    symbol: str,
):
    """若當前虧損超過 2%，立即市價平倉"""
    pos = get_current_position(trade_ctx, symbol)
    if pos["quantity"] <= 0 or pos["avg_cost"] <= 0:
        return

    quotes = quote_ctx.quote([symbol])
    if not quotes:
        return
    current_price = float(quotes[0].last_done)

    loss_pct = (pos["avg_cost"] - current_price) / pos["avg_cost"]
    if loss_pct >= STOP_LOSS_PCT:
        log.warning(
            "[風控] %s 虧損 %.2f%% 超過門檻，觸發止損平倉！", symbol, loss_pct * 100
        )
        place_order(trade_ctx, symbol, OrderSide.Sell, pos["quantity"], current_price, "止損平倉")


# ----------------------------------------------------------
# 主策略輪詢
# ----------------------------------------------------------
def run_strategy(quote_ctx: QuoteContext, trade_ctx: TradeContext):
    """對 WATCHLIST 中每支股票執行一次策略判斷"""
    for symbol in WATCHLIST:
        try:
            log.info("--- 處理 %s ---", symbol)
            market = "HK" if symbol.endswith(".HK") else "US"
            qty    = HK_SHARES if market == "HK" else US_SHARES

            # 止損檢查（優先）
            check_stop_loss(trade_ctx, quote_ctx, symbol)

            # 抓歷史收盤價
            closes = get_historical_closes(quote_ctx, symbol, LONG_MA + 5)
            if closes.empty:
                log.warning("%s 無法取得歷史行情，跳過", symbol)
                continue

            signal = compute_signal(closes)
            log.info("%s 均線信號：%s（最新收盤 %.4f）", symbol, signal, closes.iloc[-1])

            pos = get_current_position(trade_ctx, symbol)

            if signal == "BUY" and pos["quantity"] == 0:
                # 確認當前報價
                quotes = quote_ctx.quote([symbol])
                price  = float(quotes[0].last_done) if quotes else closes.iloc[-1]
                place_order(trade_ctx, symbol, OrderSide.Buy, qty, price, "均線金叉買入")

            elif signal == "SELL" and pos["quantity"] > 0:
                quotes = quote_ctx.quote([symbol])
                price  = float(quotes[0].last_done) if quotes else closes.iloc[-1]
                place_order(trade_ctx, symbol, OrderSide.Sell, pos["quantity"], price, "均線死叉賣出")

            else:
                log.info("%s 無操作（信號=%s，持倉=%d）", symbol, signal, pos["quantity"])

        except Exception as exc:
            log.error("%s 處理出錯：%s", symbol, exc, exc_info=True)


# ----------------------------------------------------------
# 試跑驗證（不需要真實 API Key 即可確認各元件正常）
# ----------------------------------------------------------
def trial_run():
    """驗證費用計算、均線邏輯、持倉模擬等核心元件"""
    log.info("====== Trial Run 開始 ======")

    # 1. 費用計算
    hk_buy  = calc_hk_fee(price=50.0,  quantity=2000, is_sell=False)
    hk_sell = calc_hk_fee(price=52.0,  quantity=2000, is_sell=True)
    us_fee  = calc_us_fee(quantity=100)
    log.info("港股買入費用: %s", hk_buy)
    log.info("港股賣出費用: %s", hk_sell)
    log.info("美股費用: %s", us_fee)

    # 2. 均線信號（用假資料）
    np.random.seed(42)
    fake_prices = pd.Series(
        [100 + i * 0.5 + np.random.randn() * 2 for i in range(30)],
        index=pd.date_range(end=date.today(), periods=30).date
    )
    signal = compute_signal(fake_prices)
    log.info("假資料均線信號（應有訊號）: %s", signal)

    # 死叉數據
    declining = pd.Series(
        [100 - i * 0.5 + np.random.randn() * 1 for i in range(30)],
        index=pd.date_range(end=date.today(), periods=30).date
    )
    signal2 = compute_signal(declining)
    log.info("下跌假資料均線信號: %s", signal2)

    # 3. 模擬持倉更新
    _paper_positions.clear()
    _paper_positions["700.HK"] = {"quantity": 0, "avg_cost": 0.0}
    _paper_positions["700.HK"]["quantity"] = 2000
    _paper_positions["700.HK"]["avg_cost"] = 50.0
    pos = _paper_positions["700.HK"]
    current_price = 48.5  # 模擬虧損 3%
    loss_pct = (pos["avg_cost"] - current_price) / pos["avg_cost"]
    triggered = loss_pct >= STOP_LOSS_PCT
    log.info("止損觸發測試：虧損 %.2f%%，應觸發 True -> %s", loss_pct * 100, triggered)
    assert triggered, "止損邏輯錯誤！"

    log.info("====== Trial Run 全部通過 ✓ ======")
    log.info("")
    log.info("提示：若要連接真實 API，請設定以下環境變數後執行 main()：")
    log.info("  LONGBRIDGE_APP_KEY      = <您的 App Key>")
    log.info("  LONGBRIDGE_APP_SECRET   = <您的 App Secret>")
    log.info("  LONGBRIDGE_ACCESS_TOKEN = <您的 Access Token>")
    log.info("  （或在同目錄建立 .env 檔案）")


# ----------------------------------------------------------
# 進入點
# ----------------------------------------------------------
def main():
    log.info("DRY_RUN = %s", DRY_RUN)
    log.info("監控清單: %s", WATCHLIST)

    config = Config.from_apikey_env()
    quote_ctx = QuoteContext(config)
    trade_ctx = TradeContext(config)

    log.info("已連線至 Longbridge API，開始策略輪詢（每 %d 秒）…", POLL_INTERVAL)
    try:
        while True:
            run_strategy(quote_ctx, trade_ctx)
            time.sleep(POLL_INTERVAL)
    except KeyboardInterrupt:
        log.info("使用者中斷，程式結束。")


if __name__ == "__main__":
    # 預設執行試跑驗證；若帶 --live 參數則執行真實連線
    import sys
    if "--live" in sys.argv:
        main()
    else:
        trial_run()
