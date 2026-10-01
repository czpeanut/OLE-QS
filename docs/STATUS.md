# 開發現況

> 最後更新：2026-09-29　｜　接手用文件，先讀這份再讀開發計畫

## 一句話

擷取管線與題庫應用的主幹都能運作，題庫已收錄 **6066 題**，涵蓋 9 個縣市、51 所學校、
4 個學年度、6 次段考時段。最大缺口是**答案覆蓋率只有 5%**，其次是 43 份掃描件完全沒處理。

## 題庫現況

| 項目 | 數字 |
|---|---|
| 已擷取 | 3,017 份視覺擷取＋168 份國一數學規則式舊卷，共 3,185 份（國一 1,291、國二 1,041、國三 853） |
| 題目 | 115,952 題，收錄 **113,388 題（97.8%）** |
| 其中掃描件 | 22,572 題（無文字層可驗證，見下方注意事項） |
| 題組 | 29,092 題屬於題組（閱讀測驗、克漏字、共用圖） |
| 有附圖 | 15,391 題 |
| 有答案 | 11,110 題 |
| 章節索引 | 88,951 題歸到教科書的節／課（見下節） |
| 擷取費用 | US$145.51（NT$4,729） |
| 章節歸類費用 | US$5.96（NT$194） |
| 合計 | **NT$4,923**（使用者上限 NT$5,000，達上限即停） |

尚未處理：約 2,550 份（預算上限停在清單第 3,019 份）；失敗 7 份
（5 份 RECITATION、2 份模型回應缺 candidates）。續跑同一份清單會自動跳過已完成的卷，
剩下的卷以每份約 NT$1.5 計，約需 NT$3,800：

```bash
python scripts/plan_rounds.py <考卷根目錄> -o rounds.txt && cut -f2 rounds.txt > list.txt
python scripts/vlm_extract.py --list list.txt --root <考卷根目錄> -o data/bank \
    --assets data/assets --ledger out/vlm_spend.json --budget-twd <上限> --workers 16
python scripts/repair_bank.py data/bank          # 套用擷取後補上的修正（免費）
python scripts/build_chapters.py data/bank --report docs/chapters.md
python scripts/classify_chapters.py data/bank --ledger out/chapter_spend.json --budget-twd <上限>
```

注意事項：
- **掃描件沒有文字層可以驗證**，內容是模型的影像辨識，錯字風險高於原生數位卷。
  這些題目帶有 `review_note: 掃描件…`，可依此篩選抽查。
- 國一數學有 168 份仍是規則式的舊結果（含已知的分數、次方攤平問題），
  會在後續輪次被視覺擷取的結果取代；這些卷沒有做章節歸類。
- 國文課名跨出版社合併，每冊列出的課數多於實際（不同版本的課文不同，多數卷沒印版本）。
- 約 80 題選擇題在題幹與題組說明中都找不到選項，由品管閘門剔除；需重新擷取才能補回。

## 已知問題

- **答案覆蓋率 5%** — 多數卷是純題目卷，沒附答案
- **掃描件未支援** —— 三批 336 份裡有 43 份是掃描件
- **114 批那 5 份是用有缺陷的管線抽的**，原始 PDF 不在版控裡，無法重跑
- 有 1 份卷（新北土城 113-1-3）的 PDF 字元對映壞掉（`x` 變成 `ݔ`，題號不在文字層裡），
  無法擷取，已跳過
- **跨區塊的分數會把文字搬到隔壁題** —— 分子與分母常被切在不同文字區塊，
  重建後的 token 只能放在其中一邊，另一邊的字就消失。實測 6149 題中有 11 題
  （0.18%）受影響，且原本就已破碎。要求同區塊可以完全避免，但會少掉 496 個
  正確的分數，因此選擇維持現況，並用「題幹括號未閉合」這條閘門規則擋下殘缺的題目
- 分類標籤未對應正式課綱代碼
- 校名跨批次統一目前是獨立步驟（`canonicalize_schools.py`），不是匯入時自動做

## 歷程

| 文件 | 內容 |
|---|---|
| `development-plan.md` | 原始開發計畫，含架構、資料模型、里程碑 |
| `poc-report-001.md` | 數學卷（掃描截圖）實測，含配分總和驗證等 5 項計畫修正 |
| `poc-report-002.md` | 自然科卷（原生數位）實測，含 SMask、共用素材等 3 項會讓題目報廢的發現 |

> 注意：開發計畫的部分內容已被後續實測推翻（最明顯的是校對工作台不再是瓶頸）。
> 兩份 PoC 報告記錄了推翻的過程與原因，比計畫本身更接近現況。

## 章節索引（2026-09-30）

章節表取自升學王「108 課綱國中版本對照表」（114、115 學年度版本）：國一至國三上下冊，
國文、數學、自然（生物、理化、地科）、社會（歷史、地理、公民）的翰林、康軒、南一三版本，
共 1,527 個單元。`scripts/build_curriculum.py` 直接讀 PDF 表格（不經模型），
結果在 `data/curriculum/junior.yaml`，可讀版本在 `docs/curriculum.md`。英文不在對照表內，
沿用 `build_chapters.py` 依考卷課名的歸類。

`scripts/classify_chapters.py` 一份卷呼叫一次 gemini-3.1-flash-lite：給該冊三版本的單元
（卷上印了版本就只給那一版）、考試範圍與全卷題目，模型判斷版本並逐題選單元。
候選單元依段考次別限縮（第 1 次最多到全冊 55%、第 2 次 85%），避免歸到還沒教的章節。
結果寫在題目的 `tags.chapter`（版本、冊、科、節次、節名、章），`tags.textbook` 是可讀標籤。

注意：
- 看不出版本時模型偏向猜翰林。數學、自然各版本節次大致對齊，影響小；社會科的節名可能套到翰林的。
- 國文字詞題（注音、字形、成語）多半看不出出自哪一課，約四分之一維持「第N次段考範圍」。
- 對照表是 114、115 學年度版本，題庫卷是 111～113 學年度，少數課次可能改版調動。

## 審題網頁

題庫超過單一網頁容量（256 MB），依年級分三站，審題編號跨站通用
（輸入別站的編號會提示並連過去）：

| 年級 | 網址 |
|---|---|
| 國一 | https://claude.ai/artifact/54hK3SSLDCFJThZm9Dju1x |
| 國二 | https://claude.ai/artifact/V1AG52HtTER52AMqnZ3LQt |
| 國三 | https://claude.ai/artifact/NZSZpSo7x5YeEEYnUKbN9c |

重建：`python scripts/build_review_site.py data/bank --assets data/assets -o out/rv7 --grade 7 --sites 7=…,8=…,9=…`。
回報存在各站的 `reports` 集合（文件 ID 是審題編號），審完的卷存在 `reviewed`。

### 附圖邊緣調整（2026-10-01）

每張附圖、選項圖下方有「調整邊緣」：顯示裁切範圍四周多留 8% 的原卷影像，拖曳四邊
（或點選後用方向鍵）調整，儲存到該站資料庫的 `crops` 集合。套用方式：

```bash
# 1. 匯出各站 crops 集合成 JSON（Claude 用 ArtifactData list，out_dir 指到 out/crops）
# 2. 從原卷 PDF 依新範圍重裁（220 DPI）並更新題庫的 crop 位置
python scripts/apply_crops.py out/crops --pdf-root <考卷根目錄>
# 3. 重建並重新發佈審題網頁
python scripts/build_review_site.py data/bank --assets data/assets -o out/rv7 --grade 7 \
    --sites 7=…,8=…,9=… --pdf-root <考卷根目錄>
```

舊卷的裁切位置用 `scripts/recover_crops.py` 以模板比對從 PDF 找回（35,084 張，6 張找不到）；
新擷取的卷在擷取時直接記錄。規則式舊卷的圖來源不同，沒有調整功能。
