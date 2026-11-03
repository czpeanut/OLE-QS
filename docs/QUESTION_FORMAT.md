# 題目格式交接：把考古題轉成 OLE-QS 題庫格式

給「另一個專案／session 在轉換考古題時，順便輸出成 OLE-QS 題庫格式」用。照這份輸出，
交回來後可以直接併進題庫（作答、分類、初審、上線由題庫這邊的管線接手）。

> 只要輸出檔案，**不要改這個 repo 的 `data/bank/`、`data/export/` 或 Supabase**。
> 轉完把產出（YAML＋附圖）交給題庫維護者，或放進另一個分支／壓縮檔。

---

## 1. 要交出什麼

```
<輸出根目錄>/
  bank/
    doc_113_台中_曉明_自然_理化_g9s1e1.yaml      ← 一份原卷一個 YAML
    doc_113_台中_曉明_數學_g7s1e1.yaml
  assets/
    doc_113_台中_曉明_自然_理化_g9s1e1/          ← 附圖 PNG，資料夾名＝卷 id
      s2_13_f1.png
      g2_30_33_f1.png
  sources/                                       ← （建議）原卷 PDF，路徑同 document.source_file
    國三/理化/113/1-1/台中/曉明.pdf
```

- **一份原卷 → 一個 YAML**。同一個 PDF 裡有好幾科（例如「全科」合併檔）要拆成好幾份。
- 附圖一律存 **PNG**，路徑寫在 YAML 裡（相對於 `assets/`）。
- 原卷 PDF 一併交回最好：題庫這邊的初審、審題台的「調整邊緣」都要對照原卷。

## 2. 交件前先查重

題庫已有 5,913 份卷。同一場考試（學年度、學期、段考、年級、科目、子科、縣市、學校都相同）不要重複輸出。
已有的卷在 `data/export/papers.jsonl.gz`（每行一份；`id` 就是下面的卷 id）：

```python
import gzip, json
have = {json.loads(l)["id"] for l in gzip.open("data/export/papers.jsonl.gz", "rt", encoding="utf-8")}
```

卷 id 規則一樣，所以算出自己的卷 id 後查 `have` 即可。也要注意「同一份卷的不同版本」（原卷／清除字跡、重複下載），只交一份，優先清除字跡版本。

---

## 3. YAML 結構

最上層兩個鍵，第三個可省略：

```yaml
document: {...}        # 這份卷的出處
questions: [...]       # 題目，依大題、題號排序
shared_assets: [...]   # （選填）題組共用的附圖
```

### 3.1 `document`（出處）

| 欄位 | 必填 | 說明 | 例 |
|---|---|---|---|
| `id` | ✔ | 卷 id，規則見下 | `doc_113_台中_曉明_自然_理化_g9s1e1` |
| `grade` | ✔ | 年級：7、8、9 | `9` |
| `subject` | ✔ | 國文／英語／數學／自然／社會（只能這五個） | `自然` |
| `sub_subject` | | 自然：理化／生物／地科；社會：歷史／地理／公民。合科卷不填 | `理化` |
| `academic_year_roc` | ✔ | 學年度（民國） | `113` |
| `semester` | ✔ | 1＝上學期、2＝下學期 | `1` |
| `exam_seq` | ✔ | 第幾次段考：1、2、3（期末考＝3） | `1` |
| `city` | ✔ | 縣市，用「台」不用「臺」：台中、台北、新北、高雄、彰化… | `台中` |
| `school_short` | ✔ | 校名簡稱（不含縣市、國中／高中字樣） | `曉明` |
| `school` | ✔ | 學校正式全名 | `臺中市私立曉明女子高級中學` |
| `title` | ✔ | `{school} {學年度}學年度第{學期}學期第{段考}次定期評量 {年級}年級{科目}（{子科}）科試題` | 見範例 |
| `exam_name` | ✔ | `{學年度}學年度第{學期}學期第{段考}次定期評量` | `113學年度第1學期第1次定期評量` |
| `sections` | ✔ | 大題：`[{ord, name, count}]`，`ord` 從 1 起依卷面順序 | 見範例 |
| `source_file` | 建議 | 原卷 PDF 的相對路徑：`國X/<科目或子科>/<學年度>/<學期>-<段考>/<縣市>/<簡稱>.pdf` | `國三/理化/113/1-1/台中/曉明.pdf` |
| `scope_note` | | 卷頭印的命題範圍（照抄） | `國三理化第一章直線運動+2-1慣性定律` |
| `extractor` | | 用什麼轉的，例：`vlm:<模型名>`、`manual` | `vlm:gemini-3.7-flash` |

**卷 id**：`doc_{學年度}_{縣市}_{簡稱}_{科目}[_{子科}]_g{年級}s{學期}e{段考}`

- 例：`doc_113_台中_曉明_自然_理化_g9s1e1`、`doc_112_高雄_五甲_國文_g7s2e3`
- 檔名＝`{卷 id}.yaml`。

### 3.2 `questions`（題目）

| 欄位 | 必填 | 說明 |
|---|---|---|
| `id` | ✔ | `{卷 id}_s{大題 ord}_{題號}`，例 `doc_113_台中_曉明_自然_理化_g9s1e1_s2_13`；同卷不可重複 |
| `section` | ✔ | 所屬大題的 `ord` |
| `number` | ✔ | 卷面題號（整數） |
| `type` | ✔ | 見下表 |
| `page` | ✔ | 這題在原卷第幾頁（1 起） |
| `stem` | ✔ | 題幹（規則見第 4 節） |
| `options` | 選擇題 ✔ | `[{label: A, content: 文字}]`，選項本身是圖時加 `asset`（見 3.3） |
| `group_stem` | 題組 ✔ | 題組共用的短文／說明，同組每題放**一字不差的同一段文字** |
| `shared_asset` | | 題組共用圖的 key（對應 `shared_assets`） |
| `assets` | | 這題自己的附圖（見 3.3） |
| `answer` | | 答案，一律是**清單**：`[A]`、`[B, D]`、`["ㄧˋ"]`、`["-1/2"]` |
| `answer_status` | 有答案時 | `verified`＝答案卷或人工確認；`ai_generated`；`disputed` |
| `answer_source` | 有答案時 | `answer_key`（答案卷）、`answer_key_vlm`（從卷上印的答案讀出）、`manual` |
| `review_note` | | 給審題者的備註；掃描檔寫 `掃描件：無文字層可驗證，內容為影像辨識` |
| `source_ocr` | | 掃描檔（沒有文字層）設 `true` |

**沒有答案卷就不要填答案**。題庫這邊會用兩個模型作答，不一致時再加第三票。
不要自己讓模型猜答案填進來，否則會被當成答案卷。

**題型 `type`**：

| 值 | 意義 | 選項 |
|---|---|---|
| `single` | 單選 | 必須剛好 4 個（A～D） |
| `multiple` | 多選 | 2 個以上 |
| `tf` | 是非 | 0 個或 2 個 |
| `fill` | 填充、國字注音、填代號 | 不用 |
| `calc` | 計算、非選擇 | 不用 |
| `essay` | 問答、作文、翻譯 | 不用 |
| `matching` | 配合題 | 共用的參考選項放 `group_stem` |

英文**聽力題不要輸出**，題庫沒有音檔，這類題目用不了。

### 3.3 附圖

每題自己的圖放 `assets`；選項是圖時放在該選項的 `asset`；題組共用的圖放最上層 `shared_assets`，各題用 `shared_asset` 指過去。

```yaml
assets:
- key: s2_13_f1                 # 同卷不重複；建議 s{大題}_{題號}_f{第幾張}
  kind: figure                  # figure／table／chart
  file: doc_113_台中_曉明_自然_理化_g9s1e1/s2_13_f1.png   # 相對於 assets/
  crop:                         # （建議）在原卷的位置，審題台「調整邊緣」要用
    page: 1
    rect: [265.73, 413.02, 371.32, 480.07]   # PDF 座標（point）：x0, y0, x1, y1
options:
- label: A
  content: ''                   # 選項只有圖時文字可空
  asset: {key: optA, file: doc_…/s2_5_optA.png, crop: {page: 1, rect: [...]}}
```

```yaml
shared_assets:
- key: g2_30_33                 # 建議 g{大題}_{第一題}_{最後一題}
  label: 題組附圖
  kind: figure
  file: doc_113_台中_曉明_自然_理化_g9s1e1/g2_30_33_f1.png
  used_by: [doc_…_s2_30, doc_…_s2_31, doc_…_s2_32, doc_…_s2_33]
  crop: {page: 2, rect: [...]}
```

- 圖要把圖內的標號（A、B、O、x、y、甲乙丙）一起框進去，不要框到旁邊的選項代號或下一題的文字。
- 校徽、浮水印、裝飾線都不算附圖。
- 純文字表格請轉成 Markdown 表格放進題幹，不要截圖；看不出表格結構的才當成圖。
- 掃描時整頁轉了 90 度的，先轉正再截圖。

---

## 4. 文字寫法（最重要）

**照抄原卷，不要改寫、潤飾、補字、翻譯。** 看不清楚的字寫成 `□`，不要猜。

| 要表達的 | 寫法 |
|---|---|
| 公式（數學、自然） | 行內 LaTeX，前後加 `$`：`$\frac{2}{3}$`、`$x^{2}$`、`$H_{2}O$`、`$\sqrt{5}$`、`$\overline{AB}$`、`$\angle A$`、`$\triangle ABC$`、`$\times$`、`$\div$`、`$\leq$` |
| 弧、向量、直線 | `$\overparen{AB}$`、`$\vec{a}$`、`$\overleftrightarrow{AB}$` |
| 其他符號 | 直接用 Unicode：∵ ∴ ≒ ° ⊥ ∥ ○ ● △ ※ → |
| **畫線處**（題目要你看的底線） | `<u>畫線部分</u>`；雙底線 `<u class="dbl">…</u>` |
| 作答空格 | 照原樣寫底線 `＿＿＿` 或 `（　）`，**不要**加 `<u>` |
| 表格 | Markdown 表格（`\| 甲 \| 乙 \|`＋分隔列 `\| --- \| --- \|`） |
| 粗體 | `**字**` |
| 換行 | 照卷面分行（YAML 字串裡的換行） |

- 普通數字、文字不要包進 `$`。英文句子裡的 `$100` 是錢，不用跳脫。
- **題組**：短文、閱讀測驗、克漏字、「根據下圖回答第 X～Y 題」這類共用素材，一律放 `group_stem`，同組每題都放同一段，**不可以**併進某一題的題幹或選項。克漏字題目沒有題幹時，`stem` 寫「（克漏字第N格）」。
- 字詞題（國字注音、注釋）題目只有兩三個字時，把大題的作答指示（例「一、國字注音」）放進 `group_stem`，單獨取用時才看得懂。
- 選項代號一律 A、B、C、D…，依卷面順序；原卷印錯代號（例如兩個 (C)）照順序改成正確代號。
- **原卷本身印錯字**：照抄原卷，在 `review_note` 寫「原卷疑誤：「錯」→「正」」，交由題庫這邊確認。
  題庫這邊確認後會改正，並在題末加註「（原卷有誤，已修正）」。
- 學生手寫的作答、老師批改（勾叉、分數、圈選）都不是題目內容，不要抄。

---

## 5. 會被品管剔除的情況（交件前請避免）

題庫匯入時逐題檢查（`apps/api/quality.py`），下列題目不會上線：

- 題幹空白或太短（字詞題有大題說明、題組有短文的除外）
- `single` 不是剛好 4 個選項；`tf` 有選項但不是 2 個
- 選項代號重複，或某個選項既沒文字也沒圖
- `$` 沒成對（LaTeX 沒閉合）；題幹括號 `(`、`)` 沒成對
- `assets` 有寫但沒有圖檔；`shared_asset` 指到不存在的 key
- 答案欄位有寫但是空的；選擇題的答案不在選項代號裡；答案寫「送分」「待確認」

`document` 少了 `title`、`school`、`exam_name`、`academic_year_roc`、`grade`、`subject` 任何一個，**整份卷**會被拒收。

---

## 6. 完整範例

取自題庫裡的真實卷（曉明國三理化），只列兩題示意；實際輸出要列出整份卷的每一題（同題組的第 31～33 題也要有）。

```yaml
document:
  id: doc_113_台中_曉明_自然_理化_g9s1e1
  grade: 9
  subject: 自然
  sub_subject: 理化
  academic_year_roc: 113
  semester: 1
  exam_seq: 1
  city: 台中
  school_short: 曉明
  school: 臺中市私立曉明女子高級中學
  title: 臺中市私立曉明女子高級中學 113學年度第1學期第1次定期評量 9年級自然（理化）科試題
  exam_name: 113學年度第1學期第1次定期評量
  scope_note: 國三理化第一章直線運動+2-1慣性定律
  sections:
  - {ord: 1, name: 是非題, count: 10}
  - {ord: 2, name: 單選題, count: 28}
  - {ord: 3, name: 填充題, count: 8}
  source_file: 國三/理化/113/1-1/台中/曉明.pdf
  extractor: vlm:gemini-3.7-flash
questions:
- id: doc_113_台中_曉明_自然_理化_g9s1e1_s2_13
  section: 2
  number: 13
  type: single
  page: 1
  stem: |-
    附圖是甲、乙兩車在同一直線跑道上的速度對時間（v-t）關係圖，則在如圖所示的時間（t＝0～4s）內，下列哪些推論正確？
    a、甲、乙兩車的移動方向相同
    b、在t＝0～1s內甲車的位移比乙大
    c、甲、乙兩車的加速度方向相反
    d、甲車的加速度量值小於乙車的加速度量值
    e、在t＝1s時兩車相遇。
  options:
  - {label: A, content: abc}
  - {label: B, content: abd}
  - {label: C, content: cde}
  - {label: D, content: bce}
  assets:
  - key: s2_13_f1
    kind: figure
    file: doc_113_台中_曉明_自然_理化_g9s1e1/s2_13_f1.png
    crop: {page: 1, rect: [265.73, 413.02, 371.32, 480.07]}
  source_ocr: true
  review_note: 掃描件：無文字層可驗證，內容為影像辨識
- id: doc_113_台中_曉明_自然_理化_g9s1e1_s2_30
  section: 2
  number: 30
  type: single
  page: 2
  group_stem: 甲將小球沿著光滑斜面向下滾動，同時以每秒拍攝10次的照相機拍攝其滾動過程，如圖為拍攝結果，試依圖回答下列問題：
  shared_asset: g2_30_33
  stem: 小球由C點到D點平均速度為多少公分/秒？
  options:
  - {label: A, content: '5'}     # 純數字的選項加引號，存成文字
  - {label: B, content: '10'}
  - {label: C, content: '25'}
  - {label: D, content: '40'}
  # 卷上有印答案（答案卷）時才填，例如：
  # answer: [D]
  # answer_status: verified
  # answer_source: answer_key
shared_assets:
- key: g2_30_33
  label: 題組附圖
  kind: figure
  file: doc_113_台中_曉明_自然_理化_g9s1e1/g2_30_33_f1.png
  used_by:                       # 用到這張圖的每一題（第 30～33 題）
  - doc_113_台中_曉明_自然_理化_g9s1e1_s2_30
  - doc_113_台中_曉明_自然_理化_g9s1e1_s2_31
  - doc_113_台中_曉明_自然_理化_g9s1e1_s2_32
  - doc_113_台中_曉明_自然_理化_g9s1e1_s2_33
  crop: {page: 2, rect: [492.0, 322.02, 702.69, 389.48]}
```

---

## 7. 交件前自我檢查

用 repo 裡的檢查程式（需要 `pyyaml`），會列出缺欄位、id 不合規則、圖檔不存在、會被品管剔除的題目：

```bash
python scripts/check_bank_yaml.py <輸出根目錄>/bank --assets <輸出根目錄>/assets
```

全部通過再交件。題庫這邊收到後依序做：查重、作答、單元與題型分類、匯入資料庫、AI 初審、上線審題台。
