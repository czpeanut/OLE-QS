#!/usr/bin/env python3
"""高中題型清單（Excel）：已採收的單元題型＋建議補充的題型。 python scripts/senior_pattern_list.py → docs/SENIOR_PATTERN_LIST.xlsx"""
import collections, glob, gzip, json
from pathlib import Path

import yaml
from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

R = Path(__file__).resolve().parent.parent
Q = [json.loads(l) for l in gzip.open(R / "data/export_senior/questions.jsonl.gz", "rt", encoding="utf-8")]
used = collections.Counter(q["pattern"]["key"] for q in Q if q.get("pattern"))
SUBS = ["數學", "物理", "化學", "生物", "地科", "歷史", "地理", "公民與社會", "國文"]
subj_of = lambda q: q["sub_subject"] or q["subject"]
nq = collections.Counter(subj_of(q) for q in Q)
fmt = collections.defaultdict(collections.Counter)
for q in Q:
    s = subj_of(q)
    fmt[s][q["type"]] += 1
    fmt[s]["題組"] += bool(q["group_id"])
    fmt[s]["有圖"] += bool(q["images"] or q["shared_images"])
units = yaml.safe_load((R / "data/curriculum/senior.yaml").read_text(encoding="utf-8"))["subjects"]
unit_name = {str(u["code"]): (b["book"], u["title"]) for s, bs in units.items() for b in bs for u in b["units"]}
n_units = {s: sum(len(b["units"]) for b in bs) for s, bs in units.items()}

# ---- 已採收的單元題型
cat_rows, cat_stat = [], collections.defaultdict(lambda: [0, 0, 0, 0])
for f in sorted(glob.glob(str(R / "data/curriculum/senior_patterns/*/*.yaml"))):
    d = yaml.safe_load(open(f, encoding="utf-8"))
    s, code = d["unit"]["subject"], str(d["unit"]["code"])
    cat_stat[s][0] += 1
    for t in d.get("types") or []:
        n = used[f"{code}:{t['code']}"]
        cat_rows.append([s, unit_name.get(code, ("", ""))[0], code, d["unit"]["title"], t["code"], t["name"], t.get("desc", ""), n])
        cat_stat[s][1] += 1
        cat_stat[s][2] += n > 0
        cat_stat[s][3] += n

# ---- 答題形式與建議補充
F = lambda s, k: fmt[s][k]
def fm(s):
    return (f"單選 {F(s,'single'):,}、多選 {F(s,'multiple'):,}、填充／選填 {F(s,'fill'):,}、計算或非選 {F(s,'calc'):,}；"
            f"題組 {F(s,'題組'):,}、有圖 {F(s,'有圖'):,}")
HAVE, MISS = "已採收", "未採收（建議補）"
rows = []
def add(s, kind, name, desc, status, note=""):
    rows.append([s, kind, name, desc, status, note])

# 數學
for name, desc, st, note in [
    ("單選題", "五選一或四選一，概念判斷與簡單計算", HAVE, f"{F('數學','single'):,} 題"),
    ("多選題", "多個選項可選，常考概念辨析與命題真假", HAVE, f"{F('數學','multiple'):,} 題"),
    ("選填題", "學測式填空：答案是整數、分數或代號，需寫出精確值", HAVE, f"{F('數學','fill'):,} 題（最大宗）"),
    ("計算題／非選擇題", "完整列式計算並求值", HAVE, f"{F('數學','calc'):,} 題，比例偏低"),
    ("題組（共用情境）", "同一情境下多個子題", HAVE, f"{F('數學','題組'):,} 題"),
    ("證明題", "證明等式、不等式、幾何性質、歸納法", MISS, "分科測驗／段考非選常見；目前幾乎沒有"),
    ("作圖／描點題", "畫函數圖形、直線區域、向量圖", MISS, "需附圖作答格式，目前無"),
    ("建模應用題（素養）", "長文情境（利率、人口、測量、機率決策）轉成數學式", MISS, "108 課綱後逐年增加"),
    ("統計圖表判讀題", "盒狀圖、散布圖、迴歸直線、相關係數判讀", MISS, "有單元但缺少專門題型"),
    ("反例／命題真偽辨析", "指出命題錯誤處、舉反例", MISS, "多選題內有，但沒單獨標出"),
    ("參數範圍／存在性問題", "討論 k 的範圍、方程式有解條件", MISS, "各單元通用，建議列為跨單元題型"),
]: add("數學", "答題形式", name, desc, st, note)
# 自然
for s, extra in (("物理", [("實驗數據判讀與誤差分析", "表格、圖線求斜率、有效數字、誤差來源", MISS, "實驗單元尤其缺"),
                          ("作圖題（力圖、光路、電路）", "畫出受力、光線、等位線", MISS, ""),
                          ("單位換算與量級估算", "公式帶單位計算與估算", MISS, "")]),
                 ("化學", [("實驗操作與安全題", "滴定、蒸餾、萃取、焰色試驗操作判斷", MISS, "只在分離單元出現"),
                          ("反應式配平與計量計算", "配平、莫耳、限量試劑、產率", MISS, "可能被歸入計算，建議獨立"),
                          ("結構式／分子模型判讀", "路易斯結構、同分異構物、官能基辨識", MISS, "")]),
                 ("生物", [("實驗設計與變因控制", "操縱、控制、應變變因，對照組判斷", MISS, ""),
                          ("遺傳圖譜與機率計算", "譜系圖判讀、孤雄交配機率計算", MISS, "遺傳單元專門題型"),
                          ("科學閱讀素養（新聞、論文摘要）", "長文閱讀＋圖表，答題需推論", MISS, "")]),
                 ("地科", [("圖表判讀（天氣圖、地質圖、星圖）", "衛星雲圖、地質剖面、H-R 圖", HAVE, "有圖 662 題"),
                          ("計算題（地震、板塊、星體距離）", "震源距離、年代、視星等換算", HAVE, f"{F('地科','calc')} 題偏少"),
                          ("地質剖面與年代判讀", "地層層序、不整合、化石定年", MISS, "")])):
    add(s, "答題形式", "單選題", "四選一／五選一", HAVE, f"{F(s,'single'):,} 題")
    add(s, "答題形式", "多選題", "至少一個正確，錯一個以上不計分", HAVE, f"{F(s,'multiple'):,} 題")
    add(s, "答題形式", "題組", "共用圖表或實驗情境", HAVE, f"{F(s,'題組'):,} 題")
    add(s, "答題形式", "計算題（非選）", "列式計算", HAVE if F(s, "calc") > 100 else MISS, f"{F(s,'calc'):,} 題")
    add(s, "答題形式", "填充題", "填入數值或名詞", HAVE if F(s, "fill") > 50 else MISS, f"{F(s,'fill'):,} 題")
    for name, desc, st, note in extra:
        add(s, "答題形式", name, desc, st, note)
# 社會三科
add("歷史", "答題形式", "單選題", "最常見", HAVE, f"{F('歷史','single'):,} 題")
add("歷史", "答題形式", "多選題", "", HAVE, f"{F('歷史','multiple'):,} 題")
add("歷史", "答題形式", "史料閱讀題組", "文獻、圖片、地圖＋多子題", HAVE, f"題組 {F('歷史','題組'):,} 題")
for name, desc in [("時間軸／事件排序題", "把事件依發生先後排列（如 甲乙丙丁 的順序組合）"),
                   ("配合題（人物／事件／制度）", "左右兩欄配對"),
                   ("圖片、地圖、統計表判讀", "考古圖像、歷史地圖、貿易統計"),
                   ("因果與比較分析題", "比較兩個時期或地區的異同、影響"),
                   ("問答題／史料分析（非選）", "申論或簡答，需文字作答")]:
    add("歷史", "答題形式", name, desc, MISS, "目前資料沒有分類" if "排序" in name or "配合" in name else "")
add("地理", "答題形式", "單選題", "", HAVE, f"{F('地理','single'):,} 題")
add("地理", "答題形式", "圖表判讀題組", "氣候圖、等高線、人口金字塔、衛星影像", HAVE, f"題組 {F('地理','題組'):,}、有圖 {F('地理','有圖'):,} 題")
add("地理", "答題形式", "計算題", "比例尺、時區、人口密度、坡度", HAVE, f"{F('地理','calc'):,} 題")
for name, desc in [("等高線地形剖面判讀／繪製", "判斷坡向、剖面、視線"), ("區位與空間分布推論題", "給定條件選擇最適區位"),
                   ("統計表／資料分析題", "人口、產業、貿易統計數字推論"), ("問答題（非選）", "簡答、申論")]:
    add("地理", "答題形式", name, desc, MISS)
add("公民與社會", "答題形式", "單選題", "", HAVE, f"{F('公民與社會','single'):,} 題")
add("公民與社會", "答題形式", "多選題", "", HAVE, f"{F('公民與社會','multiple'):,} 題")
add("公民與社會", "答題形式", "情境／個案分析題組", "新聞、判決、案例", HAVE, f"題組 {F('公民與社會','題組'):,} 題")
add("公民與社會", "答題形式", "經濟計算題", "機會成本、彈性、GDP、損益", HAVE, f"{F('公民與社會','calc')} 題")
for name, desc in [("法條適用題", "給事實、判斷適用哪一條或結果"), ("供需／模型圖形判讀", "供給需求曲線移動、均衡變動"),
                   ("統計圖表判讀（問卷、選舉結果）", "民調、投票率、基尼係數"), ("問答題／申論", "立場論述、比較分析")]:
    add("公民與社會", "答題形式", name, desc, MISS)
# 國文
add("國文", "答題形式", "單選題", "", HAVE, f"{F('國文','single'):,} 題")
add("國文", "答題形式", "多選題", "", HAVE, f"{F('國文','multiple'):,} 題")
add("國文", "答題形式", "閱讀測驗題組", "", HAVE, f"題組 {F('國文','題組'):,} 題")
add("國文", "整體狀況", "整科採收量極少", "國文只有 568 題（其他科 1,280～11,220 題）", MISS, "需補採收，包含 33 個單元的題型目錄")
for name, desc in [("字音字形題", "國字注音、同音異字、形近字辨識"), ("語詞／成語運用題", "成語、虛詞、詞義辨析"),
                   ("文言文閱讀與翻譯", "課內文言、課外文言，單選或翻譯"), ("詩詞曲賦鑑賞", "意象、修辭、體裁"),
                   ("修辭與語文表達（病句、標點、排序）", "句子重組、改錯、標點"), ("文學史／國學常識", "作家作品、流派、經典"),
                   ("圖文／非連續文本閱讀", "圖表、廣告、報導與文章整合"), ("默寫與課文填空（非選）", "名句填空、理解式默寫"),
                   ("問答題（解釋、簡答）", "非選擇的短答"), ("作文／引導寫作", "短文寫作、情意題")]:
    add("國文", "答題形式", name, desc, MISS)
# 英文
for name, desc in [("詞彙題", "單字、片語、搭配詞"), ("綜合測驗（克漏字）", "短文挖空四選一"), ("文意選填", "短文＋共用詞庫"),
                   ("篇章結構", "句子歸位、段落排序"), ("閱讀測驗題組", "單篇或多篇，細節與推論"), ("混合題", "圖表＋短文＋多子題"),
                   ("翻譯（中翻英、英翻中）", "非選"), ("引導寫作", "非選作文"), ("文法改錯／句型改寫", "")]:
    add("英文（高中尚未採收）", "答題形式", name, desc, MISS, "高中題庫目前沒有英文科")

# ---- 工作簿
wb = Workbook()
HDR = PatternFill("solid", fgColor="DDE6F3")
def sheet(title, header, data, widths):
    ws = wb.create_sheet(title)
    ws.append(header)
    for r in data:
        ws.append(r)
    for c in ws[1]:
        c.font = Font(bold=True)
        c.fill = HDR
    for i, w in enumerate(widths, 1):
        ws.column_dimensions[get_column_letter(i)].width = w
    for row in ws.iter_rows(min_row=2):
        for c in row:
            c.alignment = Alignment(wrap_text=True, vertical="top")
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = ws.dimensions
    return ws

wb.remove(wb.active)
ov = []
for s in SUBS:
    has = s in cat_stat
    ov.append([s, nq[s], n_units.get(s, ""), cat_stat[s][0] if has else 0, cat_stat[s][1] if has else 0,
               cat_stat[s][2] if has else 0, cat_stat[s][3] if has else 0,
               "有單元題型目錄（草稿，內有相似題型待合併）" if has else "尚無單元題型目錄；題目還沒標題型"])
ov.append(["英文", 0, "", 0, 0, 0, 0, "高中題庫尚未採收"])
sheet("總覽", ["科目", "題數", "單元數", "有題型的單元", "題型數", "有題目用到的題型", "已標題型題數", "狀況"], ov, [14, 10, 10, 14, 10, 16, 14, 46])
sheet("答題形式與建議補充", ["科目", "類別", "題型", "說明", "狀態", "備註"], rows, [14, 10, 30, 46, 16, 34])
sheet("已採收單元題型", ["科目", "冊", "單元代號", "單元名稱", "題型代號", "題型名稱", "說明", "題數"], cat_rows, [10, 10, 14, 24, 8, 30, 60, 8])
out = R / "docs/SENIOR_PATTERN_LIST.xlsx"
wb.save(out)
print(out, len(cat_rows), len(rows))
for r in ov:
    print(r)
