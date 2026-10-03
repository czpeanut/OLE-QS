# 管線紀錄備份

`out/` 不進版控，這裡存它花錢跑出來、無法免費重建的部分（2026-10-03 備份）：

- `raw_extract.tar.gz`：Gemini 批次擷取的原始回應（每份卷一個 JSON），重新解析不用再呼叫 API
- `answers.tar.gz`：Gemini、Qwen、第三票各自的作答（`answers/{gemini,qwen,tiebreak}/<doc>.json`）
- `batch_jobs_ledgers_logs.tar.gz`：批次工作紀錄、花費帳本（`phase2_spend.json` 等）、答案評估、執行紀錄

還原：`tar -xzf data/pipeline_records/<檔名> -C out/`（raw_extract 解到 `out/batches/`）。
