# 高中段考卷 Word 管線：一條指令從原始 Word 檔做到交件包

給**跑批次的 agent**。照國中的做法：一條程式管線、程式自動驗證、不合格的題目自動剔除，不靠人逐題修。

- 執行 agent **只跑管線、不寫解析器、不手改 YAML**。
- 發現系統性錯誤（某種排版整批出錯）寫進回報，由督察修管線後重跑。
- 管線不用模型、不花錢。

## 0. 管線做什麼

| 步驟 | 程式 | 說明 |
|---|---|---|
| 1. 改寫 EQ 欄位 | `flatten_doc_eq.py` | 照 Word 97 片段表改寫題號、選項代號、(甲)、題組標題，否則 LibreOffice 轉檔會吃掉它們 |
| 2. 轉檔 | LibreOffice | 轉出 .docx（解析用）和原卷 PDF（`sources/`、附圖裁切用） |
| 3. 段落流 | `docx_stream.py` | 底線 `<u>`、上下標、表格、附圖位置、符號字型 |
| 4. 公式 | `ole_math.py`、`mtef3.py` | MathType 5 以上：公開的 Ruby gem `mathtype_to_mathml_plus`＋pandoc；Office 方程式編輯器 3.0：依公開 MTEF 3 規格解析。只輸出網站渲染器認得的 LaTeX |
| 5. 切題 | `word_paper.py` | 大題、題號（必須連號）、題組（含沒有標題的閱讀文章）、選項、答案區、詳解 |
| 6. 附圖 | `word_pipeline.py` | 用 Word 裡的圖片尺寸到 PDF 找位置，依每題在 PDF 的起點決定掛哪一題，從 PDF 裁 PNG（寬至少 300 像素） |
| 7. 把關 | 品管閘門＋檢查程式 3.4 | 逐題檢查，不合格的題目剔除並寫進 manifest 的 `dropped`；一份卷剔除超過 10%，或原卷文字覆蓋率不到 90%，整卷不交 |

## 0.1 督察實測（2026-11-11，28 份各科真實原卷，含 5 份試做卷）

| 項目 | 結果 |
|---|---|
| 收件 | 28／28 份，1,019 題；剔除 23 題（2.2%，原因都寫在 manifest）。v4.4 重跑 |
| 答案 | 有答案區的卷，選擇／填充題答案全部附上（manifest `answered`），逐題對原卷答案區 |
| 檢查程式 3.4 `--strict`（含原卷比對） | 錯誤 0 項 |
| 對照督察驗收通過的試做卷 | 答案全部一致；屏東地理 Pilot-1 規則 A、B 全卷正確（50／50） |
| 公式 | 樣本裡約 1,200 個 MathType／方程式編輯器公式全部轉成 LaTeX |
| 速度 | 一份卷約 7 秒，1,506 份單機約 3 小時 |
| 環境 | LibreOffice 24.2.7、字型 AR PL UMing／UKai TW、Noto Sans CJK TC |

剔除的多半是原卷本身的狀況：三選一的單選題、選項畫在圖裡、兩張圖在 PDF 上疊在一起。這些照「寧可漏抓，不可錯抓」不收。

## 1. 安裝（每個環境一次）

```bash
apt-get install -y libreoffice-writer pandoc ruby fonts-arphic-uming fonts-arphic-ukai fonts-noto-cjk
gem install mathtype_to_mathml_plus pry nokogiri --no-document
python3 -m pip install pyyaml pymupdf pillow olefile
```

- 管線會自己關掉 LibreOffice 的「MathType 轉 LibreOffice 公式」，公式一律保留原物件；所以裝不裝 LibreOffice Math、用官方版或發行版套件都一樣（裁示 #011）。
- **LibreOffice 要用正式版**，不要用 LibreOfficeDev、alpha、beta。版本合不合格**以回歸測試為準**（裁示 #010）：督察用 24.2.7；PHOENIX 的 7.4.7 實測 12 份與基準一致。
- **三套中文字型都要有**：AR PL UMing TW、AR PL UKai TW、Noto Sans CJK TC。
  - 原卷用的是 Windows 字型（新細明體、標楷體），管線會固定對到這三套。
  - 字型不同，PDF 的分頁和浮動圖位置就不同，附圖歸題的結果會和督察的不一樣（裁示 #007）。

管線啟動時會自己檢查，缺字型或用了開發測試版會拒絕執行，並把 LibreOffice 版本寫進 manifest 的 `environment`。

**新環境先跑回歸測試**（裁示 #009）：用督察的 28 份基準卷，題數、剔除數、答案數、PDF 頁數都要和督察一樣：

```bash
python3 scripts/round3_regress.py --root <集中庫本機資料夾> --out out/regress
```

- 基準原卷在集中庫，雲端搜尋不一定搜得到：`python3 scripts/round3_regress.py --list` 列出每份的雲端檔案 ID 與該放的路徑，照 ID 下載即可（知道連結就能讀）。
- 可以用 `--ids` 只測自己科目的基準卷；沒有 ruby 的環境，數學類基準卷會列為「留待完整環境」，不算失敗。
- 結束碼不是 0 就停下來，把輸出整段回報督察。

**環境檢查**一次列出所有缺的東西：

```bash
python3 scripts/word_pipeline.py --check-env
```

## 1.1 沒有網路的環境（裁示 #008）

雲端另有離線包 `OLE-QS_高中第三輪_離線安裝包_v4.2.zip`，解壓到交接包根目錄（和 `scripts/` 同一層），會多出 `offline/`：

| 缺的東西 | 離線怎麼裝 |
|---|---|
| Python 套件（pymupdf、pillow、pyyaml、olefile） | `python3 -m pip install --no-index --find-links offline/wheels pymupdf pillow pyyaml olefile`（Python 3.10 以上、x86_64 Linux） |
| 字型 AR PL UMing TW、AR PL UKai TW | 不用裝：管線自動使用 `offline/fonts/`（也可用 `--fonts <資料夾>` 指定） |
| ruby、pandoc（MathType 5 公式工具） | **可以沒有**：沒有 MathType 4／5 公式的卷照常做；有的卷在 manifest 的 `skipped` 標 `deferred: true`、原因「留待完整環境」，不算失敗，之後在有工具的環境跑同一行指令會自動補做 |
| LibreOffice 正式版 | 離線包沒附，必須事先就有 |

- 字型資料夾和系統字型的結果完全相同（督察實測：題目、附圖逐檔相同，PDF 頁數相同）。
- 樣本裡史地公、地科、國文都沒有 MathType 5 公式；數學、化學大多有，需要完整環境。

## 2. 原始檔

- 雲端「校名來源題庫集中庫」（Drive 資料夾 `1J0v41hy_nyoRarajkQ1BAf9uNdcz6CNG`）整個下載到本機，保留資料夾結構。
- 任務清單：`data/high_school/round3_tasks.json`。
  - 共 1,506 份，每份有卷 id、`rel_path`（集中庫裡的相對路徑）、科目、正式校名。

## 3. 分工與批次

照監工 12:22 派工令的分工：

| 執行 agent | 科目（任務清單的 subject） | 份數 |
|---|---|---|
| Codex | 數學、物理、化學、生物 | 496＋126＋136＋106 |
| worker-agent-phoenix-01 | 地理、公民與社會、歷史、地球科學、國文 | 94＋258＋246＋29＋15 |

份數以 `data/high_school/round3_tasks.json` 為準，共 1,506 份。

- **每科第 1 批 15 份**（`--offset 0 --limit 15`），就是 G2 關卡，督察抽驗通過才繼續。
- 之後每批 50 份：`--offset 15 --limit 50`、`--offset 65 --limit 50`，依此類推。

## 3.1 跑一批

```bash
python scripts/word_pipeline.py --root <集中庫本機資料夾> --out out/數學_第1批 --subject 數學 --offset 0 --limit 15
```

- `--subject`：任務清單的科目，可用的值有數學、物理、化學、生物、地球科學、國文、歷史、地理、公民與社會。
- `--offset`／`--limit`：第幾份開始、做幾份。
- 中斷了再跑同一行就好，做過的卷會跳過。
- 一份卷大約 7 秒，一批 50 份不到 10 分鐘。

輸出：

```
out/數學_第1批/
  manifest.json      papers（交件的卷，含 dropped 剔除題號與原因）、skipped（不交的卷與原因）
  bank/  assets/  sources/        交件內容
  rejected/          不交的卷（YAML、附圖、PDF），留給人看原因，不要交
```

## 4. 交件前（監工照督察授權書第 3 節驗收）

```bash
python scripts/check_bank_yaml.py out/數學_第1批/bank --assets out/數學_第1批/assets \
  --sources out/數學_第1批/sources --existing --strict > out/數學_第1批/check_report.txt
python scripts/round3_qa.py --batch <這批原始 .doc 所在資料夾> out/數學_第1批/bank > out/數學_第1批/qa_report.txt
```

- 兩個都要通過：檢查程式結束碼 0，`round3_qa.py` 最後一行「0 份有短少」。
  - 管線已經先跑過檢查程式，正常情況一定通過；不通過就是管線有錯，回報督察。
- 再人工抽 10 題對原卷 PDF，寫進 `SUPERVISOR_QA.md`。
- `bank/`、`assets/`、`sources/`、`manifest.json`、兩份報告、`SUPERVISOR_QA.md` 打包成 `ROUND3_{科目}_第N批.zip`（用 Python `zipfile` 打包，中文檔名才不會亂碼），上傳「第三輪交件」。
- 交件包超過上傳上限（例如 10MB）時，**檢查都通過後**才可分段上傳（裁示 #013）：
  - `split -b 9000000 -d -a 2 ROUND3_國文_第1批.zip ROUND3_國文_第1批.zip.part`，各段放同一個資料夾；
  - 回報整包的 SHA-256 與段數；監工用 `cat ROUND3_國文_第1批.zip.part* > ROUND3_國文_第1批.zip` 合併後比對 SHA-256。
- 已知限制（裁示 #013）：國文「子題式題組」（一題底下 (1)(2)(3) 各帶選項）管線不支援，題目會被剔除；剔除超過 10% 的卷照規則不交，在 manifest 寫明原因即可。

## 5. 不准做的事

- 不准手改 YAML、附圖、manifest；不准改 `scripts/` 任何程式。
- 不准把 `rejected/` 的卷放進交件包。
- 回報的數字必須照 `manifest.json`。

## 6. 回報系統性問題

在「ROUND3_進度回報」寫：

- 卷 id、題號；
- manifest 裡的剔除原因或不交原因；
- 原卷長什麼樣（截圖或文字描述）。

督察修管線後通知重跑。
