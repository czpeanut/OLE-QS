# OLE-QS — 線上題庫系統

把國中考古題的 PDF／掃描檔轉成結構化題庫，依課綱與教科書章節分類，
供教師檢索、自由組卷、匯出考卷。

## 快速開始

```bash
pip install -r requirements.txt

# 1) 載入題目（已附兩份人工確認過的樣本）
python -m apps.api.importer data/samples/expected/

# 2) 啟動
uvicorn apps.api.main:app --reload
# 開啟 http://127.0.0.1:8000
```

圖檔預設放在 `data/assets/`，可用 `OLEQS_ASSETS` 指定；資料庫預設 `data/oleqs.db`，
可用 `OLEQS_DB` 指定。

## 處理新的考卷

```bash
# PDF → 頁面影像 + 文字圖層 + 圖形（自動判斷原生數位 / 掃描 / 空白頁）
python scripts/pdf_ingest.py "考古題目錄" -o out/ingest

# 從答案卷解析「題號 → 答案」
python scripts/parse_answer_key.py "考古題目錄" -o out/answer_keys

# 擷取結果的結構驗證（配分總和、題號連續性、選項、共用素材…）
python scripts/validate_extraction.py out/extracted.yaml

# 用多個模型交叉作答，只有不一致的才需人工判定
python scripts/generate_answers.py out/extracted.yaml --figures out/ingest/xxx/figures
```

## 目錄

```
apps/api/          FastAPI 後端 + 網頁介面
  models.py        資料模型（來源標註為必填欄位）
  db.py            SQLite + FTS5 trigram（中文子字串搜尋）
  importer.py      擷取結果 → 資料庫
  mathfmt.py       Markdown + 行內 LaTeX → HTML
  export.py        試題卷 / 答案卷 / 教師解答卷
  static/          單頁介面（檢索 → 題籃 → 匯出）
scripts/           擷取管線工具
data/samples/      黃金測試集（人工確認過的擷取結果）
docs/              開發計畫與各次 PoC 實測報告
```

## 授權與來源標註

題目取自公開試題。依法律諮詢意見，公開試題可使用，**但須保留來源標註**。

**出處掛在題目上，不是掛在整份卷上。** 這點很關鍵 —— 題庫的用途就是把不同卷的題目
重新組合，若出處只存在文件層，重組後就對應不回去了。

實作為 `QuestionSource`：匯入當下把來源欄位**快照**到每一題（含它在原卷的題號），
一題可以有多筆出處。這樣三種情況才成立：

| 情況 | 為什麼需要題目層的出處 |
|---|---|
| 去重合併 | 同一題出現在多份卷，合併後每個來源都要保留，外鍵只能留一個 |
| 改編題 | 改過數字或敘述後不再屬於原卷，應記為「改編自 X」 |
| 原文件被改或被刪 | 出處已快照，不會跟著變動或消失 |

其他約束：

- 快照欄位（學校、考試名稱、學年度、年級、科目）**不可為空**，
  匯入時缺少即拒絕寫入（`importer.py`）。
- 題目**不隨文件級聯刪除** —— 清理匯入紀錄不會毀掉已被考卷引用的題目。
- 匯出的每一題底下固定印自己的出處，**不提供關閉選項**；
  卷末再彙整本卷用到的所有來源（`export.py`）。

## 目前狀態

擷取管線與檢索、組卷、匯出的主幹已可運作，並在真實考卷上驗證過。
詳見 [`docs/development-plan.md`](docs/development-plan.md) 與
[`docs/poc-report-001.md`](docs/poc-report-001.md)、[`docs/poc-report-002.md`](docs/poc-report-002.md)。

尚未完成：校對工作台、課綱分類表的正式匯入、智慧組卷、Word 匯出。
