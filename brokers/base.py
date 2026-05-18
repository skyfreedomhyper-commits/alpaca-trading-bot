"""
BrokerAdapter — abstract base class for V9 broker integrations.

Every broker (Webull, IBKR, ...) implements this interface. The strategy code
and GUI code only depend on this ABC, never on broker-specific SDKs.
"""
from __future__ import annotations
from abc import ABC, abstractmethod
from datetime import datetime, timedelta, timezone

from .proxies import AccountProxy, PositionProxy, OrderProxy, ClockProxy


def _is_us_market_open() -> bool:
    """美股市場是否開盤（EST/EDT 9:30–16:00，週一至五）"""
    now = datetime.now(timezone.utc)
    if now.weekday() >= 5:
        return False
    is_edt = 3 <= now.month <= 11
    open_h, open_m   = (13, 30) if is_edt else (14, 30)
    close_h, close_m = (20,  0) if is_edt else (21,  0)
    open_dt  = now.replace(hour=open_h,  minute=open_m,  second=0, microsecond=0)
    close_dt = now.replace(hour=close_h, minute=close_m, second=0, microsecond=0)
    return open_dt <= now < close_dt


def _next_us_market_open() -> datetime:
    """下一個美股開盤時間（UTC）"""
    now = datetime.now(timezone.utc)
    for days_ahead in range(8):
        dt = now + timedelta(days=days_ahead)
        if dt.weekday() >= 5:
            continue
        is_edt = 3 <= dt.month <= 11
        open_h = 13 if is_edt else 14
        open_dt = dt.replace(hour=open_h, minute=30, second=0, microsecond=0)
        if open_dt > now:
            return open_dt
    return now + timedelta(hours=24)


class BrokerAdapter(ABC):
    """Common interface across brokers. Subclasses set name + paper in __init__."""
    name: str = "base"
    paper: bool = True

    @abstractmethod
    def connect(self) -> None: ...

    @abstractmethod
    def disconnect(self) -> None: ...

    def get_clock(self) -> ClockProxy:
        """Default: US equities clock (Webull and IBKR both trade US stocks here)."""
        return ClockProxy(is_open=_is_us_market_open(), next_open=_next_us_market_open())

    @abstractmethod
    def get_account(self) -> AccountProxy: ...

    @abstractmethod
    def get_positions(self) -> list[PositionProxy]: ...

    @abstractmethod
    def get_orders(self, limit: int = 50) -> list[OrderProxy]: ...

    @abstractmethod
    def get_position(self, symbol: str) -> dict:
        """Return {"quantity": float, "avg_cost": float} for one symbol (0/0 if none)."""

    @abstractmethod
    def place_order(self, symbol: str, side: str, qty: int,
                    price: float, reason: str = "") -> dict: ...

    @abstractmethod
    def get_latest_price(self, symbol: str) -> float | None: ...

    @property
    def account_id(self) -> str:
        return ""
