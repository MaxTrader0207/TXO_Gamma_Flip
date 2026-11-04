# TXO Gamma Flip 戰情板

台指選擇權（週三週選、週五週選、月選）造市商 Gamma 曝險與 Gamma Flip 每日自動計算。

- `scripts/txo_gex.py`：從期交所下載 TXO 選擇權與 TX 期貨每日行情，Black-76 反推 IV，計算 GEX 曲線與 Flip
- `.github/workflows/update.yml`：交易日台北時間 16:37／17:13／18:07 自動執行並提交資料
- `data/latest.json`：最新一日原始資料（儀表板讀取）
- `data/history.json`：每日 Flip／GEX 摘要（最多 250 日）
- `index.html`：儀表板，開啟 GitHub Pages 後即可瀏覽

手動補資料：Actions → 更新 TXO Gamma Flip 資料 → Run workflow，填入往回抓的天數。
