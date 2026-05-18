"""
Broker-agnostic data proxy classes — V9.

These wrap raw broker responses into a uniform attribute-access interface so the
GUI and strategy code don't need to know which broker produced the data.

Extracted from bot_gui_v8.py:139-220 (Webull-specific) and generalised: the
constructors accept already-normalised dicts; each broker adapter is responsible
for normalising raw API responses into these dict shapes before constructing
the proxy.
"""
from __future__ import annotations
from datetime import datetime, timezone


def _f(val) -> float:
    try:
        return float(val) if val is not None else 0.0
    except (TypeError, ValueError):
        return 0.0


class ClockProxy:
    """Market clock — Webull/IBKR both derive from Python market-hours helper."""
    def __init__(self, is_open: bool, next_open: datetime):
        self.is_open = bool(is_open)
        self.next_open = next_open


class AccountProxy:
    """Normalised account snapshot."""
    def __init__(self, raw: dict, account_id: str = ""):
        self.equity            = _f(raw.get("equity"))
        self.buying_power      = _f(raw.get("buying_power"))
        self.cash              = _f(raw.get("cash"))
        self.portfolio_value   = _f(raw.get("portfolio_value") or self.equity)
        last_eq = _f(raw.get("last_equity"))
        self.last_equity       = last_eq if last_eq else None
        self.account_number    = (raw.get("account_number") or account_id or "--")
        self.account_type      = raw.get("account_type") or "--"
        status                 = raw.get("status") or "ACTIVE"
        self.status            = str(status).upper() if status else "ACTIVE"
        self.daytrade_count    = raw.get("daytrade_count") or 0
        self.pattern_day_trader = bool(raw.get("pattern_day_trader", False))
        self.trading_blocked    = bool(raw.get("trading_blocked", False))
        self.transfers_blocked  = bool(raw.get("transfers_blocked", False))
        self.account_blocked    = bool(raw.get("account_blocked", False))


class PositionProxy:
    """Normalised position."""
    def __init__(self, raw: dict):
        self.symbol          = str(raw.get("symbol") or "").upper().strip()
        self.qty             = _f(raw.get("qty"))
        self.avg_entry_price = _f(raw.get("avg_entry_price"))
        self.current_price   = _f(raw.get("current_price"))
        self.unrealized_pl   = _f(raw.get("unrealized_pl"))
        self.market_value    = _f(raw.get("market_value") or
                                  (self.qty * self.current_price if self.current_price else 0))
        self.side            = (raw.get("side") or "long").lower()
        cost_basis = self.avg_entry_price * self.qty
        self.unrealized_plpc = (self.unrealized_pl / cost_basis) if cost_basis else 0.0


class OrderProxy:
    """Normalised order history entry."""
    def __init__(self, raw: dict):
        self.symbol      = str(raw.get("symbol") or "").upper().strip()
        self.side        = str(raw.get("side") or "").upper()
        self.status      = str(raw.get("status") or "--").upper()
        self.qty         = _f(raw.get("qty"))
        self.filled_qty  = _f(raw.get("filled_qty"))
        self.filled_avg_price = raw.get("filled_avg_price")
        try:
            self.filled_avg_price = float(self.filled_avg_price) if self.filled_avg_price is not None else None
        except (TypeError, ValueError):
            self.filled_avg_price = None

        ts = raw.get("submitted_at")
        if isinstance(ts, datetime):
            self.submitted_at = ts
        elif ts:
            try:
                ts_int = int(ts)
                unit = 1000 if ts_int > 1_000_000_000_000 else 1
                self.submitted_at = datetime.fromtimestamp(ts_int / unit, tz=timezone.utc)
            except Exception:
                self.submitted_at = None
        else:
            self.submitted_at = None

        self.order_type   = (raw.get("order_type") or "MARKET").upper()
        self.time_in_force = (raw.get("time_in_force") or "DAY").upper()
        self.id           = raw.get("id") or raw.get("order_id") or ""
