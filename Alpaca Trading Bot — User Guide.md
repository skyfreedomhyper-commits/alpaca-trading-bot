---
tags:
  - trading
  - alpaca
  - python
  - automation
created: 2026-05-09
updated: 2026-05-11
version: v2
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
| **v2** (現行) | 2026-05-11 | TradingView 雙重確認、2% 滾動止損、動態本金部位、擴充觀察清單 |
| v1 | 2026-05-09 | 雙均線策略、固定股數、2 小時結算報告 |

---

## Overview

An automated trading bot for **US stocks** using the Alpaca Markets API. V2 adds **TradingView technical rating confirmation** before every entry, and replaces the fixed stop-loss with a **2% trailing stop** that locks in gains as price rises.

| Feature | Detail |
|---|---|
| Strategy | Dual MA crossover (5-day / 20-day) + TradingView rating |
| Entry condition | MA golden cross **AND** TV rating = BUY / STRONG_BUY |
| Watchlist | NFLX · TSLA · X |
| Position sizing | $10,000 per trade → shares = `int(10,000 ÷ price)` |
| Risk control | 2% trailing stop → auto market-close |
| Session length | 2 hours, then prints P&L summary |
| Market hours | Skips polling when US market is closed |
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
- Required packages (all installed):

```
alpaca-py==0.43.4
tradingview_ta==3.3.0
pandas
numpy
python-dotenv
```

To reinstall if needed:
```powershell
pip install alpaca-py tradingview_ta pandas numpy python-dotenv
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

# ── STOCKS TO MONITOR ──────────────────────────────────────
WATCHLIST = [
    "NFLX",   # Netflix
    "TSLA",   # Tesla
    "X",      # U.S. Steel
]

# ── STRATEGY PARAMETERS ────────────────────────────────────
SHORT_MA = 5               # Fast moving average (days)
LONG_MA  = 20              # Slow moving average (days)

# ── POSITION SIZING ────────────────────────────────────────
CAPITAL_PER_TRADE = 10_000 # USD per trade — shares calculated dynamically

# ── RISK CONTROL ───────────────────────────────────────────
TRAILING_STOP_PCT = 0.02   # 2% trailing stop from position peak

# ── POLL INTERVAL ──────────────────────────────────────────
POLL_INTERVAL = 60         # Seconds between each strategy check
```

### Position Sizing Example

| Stock | Price (approx) | Shares bought ($10,000) |
|---|---|---|
| NFLX | ~$1,000 | 10 shares |
| TSLA | ~$200 | 50 shares |
| X | ~$40 | 250 shares |

---

## Running the Bot

### Mode 1 — Trial Run (no API keys needed)

Validates all components locally without connecting to Alpaca:

```powershell
cd "C:\Users\andyc\Claude AC\Claude AC\Python script"
& "C:\Users\andyc\Claude AC\Claude AC\.venv\Scripts\python.exe" -X utf8 alpaca_trading_bot.py
```

**Expected output:**
```
====== Trial Run 開始（V2）======
買入費用: {'佣金': 0.0, 'SEC費': 0.0, ...合計費用': 0.0}
賣出費用: {'佣金': 0.0, 'SEC費': 0.0431, ...}
滾動止損測試：回撤 2.40%，應觸發 True -> True
NFLX TV評級: SELL (買:3 賣:15) 交易所:NASDAQ
====== Trial Run 全部通過 ✓（V2）======
PAPER_TRADING = True | TRAILING_STOP_PCT = 2% | CAPITAL_PER_TRADE = $10000
```

### Mode 2 — Paper Trading (PAPER_TRADING = True, needs API keys)

```powershell
& "C:\Users\andyc\Claude AC\Claude AC\.venv\Scripts\python.exe" -X utf8 alpaca_trading_bot.py --live
```

> [!note] Orders are routed to Alpaca's paper environment — no real money is used. Bot runs for 2 hours then prints P&L summary.

### Mode 3 — Live Trading (PAPER_TRADING = False)

> [!danger] Real Money
> Only switch to live mode after thorough paper trading. Set `PAPER_TRADING = False` and ensure your `.env` contains your **live** API keys.

```powershell
& "C:\Users\andyc\Claude AC\Claude AC\.venv\Scripts\python.exe" -X utf8 alpaca_trading_bot.py --live
```

Stop the bot at any time with **Ctrl + C**.

---

## How the Strategy Works (V2)

```
Every 60 seconds for each stock in WATCHLIST:
  │
  ├─ 1. Market hours check
  │       If US market is closed → skip entire cycle, log next open time
  │
  ├─ 2. Trailing stop check (priority)
  │       Track peak price since entry
  │       (peak − current) / peak ≥ 2%  →  market sell all shares
  │       Peak resets to 0 after any sell
  │
  ├─ 3. Fetch last 25 days of daily closes (Alpaca API)
  │
  ├─ 4. Compute moving averages
  │       5-day MA  &  20-day MA
  │
  └─ 5. Signal + dual confirmation
          MA golden cross (5MA > 20MA)?
            └─ YES → fetch TradingView rating
                       BUY / STRONG_BUY?
                         └─ YES → BUY int($10,000 ÷ price) shares
                         └─ NO  → skip, log "評級不足"
          MA death cross (5MA < 20MA) + holding?
            └─ YES → SELL entire position
          Otherwise → HOLD
```

### Entry Signal Log Format

```
[分析] NFLX | TV評級: BUY (買:12 賣:3) | MA信號: BUY  | 決策: ✅ 允許入市
[分析] TSLA | TV評級: NEUTRAL (買:7 賣:9) | MA信號: BUY | 決策: ❌ 評級不足，跳過
```

### MA Signal Reference

| Yesterday 5MA vs 20MA | Today 5MA vs 20MA | MA Signal |
|---|---|---|
| 5MA ≤ 20MA | 5MA > 20MA | **BUY** (golden cross) |
| 5MA ≥ 20MA | 5MA < 20MA | **SELL** (death cross) |
| No change | No change | HOLD |

---

## TradingView Rating

The bot uses the `tradingview_ta` library to fetch TradingView's daily technical analysis consensus, which aggregates 26 indicators (RSI, MACD, Stochastics, MAs, etc.).

| TV Rating | Meaning | Bot Action |
|---|---|---|
| STRONG_BUY | 15+ bullish indicators | ✅ Entry allowed |
| BUY | Majority bullish | ✅ Entry allowed |
| NEUTRAL | Mixed signals | ❌ Entry blocked |
| SELL | Majority bearish | ❌ Entry blocked |
| STRONG_SELL | 15+ bearish indicators | ❌ Entry blocked |

> [!note] Exchange Auto-Detection
> The bot tries NASDAQ → NYSE → AMEX in order until the symbol resolves. No manual configuration needed.

---

## Trailing Stop Loss

| Item | Detail |
|---|---|
| Trigger | Position peak drops by ≥ 2% |
| Peak tracking | Updates every poll cycle if price rises |
| Action | Immediate market sell of entire position |
| Peak reset | Cleared automatically on any sell (trailing stop or MA signal) |
| Re-entry | Bot will re-enter on next qualifying golden cross + TV signal |

**Example — TSLA position:**
```
Entry price:  $200.00  → peak = $200.00
Next poll:    $210.00  → peak updated to $210.00
Next poll:    $215.00  → peak updated to $215.00
Next poll:    $209.70  → drawdown = (215 - 209.70) / 215 = 2.47% ≥ 2% → SELL
```

---

## Fee Calculations

Alpaca is **commission-free**. Sell orders have small mandatory regulatory fees:

| Fee | Rate | Cap |
|---|---|---|
| SEC fee | $0.0000278 × trade value | — |
| FINRA TAF | $0.000166 × shares | Max $8.30 |

**Example — Sell 50 TSLA shares @ $210:**
- SEC fee: 50 × 210 × 0.0000278 = **$0.29**
- FINRA TAF: 50 × 0.000166 = **$0.008**
- **Total: ~$0.30** (negligible)

Buy orders have **zero fees**.

---

## Monitoring & Logs

All activity is printed to the terminal with timestamps:

```
2026-05-11 09:30:01 [INFO] === Alpaca Trading Bot V2 ===
2026-05-11 09:30:01 [INFO] PAPER_TRADING = True | 滾動止損 = 2% | 每筆本金 = $10000
2026-05-11 09:30:02 [INFO] 帳戶狀態：ACTIVE | 可用資金：$200000
2026-05-11 09:30:03 [INFO] --- 處理 NFLX ---
2026-05-11 09:30:03 [INFO] NFLX 峰值更新：$1023.5000
2026-05-11 09:30:04 [INFO] NFLX 滾動止損監控：當前 $1023.50 | 峰值 $1023.50 | 回撤 0.00%（門檻 2%）
2026-05-11 09:30:05 [INFO] [分析] NFLX | TV評級: BUY (買:14 賣:4) | MA信號: BUY | 決策: ✅ 允許入市
2026-05-11 09:30:05 [INFO] [Paper] BUY NFLX 9 股 @ 1023.5000 | 費用：{...}
```

To save logs to a file:

```powershell
& ".venv\Scripts\python.exe" -X utf8 alpaca_trading_bot.py --live 2>&1 | Tee-Object -FilePath "bot_log.txt"
```

### End-of-Session P&L Summary

After 2 hours the bot automatically prints:
```
============================================================
TRADING SESSION SUMMARY
============================================================
帳戶狀態   : ACTIVE
淨值       : $200,150.00
可用資金   : $189,800.00
------------------------------------------------------------
NFLX  數量=9  平均成本=$1023.50  現價=$1031.20  未實現損益=$69.30
TSLA  無持倉
X     無持倉
============================================================
```

---

## Troubleshooting

| Problem | Cause | Fix |
|---|---|---|
| `ALPACA_API_KEY` not found | Missing `.env` or wrong key names | Check `.env` exists with exact variable names |
| `403 Forbidden` on order | Live key used with paper endpoint | Match key type to `PAPER_TRADING` setting |
| Signal always HOLD | Not enough history (need 21+ trading days) | Check symbol is valid; wait for market data |
| TV rating always UNKNOWN | Network issue or delisted symbol | Check internet connection; verify ticker on TradingView |
| Market closed message | Outside 9:30 AM – 4:00 PM ET | Expected — bot resumes at next market open |
| `alpaca-py` not found | Wrong Python environment | Use `.venv\Scripts\python.exe` explicitly |
| `tradingview_ta` not found | Package not installed | Run `.venv\Scripts\pip.exe install tradingview_ta` |
| Garbled Chinese in terminal | Windows code page | Always run with `python -X utf8` flag |

---

## Quick Reference

```powershell
# Set venv Python path (copy this)
$py = "C:\Users\andyc\Claude AC\Claude AC\.venv\Scripts\python.exe"

# Trial run (no API keys needed)
& $py -X utf8 "C:\Users\andyc\Claude AC\Claude AC\Python script\alpaca_trading_bot.py"

# Paper trade (PAPER_TRADING = True)
& $py -X utf8 "C:\Users\andyc\Claude AC\Claude AC\Python script\alpaca_trading_bot.py" --live

# Live trade (set PAPER_TRADING = False in script first!)
& $py -X utf8 "C:\Users\andyc\Claude AC\Claude AC\Python script\alpaca_trading_bot.py" --live

# Stop the bot
Ctrl + C
```

---

## Related Notes

- [[Longbridge Trading Bot — User Guide]]
- [[Claude + Obsidian Setup]]
- [[n8n]]

---

*Last updated: 2026-05-11 | Version: v2 | SDK: alpaca-py 0.43.4 · tradingview_ta 3.3.0*
