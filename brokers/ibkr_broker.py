"""
IbkrBroker — V9 adapter using ib_async against a local IB Gateway / TWS.

Default ports: Paper 4002 (Gateway) / 7497 (TWS), Live 4001 / 7496.
We default to IB Gateway paper (4002). Override via IBKR_HOST / IBKR_PORT /
IBKR_CLIENT_ID env vars.

Requirements:
    pip install ib_async
    IB Gateway running locally, API enabled, 127.0.0.1 trusted, Read-Only API off.
"""
from __future__ import annotations
import os
import logging
from typing import Optional

import yfinance as yf

from .base import BrokerAdapter
from .proxies import AccountProxy, PositionProxy, OrderProxy

log = logging.getLogger(__name__)


class IbkrBroker(BrokerAdapter):
    name = "ibkr"

    def __init__(self, paper: bool = True):
        self.paper = paper
        self._ib = None
        self._account_id: str = ""

        # ib_async imports deferred to connect() so that --broker webull does
        # not require ib_async to be installed.

    @property
    def account_id(self) -> str:
        return self._account_id

    def _default_port(self) -> int:
        # IB Gateway: 4002 paper / 4001 live
        return 4002 if self.paper else 4001

    def connect(self) -> None:
        try:
            from ib_async import IB  # type: ignore
        except ImportError as e:
            raise RuntimeError(
                "ib_async is not installed. Run: pip install ib_async"
            ) from e

        host      = os.getenv("IBKR_HOST", "127.0.0.1")
        port      = int(os.getenv("IBKR_PORT", str(self._default_port())))
        client_id = int(os.getenv("IBKR_CLIENT_ID", "11"))

        self._ib = IB()
        self._ib.connect(host, port, clientId=client_id)
        managed = self._ib.managedAccounts()
        self._account_id = managed[0] if managed else ""
        log.info("IBKR broker connected | %s:%s clientId=%s | account=%s",
                 host, port, client_id, self._account_id or "未知")

    def disconnect(self) -> None:
        if self._ib is not None:
            try:
                self._ib.disconnect()
            except Exception:
                pass
        self._ib = None
        self._account_id = ""

    def get_account(self) -> AccountProxy:
        if self._ib is None or not self._account_id:
            return AccountProxy({}, "")
        try:
            summary = self._ib.accountSummary(self._account_id)
            tags = {item.tag: item.value for item in summary}
            raw = {
                "equity":          tags.get("NetLiquidation") or tags.get("EquityWithLoanValue") or "0",
                "buying_power":    tags.get("BuyingPower") or tags.get("AvailableFunds") or "0",
                "cash":            tags.get("TotalCashValue") or tags.get("CashBalance") or "0",
                "portfolio_value": tags.get("GrossPositionValue") or tags.get("NetLiquidation") or "0",
                "status":          "ACTIVE",
                "account_number":  self._account_id,
                "account_type":    tags.get("AccountType") or "--",
                "daytrade_count":  tags.get("DayTradesRemaining") or 0,
            }
            return AccountProxy(raw, self._account_id)
        except Exception as e:
            log.warning("IBKR get_account 失敗：%s", e)
            return AccountProxy({}, self._account_id)

    def get_positions(self) -> list[PositionProxy]:
        if self._ib is None:
            return []
        try:
            ib_positions = self._ib.positions()
            out: list[PositionProxy] = []
            for p in ib_positions:
                sym = getattr(p.contract, "symbol", "") or ""
                current = self.get_latest_price(sym) or 0.0
                qty = float(p.position)
                avg = float(p.avgCost)
                mv  = qty * current if current else 0.0
                upl = (current - avg) * qty if current else 0.0
                out.append(PositionProxy({
                    "symbol": sym,
                    "qty": qty,
                    "avg_entry_price": avg,
                    "current_price": current,
                    "unrealized_pl": upl,
                    "market_value": mv,
                    "side": "long" if qty >= 0 else "short",
                }))
            return out
        except Exception as e:
            log.warning("IBKR get_positions 失敗：%s", e)
            return []

    def get_orders(self, limit: int = 50) -> list[OrderProxy]:
        if self._ib is None:
            return []
        try:
            trades = list(self._ib.trades())[-limit:]
            out: list[OrderProxy] = []
            for t in trades:
                contract = getattr(t, "contract", None)
                order    = getattr(t, "order", None)
                status   = getattr(t, "orderStatus", None)
                out.append(OrderProxy({
                    "symbol": getattr(contract, "symbol", "") if contract else "",
                    "side": getattr(order, "action", "") if order else "",
                    "status": getattr(status, "status", "") if status else "",
                    "qty": getattr(order, "totalQuantity", 0) if order else 0,
                    "filled_qty": getattr(status, "filled", 0) if status else 0,
                    "filled_avg_price": getattr(status, "avgFillPrice", None) if status else None,
                    "order_type": getattr(order, "orderType", "MARKET") if order else "MARKET",
                    "time_in_force": getattr(order, "tif", "DAY") if order else "DAY",
                    "id": getattr(order, "orderId", "") if order else "",
                }))
            return out
        except Exception as e:
            log.warning("IBKR get_orders 失敗：%s", e)
            return []

    def get_position(self, symbol: str) -> dict:
        if self._ib is None:
            return {"quantity": 0, "avg_cost": 0.0}
        try:
            for p in self._ib.positions():
                if getattr(p.contract, "symbol", "").upper() == symbol.upper():
                    return {"quantity": float(p.position), "avg_cost": float(p.avgCost)}
        except Exception:
            pass
        return {"quantity": 0, "avg_cost": 0.0}

    def place_order(self, symbol: str, side: str, qty: int,
                    price: float, reason: str = "") -> dict:
        if self._ib is None:
            raise RuntimeError("IBKR broker not connected")
        from ib_async import Stock, MarketOrder  # type: ignore

        contract = Stock(symbol, "SMART", "USD")
        self._ib.qualifyContracts(contract)
        order = MarketOrder("BUY" if side.upper() == "BUY" else "SELL", int(qty))
        trade = self._ib.placeOrder(contract, order)
        log.info("[IBKR %s] 訂單已提交 %s %s %s @ %.4f | 原因：%s | order_id=%s",
                 "PAPER" if self.paper else "LIVE", side, symbol, qty, price, reason,
                 getattr(order, "orderId", "?"))
        return {
            "order_id": getattr(order, "orderId", ""),
            "status": getattr(getattr(trade, "orderStatus", None), "status", ""),
        }

    def get_latest_price(self, symbol: str) -> Optional[float]:
        # Use yfinance (matches V8/Webull behaviour, avoids IBKR market-data subscription requirement)
        try:
            hist = yf.Ticker(symbol).history(period="2d", auto_adjust=True)
            if not hist.empty:
                return float(hist["Close"].iloc[-1])
        except Exception as e:
            log.warning("%s yfinance 即時報價失敗：%s", symbol, e)
        return None
