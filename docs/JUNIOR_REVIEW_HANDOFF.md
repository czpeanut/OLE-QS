# 國中題庫逐題審題（交接文件）

給執行 agent。國中題庫 220,504 題、5,913 份卷，你要**逐題**審四件事：題目文字、答案、單元，以及難度（全部補上）；數學再多審題型。

- 題目直接從線上題庫（Supabase）讀。
- 審題結果交 JSONL 到雲端硬碟，由題庫維護端驗收後寫回。
- **你只讀資料庫、不寫資料庫**。

## 0. 工作包

雲端根目錄 `OLE-QS_國中審題_工作包_1118.zip`：

| 檔案 | 用途 |
|---|---|
| `junior_review_tool.py` | 取題、取附圖、交件前檢查（只用 Python 標準函式庫，**只發 GET 請求**） |
| `taxonomy/curriculum.json` | 國中單元表：年級 → 學期 → 科目 → 子科 → 版本 → 單元 |
| `taxonomy/math_patterns.json` | 數學題型目錄，鍵是 `年級-學期_單元代號`，例如 `7-1_1-1` |
| `HANDOFF.md` | 本文件 |

## 1. 連線與金鑰

- 需要 `SUPABASE_URL`、`SUPABASE_SERVICE_ROLE_KEY`，由題庫負責人提供。
  - 放在工作資料夾的 `.env.local` 或環境變數。
  - **不要印出、不要貼到任何文件或回報、不要上傳雲端。**
- 這把金鑰有寫入權限，但**這份工作禁止任何寫入**：
  - 不准對資料庫發 POST、PATCH、PUT、DELETE；
  - 不准改 Storage；
  - 不准用網站的審題台改題。
- 一律用 `junior_review_tool.py` 讀取，它只會發 GET。真的要自己查詢，也只能用 GET。

```bash
python junior_review_tool.py batches                 # 133 批的清單
python junior_review_tool.py fetch 數學_7-1_1 -o work  # 取一批 → work/數學_7-1_1.jsonl（約 1～2 分鐘）
python junior_review_tool.py image "doc_111_台中_五權_數學_g7s1e1/s1_3_f1.png" -o fig.png
python junior_review_tool.py check work/數學_7-1_1.jsonl REVIEW_數學_7-1_1.jsonl
```

## 2. 批次與順序

- **批次名稱**：`<科目>_<年級>-<學期>_<第N份>`。同科目、同年級學期的卷依卷 id 排序，每 50 份一批，共 133 批。
- **順序**：數學 → 自然 → 社會 → 英語 → 國文。同一科由 7-1 依序做到 9-2。
- **進度**：每做完一批就交一批，不要累積。

## 3. 題目資料（`fetch` 輸出，一行一題）

```json
{"id": "doc_111_台中_五權_數學_g7s1e1_s1_3",      ← 題目 id，交件照抄
 "paper_id": "doc_111_台中_五權_數學_g7s1e1",
 "subject": "數學", "unit_subject": "數學",       ← 自然／社會的子科（理化、生物、地科、歷史、地理、公民）
 "grade": 7, "semester": 1, "exam_seq": 1, "school": "…",
 "scope_note": null, "publisher": null,         ← 卷上印的考試範圍、版本（多數沒有）
 "section_name": "一、選擇題…", "number": 3,
 "type": "single",       ← single 單選、multiple 多選、tf 是非、fill 填充、matching 配合、calc 計算、essay 問答
 "status": "reviewed",   ← rejected＝已被品管剔除，照樣要審，但答案、難度可以從簡
 "group_id": null,       ← 題組：同一個 group_id 是同一題組（值是題組第一題的 id）
 "group_stem": null, "shared_text": null,      ← 題組共用說明、共用閱讀短文
 "stem": "如右圖，數線上 A、B、C、D 四點…",
 "options": [{"label": "A", "text": "A($-1\\frac{1}{3}$)", "figure": null}],
 "figures": ["doc_…/s1_3_f1.png"],              ← 題幹附圖、題組共用圖（用 image 指令取）
 "tables": [],                                  ← 結構化表格（Markdown）
 "answer": ["D"], "answer_status": "verified", "answer_source": "answer_key_vlm",
 "explanation": null,
 "unit": {"publisher": "翰林", "code": "1-1", "title": "正數與負數", "chapter": "…"},
 "pattern": "7-1_1-1:T02",                      ← 只有數學
 "difficulty": null, "review_note": null}
```

文字格式：
- 公式是 LaTeX（`$…$`），表格是 Markdown；
- `<u>…</u>` 是原卷畫線處，`＿＿＿` 是作答空格。

**答案來源 `answer_source`**，可信度由高到低：

| 值 | 意思 | 題數 |
|---|---|---|
| `source_answer_key` | 原卷答案卷 | 1,996 |
| `answer_key_vlm` | 從答案卷影像辨識 | 13,321 |
| `ai:gemini+qwen` | 兩個模型作答一致 | 167,787 |
| `ai:majority` | 三個模型多數決 | 22,921 |
| `ai:gemini-only` | 只有一個模型 | 507 |
| 空值 | 沒有答案 | 13,972 |

`answer_status` 是 `disputed` 的 913 題，是模型之間不一致，要特別仔細判斷。

### 直接查詢時的資料表（參考）

| 資料表 | 重要欄位 |
|---|---|
| `document`（原卷） | `id`、`title`、`school`、`grade`、`semester`、`exam_seq`、`subject`、`sub_subject`、`scope_note`、`publisher` |
| `section`（大題） | `document_id`、`ord`、`name` |
| `question`（題目） | `id`、`document_id`、`section_ord`、`number`、`type`、`stem_md`、`group_stem`、`answer`（JSON 陣列）、`answer_status`、`answer_source`、`explanation_md`、`difficulty`、`parent_id`（題組第一題）、`shared_asset_key`、`unit_publisher`、`unit_subject`、`unit_code`、`unit_title`、`status`、`review_note` |
| `option`（選項） | `question_id`、`label`、`content_md`、`asset_file`、`ord` |
| `asset`（附圖、表格、共用短文） | `document_id`、`question_id`（共用的是空值）、`key`、`file`、`markdown`、`text` |
| `tag`（分類標籤） | `question_id`、`axis`（`textbook`、`pattern`…）、`value` |

- 讀資料：`GET {SUPABASE_URL}/rest/v1/<資料表>?select=…&<欄位>=eq.<值>`，標頭帶 `apikey` 與 `Authorization: Bearer <金鑰>`。
- 附圖：Storage bucket `assets`，物件鍵是檔名的 SHA-1 前 2 碼／後 24 碼加 `.png`，用 `GET {SUPABASE_URL}/storage/v1/object/authenticated/assets/<鍵>` 取。
- 原卷 PDF **不在**資料庫裡，審題靠題目文字與附圖。

## 4. 每題審什麼

### 4.1 題目文字 `text`

- 判斷題目能不能正常閱讀、作答：有沒有缺字、亂碼、公式殘缺、選項錯位或缺漏、題幹提到圖卻沒有圖、兩題黏在一起。
- 正常寫 `ok`，有問題寫 `issue`，並在 `text_issue` 具體寫哪裡有問題。
- **不要改寫題目**。

### 4.2 答案 `answer`

**每題都要自己解一次**，再和現有答案比對：

| 值 | 什麼時候用 |
|---|---|
| `agree` | 你解出來和現有答案相同 |
| `fix` | 現有答案錯、或原本沒有答案：`answer_fix` 寫正確答案（陣列，格式同 `answer`，例如 `["B"]`、`["A","C"]`、`["12"]`），`answer_reason` 用一兩句說明 |
| `unsure` | 題目缺圖、缺字，或有多種合理答案，判斷不了：`note` 寫原因 |
| `none` | 開放式題目（作文、自由作答），本來就沒有標準答案 |

- `answer_source` 是 `source_answer_key`、`answer_key_vlm` 的題，答案來自原卷答案卷。你要改的話，`answer_reason` 必須寫清楚原卷錯在哪裡，題庫端會另外核對。
- 填充題的答案照題目要求的格式（分數、單位）。有多格時，依序一格一個元素。

### 4.3 單元 `unit`

- 對照 `taxonomy/curriculum.json` 裡該年級、學期、科目、版本的單元表。版本沒標的用翰林版。
- 正確寫 `ok`。錯了寫 `fix`，並在 `unit_fix` 填正確的單元代號，例如 `"2-3"`。
- 題目屬於其他學期的單元（例如複習卷）時，另外加 `"unit_fix_book": "7-2"`。
- **依題目考的觀念或方法判斷**，不是依關鍵字。

### 4.4 難度 `difficulty`（每題都要給）

| 值 | 標準 | 網站分級 |
|---|---|---|
| 1 | 單一觀念直接套用、記憶或一步計算 | 易 |
| 2 | 一般段考題：兩三個步驟，或需要判讀圖表 | 中 |
| 3 | 多步驟推理、條件較多，或需要轉換題意 | 難 |
| 4 | 跨單元綜合，或需要巧思 | 難／挑戰 |
| 5 | 競賽或資優程度，少數學生能解 | 挑戰 |

- 依題目本身判斷，**不要依題型套用**。
- 選擇題也可以是 3～5，計算題也可以是 1。
- 現有難度只有數學有部分題目填了，一樣要重新判斷。

### 4.5 數學題型 `pattern`（只有數學）

| 值 | 什麼時候用 |
|---|---|
| `ok` | 現有題型正確 |
| `fix` | 題型錯了：`pattern_fix` 填 `taxonomy/math_patterns.json` 裡的鍵加題型代號，例如 `"7-1_1-2:T03"` |
| `none` | 現有題型是空的，也找不到合適的 |

- 目錄裡沒有合適的題型，寫 `"7-1_1-2:NEW:題型名稱"`，題庫端會統一新增。

## 5. 交件格式

每批一個檔 `REVIEW_<批次>.jsonl`，一行一題，**這一批每一題都要有，不能漏、不能重複**：

```json
{"id": "doc_111_台中_五權_數學_g7s1e1_s1_3", "text": "ok", "answer": "agree", "unit": "ok", "difficulty": 1, "pattern": "ok"}
{"id": "…_s1_4", "text": "ok", "answer": "fix", "answer_fix": ["C"], "answer_reason": "…", "unit": "fix", "unit_fix": "1-2", "difficulty": 2, "pattern": "fix", "pattern_fix": "7-1_1-2:T03"}
{"id": "…_s2_1", "text": "issue", "text_issue": "題幹提到右圖但沒有附圖", "answer": "unsure", "unit": "ok", "difficulty": 2, "pattern": "ok", "note": "缺圖無法作答"}
```

| 欄位 | 必填 | 值 |
|---|---|---|
| `id` | 是 | 照抄 |
| `text` | 是 | `ok`／`issue`（`issue` 要有 `text_issue`） |
| `answer` | 是 | `agree`／`fix`／`unsure`／`none`（`fix` 要有 `answer_fix`、`answer_reason`） |
| `unit` | 是 | `ok`／`fix`（`fix` 要有 `unit_fix`，跨學期加 `unit_fix_book`） |
| `difficulty` | 是 | 1～5 |
| `pattern` | 數學必填 | `ok`／`fix`／`none` |
| `note` | 否 | 其他要讓題庫端知道的事 |

## 6. 交件前自我檢查

1. 跑 `python junior_review_tool.py check work/<批次>.jsonl REVIEW_<批次>.jsonl`，**結束碼要是 0**。
2. 自己隨機抽 20 題重看一次（種子 20261118）。不一致的修正後，寫進 `REPORT_<批次>.md`，列出題號與修正內容。
3. `REPORT_<批次>.md` 至少寫：題數，以及 `fix`、`unsure`、`issue` 各幾題、難度分布、自我抽驗結果。

## 7. 上傳

- 交到雲端「**國中審題交件**」資料夾，每批兩個檔：`REVIEW_<批次>.jsonl`、`REPORT_<批次>.md`。
- 檔案大於 5MB 再分段。
- 在 `ROUND3_進度回報` 寫一筆：批次、題數、`answer fix` 與 `unsure` 數、自我抽驗結果。

## 8. 驗收

- 題庫端每批抽 30 題重新判斷。答案、單元、難度合計錯誤超過 5%（2 題以上）退回該批，退回要**整批重看**。
- 特別抽查答案被改成 `fix` 的題目，以及 `answer_source` 是答案卷但被改掉的題目。
- 通過的批次由題庫端寫回資料庫：
  - 答案標記為「agent 審定」，原答案保留可追溯；
  - 原本沒有難度的題目補上難度。

## 9. 規則

- 一律用中文回報。照實回報，不確定就寫 `unsure`，不要猜。
- 不准寫入資料庫、不准改題目文字、不准把金鑰寫進任何檔案或回報。
- 只有這些情況才停下來回報：
  - 連不上資料庫；
  - 工具出錯；
  - 某批超過 15% 的題目無法閱讀。
