# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Environment

- **Python virtual environment**: `C:\Users\andyc\Claude AC\Claude AC\.venv\Scripts\python.exe`
- **Run with UTF-8**: Always use `python -X utf8` to avoid encoding issues with Chinese log output on Windows.
- **Credentials**: Stored in `.env` (gitignored). Required vars: `ALPACA_API_KEY`, `ALPACA_API_SECRET`.

## Common Commands

```powershell
# Activate venv (if needed for pip installs)
& "C:\Users\andyc\Claude AC\Claude AC\.venv\Scripts\Activate.ps1"

# Install dependencies
pip install alpaca-py pandas numpy python-dotenv tradingview_ta PyQt6 matplotlib

# Run trial/dry-run (no API connection, no real orders)
python -X utf8 alpaca_trading_bot.py

# Run live bot (Alpaca Paper Trading)
python -X utf8 alpaca_trading_bot.py --live

# Run live bot (real money — use with caution)
python -X utf8 alpaca_trading_bot.py --live --real

# Run portfolio backtest (365 days default)
python -X utf8 alpaca_trading_bot.py --backtest
python -X utf8 alpaca_trading_bot.py --backtest --days 180

# Launch GUI dashboard
python -X utf8 bot_gui.py

# Run via PowerShell script (logs to bot_log.txt)
.\run_bot.ps1

# Syntax check after edits
python -m py_compile alpaca_trading_bot.py
python -m py_compile bot_gui.py
```

## Architecture Overview

This is a single-file-per-concern design. There is intentional duplication between the bot and backtest modules — they share strategy parameters but are kept independent.

### Files

| File | Purpose |
|---|---|
| `alpaca_trading_bot.py` | Core bot: strategy logic, order execution, trial run, backtest engine, `main()` |
| `bot_gui.py` | PyQt6 GUI dashboard: tabbed interface, background workers, candlestick charts |
| `trading_view_backtest_bot.py` | Standalone backtest-only script (mirrors strategy params from the bot) |
| `watchlist.json` | Shared state: GUI writes this; bot reads it every poll cycle to update `WATCHLIST` |
| `peaks.json` | Persistent trailing-stop peak prices, survives restarts |
| `settings.json` | GUI-managed settings (not tracked in git); read by bot at startup via `load_settings()` |
| `.env` | API credentials (gitignored) |
| `run_bot.ps1` | PowerShell launcher that tees stdout to `bot_log.txt` |

### Bot strategy flow (`alpaca_trading_bot.py`)

Entry signals require **triple confirmation** — all three must agree before buying:
1. **MA crossover** (`compute_signal`): 5-day MA crosses above 20-day MA within the last `BUY_WINDOW` (30) days
2. **TradingView rating** (`analyse_stock`): must be `BUY` or `STRONG_BUY` (tries NASDAQ → NYSE → AMEX)
3. **Candlestick pattern** (`has_recent_candle_signal`): bullish pattern detected within the last `CANDLE_LOOKBACK` (3) bars

Exit signals:
- **MA death cross** (`compute_signal` returns `SELL`): closes position immediately
- **Trailing stop** (`update_trailing_stop`): closes position if price falls ≥ `TRAILING_STOP_PCT` (2%) from `_position_peaks` high; peaks persisted in `peaks.json`

The main loop runs every `POLL_INTERVAL` (10s). Crypto runs in parallel on its own `CRYPTO_POLL_INTERVAL` (10s) — crypto continues during stock market close. Pre-market screening runs once per trading day using `SCREEN_CANDIDATES` list.

### GUI architecture (`bot_gui.py`)

Two `QThread` workers drive the dashboard:
- **`RefreshWorker`**: runs every 10 seconds; fetches account, clock, positions, orders, and latest bar prices from Alpaca. Emits `done` signal → `_on_data()` updates all tabs.
- **`BacktestWorker`**: runs on demand; delegates to `alpaca_trading_bot` backtest functions, emits `progress` lines and a final `done` dict for chart rendering.

The bot subprocess (`QProcess`) is launched from the GUI's Start/Stop button — it runs `alpaca_trading_bot.py --live` as a child process and pipes its stdout/stderr into the GUI log panel.

The GUI reads `watchlist.json` at startup and writes it on every add/remove. The bot reads the same file on every poll cycle — this is the sole IPC mechanism between GUI and bot process.

### Crypto symbol normalization

Crypto symbols must be in `BTC/USD` format internally. The function `_normalize_crypto_symbol()` converts Alpaca position format (`BTCUSD`) to the canonical slash format. This runs in both the bot and the GUI worker on every symbol before routing to stock vs. crypto data clients.

### Key constants (top of `alpaca_trading_bot.py`)

`PAPER_TRADING`, `WATCHLIST`, `CRYPTO_WATCHLIST`, `SHORT_MA` (5), `LONG_MA` (20), `BUY_WINDOW` (30), `CAPITAL_PER_TRADE`, `TRAILING_STOP_PCT` (0.02), `POLL_INTERVAL` (10), `CANDLE_LOOKBACK` (3).

## Important Patterns

- **Trial run** (`trial=True` parameter): `place_order()` and `get_current_position()` operate on `_paper_positions` dict in memory instead of calling Alpaca APIs. The default `main()` invocation with no args runs the trial.
- **`_data_failed_symbols`**: Set of symbols where data fetch failed this session — used to suppress repeated warnings per poll cycle, not across restarts.
- **TradingView fallback**: If TV rating is `UNKNOWN` (network failure), `tv_ok` is set to `True` so the MA + candle signals alone can trigger entry. This is intentional — TV is confirmation, not a gate.
- **Log file**: `bot_log.txt` is written in `mode="w"` (overwrite) on each startup. Historical logs are not retained unless captured externally (e.g., `run_bot.ps1` appends).
