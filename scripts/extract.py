#!/usr/bin/env python3
"""原生數位考卷 PDF → 結構化題目 YAML。

只處理有文字圖層的 PDF。文字直接讀出（無損），圖形由頁面區域渲染，
答案由答案頁的表格解析 —— 三者都不需要 OCR，也不需要視覺模型。

刻意寫成規則式而非模型式：這類考卷的版面規律性很高（題號、選項標籤、
大題標題都有固定寫法），規則涵蓋不到的題目會被下游的品管閘門剔除，
而不是被猜錯後混進題庫。**寧可漏抓，不可錯抓。**

用法:
    python scripts/extract.py 考卷.pdf -o out/extracted
    python scripts/extract.py 目錄/ -o out/extracted
"""

from __future__ import annotations

import argparse
import re
import sys
import unicodedata
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scripts.parse_answer_key import parse_pdf as parse_answers  # noqa: E402

try:
    import pymupdf as fitz
except ImportError:
    try:
        import fitz
    except ImportError:
        sys.exit("需要 PyMuPDF，請先執行：pip install pymupdf")

# 同一個年級有四種寫法：「一年級」「1年級」（校內序號）
# 與「七年級」「7年級」（課綱序號），同一所學校的相鄰兩次段考都可能不一致
GRADE_MAP = {"一": 7, "二": 8, "三": 9, "七": 7, "八": 8, "九": 9,
             "1": 7, "2": 8, "3": 9, "7": 7, "8": 8, "9": 9}
SEMESTER_MAP = {"一": 1, "二": 2, "1": 1, "2": 2}
CN_NUM = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6}

# 大題標題：「一、選擇題：(每題 4 分，共 40 分)」
SECTION_RE = re.compile(r"^\s*([一二三四五六七八九十])\s*[、.]\s*([^：:（(]{1,12})\s*[：:（(]?")
# 題目開頭：「14.(  )」「1.」「(  )1.」「(   ) 1.」「(B)10.」
# 作答括號可能在題號前也可能在題號後，兩種寫法在同一批考卷裡都很常見；
# 只認「題號在前」會讓括號在前的整份卷幾乎抓不到題目。
# 括號裡也可能已經填了答案（老師改過的卷），那也是題號。
# 題號後面緊接數字的不算（「(A) 1.5 公尺」是選項裡的小數，不是第 1 題）。
QSTART_RE = re.compile(r"^\s*(?:[(（]\s*[A-EＡ-Ｅ]?\s*[)）]\s*)?(\d{1,3})\s*[.、．](?!\d)\s*"
                       r"(\(\s*\)|（\s*）)?\s*")
# 選項：「(A)」「（Ａ）」，可連續出現在同一行
OPTION_RE = re.compile(r"[(（]\s*([A-EＡ-Ｅ])\s*[)）]")
# 答案頁不參與題目擷取。
#
# 判斷只看頁首那一小段，而且要比「有沒有出現這幾個字」更講究一點：
#   - 「答案卷」最常出現的地方其實是題目卷的注意事項（「請在答案卷上作答」），
#     整頁掃描的話這些卷會被當成答案頁丟掉，一題都抓不到。
#   - 但題目卷的頁首也會寫「本試卷(含作答卷)共3頁」，所以連頁首都不能只看有無。
# 因此改用「誰先出現」：答案類字樣排在試卷類字樣前面，才是答案頁。
# 「答案卷」前面接動詞時是指示語而非頁名（「請依照號碼依序填入答案卷」）；
# 單獨的「答案」也可能是答案頁的標題（「…數學科- 答案」），但同樣是注意事項的
# 高頻詞（「每個答案4分」「答案請寫在…」），所以排除接著量詞或動詞的用法。
ANSWER_PAGE_RE = re.compile(r"解答|解析卷|參考答案"
                            r"|(?<![填寫劃畫入在到])(?:答案卷|作答卷)"
                            r"|答案(?![卡欄卷0-9]|\s*[請寫填劃畫])")
# 大題標題也算「這是題目頁」的證據：答案頁的標題一定排在大題標題前面，
# 反過來若「二、填充題(每個答案4分…)」先出現，那個「答案」就只是配分說明。
QUESTION_PAGE_RE = re.compile(r"試題卷|題目卷|試卷|試題|[一二三四五六七八九十]\s*[、．]")
ANSWER_PAGE_HEAD_CHARS = 80
# 卷頭資訊（年級）只在這段範圍內找
GRADE_HEAD_CHARS = 200
# 一頁的影像置放數超過這個值就不是「有很多圖」，而是整頁文字被存成小圖。
# 實測有考卷把每個字都存成 12×12 的影像，單頁 15598 個置放 ——
# 照單全收的話一題會掛上兩千多張圖，圖檔目錄也會爆掉。
MAX_FIGURES_PER_PAGE = 60

# 題幹自己說「需要看圖」的寫法。圖形歸屬時用它加權。
# 涵蓋面要夠寬：漏掉一種寫法，那類題目的圖就會被旁邊不需要圖的題搶走
# —— 實測「圖為七年18班…次數分配折線圖」因為不是「右圖／如圖」開頭，
# 那張折線圖被判給了隔壁的「點P在第三象限」。
FIG_REF_RE = re.compile(
    r"右圖|下圖|上圖|左圖|如圖|附圖|依圖|由圖|圖[(（]|圖為|圖中|圖示"
    r"|折線圖|長條圖|圓形圖|統計圖|示意圖|分布圖|圖形|數線|坐標|方格")

TYPE_BY_NAME = [
    ("選擇", "single"), ("單選", "single"), ("多選", "multiple"),
    ("是非", "tf"), ("填充", "fill"), ("填空", "fill"),
    ("計算", "calc"), ("非選", "calc"), ("問答", "essay"),
    ("作文", "essay"), ("題組", "single"), ("配合", "matching"),
    ("翻譯", "essay"), ("默寫", "fill"), ("解釋", "fill"), ("國字", "fill"),
]


def norm(s: str) -> str:
    """正規化文字圖層的固定雜訊。

    實測兩所學校的考卷都有這兩種：
      - 中文與半形數字之間被插入多餘空白（「113 學年度第1 學期」）
      - 度數符號被編成半形片假名濁音符（「37 ﾟC」）
    """
    s = s.replace("ﾟ", "°").replace("゜", "°")
    s = unicodedata.normalize("NFKC", s)
    # 中文 + 空白 + 數字/英文 → 去掉空白（只去單一空白，保留刻意的排版空白）
    s = re.sub(r"(?<=[一-鿿]) (?=[0-9A-Za-z])", "", s)
    s = re.sub(r"(?<=[0-9A-Za-z]) (?=[一-鿿])", "", s)
    return s.strip()


CELL_NUM_RE = re.compile(r"^\s*[(（]?\s*(\d{1,3})\s*[)）]?\s*[.、．]?\s*$")


def table_text(tab) -> str:
    """把一個表格攤平成閱讀順序的文字，每題一行。

    考卷常把題目排進表格 —— 國文的字音字形、解釋題就是四欄兩題一列。
    逐行掃描文字區塊時，儲存格的順序會被打亂（同一列的兩題交錯），
    題號因此不再遞增而被切分規則整段丟棄。

    這裡改走表格結構：依「列優先」走訪儲存格，遇到只含題號的儲存格就換行，
    後續儲存格接在該題後面。一列有兩題時就正確拆成兩行。
    """
    lines: list[str] = []
    cur: list[str] = []
    try:
        rows = tab.extract()
    except Exception:
        return ""
    for row in rows:
        for cell in row:
            text = (cell or "").strip()
            if not text:
                continue
            if CELL_NUM_RE.match(text):
                if cur:
                    lines.append(" ".join(cur))
                cur = [text.rstrip(".、．") + "."]
            elif cur:
                cur.append(text)
            else:
                lines.append(text)
    if cur:
        lines.append(" ".join(cur))
    return "\n".join(lines)


# ───────────────────────── 分數結構重建 ─────────────────────────
#
# PDF 裡的分數沒有「分數」這個東西，只有三樣分開的物件：上面一列字、
# 一條線段、下面一列字。get_text() 只看得到文字，於是 ½ 被攤平成「1 2」，
# 而 -5½ 變成「-5 1 2」—— 題目字面上就已經是錯的，之後不管誰來作答
# （老師、模型）都在解一題不存在的題目。實測影響 21% 的題目。
#
# 重建的依據是那條線：找出分數線，再把貼著它上下兩側的字撿回來。

CJK_RANGE = ("㐀", "鿿")


def _has_cjk(s: str) -> bool:
    return any(CJK_RANGE[0] <= c <= CJK_RANGE[1] for c in s)


def fraction_bars(page) -> list[tuple[float, float, float]]:
    """頁面上所有可能是分數線的水平線，回傳 (x0, x1, y)。

    上限 60pt 是因為分數線只需容納分子分母，再長就是表格框線或填答底線。

    線段與矩形都要看 —— 有些排版軟體把分數線畫成線段（items 的 "l"），
    有些畫成高度不到 1pt 的填滿矩形（"re"）。只認線段的話，後者整份卷的
    分數都會漏掉（實測板橋卷即是如此）。
    """
    out = []
    for drawing in page.get_drawings():
        for item in drawing["items"]:
            if item[0] == "l":
                a, b = item[1], item[2]
                if abs(a.y - b.y) < 0.8 and 3 < abs(a.x - b.x) < 60:
                    out.append((min(a.x, b.x), max(a.x, b.x), (a.y + b.y) / 2))
            elif item[0] == "re":
                r = item[1]
                if r.height < 2 and 3 < r.width < 60:
                    out.append((r.x0, r.x1, (r.y0 + r.y1) / 2))
    return out


def _side_chars(chars, bx0, bx1, by, lo, hi):
    """分數線某一側的字元，且必須整個字都落在線的寬度之內。"""
    got = []
    for ch in chars:
        if ch["frac"] is not None or ch["c"].isspace():
            continue
        h = ch["h"] or 8.0
        if not (lo * h < ch["cy"] - by < hi * h):
            continue
        if ch["x0"] < bx0 - 2 or ch["x1"] > bx1 + 2:
            continue
        got.append(ch)
    return got


def find_fractions(page, chars: list[dict]) -> list[dict]:
    """配對分數線與其上下的字元，就地在 chars 標記歸屬。

    誤判的來源是表格框線與填答底線 —— 它們同樣是水平線，上下也同樣有字。
    分辨的關鍵是**貼合度**：分數線是為了分子分母而畫的，長度跟著它們走；
    框線則遠寬於碰巧落在範圍內的那幾個字。
    """
    found: list[dict] = []
    for bx0, bx1, by in fraction_bars(page):
        width = bx1 - bx0
        above = _side_chars(chars, bx0, bx1, by, -1.7, -0.15)
        below = _side_chars(chars, bx0, bx1, by, 0.15, 1.7)
        if not above or not below:
            continue

        num = "".join(c["c"] for c in sorted(above, key=lambda c: c["x0"])).strip()
        den = "".join(c["c"] for c in sorted(below, key=lambda c: c["x0"])).strip()
        if not num or not den or len(num) > 8 or len(den) > 8:
            continue
        if _has_cjk(num) or _has_cjk(den):
            continue
        # 分子分母都得有實質內容 —— 擋掉「……」與連續底線這類版面裝飾
        if not any(c.isalnum() for c in num) or not any(c.isalnum() for c in den):
            continue
        # 括號必須成對。答案格的編號「(1) (2)」上下相鄰時很像分子分母，
        # 湊出 \frac{(1}{4)} 這種東西 —— 真正的分數不會把括號拆開。
        if any(s.count("(") != s.count(")") or s.count("（") != s.count("）")
               for s in (num, den)):
            continue

        # 線的長度是照分子分母裡**較寬的那一側**畫的（1/12 的分子只有一個字，
        # 線卻有兩位數那麼寬），所以只能要求較寬的一側貼合，不能兩側都要求。
        span_n = max(c["x1"] for c in above) - min(c["x0"] for c in above)
        span_d = max(c["x1"] for c in below) - min(c["x0"] for c in below)
        if max(span_n, span_d) < width * 0.35:
            continue

        idx = len(found)
        for ch in above:
            ch["frac"], ch["role"] = idx, "num"
        for ch in below:
            ch["frac"], ch["role"] = idx, "den"
        found.append({"num": num, "den": den,
                      "anchor": min(c["x0"] for c in above)})
    return found


def text_blocks(page) -> list[tuple]:
    """與 page.get_text("blocks") 同形狀，但堆疊的分數已還原成 $\\frac{a}{b}$。

    分子與分母在 rawdict 裡是同一區塊的相鄰兩行。做法是照原本的行結構
    重組文字，把分子那一段換成 LaTeX，分母那幾個字直接略過；
    只剩空白的行（原本整行都是分母）就不輸出，免得多出空行。
    """
    raw = page.get_text("rawdict")["blocks"]

    chars: list[dict] = []
    for bi, blk in enumerate(raw):
        if blk.get("type") != 0:
            continue
        for li, line in enumerate(blk.get("lines", [])):
            for span in line.get("spans", []):
                for ch in span.get("chars", []):
                    x0, y0, x1, y1 = ch["bbox"]
                    chars.append({"c": ch["c"], "bi": bi, "li": li,
                                  "x0": x0, "x1": x1,
                                  "cy": (y0 + y1) / 2, "h": y1 - y0,
                                  "frac": None, "role": None})

    fracs = find_fractions(page, chars)
    if not fracs:
        return list(page.get_text("blocks"))

    grouped: dict[tuple[int, int], list[dict]] = {}
    for ch in chars:
        grouped.setdefault((ch["bi"], ch["li"]), []).append(ch)

    # 每個分數整頁只輸出一次，位置取它第一個字元出現的地方。
    # 不能改成「每行一次」—— 數學排版的 PDF 會把分子的每個字元各放一行
    # （x、－、20 各自成行），那樣同一個分數會被輸出三次。
    emitted: set[int] = set()

    out: list[tuple] = []
    for bi, blk in enumerate(raw):
        if blk.get("type") != 0:
            continue
        lines_out: list[str] = []
        for li, _ in enumerate(blk.get("lines", [])):
            buf: list[str] = []
            for ch in sorted(grouped.get((bi, li), []), key=lambda c: c["x0"]):
                fid = ch["frac"]
                if fid is None:
                    buf.append(ch["c"])
                    continue
                if fid not in emitted:
                    f = fracs[fid]
                    buf.append(f"$\\frac{{{f['num']}}}{{{f['den']}}}$")
                    emitted.add(fid)
                # 該分數的其餘字元（含分母）都已包在上面那個 token 裡
            text = "".join(buf)
            if text.strip():
                lines_out.append(text)
        body = "\n".join(lines_out)
        if body.strip():
            x0, y0, x1, y1 = blk["bbox"]
            out.append((x0, y0, x1, y1, body + "\n", bi, 0))
    return out


def page_items(page, tables) -> list[tuple]:
    """本頁的文字區塊與表格，落在表格範圍內的文字區塊會跳過。"""
    def inside_table(b) -> bool:
        cx, cy = (b[0] + b[2]) / 2, (b[1] + b[3]) / 2
        return any(x0 <= cx <= x1 and y0 <= cy <= y1
                   for x0, y0, x1, y1, _ in tables)

    blocks = [b for b in text_blocks(page)
              if (b[4] or "").strip() and not inside_table(b)]
    return blocks + tables


def is_two_column(items: list[tuple], mid: float) -> bool:
    """這一頁是不是雙欄排版。

    圖形歸屬必須知道這件事。單欄版面裡，「右圖」就放在題目文字的右側，
    x 座標自然超過頁面中線 —— 若無條件把它算成「右欄」，而題目文字算成
    「左欄」，兩者永遠配不起來，圖就掛不上任何題目。
    """
    right = [b for b in items if b[0] > mid]
    return len(right) >= max(3, len(items) * 0.2)


def reading_order(page, tables=None) -> list[tuple[float, float, float, float, str]]:
    """回傳依閱讀順序排好的文字區塊。雙欄時先左欄由上而下，再右欄。

    表格會先被抽出來整塊處理（見 table_text），落在表格範圍內的文字區塊
    則跳過，避免同一段文字出現兩次。表格偵測不便宜，呼叫端若已經算過
    就把結果傳進來。
    """
    mid = (page.rect.x0 + page.rect.x1) / 2
    tables = table_regions(page) if tables is None else tables
    items = page_items(page, tables)
    if not items:
        return []
    two_col = is_two_column(items, mid)

    def key(b):
        col = 1 if (two_col and b[0] > mid) else 0
        return (col, round(b[1], 1), round(b[0], 1))

    return sorted(items, key=key)


def table_regions(page) -> list[tuple[float, float, float, float, str]]:
    """頁面上真正的表格，回傳 (x0, y0, x1, y1, 攤平後的文字)。"""
    tables = []
    try:
        found = page.find_tables().tables
    except Exception:
        found = []
    page_area = max(1.0, page.rect.width * page.rect.height)
    for tab in found:
        # find_tables() 會在純文字的多欄版面上誤判出「表格」，
        # 一旦誤判，落在其範圍內的文字與圖形都會被吞掉 ——
        # 實測社會卷因此從 50 題掉到 27 題、圖從 10 張掉到 2 張。
        # 真正的表格必須同時滿足：至少 2x2、不佔滿整頁、儲存格填充率夠高。
        try:
            rows = tab.extract()
        except Exception:
            continue
        n_rows = len(rows)
        n_cols = max((len(r) for r in rows), default=0)
        if n_rows < 2 or n_cols < 2:
            continue
        x0, y0, x1, y1 = tab.bbox
        if (x1 - x0) * (y1 - y0) > page_area * 0.5:
            continue
        filled = sum(1 for r in rows for c in r if (c or "").strip())
        if filled < n_rows * n_cols * 0.5:
            continue
        text = table_text(tab)
        if text:
            tables.append((x0, y0, x1, y1, text))
    return tables


# ───────────────────────── 向量圖形 ─────────────────────────
#
# get_images() 只找得到「嵌入的點陣圖」。國中數學卷的幾何圖、數線、
# 長條圖多半是**向量繪製**的，在 get_images() 眼中根本不存在 ——
# 於是題幹寫著「右圖△ABC 有三條對稱軸」，卻沒有任何圖被掛上去。
# 這種題目模型會直接回報缺素材（校準時 70 題有 12 題如此），
# 老師看到的也是一題無法作答的題目。

def _drawing_rects(page) -> list:
    """向量繪圖元件的外框。

    刻意**不**排除純水平／純垂直的細線。直覺上細線是表格框線與填答底線、
    不是圖形，但坐標平面、數線、長條圖、方格紙正是由這種線組成的 ——
    排掉它們，這幾類圖會整批消失（只有三角形這種斜邊圖形留得下來）。
    該濾掉的東西改用「聚起來之後夠不夠像一張圖」來判斷。

    完美水平或垂直的線，外框的寬或高會是 0，Rect.is_empty 為真 ——
    但那正是坐標軸與數線本身。這裡給它一點厚度而不是丟掉它。
    """
    out = []
    for drawing in page.get_drawings():
        r = fitz.Rect(drawing["rect"])
        if r.width <= 0 and r.height <= 0:
            continue                       # 真的是一個點，沒有內容
        if r.width == 0:
            r.x1 = r.x0 + 0.5
        if r.height == 0:
            r.y1 = r.y0 + 0.5
        out.append(r)
    return out


def _merge_near(boxes: list, gap: float) -> list[tuple]:
    """把彼此靠近的外框合併，回傳 (合併後外框, 合併了幾個元件)。

    一張圖是由很多線段與曲線拼出來的；元件數量本身也是訊號 ——
    單獨一條線是底線，幾十條線聚在一起才是圖。
    """
    items = [(fitz.Rect(b), 1) for b in boxes]
    changed = True
    while changed:
        changed = False
        merged: list[tuple] = []
        while items:
            box, count = items.pop()
            touching = [it for it in items
                        if fitz.Rect(box.x0 - gap, box.y0 - gap,
                                     box.x1 + gap, box.y1 + gap).intersects(it[0])]
            if touching:
                changed = True
                for it in touching:
                    items.remove(it)
                    box = box | it[0]
                    count += it[1]
            merged.append((box, count))
        items = merged
    return items


def split_answer(raw: str, q: dict) -> list[str]:
    """答案卷的一格 → 答案清單。

    多選題的答案寫在同一格裡（「B、D」），照原樣存成單一字串的話，
    它既不等於 B 也不等於 D，跟選項對不起來，最後被品管閘門當成壞答案剔除
    —— 那是一個本來完全正確、只是沒被拆開的答案。

    選項代號也順手轉成大寫：有的答案卷寫小寫 d，題目卷的選項卻是 D。
    """
    text = (raw or "").strip()
    labels = {o.get("label") for o in (q.get("options") or [])}
    if not labels:
        return [text]

    parts = [p.strip() for p in re.split(r"[、,，/／\s]+", text) if p.strip()]
    upper = [p.upper() for p in parts]
    if upper and all(p in labels for p in upper):
        return upper
    return [text]


def drop_page_furniture(page, rects: list) -> list:
    """濾掉不是題目附圖、而是版面本身的東西。點陣圖與向量圖都要過這一關。

    兩種都會被誤當成附圖掛到題目上，而且都比「沒有圖」更糟 ——
    那題看起來有圖，圖裡卻不是這題要的東西：

    浮水印：校名與「試題僅供參考」的底圖蓋住整片題目文字，照它的範圍渲染，
        出來的是好幾題的題目文字。真正的附圖裡只有零星標號（A、B、O、x），
        區域內的字數是最乾淨的分野。

    卷頭卷尾：校名橫幅、科目姓名座號的表格、頁尾裝飾線。它們橫貫版面寬度，
        而且貼在頁面的上下緣。
    """
    words = [fitz.Rect(w[:4]) for w in page.get_text("words")]
    keep = []
    for r in rects:
        box = fitz.Rect(r)
        if sum(1 for w in words if box.intersects(w)) > 25:
            continue
        wide = box.width > page.rect.width * 0.6
        if wide and (box.y0 < page.rect.height * 0.12
                     or box.y1 > page.rect.height * 0.92):
            continue
        keep.append(r)
    return keep


def vector_figures(page, tables, taken, min_side=28.0, min_area=2200.0) -> list:
    """向量繪製的圖形區域。tables／taken 內的區域會排除。

    會把緊鄰的短文字一起框進來：三角形的頂點標號 A、B、C 是文字不是繪圖，
    不在繪圖外框內，只框繪圖的話會渲染出一個沒有標號的三角形 ——
    而題目問的往往正是「∠A 是幾度」。
    """
    page_area = max(1.0, page.rect.width * page.rect.height)
    labels = []
    for blk in page.get_text("dict")["blocks"]:
        if blk.get("type") != 0:
            continue
        for line in blk.get("lines", []):
            for span in line.get("spans", []):
                if len((span.get("text") or "").strip()) <= 6:
                    labels.append(fitz.Rect(span["bbox"]))

    out = []
    for box, parts in _merge_near(_drawing_rects(page), gap=12.0):
        if box.width < min_side or box.height < min_side:
            continue
        if box.width * box.height < min_area:
            continue
        if box.width * box.height > page_area * 0.35:
            continue
        # 只由一兩個元件組成、又沒有相當面積的，多半是框線而不是圖
        if parts < 3 and box.width * box.height < min_area * 3:
            continue
        if any(fitz.Rect(t[:4]).intersects(box) for t in tables):
            continue
        if any(fitz.Rect(r).intersects(box) for r in taken):
            continue
        grown = fitz.Rect(box.x0 - 10, box.y0 - 10, box.x1 + 10, box.y1 + 10)
        for lab in labels:
            if grown.intersects(lab):
                box = box | lab
        out.append(box)
    return out


def is_answer_page(page_text: str) -> bool:
    """這一頁是答案卷／解答／空白作答卷嗎？（見 ANSWER_PAGE_RE 的說明）"""
    head = norm(page_text[:ANSWER_PAGE_HEAD_CHARS * 2].replace("\n", " "))
    head = head[:ANSWER_PAGE_HEAD_CHARS]
    ans = ANSWER_PAGE_RE.search(head)
    if not ans:
        return False
    que = QUESTION_PAGE_RE.search(head)
    return que is None or ans.start() < que.start()


def parse_grade(t: str) -> int | None:
    """從卷面文字判斷年級。

    考卷寫年級的方式沒有共識，實測到的至少五種都在這裡：
    「七年級」「一年級」「國一」「七學級」（錯字），以及只印在
    作答欄位上的「一年　班　號」。

    都沒有時退而求其次看**冊次**：教科書一冊對應一個學期，
    第一、二冊＝七年級，三、四＝八年級，五、六＝九年級。
    這是課綱的固定對應，不是推測。
    """
    # 由強到弱三層證據，強的有命中就不看弱的
    tiers = (
        # 明寫年級。數字可能被括號或方括號包起來（「【七】年級」），
        # 「七學級」是實測到的錯字，「七年 ___班」是作答欄位
        [r"[【（(\[]?\s*([一二三七八九1-3789])\s*[】）)\]]?\s*年級",
         r"([一二三七八九])\s*[年學][\s_＿]*[級班]"],
        [r"國([一二三])"],
        # 冊次：教科書一冊對應一個學期，第一、二冊＝七年級，三、四＝八年級，
        # 五、六＝九年級。這是課綱的固定對應，不是推測。
        [r"第([一二三四五六])冊", r"\bB([1-6])\b"],
    )
    def volume_to_grade(g: str) -> int:
        return 7 + ((CN_NUM.get(g) or int(g)) - 1) // 2

    # 一頁上可能出現好幾個年級，而且互相矛盾：題目內文會寫「某校二年級學生…」，
    # 有學校把上一屆的頁首留著（頁首「九年級數學-p1」，卷名卻是「(七年級)」）。
    # 卷名才是這份卷的年級，所以取**離「學年度」最近**的那個。
    # 沒有「學年度」可當錨點時只看頁首那一段，不然會抓到題目內文裡的年級。
    anchor = t.find("學年度")
    scope = t if anchor >= 0 else t[:GRADE_HEAD_CHARS]
    for i, patterns in enumerate(tiers):
        hits = [(m.start(), m.group(1)) for p in patterns for m in re.finditer(p, scope)]
        if not hits:
            continue
        _, g = (min(hits, key=lambda h: abs(h[0] - anchor)) if anchor >= 0
                else min(hits))
        return volume_to_grade(g) if i == 2 else GRADE_MAP[g]
    return None


def parse_school(t: str) -> str | None:
    """從卷面文字抓完整校名。

    校名前面常黏著別的字（「第1頁高雄市立大灣國民中學」「第2學期臺中市立
    向上國民中學」），所以縣市必須直接寫進樣式裡比對 ——
    寫成「任意兩三個中文字＋市/縣」再事後檢查是不夠的：那樣會先比對到
    「期臺中市」，判定縣市不合法後就跳過整段，真正的校名反而抓不到。
    """
    m = SCHOOL_RE.search(t)
    return re.sub(r"\s+", "", m.group(0)) if m else None


SUBJECTS = ("數學", "國文", "英語", "英文", "自然", "理化", "生物", "地球科學",
            "地科", "社會", "歷史", "地理", "公民", "健康教育", "健教", "體育",
            "藝術", "音樂", "表演藝術", "視覺藝術", "科技", "資訊")


def parse_subject(t: str) -> str | None:
    """抓科目。只認課程清單裡的名稱，且必須出現在「這是哪一科」的位置上。

    早期寫法是「取『科』字前面的 1~3 個中文字」，但卷頭把科目黏在別的字後面
    （「第2次段考數學科試題」），就會切出「考數學」「級數學」這種科目名 ——
    一個科目在題庫裡出現三種寫法，科目篩選就廢了。

    先把空白全部去掉再比對：卷頭為了排版會把科目名拆開（「科目:數 學(代碼03)」）。
    """
    flat = re.sub(r"\s+", "", t)
    for name in SUBJECTS:
        if any(pat in flat for pat in (
                f"{name}科", f"{name}領域", f"科目:{name}", f"科目：{name}",
                f"年級{name}", f"{name}試題", f"{name}試卷", f"{name}題目卷")):
            return name
    return None


def parse_header(text: str) -> dict:
    """從第一頁抓考卷的來源資訊。"""
    t = norm(text.replace("\n", " "))
    meta: dict = {}
    if m := re.search(r"(\d{3})學年度", t):
        meta["academic_year_roc"] = int(m.group(1))
    if m := re.search(r"第([一二12])學期", t):
        meta["semester"] = SEMESTER_MAP.get(m.group(1))
    if m := re.search(r"第([一二三四1-4])次", t):
        g = m.group(1)
        meta["exam_seq"] = CN_NUM.get(g) or int(g)
    if school := parse_school(t):
        meta["school"] = school
    if grade := parse_grade(t):
        meta["grade"] = grade
    if subject := parse_subject(t):
        meta["subject"] = subject
    if m := re.search(r"[◎※]?\s*(?:段考|命題)?範圍[：:]\s*([^\n]{1,60})", t):
        meta["scope_note"] = m.group(1).strip("（） ")
    return meta


def section_type(name: str) -> str:
    for kw, t in TYPE_BY_NAME:
        if kw in name:
            return t
    return "fill"


def split_options(text: str) -> tuple[str, list[dict]]:
    """把一段題目文字切成 (題幹, 選項)。找不到選項就整段當題幹。"""
    hits = list(OPTION_RE.finditer(text))
    if len(hits) < 2:
        return text.strip(), []
    stem = text[: hits[0].start()].strip()
    opts = []
    for i, m in enumerate(hits):
        end = hits[i + 1].start() if i + 1 < len(hits) else len(text)
        label = unicodedata.normalize("NFKC", m.group(1))
        content = text[m.end():end].strip(" 　\n")
        opts.append({"label": label, "content": content})
    return stem, opts


def decode_mojibake(s: str) -> str:
    """還原 unzip 對非 ASCII 檔名的 #UXXXX 編碼。"""
    return re.sub(r"#U([0-9a-fA-F]{4})", lambda m: chr(int(m.group(1), 16)), s)


CITY_SET = {"台北", "新北", "桃園", "台中", "台南", "高雄", "基隆", "新竹", "嘉義",
            "苗栗", "彰化", "南投", "雲林", "屏東", "宜蘭", "花蓮", "台東",
            "澎湖", "金門", "連江", "臺北", "臺中", "臺南", "臺東"}

# 校名樣式（parse_school 用）：縣市名直接列舉，長的排前面避免被短的截斷。
# 中間允許空白 —— 卷頭常為了排版把校名拆開（「台北市立 新興國民中學」）。
SCHOOL_RE = re.compile(
    r"(?:" + "|".join(sorted(CITY_SET, key=len, reverse=True)) + r")"
    r"\s*[市縣]\s*立?\s*[一-鿿]{2,6}?\s*(?:國民中學|國中|高級中學國中|高級中學)")

# 縣與市不能猜 ——「彰化市溪湖國中」是錯的校名，正確是「彰化縣立溪湖國中」。
# 只在卷面沒印校名、必須自行組出來時才會用到這張表。
COUNTY_CITIES = {"苗栗", "彰化", "南投", "雲林", "屏東", "宜蘭",
                 "花蓮", "台東", "澎湖", "金門", "連江"}


def parse_path(path: Path, root: Path | None = None) -> dict:
    """從檔案路徑取來源資訊。

    整批考古題的目錄慣例是「學年度／學期-次數／縣市／學校.pdf」，
    例如 112/1-2/彰化/埔心.pdf ＝ 112 學年度第 1 學期第 2 次段考。
    這比解析卷頭可靠得多 —— 有相當比例的考卷根本沒在題目卷上印
    學校或學年度，那些資訊只存在於檔名與資料夾。

    ⚠️ 「1-2」曾被讀成「一年級第二學期」。實際是「第一學期第二次段考」，
    卷頭寫得很清楚（「111學年度第一學期第二次段考」），而且一個學年只有
    兩個學期卻有 1-3、2-3 這樣的目錄，年級解讀根本擺不下。
    **年級不在路徑裡**，只能從卷面取得。
    """
    parts = [decode_mojibake(x) for x in path.parts]
    meta: dict = {}
    for i, part in enumerate(parts):
        norm_part = part.replace("臺", "台")
        if norm_part in {c.replace("臺", "台") for c in CITY_SET}:
            meta["city"] = norm_part
            if i + 1 < len(parts):
                meta["school_short"] = Path(parts[i + 1]).stem
        if re.fullmatch(r"1\d{2}", part):
            meta["academic_year_roc"] = int(part)
        if m := re.fullmatch(r"([12])-([1-4])", part):
            meta["semester"] = int(m.group(1))
            meta["exam_seq"] = int(m.group(2))
    return meta


def build_school(city: str | None, short: str | None) -> str | None:
    """卷面沒印校名時，用縣市＋校名簡稱組一個。"""
    if not (city and short):
        return None
    kind = "縣" if city.replace("臺", "台") in COUNTY_CITIES else "市"
    return f"{city}{kind}立{short}國中"


def extract_pdf(path: Path, dpi: int = 200, fig_dir: Path | None = None,
                school_names: dict[tuple[str, str], str] | None = None,
                default_subject: str | None = None,
                default_grade: int | None = None) -> dict | None:
    doc = fitz.open(path)
    meta = parse_header(doc[0].get_text("text"))
    # 路徑優先於卷頭：卷頭常缺學年度與學校，路徑的目錄慣例則穩定。
    # 但年級不在路徑裡（見 parse_path），只能靠卷面。
    path_meta = parse_path(path)
    meta.update({k: v for k, v in path_meta.items() if v})
    if not meta.get("academic_year_roc"):
        # 卷頭沒印學年度時，在全文找一次
        allyears = re.findall(r"(1\d{2})\s*學年度", " ".join(
            pg.get_text("text") for pg in doc))
        if allyears:
            meta["academic_year_roc"] = int(max(set(allyears), key=allyears.count))
    if not meta.get("grade"):
        # 年級常只印在後面幾頁的頁首。只看每頁開頭一小段，
        # 避免把題目內文裡的「八年級的學生…」當成本卷的年級。
        for pg in list(doc)[1:]:
            if grade := parse_grade(norm(pg.get_text("text")[:200].replace("\n", " "))):
                meta["grade"] = grade
                break

    # ── 校名 ───────────────────────────────────────────────
    # 卷面印出來的校名是最權威的（「彰化縣立溪湖國中」），優先採用，
    # 但要與路徑的校名簡稱對得上，避免抓到別份卷或頁碼黏成的字串。
    short = meta.get("school_short")
    # 全批對照表優先於這一份卷自己印的校名：同一所學校在不同次段考會寫成
    # 不同的名字，照抄的話一所學校會在題庫裡分裂成好幾所（見 collect_school_names）。
    canonical = (school_names or {}).get((meta.get("city"), short))
    header_school = meta.get("school")
    if header_school and short and short not in header_school:
        header_school = None
    meta["school"] = (canonical or header_school
                      or build_school(meta.get("city"), short))

    if not meta.get("subject") and default_subject:
        meta["subject"] = default_subject
    if not meta.get("grade") and default_grade:
        meta["grade"] = default_grade
    for field in ("academic_year_roc", "grade", "subject", "school"):
        if not meta.get(field):
            print(f"    ⚠ {path.name}：抓不到 {field}，跳過（來源標註不可缺）")
            doc.close()
            return None

    # 文件 ID 必須同時含「科目」與「第幾次段考」——
    # 一所學校同一學期有 3 次段考，一次段考又有 5 個科目的卷，
    # 少了任何一個都會撞成同一個 ID，後匯入的那份直接覆蓋前一份。
    stem_id = re.sub(r"[^\w]+", "_", decode_mojibake(str(path.stem))).strip("_").lower()
    stem_id = (f"{meta.get('city','')}_{stem_id}_{meta['subject']}"
               f"_g{meta['grade']}s{meta.get('semester','?')}e{meta.get('exam_seq','?')}")
    stem_id = re.sub(r"[^\w]+", "_", stem_id).strip("_")
    doc_id = f"doc_{meta['academic_year_roc']}_{stem_id}"

    sections: list[dict] = []
    questions: list[dict] = []
    figures: list[dict] = []
    cur_section = 0
    cur: dict | None = None
    buf: list[str] = []
    lead: list[str] = []
    last_num: dict[int, int] = {}
    section_lead: dict[int, str] = {}

    def flush() -> None:
        nonlocal cur, buf
        if cur is None:
            return
        text = norm(" ".join(buf))
        stem, opts = split_options(text)
        cur["stem"] = stem
        if opts:
            cur["options"] = opts
            cur["type"] = "single" if len(opts) >= 2 else cur["type"]
        questions.append(cur)
        cur, buf = None, []

    for pno, page in enumerate(doc, start=1):
        page_text = page.get_text("text")
        if is_answer_page(page_text):
            continue                      # 答案頁另外由 parse_answer_key 處理

        # 圖形位置，之後依 y 座標歸給題目
        mid = (page.rect.x0 + page.rect.x1) / 2
        tables = table_regions(page)
        two_col = is_two_column(page_items(page, tables), mid)
        placements = []
        for info in page.get_images(full=True):
            placements.extend(page.get_image_rects(info[0]))
        if len(placements) > MAX_FIGURES_PER_PAGE:
            print(f"    ⚠ {path.name} p{pno}：{len(placements)} 個影像置放，"
                  f"整頁文字被存成小圖，本頁不取圖形")
            placements = []
        # 向量繪製的圖形（幾何圖、數線、長條圖）——
        # 已經被點陣圖蓋到的區域不重複取
        placements += vector_figures(page, tables, placements)
        placements = drop_page_furniture(page, placements)
        if len(placements) > MAX_FIGURES_PER_PAGE:
            print(f"    ⚠ {path.name} p{pno}：{len(placements)} 個圖形區域，"
                  f"版面判讀可能有誤，本頁不取圖形")
            placements = []
        two_col_figs = sorted(placements,
                              key=lambda r: (1 if (two_col and r.x0 > mid) else 0, r.y0, r.x0))
        for i, r in enumerate(two_col_figs, start=1):
            key = f"p{pno}_f{i:02d}"
            # 圖檔名必須帶文件 ID。「p1_f01.png」在每一份卷裡都會出現，
            # 全部寫進同一個資產目錄的話會互相覆蓋 —— 實測 673 筆圖形紀錄
            # 只剩 136 個檔案，題目顯示的是別間學校的圖。
            # 缺圖只是那題被剔除，貼錯圖卻是看起來能作答但整題是錯的。
            rel = f"{doc_id}/{key}.png"
            if fig_dir:
                (fig_dir / doc_id).mkdir(parents=True, exist_ok=True)
                clip = fitz.Rect(r.x0 - 2, r.y0 - 2, r.x1 + 2, r.y1 + 2) & page.rect
                # 退化的圖形區域（寬或高為 0）渲染時會丟例外。一張圖不值得
                # 中斷整批 —— 少一張圖只是那題被品管閘門剔除，中斷卻是全批停擺。
                if clip.is_empty or clip.width < 1 or clip.height < 1:
                    continue
                try:
                    page.get_pixmap(dpi=dpi, clip=clip).save(fig_dir / rel)
                except Exception as exc:
                    print(f"    ⚠ {path.name} p{pno} {key}：圖形渲染失敗（{exc}），略過")
                    continue
            figures.append({"key": key, "page": pno,
                            "col": 1 if (two_col and r.x0 > mid) else 0,
                            "y": r.y0, "y1": r.y1, "file": rel})

        for b in reading_order(page, tables):
            x0, y0, _, _, raw = b[0], b[1], b[2], b[3], b[4]
            for line in raw.splitlines():
                line = line.rstrip()
                if not line.strip():
                    continue
                text = norm(line)

                if m := SECTION_RE.match(text):
                    flush()
                    lead = []
                    cur_section += 1
                    name = f"{m.group(1)}、{m.group(2).strip()}"
                    sections.append({"ord": cur_section, "name": name,
                                     "type": section_type(name),
                                     "score_rule": text})
                    continue

                if m := QSTART_RE.match(text):
                    num = int(m.group(1))
                    # 題號在同一大題內必然「小幅遞增」。
                    # 只用「必須遞增」不夠：內文中一個大號碼（例：年份、頁碼）
                    # 被誤判成題號後，後面所有真題號都會低於它而被整段擋掉
                    # —— 實測社會卷因此從 50 題掉到 17 題。
                    # 容許最多跳 3 號，吸收少數真的沒解析出來的題目。
                    prev = last_num.get(max(cur_section, 1), 0)
                    if not (prev < num <= prev + 3):
                        if cur is not None:
                            buf.append(text)
                        continue
                    last_num[max(cur_section, 1)] = num
                    if lead and cur_section and cur_section not in section_lead:
                        text_lead = " ".join(lead).strip()
                        if len(text_lead) >= 20:
                            section_lead[cur_section] = text_lead
                        lead = []
                    flush()
                    stype = (sections[cur_section - 1]["type"]
                             if sections and cur_section else "fill")
                    cur = {"id": f"{doc_id}_s{max(cur_section,1)}_{m.group(1)}",
                           "section": max(cur_section, 1),
                           "number": num,
                           "type": stype, "page": pno,
                           "_col": 1 if (two_col and x0 > mid) else 0, "_y": y0}
                    buf = [text[m.end():]]
                    continue

                if cur is not None:
                    buf.append(text)
                elif cur_section:
                    # 題號出現前的文字＝這個大題的共用前文。
                    # 克漏字與閱讀測驗的題目本身沒有題幹，全靠這段短文；
                    # 丟掉它，那些題目會變成空題幹而被剔除。
                    lead.append(text)
    flush()
    doc.close()

    # ── 圖形歸屬：同頁同欄、位置落在本題與下一題之間 ──────────────
    # 一張圖只能歸給一題。跨欄或跨頁時「下一題」的位置判斷會失效，
    # 若不去重，同一張圖會被多題認領，寫入時撞上唯一性約束。
    claimed: set[str] = set()
    # 沒有題幹的題目（克漏字、閱讀測驗）補上該大題的共用前文
    for q in questions:
        if not (q.get("stem") or "").strip() and q["section"] in section_lead:
            q["group_stem"] = section_lead[q["section"]]
            q["stem"] = f"依上文選出第 {q['number']} 格最適當的答案"

    # 每題在版面上佔的垂直範圍：從自己的第一行到下一題的第一行。
    spans: dict[str, tuple[float, float]] = {}
    for i, q in enumerate(questions):
        nxt = questions[i + 1] if i + 1 < len(questions) else None
        same_flow = (nxt is not None and nxt["page"] == q["page"]
                     and nxt["_col"] == q["_col"])
        spans[q["id"]] = (q["_y"] - 4, nxt["_y"] if same_flow else float("inf"))

    # 用「圖與題目垂直範圍的重疊量」歸屬，而不是「圖的頂端落在誰的範圍內」。
    #
    # 「右圖」是排在題目文字**右側**的，它的頂端通常比題目的第一行還高一點
    # ——用頂端判斷會把它判給上一題。實測某卷第 7 題寫著「如右圖(一)的坐標
    # 平面」，那張坐標圖卻被掛到第 6 題的「營隊分組」上：兩題都看起來有圖，
    # 兩題都是錯的。改看重疊量，圖就會落在真正涵蓋它的那一題。
    for f in figures:
        best, best_score = None, 0.0
        for q in questions:
            if f["page"] != q["page"] or f["col"] != q["_col"]:
                continue
            top, bottom = spans[q["id"]]
            overlap = min(f["y1"], bottom) - max(f["y"], top)
            if overlap <= 0:
                continue
            # 題幹明講「右圖／如圖」的，同分時優先 —— 那是題目自己說它需要圖
            score = overlap * (1.5 if FIG_REF_RE.search(q.get("stem") or "") else 1.0)
            if score > best_score:
                best, best_score = q, score
        if best is None:
            continue
        claimed.add(f["key"])
        best.setdefault("assets", []).append(
            {"key": f["key"], "kind": "figure", "file": f["file"]})

    # ── 掛答案 ────────────────────────────────────────────────
    keys = parse_answers(path)
    by_section: dict[int, dict[int, str]] = {}
    for pg in keys["answer_pages"]:
        if pg.get("likely_blank_sheet"):
            continue
        for e in pg["entries"]:
            by_section.setdefault(e["table_index"], {})[e["number"]] = e["answer"]

    # 答案表與大題的配對：依「題號集合的重疊度」比對，不要求數量相同。
    # 數學卷有 3 個大題但只有 2 張答案表（計算題的答案是詳解文字，不成表），
    # 若要求數量相同就會整份卷掛不上答案。
    # 一個大題的答案可能拆成多張表（國文選擇題就拆成 1~29 與 30~35 兩張），
    # 所以不是「一個大題配一張表」，而是把所有「題號多半落在本大題內」的表合併。
    used: set[int] = set()
    for sec in sections:
        nums = {q["number"] for q in questions if q["section"] == sec["ord"]}
        if not nums:
            continue
        # 分母取「較小者」：擷取不全時本大題的題號會比答案表少很多，
        # 用聯集或最大值當分母會讓命中率被稀釋而配不上。
        # 每張表只配給一個大題（best-match），避免相鄰大題互搶。
        merged: dict[int, str] = {}
        scored = []
        for tbl, mapping in sorted(by_section.items()):
            if tbl in used:
                continue
            keys = set(mapping)
            hit = len(keys & nums) / max(1, min(len(keys), len(nums)))
            if hit >= 0.6:
                scored.append((hit, tbl, mapping))
        if scored:
            scored.sort(reverse=True, key=lambda x: x[0])
            _, tbl, mapping = scored[0]
            used.add(tbl)
            merged.update(mapping)
        for q in questions:
            if q["section"] == sec["ord"] and q["number"] in merged:
                q["answer"] = split_answer(merged[q["number"]], q)

    for q in questions:
        q.pop("_col", None)
        q.pop("_y", None)

    for s in sections:
        s["count"] = sum(1 for q in questions if q["section"] == s["ord"])

    meta.update({"id": doc_id, "title": f"{meta['school']} {meta['academic_year_roc']}"
                 f"學年度第{meta.get('semester','?')}學期"
                 f"第{meta.get('exam_seq','?')}次定期評量"
                 f"{meta['grade']}年級{meta['subject']}科試題",
                 "exam_name": f"{meta['academic_year_roc']}學年度"
                              f"第{meta.get('semester','?')}學期"
                              f"第{meta.get('exam_seq','?')}次定期評量",
                 "sections": sections})
    return {"document": meta, "questions": questions}


def collect_school_names(pdfs: list[Path]) -> dict[tuple[str, str], str]:
    """先掃一遍全批，替每所學校決定一個校名。

    兩件事都靠這一輪：
      - 沒印校名的卷可以沿用同校其他卷印出來的名字，不必用縣市＋簡稱去組
        （組出來的不是官方名稱，而校名是出處標註的必填欄位）。
      - **同一所學校只能有一個名字**。同一所學校的相鄰兩次段考會寫成
        「桃園市立石門國中」與「桃園市立石門國民中學」，若各自照抄，
        檢索與統計會把一所學校算成兩所。取最完整的那個寫法當標準名。
    """
    variants: dict[tuple[str, str], list[str]] = {}
    for pdf in pdfs:
        p = parse_path(pdf)
        city, short = p.get("city"), p.get("school_short")
        if not (city and short):
            continue
        try:
            doc = fitz.open(pdf)
        except Exception:
            continue
        text = norm(doc[0].get_text("text").replace("\n", " ")) if len(doc) else ""
        doc.close()
        school = parse_school(text)
        if school and short in school:
            variants.setdefault((city, short), []).append(school)
    return {k: max(v, key=lambda s: (len(s), v.count(s))) for k, v in variants.items()}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("target", type=Path)
    ap.add_argument("-o", "--out", type=Path, default=Path("out/extracted"))
    ap.add_argument("--assets", type=Path, default=Path("data/assets"))
    ap.add_argument("--dpi", type=int, default=200)
    ap.add_argument("--subject", help="卷面沒印科目時採用（整批只有一科時才給）")
    ap.add_argument("--grade", type=int, help="卷面沒印年級時採用（同上）")
    args = ap.parse_args()

    pdfs = sorted(args.target.rglob("*.pdf")) if args.target.is_dir() else [args.target]
    args.out.mkdir(parents=True, exist_ok=True)

    school_names = collect_school_names(pdfs)

    total_q = 0
    for pdf in pdfs:
        res = extract_pdf(pdf, args.dpi, args.assets, school_names,
                          args.subject, args.grade)
        if not res:
            continue
        if not res["questions"]:
            # 一題都沒抓到的多半是掃描件（沒有文字圖層）。寫出空文件只會在
            # 題庫裡留下一份沒有題目的來源紀錄，不如直接報出來。
            print(f"    ⚠ {pdf.name}：0 題，跳過（掃描件或版面無法辨識）")
            continue
        dest = args.out / f"{res['document']['id']}.yaml"
        dest.write_text(yaml.safe_dump(res, allow_unicode=True, sort_keys=False),
                        encoding="utf-8")
        n = len(res["questions"])
        with_ans = sum(1 for q in res["questions"] if q.get("answer"))
        with_fig = sum(1 for q in res["questions"] if q.get("assets"))
        total_q += n
        print(f"  {pdf.name}  →  {n} 題（{with_ans} 題有答案、{with_fig} 題有圖）"
              f"  {res['document']['subject']}"
              f"  {len(res['document']['sections'])} 個大題")

    print(f"\n{len(pdfs)} 份，共擷取 {total_q} 題 → {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
