# 國中「題目有問題」題目修正（交接文件）

給執行 agent。上一輪逐題審題中，你標了 4,793 題 `text: issue`（缺選項、缺圖、公式殘缺、題幹不完整…）。這些題目目前**原封不動留在題庫**，
現在請你**逐題修成可以正常作答的完整題目**。題庫負責人的原則：**要的是正確、可作答的題目，不必拘泥原卷原樣**——缺的選項可以補、
圖裡的資訊可以改寫進題幹；題庫端收下後，來源會標「(修正)」。

- 你**只讀不寫資料庫**（和審題一樣，只用 `junior_review_tool.py` 的 GET；不准 POST／PATCH／DELETE）。金鑰不要印出、不要寫進交件檔、不要上傳。
- 修完交 JSONL 到雲端硬碟，題庫端驗收後寫回。

## 0. 工作包

| 檔案 | 用途 |
|---|---|
| `tasks/修正_<科目>_<N>.jsonl` | 34 批（每批至多 150 題）。每行一題：目前題目內容、你上次標的問題、附圖檔名 |
| `junior_fix_check.py` | 交件前自我檢查（只用標準函式庫） |
| `junior_review_tool.py` | 取附圖用：`python junior_review_tool.py image "<figures 裡的檔名>" -o fig.png` |
| `HANDOFF.md` | 本文件 |

任務檔每行欄位：`id`、`subject`、`sub_subject`、`grade`、`semester`、`category`（缺選項／缺圖／公式或亂碼／其他，依你上次說明自動分類）、
`problem`（你上次寫的問題）、`note`、`type`、`stem`、`group_stem`、`options`（`label`、`content`、`image`）、`answer`（目前答案）、
`answer_status`、`agent_answer`（你上次審題算出的答案，可能為空）、`figures`（這題的附圖檔名，多數題是空的）。

## 1. 怎麼修

| 問題類型 | 修法 |
|---|---|
| **缺選項**（約 2,480 題） | 依題意**補出選項**（選擇題一般 A～D 四個），讓答案成立；干擾選項要合理。答案以你自己解出的為準。 |
| **缺圖**（約 860 題，這些題**沒有圖檔**） | 能用文字把圖的資訊講清楚就**改寫題幹**（表格用 Markdown 表格，座標、數值、位置關係寫明白）；題目的重點就是看圖、無法用文字重現，標 `unfixable`。 |
| **公式或亂碼** | 還原成正確的 LaTeX／文字（格式見 `docs/QUESTION_FORMAT.md` 第 4 節：行內 `$…$`、畫線處 `<u>…</u>`、作答空格照原樣）。 |
| **題幹不完整、答案卷錯位等** | 補全題幹；題組材料放 `group_stem`（一字不差，同組每一題都要有）。 |

- 只改有問題的地方，**題目考的觀念和難度不要變**；不要把題目改得比原題簡單到失去意義。
- 題目有附圖（`figures` 不是空的）：圖檔不動，用 `image` 指令看圖再修；**選項若有 `image`，選項代號和數量不要改**。
- 修完必須**自己重新解一次**，`answer` 要和你的選項、題幹一致；單選只給一個字母，填充題依序一格一個元素。
- 沒把握的題，寧可標 `unfixable`（會被暫緩，不會上站），不要硬補。
- 題型 `type` 只能是：`single`、`multiple`、`fill`、`calc`、`tf`、`matching`、`essay`。選擇題一定要有選項；原本缺選項的題若其實是填充題，可改 `type: fill` 並不放選項。

## 2. 交件格式

每批一個檔 `REVISED_<批次名>.jsonl`（例如 `REVISED_修正_數學_1.jsonl`），每題一行，**id 要和任務檔完全對應，每題都要有一行**：

```json
{"id": "…", "result": "fixed", "type": "single", "stem": "…", "group_stem": null,
 "options": [{"label": "A", "content": "…"}, {"label": "B", "content": "…"}, {"label": "C", "content": "…"}, {"label": "D", "content": "…"}],
 "answer": ["B"], "explanation": "（選填）解析", "fix_note": "補上四個選項；答案 B 由題幹算出"}
{"id": "…", "result": "unfixable", "unfixable_reason": "題目要看地圖判斷位置，無圖且無法用文字重現"}
```

| 欄位 | 規則 |
|---|---|
| `result` | `fixed` 或 `unfixable` |
| `fixed` 必填 | `type`、`stem`、`options`（選擇題）、`answer`、`fix_note`（一句話：改了什麼） |
| `group_stem` | 沒有題組就 `null` 或不寫 |
| `options` | 選項只放選項本身，不要混入表格或下一題材料；代號連續 `A`、`B`、`C`… |
| `unfixable` 必填 | `unfixable_reason` |

## 3. 交件前自檢

```bash
python junior_fix_check.py tasks/修正_數學_1.jsonl REVISED_修正_數學_1.jsonl
```

檢查：id 完全對應無重複；`result` 合法；選擇題至少 2 個選項、代號連續、答案字母都在選項內、單選只有一個答案；`$` 成對；題幹不得為空；
沒有圖檔的題，修好的題幹不得仍寫「如圖／下圖／附圖」；`fixed` 的內容不得和原題完全相同。**有錯誤不要交。**

## 4. 回報

每批交 `REPORT_<批次名>.md`，列：題數、`fixed`／`unfixable` 各幾題、`unfixable` 原因分類、特別提醒題目。
不要手改交件檔以外的任何檔案（腳本、設定一律不改）。有疑問在回報裡寫，不要自行變更格式。
