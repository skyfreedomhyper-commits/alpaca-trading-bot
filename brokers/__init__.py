"""
brokers — V9 broker abstraction layer.

Usage:
    from brokers import get_broker
    broker = get_broker("webull", paper=True)   # or "ibkr"
    broker.connect()
    account = broker.get_account()       # AccountProxy
    positions = broker.get_positions()   # list[PositionProxy]
    broker.place_order("AAPL", "BUY", 10, 195.0, "test")
    broker.disconnect()
"""

from .base import BrokerAdapter
from .proxies import AccountProxy, PositionProxy, OrderProxy, ClockProxy


def get_broker(name: str, paper: bool = True) -> BrokerAdapter:
    name = (name or "").lower()
    if name == "webull":
        from .webull_broker import WebullBroker
        return WebullBroker(paper=paper)
    if name == "ibkr":
        from .ibkr_broker import IbkrBroker
        return IbkrBroker(paper=paper)
    raise ValueError(f"Unknown broker: {name!r}. Expected 'webull' or 'ibkr'.")


__all__ = [
    "BrokerAdapter",
    "AccountProxy", "PositionProxy", "OrderProxy", "ClockProxy",
    "get_broker",
]
