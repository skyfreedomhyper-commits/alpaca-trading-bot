# V8 Baseline

This file marks the V8 baseline of the Webull Trading Bot, captured immediately before the V9 multi-broker refactor begins.

- **Date**: 2026-05-19
- **Last V8 files**:
  - `webull_trading_bot_v8.py` — core bot (1132 lines)
  - `bot_gui_v8.py` — PyQt6 dashboard (1606 lines)
- **Capability**: Webull-only (UAT / Live). Strategy = 5/20 MA crossover + TradingView rating + candlestick confirmation + 2% trailing stop. Quotes via yfinance.
- **Git tag**: `v8-baseline`

V9 introduces a `brokers/` abstraction layer to support Webull and Interactive Brokers (IBKR via `ib_async` + IB Gateway) with a one-click broker switch in the GUI. V8 remains untouched as a fallback.
