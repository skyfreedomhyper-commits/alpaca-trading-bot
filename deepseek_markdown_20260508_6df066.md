# 角色
你是一位熟悉 Longbridge（長橋證券）OpenAPI 的量化交易工程師。

# 任務
請幫我撰寫一個基於 Longbridge 證券 API 的自動交易腳本（實盤下單邏輯必須包含模擬/實盤切換），滿足以下要求：

1. **平台與市場**：使用 Longbridge Python SDK（長橋 OpenAPI），交易市場為港股與美股。
2. **策略邏輯**：實作一個簡單的雙均線交叉策略（5日線 / 20日線），當 5日線上穿 20日線時買入，下穿時賣出。
3. **資金管理**：
   - 每次交易使用「固定股數」模式（港股：2000股，美股：100股）
   - 不開槓桿，僅用賬戶現金餘額交易
4. **成本計算**：
   - **港股**：平臺費每單上限 HKD 15，免除佣金（終身免佣）；賣出需繳印花稅（成交金額 0.1%）、交易費（0.00565%）、結算費（0.0042%）等[reference:2][reference:3]。`
   - **美股**：免除佣金；平臺費 US$0.005/股（最低 US$0.99，最高 US$1.5）[reference:4]。`
5. **模組要求**：
   - 行情連線與即時報價獲取（`longbridge.QuoteContext`）
   - 下單邏輯（`longbridge.TradeContext`），需能選擇 **真實下單** 或 **模擬模式**
   - 訂單狀態查詢與風險控制：單筆虧損超過 2% 即市價平倉
6. **輸出內容**：
   - 完整可運行的 Python 程式碼（需包含 Longbridge SDK 安裝與認證步驟）
   - 以台灣繁體中文撰寫註解
   - 包含關鍵環境變數的設定說明（`LONGBRIDGE_APP_KEY`、`LONGBRIDGE_APP_SECRET`、`LONGBRIDGE_ACCOUNT_ID`）

# 強力限制
- 必須使用 Longbridge 官方 Python SDK
- **請包含一個明確的模擬交易開關（`DRY_RUN = True`），預設在模擬模式下執行，避免意外實盤下單**
- 不得串接其他非 Longbridge 的券商 API
- 生成的程式碼可直接複製執行（假設使用者的 Longbridge 賬戶已開通 OpenAPI 權限）