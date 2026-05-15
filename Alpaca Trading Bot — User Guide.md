---
tags:
  - trading
  - alpaca
  - python
  - automation
created: 2026-05-09
updated: 2026-05-15
version: v3.1
status: active
---

# Alpaca Trading Bot — User Guide

> [!info] Script Location
> `C:\Users\andyc\Claude AC\Claude AC\Python script\alpaca_trading_bot.py`
> GitHub: `https://github.com/skyfreedomhyper-commits/alpaca-trading-bot`

---

## Version History

| 版本 | 日期 | 主要更新 |
|---|---|---|
| **v3.1** (現行) | 2026-05-15 | 修復 `trading_view_backtest_bot.py` 語法錯誤（escaped quotes 導致 SyntaxError） |
| v4 | 2026-05-13 | 24/7 加密貨幣策略、持倉監察至平倉、GUI Dashboard、瞬斷錯誤分類、單一 log 檔 |
| v3 | 2026-05-12 | 開市前自動選股（5年回測評分）、watchlist.json 共用狀態、IEX 資料覆蓋修正（X→NVDA） |
| v2 | 2026-05-11 | TradingView 雙重確認、2% 滾動止損、動態本金部位、擴充觀察清單 |
| v1 | 2026-05-09 | 雙均線策略、固定股數、2 小時結算報告 |

---

## Overview

An automated trading bot for **US stocks and cryptocurrencies** using the Alpaca Markets API. V4 adds a **24/7 crypto strategy**, **continuous position monitoring**, a **PyQt6 GUI dashboard**, and pre-market stock screener from V3.

| Feature | Detail |
|---|---|
| Stock strategy | Dual MA crossover (5-day / 20-day) + TradingView rating |
| Stock entry | MA golden cross **AND** TV rating = BUY / STRONG_BUY, within 30-day window |
| Stock watchlist | NFLX · TSLA · NVDA (updated dynamically by pre-market screener) |
| Crypto strategy | Same dual MA + TradingView, runs **24/7** regardless of US market hours |
| Crypto watchlist | BTC/USD · ETH/USD · SOL/USD · AVAX/USD · LINK/USD |
| Position sizing | $10,000 per stock trade · $1,000 per crypto trade |
| Risk control | 2% trailing stop on all positions → auto market-close |
| Position monitoring | Held positions (stock & crypto) remain monitored until flat, even if removed from watchlist |
| Pre-market screener | Runs before market open — scores 200+ stocks on 5-year backtests, refreshes WATCHLIST |
| GUI Dashboard | `bot_gui.py` — PyQt6 dashboard with live log panel, account info, positions |
| Log file | Single `bot_log.txt` (overwritten each run) |
| Default mode | **Alpaca Paper Trading (PAPER_TRADING = True)** |

---

## Prerequisites

### 1. Alpaca Account & API Access

1. Sign up at [alpaca.markets](https://alpaca.markets) (free)
2. Navigate to **Paper Trading → API Keys** to generate keys for paper trading
3. For live trading, switch to **Live Trading → API Keys**
4. You will get two credentials:
   - `API Key ID`
   - `API Secret Key`

> [!warning] Keep Credentials Secret
> Never commit your `.env` file to git or share these keys. They grant full trading access to your account.

> [!tip] Paper vs Live Keys
> Alpaca issues **separate keys** for paper and live accounts. The `PAPER_TRADING = True` flag in the script controls which endpoint is used — make sure the key in your `.env` matches the mode you are running.

### 2. Python Environment

- Python **3.10+**
- Required packages:

```
alpaca-py==0.43.4
tradingview_ta==3.3.0
pandas
numpy
python-dotenv
PyQt6          ← required for bot_gui.py only
```

To install / reinstall:
```powershell
pip install alpaca-py tradingview_ta pandas numpy python-dotenv PyQt6
```

---

## Setup: Environment Variables

Create a file named **`.env`** in the same folder as the script:

```
C:\Users\andyc\Claude AC\Claude AC\Python script\.env
```

```dotenv
ALPACA_API_KEY=your_api_key_id_here
ALPACA_API_SECRET=your_api_secret_key_here
```

> [!tip] The bot auto-loads `.env` via `python-dotenv` — no extra code needed.

---

## Configuration (Inside the Script)

Open `alpaca_trading_bot.py` and adjust these settings near the top:

```python
# ── SAFETY SWITCH ──────────────────────────────────────────
PAPER_TRADING = True       # True = paper account | False = live account

# ── STOCKS TO MONITOR (updated by pre-market screener) ─────
WATCHLIST = [
    "NFLX",   # Netflix
    "TSLA",   # Tesla
    "NVDA",   # NVIDIA
]

# ── CRYPTO WATCHLIST (24/7, always active) ─────────────────
CRYPTO_WATCHLIST = [
    "BTC/USD", "ETH/USD", "SOL/USD", "AVAX/USD", "LINK/USD"
]

# ── STRATEGY PARAMETERS ────────────────────────────────────
SHORT_MA   = 5             # Fast moving average (days)
LONG_MA    = 20            # Slow moving average (days)
BUY_WINDOW = 30            # MA cross valid within N days

# ── POSITION SIZING ────────────────────────────────────────
CAPITAL_PER_TRADE        = 10_000   # USD per stock trade
CAPITAL_PER_CRYPTO_TRADE = 1_000    # USD per crypto trade

# ── RISK CONTROL ───────────────────────────────────────────
TRAILING_STOP_PCT = 0.02   # 2% trailing stop from position peak

# ── POLL INTERVALS ─────────────────────────────────────────
POLL_INTERVAL        = 15    # Seconds between stock strategy checks
CRYPTO_POLL_INTERVAL = 300   # Seconds between crypto strategy checks (5 min)
```

> [!note] Watchlist symbols must be on the IEX data feed. Some tickers (e.g. X/U.S.Steel) are not covered — the pre-market screener avoids these automatically.

---

## Files in This Project

| File | Purpose |
|---|---|
| `alpaca_trading_bot.py` | Main bot — stock + crypto strategy, screener, position monitoring |
| `bot_gui.py` | PyQt6 GUI Dashboard — start/stop bot, live log, account & positions |
| `run_bot.ps1` | PowerShell launcher — runs bot and tees output to `bot_log.txt` |
| `watchlist.json` | Shared state — screener writes stock picks here; GUI reads from here |
| `bot_log.txt` | Single overwriting log file for the current session |
| `.env` | API keys — **never commit this file** |

---

## Running the Bot

### Option A — PowerShell Launcher (Recommended)

Uses `run_bot.ps1` to start the bot and save all output to `bot_log.txt`:

```powershell
& "C:\Users\andyc\Claude AC\Claude AC\.venv\Scripts\powershell.exe" -File "C:\Users\andyc\Claude AC\Claude AC\Python script\run_bot.ps1"
```

Or double-click the **Alpaca Trading Bot** desktop shortcut.

### Option B — GUI Dashboard

Start the PyQt6 dashboard which can launch/stop the bot and display the live log:

```powershell
$py = "C:\Users\andyc\Claude AC\Claude AC\.venv\Scripts\python.exe"
& $py -X utf8 "C:\Users\andyc\Claude AC\Claude AC\Python script\bot_gui.py"
```

Or double-click the **Alpaca Dashboard** desktop shortcut.

### Option C — Direct Python (Terminal)

```powershell
$py = "C:\Users\andyc\Claude AC\Claude AC\.venv\Scripts\python.exe"

# Trial run (validates all components, no API keys needed)
& $py -X utf8 "C:\Users\andyc\Claude AC\Claude AC\Python script\alpaca_trading_bot.py"

# Paper / Live trading
& $py -X utf8 "C:\Users\andyc\Claude AC\Claude AC\Python script\alpaca_trading_bot.py" --live
```

Stop the bot at any time with **Ctrl + C**.

> [!note] Always use `-X utf8` flag on Windows to ensure correct Chinese character encoding in logs.

---

## How the Strategy Works (V4)

### Stock Strategy (Market Hours Only)

```
Every 15 seconds for each symbol in WATCHLIST ∪ held stock positions:
  │
  ├─ 1. Market hours check
  │       US market closed → sleep until next open
  │       (Crypto strategy continues running regardless)
  │
  ├─ 2. Pre-market screener (runs once before open)
  │       Scores 200+ stocks on 5-year MA backtest performance
  │       Top picks replace WATCHLIST, written to watchlist.json
  │
  ├─ 3. Position monitoring expansion
  │       Any currently held stock NOT in WATCHLIST is added to
  │       this cycle's monitor list (sell/trailing stop only — no new buys)
  │
  ├─ 4. Trailing stop check (priority over entry signals)
  │       Track peak price since entry
  │       (peak − current) / peak ≥ 2%  →  market sell all shares
  │
  ├─ 5. Fetch last 25 days of daily closes (Alpaca IEX feed)
  │
  ├─ 6. Compute moving averages: 5-day MA & 20-day MA
  │
  └─ 7. Signal + dual confirmation (entry only if in WATCHLIST)
          MA golden cross within last 30 days?
            └─ YES → fetch TradingView rating (NASDAQ → NYSE → AMEX)
                       BUY / STRONG_BUY?
                         └─ YES → BUY int($10,000 ÷ price) shares
                         └─ NO  → skip, log "評級不足"
          MA death cross + holding?
            └─ YES → SELL entire position (integer shares, TimeInForce.DAY)
          Otherwise → HOLD
```

### Crypto Strategy (24/7, Every 5 Minutes)

```
Every 300 seconds for each symbol in CRYPTO_WATCHLIST ∪ held crypto positions:
  │
  ├─ 1. No market-hours gate — crypto trades around the clock
  │
  ├─ 2. Position monitoring expansion
  │       Any held crypto NOT in CRYPTO_WATCHLIST is monitored
  │       for trailing stop / death cross (no new buys)
  │
  ├─ 3. Trailing stop check
  │       Same 2% peak-drawdown logic
  │       Sells float quantity via TimeInForce.GTC
  │
  ├─ 4. Fetch last 25 days of daily crypto bars (no DataFeed filter)
  │
  ├─ 5. Compute 5-day MA & 20-day MA
  │
  └─ 6. Signal + TradingView confirmation
          TradingView screener="crypto", tries COINBASE then BINANCE
          Entry: float qty = round($1,000 ÷ price, 8) — fractional allowed
          Orders use TimeInForce.GTC (required for crypto)
```

### Symbol Format Notes (Crypto)

| Context | Format | Example |
|---|---|---|
| CRYPTO_WATCHLIST / orders | `BASE/QUOTE` | `BTC/USD` |
| Position lookup (Alpaca) | Concatenated | `BTCUSD` |
| TradingView screener | Concatenated | `BTCUSD` |

---

## Pre-Market Screener

Before each market open, the bot runs `screen_stocks()` which:

1. Pulls 5 years of daily closes for 200+ candidate symbols
2. Simulates the 5/20 MA strategy on historical data
3. Scores each symbol by: win rate × Sharpe ratio × trade frequency
4. Selects the top-N symbols and writes them to `watchlist.json`
5. The live strategy then reads from `watchlist.json` each cycle

```
[選股] 開始篩選 200+ 支股票...
[選股] NVDA | 均量 42,830,000 | 5y回測 32勝9負 | Sharpe 1.84 | 得分 58.9
[選股] 篩選完成 → 今日觀察清單：['NFLX', 'NVDA', 'TSLA']
[選股] 選股結果已同步至 watchlist.json
```

> [!note] watchlist.json is also read by `bot_gui.py` to display the current watchlist in the dashboard.

---

## Continuous Position Monitoring

V4 ensures positions are never abandoned:

- **Stock**: If you hold TSLA but the screener drops it from WATCHLIST, TSLA is still monitored every 15 seconds. The trailing stop and death-cross sell logic run normally. A new buy will NOT be triggered (only watchlist symbols can generate buys).
- **Crypto**: Same logic — held crypto not in CRYPTO_WATCHLIST remains monitored for trailing stop and death cross.

```
[持倉監察] 以下持倉不在觀察清單，加入本輪監察直至平倉：['TSLA']
```

Positions are dropped from extra monitoring once quantity reaches zero.

---

## GUI Dashboard (`bot_gui.py`)

A standalone PyQt6 application providing a graphical view of the bot.

| Panel | Content |
|---|---|
| Account | Equity, buying power, paper/live mode, account status |
| Market Status | Open/closed indicator, session hours, next open countdown |
| Watchlist | Current watchlist from `watchlist.json` with live prices and % change |
| Open Positions | Symbol, shares/qty, avg cost, current price, unrealised P&L |
| Live Log (right panel) | Real-time stdout from bot process — INFO/WARNING/ERROR colour-coded |

**Controls:**
- `▶ 啟動 Bot` — launches `alpaca_trading_bot.py --live` as a subprocess
- `⏹ 停止 Bot` — gracefully terminates the bot process
- `🗑 清除 Log` — clears the log panel

Dashboard refreshes every **30 seconds** automatically.

---

## TradingView Rating

| TV Rating | Meaning | Bot Action |
|---|---|---|
| STRONG_BUY | 15+ bullish indicators | ✅ Entry allowed |
| BUY | Majority bullish | ✅ Entry allowed |
| NEUTRAL | Mixed signals | ❌ Entry blocked |
| SELL | Majority bearish | ❌ Entry blocked |
| STRONG_SELL | 15+ bearish indicators | ❌ Entry blocked |

Stock analysis: tries NASDAQ → NYSE → AMEX in order.
Crypto analysis: tries COINBASE → BINANCE in order.

---

## Trailing Stop Loss

| Item | Detail |
|---|---|
| Trigger | Position peak drops by ≥ 2% |
| Peak tracking | Updates every poll cycle if price rises |
| Action | Immediate market sell of entire position |
| Peak reset | Cleared automatically on any sell |
| Re-entry | Bot will re-enter on next qualifying signal |
| Applies to | Both stock and crypto positions |

**Example — TSLA:**
```
Entry:   $200.00 → peak = $200.00
Poll +1: $210.00 → peak updated to $210.00
Poll +2: $215.00 → peak updated to $215.00
Poll +3: $209.70 → drawdown = (215 − 209.70) / 215 = 2.47% ≥ 2% → SELL
```

---

## Fee Calculations

Alpaca is **commission-free**. Sell orders have small mandatory regulatory fees:

| Fee | Rate | Cap |
|---|---|---|
| SEC fee | $0.0000278 × trade value | — |
| FINRA TAF | $0.000166 × shares | Max $8.30 |

**Example — Sell 50 TSLA @ $210:**
- SEC fee: 50 × 210 × 0.0000278 = **$0.29**
- FINRA TAF: 50 × 0.000166 = **$0.008**
- **Total: ~$0.30** (negligible)

Crypto orders: fees are embedded in the spread — Alpaca charges no explicit commission.

---

## Monitoring & Logs

All activity is written to **`bot_log.txt`** (overwritten each run) and printed to terminal:

```
2026-05-13 09:30:01 [INFO] === Alpaca Trading Bot V4 ===
2026-05-13 09:30:01 [INFO] PAPER_TRADING = True | 滾動止損 = 2%
2026-05-13 09:30:02 [INFO] 帳戶狀態：ACTIVE | 可用資金：$200,000.00
2026-05-13 09:30:03 [INFO] [選股] 開始篩選 200+ 支股票...
2026-05-13 09:30:10 [INFO] [選股] 篩選完成 → 今日觀察清單：['NVDA', 'TSLA', 'NFLX']
2026-05-13 09:30:11 [INFO] --- 處理 NVDA ---
2026-05-13 09:30:12 [INFO] [分析] NVDA | TV評級: BUY (買:14 賣:4) | MA信號: BUY | 決策: ✅ 允許入市
2026-05-13 09:30:12 [INFO] [Paper] BUY NVDA 9 股 @ 1023.50
2026-05-13 09:30:13 [INFO] [Crypto] BTC/USD | MA信號: HOLD | 持倉：0.00 BTC
2026-05-13 09:30:13 [WARNING] 網路瞬斷，將在 15 秒後重試：RemoteDisconnected(...)
```

Log colour coding in GUI dashboard:
- `[INFO]` → white
- `[WARNING]` → yellow
- `[ERROR]` → red
- Timestamps → grey

> [!tip] Transient network errors (`RemoteDisconnected`, connection aborted) are classified as WARNING — the bot self-heals on the next poll and no action is needed.

---

## Troubleshooting

| Problem | Cause | Fix |
|---|---|---|
| `ALPACA_API_KEY` not found | Missing `.env` or wrong key names | Check `.env` exists with exact variable names |
| `403 Forbidden` on order | Live key used with paper endpoint | Match key type to `PAPER_TRADING` setting |
| Signal always HOLD | Not enough history (need 21+ trading days) | Check symbol is valid; wait for market data |
| TV rating always UNKNOWN | Network issue or delisted symbol | Check internet connection; verify ticker on TradingView |
| Market closed message | Outside 9:30 AM – 4:00 PM ET | Expected — stock strategy resumes at next open; crypto continues |
| `alpaca-py` not found | Wrong Python environment | Use `.venv\Scripts\python.exe` explicitly |
| `tradingview_ta` not found | Package not installed | Run `.venv\Scripts\pip.exe install tradingview_ta` |
| Garbled Chinese in terminal | Windows code page | Always run with `python -X utf8` flag |
| Symbol not found on IEX | Ticker not in IEX data feed (~8,000 US stocks covered) | Replace with a major-exchange symbol; screener avoids these automatically |
| Crypto order rejected | Wrong `TimeInForce` or integer qty | Bot uses `GTC` and float quantities — verify SDK version `alpaca-py ≥ 0.43` |
| `get_all_positions` error | Outdated SDK version | Upgrade: `pip install --upgrade alpaca-py` |
| `[WARNING] 網路瞬斷` | Transient TCP disconnect from Alpaca servers | Self-healing — no action needed; bot retries next poll |
| GUI dashboard blank panels | API keys not loaded | Ensure `.env` is in the Python script folder |
| PyQt6 import error | PyQt6 not installed | `pip install PyQt6` |

---

## Quick Reference

```powershell
$py  = "C:\Users\andyc\Claude AC\Claude AC\.venv\Scripts\python.exe"
$dir = "C:\Users\andyc\Claude AC\Claude AC\Python script"

# Trial run (no API keys needed — validates all imports and logic)
& $py -X utf8 "$dir\alpaca_trading_bot.py"

# Paper / Live trading (direct)
& $py -X utf8 "$dir\alpaca_trading_bot.py" --live

# GUI Dashboard (recommended — launch/stop bot from GUI)
& $py -X utf8 "$dir\bot_gui.py"

# PowerShell launcher (saves log to bot_log.txt automatically)
& "$dir\run_bot.ps1"

# Stop the bot
Ctrl + C   (terminal)   or   ⏹ Stop Bot   (GUI)
```

---

## Related Notes

- [[Longbridge Trading Bot — User Guide]]
- [[Claude + Obsidian Setup]]
- [[n8n]]

---

*Last updated: 2026-05-15 | Version: v3.1 | SDK: alpaca-py 0.43.4 · tradingview_ta 3.3.0 · PyQt6*
