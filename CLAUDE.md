# OLE-QS 題庫 repo — 給 Claude 的說明

台灣國中七～九年級段考題庫：208,217 題、5,563 份原卷，五科，含答案、單元、數學題型與難度。

## 你是來「取用題目」的嗎？

**直接讀 `docs/USING_THE_BANK.md`**，照著做就好，不需要讀其他程式碼。重點：

- 題目快照在 `data/export/`（`questions.jsonl.gz` 每行一題），跟著 repo 一起下來，不需要任何金鑰。
- 取用模組 `client/oleqs_bank.py`（只用標準函式庫）：`Bank().filter(...)`、`Bank().random(n, ...)`、`Bank().image(file)`。
- 附圖不在 repo 裡：需要網站通行碼 `OLEQS_SITE_CODE`（建議），或 Supabase 金鑰。
- 題組（同一個 `group_id`）要整組一起出。

## 規則

- **一律用中文回覆**，包括過程中的進度說明。
- **金鑰**：只放在 `.env.local`（已 gitignore）。不要印出、不要 commit，也**不要請使用者把金鑰或通行碼貼到對話裡**；請使用者上傳 `.env.local` 檔案。
- 取用題目的專案**不要修改**這個 repo 的題庫（`data/bank/`、`data/export/`）或 Supabase 資料；發現題目有錯，記下題目 `id` 告訴使用者。

## 這個 repo 其他部分（題庫維護用，取用題目不需要）

| 路徑 | 內容 |
|---|---|
| `data/bank/*.yaml` | 每份原卷一個檔，擷取管線的原始輸出 |
| `scripts/` | 擷取、作答、分類、初審、匯出的管線（`export_bank.py` 產生 `data/export/`） |
| `web/` | 選題組卷網站與審題台（Next.js，部署在 Vercel：ole-qs.vercel.app） |
| `apps/api/` | 舊版本機後端（FastAPI＋SQLite） |
| `docs/` | 進度、統計、課程對照等文件 |
