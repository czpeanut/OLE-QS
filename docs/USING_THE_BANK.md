# 取用 OLE 題庫（給其他專案／其他 Claude session）

這份文件寫給**要從這個 repo 拿題目來用**的專案：出練習、組考卷、做題目推薦、做學習 App 等等。
照著做，不必讀其他程式碼。

## 這是什麼

台灣國中七～九年級段考題庫，題目從各校段考卷（PDF）擷取、驗證、分類而來。

| 項目 | 數量 |
|---|---|
| 題目 | 220,504 題（可用 216,966 題；其餘 3,538 題被品管剔除，`status == "rejected"`） |
| 原卷 | 5,913 份，108 所學校（以縣市＋校名計）、10 個縣市，111～114 學年度 |
| 科目 | 國文、英語、數學、自然（理化、生物、地科）、社會（歷史、地理、公民） |
| 附圖 | 約 5.3 萬張 PNG（題幹圖、題組共用圖、選項圖） |

## 三十秒上手

```python
import sys; sys.path.insert(0, "client")          # 從 repo 根目錄執行
from oleqs_bank import Bank, to_text

bank = Bank()                                      # 讀 data/export/ 的快照，約 15 秒、記憶體約 1.5 GB
qs = bank.filter(subject="數學", grade=7, semester=1, unit_codes=["1-1"], difficulty=["易"])
print(to_text(qs[0]))                              # 題幹、選項、答案的純文字
pick = bank.random(20, subject="自然", sub_subject="理化", grade=8, semester=2, seed=1)
```

指令列（不寫程式也能查）：

```bash
python client/oleqs_bank.py stats
python client/oleqs_bank.py search --subject 數學 --grade 7 --semester 1 --unit 1-1 --difficulty 易 --limit 5
python client/oleqs_bank.py random 10 --subject 英語 --grade 8 --seed 1 --json > picked.json
python client/oleqs_bank.py show doc_111_台北_新興_數學_g7s1e1_s1_8
python client/oleqs_bank.py image "doc_111_台北_新興_數學_g7s1e1/s1_8_f1.png" -o fig.png
```

`client/oleqs_bank.py` 只用 Python 標準函式庫（3.10 以上），不必 `pip install`；
也可以整個檔案複製到別的專案，搭配 `data/export/` 使用（設環境變數 `OLEQS_EXPORT` 指到快照資料夾）。

## 三種取用方式

| 方式 | 需要什麼 | 拿得到什麼 | 什麼時候用 |
|---|---|---|---|
| **A. repo 裡的快照**（建議） | 什麼都不用 | 全部題目文字、答案、分類、難度、附圖檔名 | 幾乎所有情況 |
| **B. 選題網站 API** | 網站通行碼 `OLEQS_SITE_CODE` | 附圖、即時資料、排版好的考卷 HTML | 要附圖、要最新修訂 |
| **C. Supabase 直連** | `SUPABASE_URL`、`SUPABASE_SERVICE_ROLE_KEY` | 全部（含附圖原檔） | 只有在 B 不能用時 |

- **只要題目文字，用 A 就夠了**：快照在 `data/export/`，跟著 repo 一起 clone 下來。
- **附圖不在 repo 裡**（約 4 GB），要用 B 或 C 下載；`Bank.image()` 會自動選可用的方式，並快取在 `~/.cache/oleqs/`。
- **快照是某個時間點的副本**（時間見 `data/export/manifest.json` 的 `exported_at`）。之後在審題台修正的內容，要重新匯出才會進快照（見「更新快照」）。

## 金鑰與安全（一定要遵守）

- **不要在對話裡請使用者貼金鑰或通行碼。** 請使用者把 `.env.local` 檔案**上傳**到 repo 根目錄，`oleqs_bank.py` 會自動讀。
- `.env.local` 已在 `.gitignore`，**絕不 commit、不印出金鑰值**。
- 優先用網站通行碼（B，唯讀）。`SUPABASE_SERVICE_ROLE_KEY` 有完整寫入權限，只拿來讀，不要用它改資料。

`.env.local` 範例：

```
OLEQS_SITE_CODE=（選題網站的通行碼）
# 或者
SUPABASE_URL=https://xxxx.supabase.co
SUPABASE_SERVICE_ROLE_KEY=（金鑰）
```

## 快照的檔案

```
data/export/
├── questions.jsonl.gz      每行一題（JSON），約 42 MB
├── papers.jsonl.gz         每行一份原卷：學校、段考、大題
├── taxonomy/
│   ├── curriculum.json     各冊、各版本的單元表
│   └── math_patterns.json  數學題型目錄
└── manifest.json           匯出時間、筆數、schema_version
```

不用 Python 也能讀：`zcat data/export/questions.jsonl.gz | head -1`，或任何語言的 gzip＋JSON Lines。

## 一題的欄位（questions.jsonl.gz）

```json
{
  "id": "doc_111_台北_新興_數學_g7s1e1_s1_8",   // 題目 ID，全庫唯一、不會變
  "paper_id": "doc_111_台北_新興_數學_g7s1e1",  // 原卷 ID（papers.jsonl.gz）
  "subject": "數學",            // 國文 英語 數學 自然 社會
  "sub_subject": null,          // 自然：理化 生物 地科；社會：歷史 地理 公民；其他科 null
  "grade": 7, "semester": 1,    // 七上 = 7, 1
  "exam_seq": 1,                // 第幾次段考
  "year": 111,                  // 學年度（民國）
  "school": "台北市立 新興國民中學", "city": "台北", "school_short": "新興",
  "section": 1, "section_name": "一、單選題：(每題3 分，共60 分)", "number": 8,   // 原卷大題、題號
  "type": "single",             // 見下表
  "format": "choice",           // 作答形式：choice 選擇、fill 填充、apply 應用、other 其他、drill 國文字音字義
  "stem": "附圖是數線上 A、B、C、D 四點…",   // 題幹（Markdown＋$…$ LaTeX）
  "group_stem": null,           // 題組說明／閱讀短文（題組各小題相同）
  "group_id": null,             // 題組 ID（＝題組第一小題的 id）；同一個 group_id 的題目要一起出
  "options": [{"label": "A", "text": "| a |＋| c |", "image": null}],   // image：選項是圖時的附圖檔名
  "answer": ["B"],              // 答案陣列；選擇題是選項標號，填充題是文字；可能是 null（沒有答案）
  "answer_status": "ai_generated", "answer_source": "ai:gemini+qwen",  // 答案可信度，見下表
  "explanation": null,          // 詳解（多數沒有）
  "score": null,                // 原卷配分（多數沒有）
  "difficulty": 2,              // 1–5，只有數學有（null＝未評）
  "unit": {"publisher": "翰林", "subject": "數學", "code": "1-1", "title": "正數與負數", "chapter": "第1章 數與數線"},
  "pattern": {"key": "7-1_1-1:T07", "name": "數線上兩點距離與坐標求解"},  // 數學題型，其他科 null
  "textbook_tags": ["七上 1-1 正數與負數"],
  "images": [{"key": "s1_8_f1", "label": null, "file": "doc_111_台北_新興_數學_g7s1e1/s1_8_f1.png"}],  // 題幹附圖
  "shared_images": [],          // 題組共用的附圖（同組每題都附）
  "shared_text": null,          // 題組共用的文字素材（例如表格），少數題目才有
  "sort_key": 46306,            // 題庫排序：小的在前（選擇→填充→應用→其他，各自由易到難）
  "status": "reviewed",         // reviewed 可用；rejected 品管剔除（filter 預設排除）
  "corrected": null,            // "原卷有誤"：原卷印錯、已修正（題幹結尾有「（原卷有誤，已修正）」）；"擷取錯誤"：已修正
  "page": 1                     // 在原卷第幾頁
}
```

### type（題型）

| type | 意思 | 題數 |
|---|---|---|
| `single` | 單選 | 179,049 |
| `multiple` | 多選 | 31 |
| `tf` | 是非 | 928 |
| `fill` | 填充 | 20,484 |
| `calc` | 計算 | 1,721 |
| `essay` | 問答、簡答 | 4,082 |
| `matching` | 配合 | 1,922 |

`format` 是出題時的作答形式分組，排序用（`sort_key // 10,000,000`：0 choice、1 fill、2 apply、3 other、9 drill）。
題組小題的 `format` 是 `other`；國文純字音字義練習是 `drill`，排在最後。

### 答案可信度

| answer_status | answer_source | 意思 | 題數 |
|---|---|---|---|
| `verified` | `answer_key_vlm`、`source_answer_key` | 原卷附的答案卷 | 14,358 |
| `ai_generated` | `ai:gemini+qwen` | 兩個模型獨立作答一致（抽驗約 95% 正確） | 157,456 |
| `ai_generated` | `ai:majority` | 兩模型不一致，第三票多數決 | 21,696 |
| `ai_generated` | `ai:gemini-only` | 只有一個模型作答 | 491 |
| `disputed` | — | 答案卷與模型不一致，待人判定 | 911 |
| `missing` | `null` | 沒有答案（計算、問答等非選題居多） | 13,305 |

要正確答案很重要的場合（例如自動計分），用 `answer_status in ("verified",)` 或 `answer_source == "ai:gemini+qwen"`，
並避開 `disputed`、`missing`。

### 難度（只有數學）

`difficulty` 1–5。網站用的分級：**易＝1、中＝2、難＝3–5、挑戰＝4–5**（`filter(difficulty=["易"])` 也接受這些字）。
其他科目目前沒有難度（`null`）。

## 單元與分類

**單元代號**（`unit.code`）依科目規則不同：

| 科目 | 代號 | 跨版本 |
|---|---|---|
| 數學、自然 | 節次，例如 `1-1`、`3-2` | 三個版本（翰林、康軒、南一）節次對齊，**只比代號** |
| 國文 | 課名代號，例如 `第一課`、`語文常識(二)` | 各版本不同，**要連同 `unit.publisher` 一起比**（`filter(unit_codes=["第一課"], publisher="翰林")`） |
| 社會 | 節次，例如 `1-1` | 同國文，要連同 `publisher`，也要配 `sub_subject` |
| 英語 | 跨版本主題：文法 `B1-03` 這類、字彙 `V1`、片語 `V2`、閱讀 `R1`、對話 `R2`、其他文法 `G0` | 不分版本 |

約有 1% 的題目沒有單元（`unit` 為 `null`），例如綜合題、跨單元題。

- 社會、自然的單元還要配 `sub_subject`：歷史、地理、公民的節次代號會重複。
- 查單元表：`bank.units("數學", 7, 1)`、`bank.units("自然", 8, 2, sub_subject="理化")`、`bank.units("國文", 7, 1, publisher="康軒")`。
  資料來自 `taxonomy/curriculum.json`。
- 英語主題表在 `curriculum.json` 的 `english`：每個主題有 `id`、`title`、適用冊別、各版本對應課次。

**數學題型**（`pattern`）：61 個單元、648 個題型，鍵是「年級-學期_單元代號:題型代號」，例如 `7-1_1-1:T07`。
`bank.patterns(7, 1, "1-1")` 列出某單元的題型（名稱、說明、題數），資料在 `taxonomy/math_patterns.json`。

## 題組（閱讀短文、共用圖）

- 同一篇短文或同一張圖的幾個小題，`group_id` 相同，`group_stem`（短文）與 `shared_images`（共用圖）也相同。
- **題組要整組一起出**，不要只抽其中一題。`bank.group(q)` 拿整組；`bank.random(...)` 預設整組一起抽（`whole_groups=True`）。
- 呈現時：先印一次 `group_stem` 與 `shared_images`，再依 `number` 印各小題。

## 呈現：公式、附圖、格式

- **公式**：題幹、選項、短文裡的數學式是 `$…$` 包住的 LaTeX，用 KaTeX 或 MathJax 渲染。
  英文句子裡的 `$` 可能是錢的符號，判斷規則可參考 `web/lib/mathfmt.ts`。
- **表格**：用 Markdown 表格（`| a | b |`）。
- **畫線處**：題目用底線標出的字詞寫成 `<u>…</u>`，雙底線是 `<u class="dbl">…</u>`（例如「依<u>畫線部分</u>造問句」）。
  顯示時保留成底線；轉純文字時可以改成「」或直接去掉標籤。只是留給學生作答的空格（＿＿＿）不會加標籤。
- **畫線處**：原卷的底線、粗體不一定保留；題目問「畫線處」時請以原卷為準。
- **附圖**：`images[].file`、`shared_images[].file`、`options[].image` 都是附圖檔名（相對路徑，例如 `doc_…/s1_8_f1.png`）。
  - 取得 PNG：`bank.image(file)` 回傳 bytes；或指令列 `oleqs_bank.py image <file> -o x.png`。
  - 經網站：`GET {OLEQS_SITE_URL}/api/assets/<file>`（路徑每段做 URL 編碼），帶 Cookie `oleqs_access=<sha256(通行碼)>`。
  - 直連 Storage：bucket `assets`，物件鍵是 `sha1(file)` 的前 2 碼／後 24 碼（`storage_key()`），
    `GET {SUPABASE_URL}/storage/v1/object/authenticated/assets/<鍵>`。
- **純文字呈現**：`to_text(q)`，附圖以 `[圖:檔名]` 標示。

## 網站 API（即時資料）

網站：<https://ole-qs.vercel.app>（`OLEQS_SITE_URL` 可改）。所有請求帶 Cookie `oleqs_access=<通行碼的 SHA-256 十六進位>`。

| 端點 | 用途 | 參數 |
|---|---|---|
| `GET /api/questions` | 篩選題目（分頁） | `subject`、`sub`、`grade`、`semester`、`year`、`publisher`、`units`（逗號分隔，格式 `子科或科目\|代號`，例如 `數學\|1-1`）、`type`、`difficulty`、`patterns`、`has_figure`、`q`（全文搜尋）、`limit`（≤200）、`offset` |
| `POST /api/random` | 隨機抽題 | JSON：上面的篩選條件＋`n`（≤100）、`spread`（`unit`/`pattern`/`none`）、`mix`（`even`/`easy`/`hard`，數學難度比例）、`exclude`（題目 ID 陣列） |
| `POST /api/render` | 排版成考卷 HTML（可列印成 PDF） | JSON：`items: [{question_id, score?}]`、`title`、`mode`（`exam` 試題卷、`answer` 答案卷、`key` 教師解答卷：題目附答案）、`settings`（版面設定，可省略） |
| `GET /api/assets/<file>` | 附圖 PNG | — |
| `GET /api/taxonomy` | 科目、子科、冊別、版本的分類樹 | — |
| `GET /api/units` | 某冊的單元表（含題數） | `subject`、`sub`、`grade`、`semester`、`publisher` |

網站回傳的題目欄位與快照略有不同（例如 `stem` 另附 `stem_html` 已渲染好的 HTML），以實際回應為準。

## 常見任務

**出一份數學練習卷（某單元、易到中、20 題）**
```python
qs = bank.random(20, subject="數學", grade=8, semester=1, unit_codes=["2-1", "2-2"],
                 difficulty=["易", "中"], formats=["choice"], seed=42)
```

**只要有可靠答案的選擇題**
```python
qs = [q for q in bank.filter(subject="社會", sub_subject="地理", grade=7, formats=["choice"])
      if q["answer_source"] in ("ai:gemini+qwen", "answer_key_vlm", "source_answer_key")]
```

**某份原卷的全部題目（照原卷順序）**
```python
qs = sorted(bank.filter(paper_id="doc_113_高雄_鼎金_自然_g8s2e2"), key=lambda q: (q["section"], q["number"]))
```

**輸出給別的 App（JSON）**
```bash
python client/oleqs_bank.py random 30 --subject 英語 --grade 7 --semester 2 --seed 7 --json > quiz.json
```

**排版成可列印的考卷**：用網站 `POST /api/render`（需通行碼），把題目 ID 傳過去。

## 更新快照

快照由線上題庫（Supabase）匯出。審題台修正、重新分類後，在這個 repo 執行：

```bash
python scripts/export_bank.py      # 需要 .env.local：SUPABASE_URL、SUPABASE_SERVICE_ROLE_KEY，約 8 分鐘
git add data/export && git commit -m "Refresh bank export"
```

欄位有不相容的變動時會調高 `manifest.json` 的 `schema_version`。

## 注意與限制

- 題目文字由 AI 擷取並經 AI 初審，仍可能有少數錯字、漏圖；`corrected` 標示的是已修正的原卷或擷取錯誤。
  發現錯誤請記下題目 `id` 回報給題庫維護者（審題台：網站 `/review/7-1` 等）。
- 難度、數學題型只有數學有；其他科的分類只到單元。
- 同一題可能出現在多份卷（各校互相改編），目前沒有去重。
- 聽力測驗、作文不收錄。
- 這個 repo 的其他部分（`scripts/` 擷取管線、`web/` 網站、`apps/` 後端）是題庫維護用的，取用題目不需要動它們。
