---
tags:
  - audit
  - python
  - streamlit
  - GL
  - sampling
created: 2026-05-11
status: active
---

# Audit Sampling Tool — User Guide

> [!info] Script Location
> `C:\Users\andyc\Claude AC\Claude AC\Python script\deepseek_python_20260508_4699c2.py`

---

## Overview

A Streamlit web app for auditors that combines **GL data cleanup**, **Substantive Analytical Procedures (SAP)**, and **statistical sampling** in one tool. Designed for large GL datasets (50,000+ entries) where no prior-period comparatives are available.

| Feature | Detail |
|---|---|
| GL Cleanup | Auto-detect headers, merged cells, blank columns, currency symbols |
| SAP | Monthly standard deviation anomaly detection per account |
| Sampling | Mandatory (≥ materiality) + random/systematic/MUS for remainder |
| Export | Cleaned GL Excel + audit sample workpaper Excel |
| Language | Traditional Chinese UI |

---

## How to Run

```powershell
python -m streamlit run "C:\Users\andyc\Claude AC\Claude AC\Python script\deepseek_python_20260508_4699c2.py"
```

Then open **http://localhost:8501** in your browser.

> [!warning] Use `python -m streamlit run`, not bare `streamlit run`
> Streamlit is installed in the Windows Store Python path which is not on the system PATH.

---

## Tab 0 — GL Excel 清理 (GL Cleanup)

Use this tab first if your GL export is messy (Sage 50, PeachTree, SAP, etc.).

### Step-by-step

1. **Upload** your raw GL file (`.xlsx`, `.xls`, or `.csv`)
2. **Preview** — the first 15 rows are shown with 1-based row numbers
3. **Set header row** — the tool auto-detects the header row; override manually if needed
4. Click **套用清理設定** — this:
   - Skips rows above the header
   - Drops columns that are >80% empty
   - Forward-fills merged cells (resolves blank cells from merged cell formatting)
5. **Map columns**:
   - Voucher / reference number column (used to identify and remove subtotal rows)
   - Amount: either a single amount column **or** separate Debit / Credit columns
   - Date column
   - Account column (optional)
6. Click **執行欄位清理 & 移除雜訊列** — this:
   - Strips currency symbols (`NT$`, `HK$`, `$`, `¥`, `£`, `€`) and commas
   - Converts accounting negatives `(1,234.56)` → `-1234.56`
   - Removes rows where voucher is blank or matches keywords: `合計 小計 總計 Total Grand Sub-total`
7. **Download** cleaned Excel (`gl_cleaned.xlsx`) **or** click **➡️ 傳送並前往 Step 1~2** to pass the data directly to Tab 1

---

## Tab 1 — SAP + 抽樣

### Path A: Data from Tab 0 Cleanup

A green banner confirms the data was received. Column dropdowns are pre-populated from the cleanup mapping. Skip to **Audit Parameters**.

### Path B: Direct Upload

1. Upload an Excel or CSV file
2. Set the **header row number** (default: row 1)
3. **Debit/Credit mode** — check if your GL has separate debit and credit columns; the tool combines them into a single signed `金額` column
4. **Fill down account name** — check this for Sage 50 / PeachTree formats where the account name only appears on the first row of each account group; select the column to forward-fill
5. Map: voucher column, amount column, date column, account column

---

### Audit Parameters

| Parameter | Description |
|---|---|
| **重大性門檻** | Materiality threshold — transactions ≥ this amount are mandatory samples |
| **SAP 異常靈敏度** | Anomaly sensitivity — see table below |

#### SAP Sensitivity Options

| Option | Threshold | Use When |
|---|---|---|
| 高（±1σ，較嚴格） | ±1 std dev | High-risk accounts, tight review |
| 中（±2σ，標準）| ±2 std dev | Standard audit (default) |
| 低（±3σ，較寬鬆） | ±3 std dev | Low-risk, high volatility accounts |
| 無（停用 SAP）| — | When no SAP is required |

Click **載入並執行實質性分析程序** to run SAP.

---

### Step 2 — Sampling

#### Account Filter
Select specific accounts with the **會計科目篩選** multiselect. Leave empty to include all accounts.

#### Sampling Logic

| Group | Criteria | Method |
|---|---|---|
| **必抽** (Mandatory) | \|Amount\| ≥ Materiality threshold | All included automatically |
| **隨機抽樣** (Random) | Remaining population | Selected method below |

#### Sampling Methods

| Method | Description |
|---|---|
| 隨機抽樣 | Simple random sampling |
| 系統抽樣 | Systematic (every N-th item) |
| MUS | Monetary Unit Sampling — probability proportional to absolute amount |

Set the **抽樣率** (sample rate %) and **亂數種子** (random seed, for reproducibility), then click **執行抽樣**.

---

## Output Excel

Downloaded as `audit_sample_result.xlsx` with two sheets:

| Sheet | Contents |
|---|---|
| 抽樣結果 | All sampled transactions with `抽樣類別`, `抽樣方法`, `查核結果` (blank, for auditor to fill), `抽樣日期`, `亂數種子` |
| 抽樣工作底稿 | Summary: accounts selected, population size, materiality, sample counts, method, date, seed |

---

## GL Format Compatibility

| System | Recommended Approach |
|---|---|
| Sage 50 / PeachTree | Tab 1 direct upload → enable **Fill down account name** |
| SAP export (raw) | Tab 0 cleanup → set correct header row |
| QuickBooks / Xero CSV | Tab 1 direct upload, usually no cleanup needed |
| Custom Excel with merged cells | Tab 0 cleanup → ffill handles merged cells automatically |
| Debit/Credit separate columns | Either tab → enable **借貸分欄模式** |

---

## Troubleshooting

| Issue | Fix |
|---|---|
| `KeyError: '預期下限'` | Fixed in current version (2026-05-11) |
| Chinese characters appear as boxes in charts | Font issue with matplotlib; chart labels may need English |
| Amount column shows NaN | Check currency symbols or text in the amount field; use Tab 0 cleanup |
| `streamlit: not recognized` | Use `python -m streamlit run` instead |
| App not refreshing after code change | Streamlit hot-reloads automatically; if stuck, press R in the browser |

---

## Dependencies

```
streamlit
pandas
numpy
matplotlib
openpyxl
```

Install with:
```powershell
pip install streamlit pandas numpy matplotlib openpyxl
```
