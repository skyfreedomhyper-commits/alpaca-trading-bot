"""
WebullBroker — V9 adapter wrapping webull-openapi-python-sdk v2.0.7.

Behaviour matches V8 (webull_trading_bot_v8.py + bot_gui_v8.py) but exposes
the BrokerAdapter interface so V9 strategy/GUI code is broker-agnostic.
"""
from __future__ import annotations
import os
import uuid
import logging
from typing import Optional

import yfinance as yf

# Patch Webull SDK token check (matches V8 monkey-patch in webull_trading_bot_v8.py:47-48)
from webull.core.http.initializer.client_initializer import ClientInitializer  # type: ignore
ClientInitializer._check_token_enable = staticmethod(lambda _: False)

from webull.core.client import ApiClient  # type: ignore
from webull.trade.trade_client import TradeClient  # type: ignore

from .base import BrokerAdapter
from .proxies import AccountProxy, PositionProxy, OrderProxy

log = logging.getLogger(__name__)

TRADE_ENDPOINT_PAPER = "us-openapi-alb.uat.webullbroker.com"
TRADE_ENDPOINT_LIVE  = "api.webull.com"


# ── raw-response parsers (lifted from webull_trading_bot_v8.py:177-225) ──
def _parse_positions(resp) -> list[dict]:
    try:
        data = resp.json()
        return (
            (data.get("data") or {}).get("positions") or
            data.get("positions") or
            (data.get("data") if isinstance(data.get("data"), list) else None) or
            []
        )
    except Exception as e:
        log.warning("解析持倉回應失敗：%s", e)
        return []


def _parse_account_balance(resp) -> dict:
    try:
        data = resp.json()
        d = data.get("data") or data
        if isinstance(d, list) and d:
            d = d[0]
        return {
            "equity":          d.get("netLiquidation") or d.get("equity") or d.get("totalValue") or "0",
            "buying_power":    d.get("buyingPower") or d.get("cashBalance") or d.get("availableFunds") or "0",
            "cash":            d.get("totalCashValue") or d.get("cashBalance") or d.get("cash") or "0",
            "portfolio_value": d.get("grossPositionValue") or d.get("portfolioValue") or "0",
            "status":          d.get("accountStatus") or d.get("status") or "ACTIVE",
            "account_number":  d.get("accountId") or d.get("id") or "",
            "account_type":    d.get("accountType") or d.get("type") or "--",
            "daytrade_count":  d.get("dayTradeCount") or d.get("dayTradingCount") or 0,
            "pattern_day_trader": bool(d.get("patternDayTrader", False)),
            "trading_blocked":    bool(d.get("tradingBlocked", False)),
            "transfers_blocked":  bool(d.get("transfersBlocked", False)),
            "account_blocked":    bool(d.get("accountBlocked", False)),
            "last_equity":     d.get("lastEquity") or d.get("previousEquity"),
        }
    except Exception as e:
        log.warning("解析帳戶餘額失敗：%s", e)
        return {"equity": "0", "buying_power": "0", "cash": "0",
                "portfolio_value": "0", "status": "UNKNOWN"}


def _parse_orders(resp) -> list[dict]:
    try:
        data = resp.json()
        return (
            (data.get("data") or {}).get("orders") or
            data.get("orders") or
            (data.get("data") if isinstance(data.get("data"), list) else None) or
            []
        )
    except Exception as e:
        log.warning("解析訂單歷史失敗：%s", e)
        return []


def _normalise_position(raw: dict) -> dict:
    sym = (raw.get("symbol") or raw.get("tickerSymbol") or raw.get("ticker") or "")
    return {
        "symbol": sym.upper().strip(),
        "qty": raw.get("position") or raw.get("quantity") or raw.get("qty") or raw.get("holdingQty"),
        "avg_entry_price": (raw.get("averageCost") or raw.get("avgCost") or
                            raw.get("costPrice") or raw.get("cost") or raw.get("avgEntryPrice")),
        "current_price": (raw.get("lastPrice") or raw.get("currentPrice") or
                          raw.get("close") or raw.get("marketPrice")),
        "unrealized_pl": (raw.get("unrealizedPnl") or raw.get("unrealizedPL") or
                          raw.get("openPnl") or raw.get("pnl")),
        "market_value": raw.get("marketValue") or raw.get("positionValue"),
        "side": raw.get("side") or "long",
    }


def _normalise_order(raw: dict) -> dict:
    sym = raw.get("symbol") or raw.get("tickerSymbol") or ""
    ts = (raw.get("createTime") or raw.get("submitTime") or
          raw.get("placeTime") or raw.get("createTimestamp"))
    return {
        "symbol": sym.upper().strip(),
        "side": raw.get("side") or raw.get("action") or "",
        "status": raw.get("status") or raw.get("orderStatus") or "--",
        "qty": raw.get("quantity") or raw.get("qty"),
        "filled_qty": raw.get("filledQuantity") or raw.get("filledQty"),
        "filled_avg_price": raw.get("avgFilledPrice") or raw.get("filledAvgPrice"),
        "submitted_at": ts,
        "order_type": raw.get("orderType") or raw.get("order_type") or "MARKET",
        "time_in_force": raw.get("timeInForce") or "DAY",
        "id": raw.get("orderId") or raw.get("clientOrderId") or "",
    }


class WebullBroker(BrokerAdapter):
    name = "webull"

    def __init__(self, paper: bool = True):
        self.paper = paper
        self._client: Optional[TradeClient] = None
        self._account_id: str = ""

    @property
    def account_id(self) -> str:
        return self._account_id

    def connect(self) -> None:
        app_key    = os.getenv("WEBULL_APP_KEY", "")
        app_secret = os.getenv("WEBULL_APP_SECRET", "")
        if not app_key or not app_secret:
            raise RuntimeError("Missing WEBULL_APP_KEY / WEBULL_APP_SECRET in environment")

        endpoint = TRADE_ENDPOINT_PAPER if self.paper else TRADE_ENDPOINT_LIVE
        api = ApiClient(app_key, app_secret, "us")
        api.add_endpoint("us", endpoint)
        api._stream_logger_set = True
        api._file_logger_set   = True
        self._client = TradeClient(api)

        try:
            resp = self._client.account_v2.get_account_list()
            raw  = resp.json()
            accounts = raw.get("data") or raw.get("accounts") or []
            if isinstance(accounts, list) and accounts:
                first = accounts[0]
                self._account_id = (first.get("accountId") or first.get("account_id") or
                                    first.get("id") or "")
        except Exception as e:
            log.warning("Webull get_account_list 失敗：%s", e)

        log.info("Webull broker connected | endpoint=%s | account=%s",
                 endpoint, self._account_id or "未知")

    def disconnect(self) -> None:
        self._client = None
        self._account_id = ""

    def get_account(self) -> AccountProxy:
        if not self._client:
            return AccountProxy({}, "")
        try:
            resp = self._client.account_v2.get_account_balance(self._account_id)
            bal  = _parse_account_balance(resp)
            return AccountProxy(bal, self._account_id)
        except Exception as e:
            log.warning("取得帳戶餘額失敗：%s", e)
            return AccountProxy({}, self._account_id)

    def get_positions(self) -> list[PositionProxy]:
        if not self._client:
            return []
        try:
            resp = self._client.account_v2.get_account_position(self._account_id)
            return [PositionProxy(_normalise_position(p)) for p in _parse_positions(resp)]
        except Exception as e:
            log.warning("取得持倉失敗：%s", e)
            return []

    def get_orders(self, limit: int = 50) -> list[OrderProxy]:
        if not self._client:
            return []
        try:
            resp = self._client.order_v2.get_order_history_list(self._account_id, page_size=limit)
            return [OrderProxy(_normalise_order(o)) for o in _parse_orders(resp)]
        except Exception as e:
            log.warning("取得訂單歷史失敗：%s", e)
            return []

    def get_position(self, symbol: str) -> dict:
        if not self._client:
            return {"quantity": 0, "avg_cost": 0.0}
        try:
            resp = self._client.account_v2.get_account_position(self._account_id)
            for p in _parse_positions(resp):
                pos_sym = (p.get("symbol") or p.get("ticker") or "").upper()
                if pos_sym == symbol.upper():
                    qty  = float(p.get("qty") or p.get("quantity") or p.get("holdingQty") or 0)
                    cost = float(p.get("avgCost") or p.get("costPrice") or
                                 p.get("avgEntryPrice") or p.get("avg_cost") or 0)
                    return {"quantity": qty, "avg_cost": cost}
        except Exception:
            pass
        return {"quantity": 0, "avg_cost": 0.0}

    def place_order(self, symbol: str, side: str, qty: int,
                    price: float, reason: str = "") -> dict:
        if not self._client:
            raise RuntimeError("Webull broker not connected")
        order_dict = {
            "combo_type":              "NORMAL",
            "client_order_id":         str(uuid.uuid4()),
            "symbol":                  symbol,
            "instrument_type":         "STOCK",
            "market":                  "US",
            "order_type":              "MARKET",
            "quantity":                str(qty),
            "support_trading_session": "CORE",
            "side":                    side,
            "time_in_force":           "DAY",
            "entrust_type":            "QTY",
        }
        resp = self._client.order_v2.place_order(self._account_id, [order_dict])
        try:
            body = resp.json()
        except Exception:
            body = {}
        log.info("[Webull %s] 訂單已提交 %s %s %s @ %.4f | 原因：%s | 回應：%s",
                 "UAT" if self.paper else "LIVE", side, symbol, qty, price, reason,
                 str(body)[:200])
        return body

    def get_latest_price(self, symbol: str) -> Optional[float]:
        try:
            hist = yf.Ticker(symbol).history(period="2d", auto_adjust=True)
            if not hist.empty:
                return float(hist["Close"].iloc[-1])
        except Exception as e:
            log.warning("%s yfinance 即時報價失敗：%s", symbol, e)
        return None
