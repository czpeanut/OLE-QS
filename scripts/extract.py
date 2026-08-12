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

GRADE_MAP = {"一": 7, "二": 8, "三": 9}
SEMESTER_MAP = {"一": 1, "二": 2, "1": 1, "2": 2}

# 大題標題：「一、選擇題：(每題 4 分，共 40 分)」
SECTION_RE = re.compile(r"^\s*([一二三四五六七八九十])\s*[、.]\s*([^：:（(]{1,12})\s*[：:（(]?")
# 題目開頭：「14.(  )」「1.」「(1)」
QSTART_RE = re.compile(r"^\s*(\d{1,3})\s*[.、．]\s*(\(\s*\)|（\s*）)?\s*")
# 選項：「(A)」「（Ａ）」，可連續出現在同一行
OPTION_RE = re.compile(r"[(（]\s*([A-EＡ-Ｅ])\s*[)）]")
# 答案頁不參與題目擷取
ANSWER_PAGE_RE = re.compile(r"解答|答案卷|解析卷|參考答案")

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


def reading_order(page) -> list[tuple[float, float, float, float, str]]:
    """回傳依閱讀順序排好的文字區塊。雙欄時先左欄由上而下，再右欄。

    表格會先被抽出來整塊處理（見 table_text），落在表格範圍內的文字區塊
    則跳過，避免同一段文字出現兩次。
    """
    mid = (page.rect.x0 + page.rect.x1) / 2

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

    def inside_table(b) -> bool:
        cx, cy = (b[0] + b[2]) / 2, (b[1] + b[3]) / 2
        return any(x0 <= cx <= x1 and y0 <= cy <= y1
                   for x0, y0, x1, y1, _ in tables)

    blocks = [b for b in page.get_text("blocks")
              if (b[4] or "").strip() and not inside_table(b)]
    items = blocks + tables
    if not items:
        return []
    right = [b for b in items if b[0] > mid]
    two_col = len(right) >= max(3, len(items) * 0.2)

    def key(b):
        col = 1 if (two_col and b[0] > mid) else 0
        return (col, round(b[1], 1), round(b[0], 1))

    return sorted(items, key=key)


def parse_header(text: str) -> dict:
    """從第一頁抓考卷的來源資訊。"""
    t = norm(text.replace("\n", " "))
    meta: dict = {}
    if m := re.search(r"(\d{3})學年度", t):
        meta["academic_year_roc"] = int(m.group(1))
    if m := re.search(r"第([一二12])學期", t):
        meta["semester"] = SEMESTER_MAP.get(m.group(1))
    if m := re.search(r"第([一二三四1-4])次", t):
        meta["exam_seq"] = SEMESTER_MAP.get(m.group(1), 1) if m.group(1) in "一二12" else 3
    if m := re.search(r"([一-鿿]{2,4}[市縣](?:立)?[一-鿿]{2,6}(?:國民中學|國中))", t):
        meta["school"] = m.group(1)
    if m := re.search(r"([一二三])年級", t):
        meta["grade"] = GRADE_MAP.get(m.group(1))
    if m := re.search(r"年級\s*([一-鿿]{1,3})科", t):
        meta["subject"] = m.group(1)
    elif m := re.search(r"([一-鿿]{1,3})科(?:試題|題目卷|試卷)", t):
        meta["subject"] = m.group(1)
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


def extract_pdf(path: Path, dpi: int = 200, fig_dir: Path | None = None) -> dict | None:
    doc = fitz.open(path)
    meta = parse_header(doc[0].get_text("text"))
    for field in ("academic_year_roc", "grade", "subject", "school"):
        if not meta.get(field):
            print(f"    ⚠ {path.name}：抓不到 {field}，跳過（來源標註不可缺）")
            doc.close()
            return None

    stem_id = re.sub(r"[^\w]+", "_", path.stem).strip("_").lower()
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
        if ANSWER_PAGE_RE.search(page_text):
            continue                      # 答案頁另外由 parse_answer_key 處理

        # 圖形位置，之後依 y 座標歸給題目
        mid = (page.rect.x0 + page.rect.x1) / 2
        placements = []
        for info in page.get_images(full=True):
            placements.extend(page.get_image_rects(info[0]))
        two_col_figs = sorted(placements,
                              key=lambda r: (1 if r.x0 > mid else 0, r.y0, r.x0))
        for i, r in enumerate(two_col_figs, start=1):
            key = f"p{pno}_f{i:02d}"
            if fig_dir:
                fig_dir.mkdir(parents=True, exist_ok=True)
                clip = fitz.Rect(r.x0 - 2, r.y0 - 2, r.x1 + 2, r.y1 + 2) & page.rect
                page.get_pixmap(dpi=dpi, clip=clip).save(fig_dir / f"{key}.png")
            figures.append({"key": key, "page": pno,
                            "col": 1 if r.x0 > mid else 0,
                            "y": r.y0, "file": f"{key}.png"})

        for b in reading_order(page):
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
                           "_col": 1 if x0 > mid else 0, "_y": y0}
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

    for i, q in enumerate(questions):
        nxt = questions[i + 1] if i + 1 < len(questions) else None
        owned = [f for f in figures
                 if f["key"] not in claimed
                 and f["page"] == q["page"] and f["col"] == q["_col"]
                 and f["y"] >= q["_y"] - 4
                 and (nxt is None or nxt["page"] != q["page"]
                      or nxt["_col"] != q["_col"] or f["y"] < nxt["_y"])]
        if owned:
            claimed.update(f["key"] for f in owned)
            q["assets"] = [{"key": f["key"], "kind": "figure", "file": f["file"]}
                           for f in owned]

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
                q["answer"] = [merged[q["number"]]]

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


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("target", type=Path)
    ap.add_argument("-o", "--out", type=Path, default=Path("out/extracted"))
    ap.add_argument("--assets", type=Path, default=Path("data/assets"))
    ap.add_argument("--dpi", type=int, default=200)
    args = ap.parse_args()

    pdfs = sorted(args.target.rglob("*.pdf")) if args.target.is_dir() else [args.target]
    args.out.mkdir(parents=True, exist_ok=True)

    total_q = 0
    for pdf in pdfs:
        res = extract_pdf(pdf, args.dpi, args.assets)
        if not res:
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
