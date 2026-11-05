# TXO Gamma Flip 戰情板

台指選擇權（週三週選、週五週選、月選）造市商 Gamma 曝險與 Gamma Flip 每日自動計算，提供電腦版與手機版兩種頁面。

## 檔案

- `scripts/txo_gex.py`：從期交所下載 TXO 選擇權與 TX 期貨每日行情，Black-76 反推 IV，計算 GEX 曲線、Flip、近 5 日 ATR
- `.github/workflows/update.yml`：交易日台北時間 16:37／17:13／18:07 自動執行並提交資料
- `data/latest.json`：最新一日原始資料；`data/history.json`：每日 Flip／GEX／OHLC／ATR 摘要
- `src/desktop.html`：電腦版原始檔，也是計算引擎（Black-76、GEX、Flip、牆、區塊建議）的唯一來源
- `src/mobile.src.html`：手機版樣板，計算引擎由建置腳本自電腦版複製進去
- `scripts/build_pages.py`：產生 `index.html`（電腦版）、`mobile.html`（手機版）與 `build/` 下給 Claude 頁面發布的檔案

## 修改流程

1. 只改 `src/desktop.html`（計算或建議邏輯）或 `src/mobile.src.html`（手機版版面）
2. 執行 `python scripts/build_pages.py`
3. 提交 `src/`、`build/`、`index.html`、`mobile.html`，並重新發布兩個 Claude 頁面

## 瀏覽

開啟 GitHub Pages（Settings → Pages → main／根目錄）後：電腦版 `/`、手機版 `/mobile.html`。

手動補資料：Actions → 更新 TXO Gamma Flip 資料 → Run workflow，填入往回抓的天數。
