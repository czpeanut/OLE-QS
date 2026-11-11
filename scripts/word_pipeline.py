#!/usr/bin/env python3
"""高中段考卷 Word 原檔 → 題庫交件包（YAML＋附圖＋原卷 PDF＋manifest），一條指令跑完一批。

    python scripts/word_pipeline.py --root <校名來源題庫集中庫 本機資料夾> --out high_school \\
        [--subject 數學] [--ids doc_…,doc_…] [--limit 15] [--offset 0]

流程（每份卷）：
  1. flatten_doc_eq.py 改寫 EQ 欄位 → LibreOffice 轉 .docx 與原卷 PDF（sources/ 放這份 PDF）
  2. docx_stream.py 讀段落流（底線、上下標、表格、附圖、MathType→LaTeX）
  3. word_paper.py 切大題、題組、題目、選項，接答案與詳解
  4. 附圖：用 Word 裡的版面尺寸到 PDF 找位置，從 PDF 裁 PNG（crop 一定對得上）
  5. 品管閘門（apps/api/quality.py）＋檢查程式（scripts/check_bank_yaml.py --sources）逐題檢查，
     不合格的題目剔除並列入 manifest 的 dropped；剔除超過 10% 或原卷文字覆蓋率不足的整卷不交，列入 skipped。
不用模型、不花錢。需要：LibreOffice、pandoc、ruby（gem mathtype_to_mathml_plus pry nokogiri）、
python 套件 pyyaml pymupdf pillow olefile。安裝方式見 docs/HIGH_SCHOOL_PIPELINE.md。
"""
from __future__ import annotations

import argparse
import datetime
import json
import re
import shutil
import subprocess
import sys
import tempfile
import traceback
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(REPO))

import docx_stream  # noqa: E402
import flatten_doc_eq  # noqa: E402
import word_paper as wp  # noqa: E402

try:
    import pymupdf as fitz
except ImportError:                                     # 舊版 PyMuPDF
    import fitz

GRADE_NAME = {10: "高一", 11: "高二", 12: "高三"}
SUBS = {"物理", "化學", "生物", "地科", "歷史", "地理", "公民與社會"}
DROP_LIMIT = 0.10
TOKEN = re.compile(r"⟦IMG:(\d+)⟧")


# ── 卷的出處 ──────────────────────────────────────────────────────────────
def meta(task: dict) -> dict:
    m = re.match(r"^doc_(\d+)_([^_]+)_([^_]+)_(.+)_g(1[0-2])s([12])e([123])$", task["id"])
    if not m:
        raise ValueError(f"卷 id 格式不對：{task['id']}")
    year, city, short, rest, grade, sem, exam = m.groups()
    parts = rest.split("_")
    subject, sub, course = parts[0], None, None
    for p in parts[1:]:
        if p in SUBS and not sub:
            sub = p
        else:
            course = p
    grade, sem, exam = int(grade), int(sem), int(exam)
    school = task["school"]
    detail = sub or course
    if sub and course:
        detail = f"{sub}，{course}"
    title = (f"{school} {year}學年度第{sem}學期第{exam}次定期評量 {GRADE_NAME[grade]}"
             f"{subject}" + (f"（{detail}）" if detail else "") + "科試題")
    folder = sub or subject
    doc = {"id": task["id"], "level": "senior", "grade": grade, "subject": subject}
    if sub:
        doc["sub_subject"] = sub
    if course:
        doc["course"] = course
    doc.update({"academic_year_roc": int(year), "semester": sem, "exam_seq": exam, "city": city,
                "school_short": short, "school": school, "title": title,
                "exam_name": f"{year}學年度第{sem}學期第{exam}次定期評量",
                "source_file": f"{GRADE_NAME[grade]}/{folder}/{year}/{sem}-{exam}/{city}/{short}.pdf",
                "extractor": "programmatic:word_pipeline"})
    return doc


# ── 轉檔 ──────────────────────────────────────────────────────────────────
def convert(src: Path, work: Path, profile: Path) -> tuple[Path, Path]:
    work.mkdir(parents=True, exist_ok=True)
    base = work / "paper"
    exe = shutil.which("soffice") or shutil.which("libreoffice")
    if not exe:
        raise RuntimeError("找不到 LibreOffice（soffice）")

    def lo(fmt: str, doc: Path) -> None:
        subprocess.run([exe, f"-env:UserInstallation=file://{profile}", "--headless", "--convert-to", fmt,
                        "--outdir", str(work), str(doc)], capture_output=True, timeout=600)

    if src.suffix.lower() == ".doc":
        # 解析用：所有 EQ 欄位改寫（公式類寫成 ⟦EQ …⟧ 字面，docx_stream 轉 LaTeX）
        doc = base.with_suffix(".doc")
        flatten_doc_eq.flatten_file(src, doc, literal=True)
        lo("docx", doc)
        # 原卷 PDF 用：公式類 EQ 改寫成給人看的純文字（LibreOffice 不會排 EQ 公式，原樣會留白）
        pdoc = work / "source.doc"
        flatten_doc_eq.flatten_file(src, pdoc, literal=False)
        lo("pdf", pdoc)
        if (work / "source.pdf").is_file():
            (work / "source.pdf").replace(base.with_suffix(".pdf"))
    else:
        doc = base.with_suffix(src.suffix.lower())
        shutil.copyfile(src, doc)
        lo("pdf", doc)
    docx, pdf = base.with_suffix(".docx"), base.with_suffix(".pdf")
    if not docx.is_file() or not pdf.is_file():
        raise RuntimeError("LibreOffice 轉檔失敗")
    return docx, pdf


# ── 文字 ──────────────────────────────────────────────────────────────────
def wrap_scripts(seg: str) -> str:
    """$ 外面的 x^{2}、H_{2}O 包進 $…$（上下標遺失檢查要求）。"""
    out = re.sub(r"([A-Za-z0-9]+(?:\.[0-9]+)?)?((?:[\^_]\{[^{}$]*\})+)",
                 lambda m: "$" + (m.group(1) or "") + m.group(2) + "$", seg)
    return out


ELEMENTS = set("""H He Li Be B C N O F Ne Na Mg Al Si P S Cl Ar K Ca Sc Ti V Cr Mn Fe Co Ni Cu Zn Ga Ge As Se Br Kr
Rb Sr Y Zr Nb Mo Tc Ru Rh Pd Ag Cd In Sn Sb Te I Xe Cs Ba La Ce Pr Nd Pm Sm Eu Gd Tb Dy Ho Er Tm Yb Lu Hf Ta W Re Os
Ir Pt Au Hg Tl Pb Bi Po At Rn Fr Ra Ac Th Pa U Np Pu""".split())
CHEM = re.compile(r"(?<![A-Za-z0-9$\\{])((?:\(?(?:[A-Z][a-z]?\d*)+\)?\d*){1,10})(?![A-Za-z0-9}])")


def chem_formulas(seg: str) -> str:
    """自然科：原卷沒設下標的化學式（CO2、C6H5ONa、Fe2(SO4)3）→ $C_{6}H_{5}ONa$。
    整段都要是合法元素符號、而且至少有一個數字，才當成化學式。"""
    def fix(m: re.Match) -> str:
        tok = m.group(1)
        if not re.search(r"\d", tok) or not re.search(r"[A-Z]", tok):
            return tok
        syms = re.findall(r"[A-Z][a-z]?", tok)
        if any(x not in ELEMENTS for x in syms) or re.fullmatch(r"[A-Z]\d", tok) and syms[0] not in ("H", "O", "N", "C"):
            return tok
        return "$" + re.sub(r"(?<=[A-Za-z)])(\d+)", r"_{\1}", tok) + "$"
    return CHEM.sub(fix, seg)


def clean(text: str, chem: bool = False) -> str:
    t = text.replace(wp.ZW, "").replace("　", " ")
    lines = []
    for line in t.split("\n"):
        if line.lstrip().startswith("|"):
            lines.append(line.strip())
            continue
        line = re.sub(r"[ \t]+", " ", line).strip()
        if not line or wp.ANS_BLANK_LINE.match(line):
            continue
        lines.append(line)
    t = "\n".join(lines)
    parts = t.split("$")
    for k in range(0, len(parts), 2):
        parts[k] = wrap_scripts(parts[k])
        if chem:
            parts[k] = chem_formulas(parts[k])
    t = "$".join(parts)
    t = re.sub(r"\$\$", "", t)                              # $a$$b$ → $ab$
    t = re.sub(r"<u>\s*</u>", "", t)
    return t.strip()


def bad_text(t: str) -> str | None:
    if re.search(r"⟦(公式無法轉換|符號|EQ|eq)", t):
        return "公式或符號轉不出來"
    if "\x00" in t or "⟦公式⟧" in t:
        return "公式轉換工具沒有執行"
    return None


def is_layout_table(md: str) -> bool:
    """只放圖和圖說的表格（排版用），不當成題目的表格：文字格要是圖說，而且不比圖多太多。"""
    cells = [c.strip() for line in md.split("\n") if line.strip().startswith("|") and not re.match(r"^\|\s*-", line.strip())
             for c in line.strip().strip("|").split("|")]
    imgs = sum(len(TOKEN.findall(c)) for c in cells)
    if not imgs:
        return False
    texts = [re.sub(r"⟦IMG:\d+⟧|\s", "", c) for c in cells]
    texts = [t for t in texts if t]
    caption = re.compile(r"^[▲▼△▽]?[（(]?[圖表照][一二三四五六七八九十0-9（(）)甲乙丙丁]*.{0,10}$|^[（(]?[甲乙丙丁戊己A-Ea-e一二三四五]?[）)]?$")
    return len(texts) <= 2 * imgs and all(caption.match(t) for t in texts)


def table_options(text: str) -> list[dict] | None:
    """選項是表格的欄（表頭寫 (A)作用力、(B)坡度…）→ 每欄一個選項，內容是該欄各列。"""
    for block in re.findall(r"(?:^\|.*\n?)+", text, re.M):
        rows = [[c.strip() for c in l.strip().strip("|").split("|")] for l in block.strip().split("\n")
                if not re.match(r"^\|\s*-", l.strip())]
        if len(rows) < 2:
            continue
        head = rows[0]
        labs = [(k, m.group(1), c[m.end():].strip()) for k, c in enumerate(head) if (m := wp.OPT.match(c))]
        if len(labs) >= 4 and [l for _, l, _ in labs] == list("ABCDE"[:len(labs)]):
            opts = []
            for k, lab, name in labs:
                cells = [f"{r[0]}：{r[k]}" if r[0] else r[k] for r in rows[1:] if k < len(r) and r[k].strip()]
                body = "；".join(cells)
                opts.append({"label": lab, "content": (f"{name}（{body}）" if name and body else name or body)})
            return opts
    return None


# ── 附圖：用 Word 裡的版面尺寸到 PDF 找位置 ────────────────────────────────────
class PdfFigures:
    def __init__(self, pdf: Path):
        self.doc = fitz.open(str(pdf))
        self.used: set[tuple[int, tuple]] = set()
        self.cands: dict[int, list] = {}
        self.text = [norm_cjk(p.get_text()) for p in self.doc]
        self.chars: list[tuple[int, float]] = []          # 全卷中文字的 (頁, y)，和 self.cjk 一一對應
        cjk = []
        for pno, page in enumerate(self.doc):
            for b in page.get_text("rawdict")["blocks"]:
                for line in b.get("lines", []):
                    for span in line["spans"]:
                        for ch in span["chars"]:
                            if "\u4e00" <= ch["c"] <= "\u9fff":
                                cjk.append(ch["c"])
                                self.chars.append((pno, self.ykey(pno, ch["bbox"][0], ch["bbox"][1])))
        self.cjk = "".join(cjk)

    def ykey(self, pno: int, x: float, y: float) -> float:
        """閱讀順序的縱座標：橫式（寬大於高）的頁面當成左右兩欄，右欄排在左欄之後。"""
        page = self.doc[pno].rect
        if page.width > page.height * 1.15 and x > page.width / 2:
            return y + 10000
        return y

    def center(self, p: int, r: "fitz.Rect") -> tuple[int, float]:
        return p, self.ykey(p, (r.x0 + r.x1) / 2, (r.y0 + r.y1) / 2)

    def find(self, text: str, start: int, window: int = 2500) -> tuple[int, int, float] | None:
        """題幹的中文字（不管標點、空白、換行）在 start 之後 window 字以內找 → (字串位置, 頁, y)。
        用題幹裡不同位置的幾段字分別找，取最前面的命中，避免「下列敘述何者正確」這種常見句配到別題。"""
        key = norm_cjk(text)
        for n in (12, 8, 6):
            if len(key) < n:
                continue
            hits = []
            for off in range(0, min(len(key) - n, 48) + 1, 4):
                i = self.cjk.find(key[off:off + n], start, start + window + off + n)
                if i >= 0 and i - off >= start - 2:
                    hits.append(max(start, i - off))
            if hits:
                i = min(hits)
                return i, self.chars[i][0], self.chars[i][1]
        return None

    def candidates(self, pno: int) -> list:
        if pno not in self.cands:
            page = self.doc[pno]
            out = []
            for info in page.get_image_info():
                r = fitz.Rect(info["bbox"]) & page.rect
                if r.width > 4 and r.height > 4:
                    out.append(("img", r))
            try:
                import extract                                # 國中管線的向量圖偵測
                taken = [r for _, r in out]
                for r in extract.vector_figures(page, [], taken, min_side=12.0, min_area=300.0):
                    out.append(("vec", fitz.Rect(r)))
            except Exception:                                # noqa: BLE001
                pass
            self.cands[pno] = out
        return self.cands[pno]

    def page_of(self, key: str, start: int) -> int | None:
        if len(key) < 6:
            return None
        for p in range(max(0, start), len(self.text)):
            if key in self.text[p]:
                return p
        return None

    def locate(self, size: str, pages: list[int]) -> tuple[int, "fitz.Rect"] | None:
        m = re.match(r"([\d.]+)x([\d.]+)", size or "")
        if not m:
            return None
        w, h = float(m.group(1)), float(m.group(2))
        best = None
        for rank, p in enumerate(pages):
            if not 0 <= p < len(self.doc):
                continue
            for kind, r in self.candidates(p):
                if (p, tuple(r)) in self.used:
                    continue
                dw, dh = abs(r.width - w), abs(r.height - h)
                tol = 0.06 if kind == "img" else 0.25
                if dw <= max(4, w * tol) and dh <= max(4, h * tol):
                    score = (rank, dw + dh, r.y0)
                    if best is None or score < best[0]:
                        best = (score, p, r)
        if not best:
            return None
        _, p, r = best
        kind0 = next(k for k, r2 in self.candidates(p) if tuple(r2) == tuple(r))
        for kind, r2 in self.candidates(p):                  # 同一張圖的點陣框與向量框都算用掉（兩張不同的點陣圖可以重疊）
            if kind == kind0:
                continue
            inter = r & r2
            if not inter.is_empty and inter.get_area() > 0.5 * min(r.get_area(), r2.get_area()):
                self.used.add((p, tuple(r2)))
        self.used.add((p, tuple(r)))
        return p, self.with_caption(p, r)

    def with_caption(self, p: int, r: "fitz.Rect") -> "fitz.Rect":
        """圖說（▲圖(一)、圖 2）在圖的正下方或正上方時一起框進來。"""
        page = self.doc[p]
        for b in page.get_text("blocks"):
            br = fitz.Rect(b[:4])
            txt = b[4].strip()
            if len(txt) > 24 or not re.match(r"^[▲▼△▽]?\s*(圖|表|Fig)", txt):
                continue
            if br.x1 < r.x0 - 5 or br.x0 > r.x1 + 5:
                continue
            if 0 <= br.y0 - r.y1 <= 16 or 0 <= r.y0 - br.y1 <= 16:
                r = r | br
        return r

    def crop(self, p: int, r: "fitz.Rect", png: Path) -> dict:
        page = self.doc[p]
        clip = fitz.Rect(r.x0 - 3, r.y0 - 3, r.x1 + 3, r.y1 + 3) & page.rect
        dpi = min(600, max(220, int(300 * 72 / max(clip.width, 1)) + 1))   # 寬至少 300 像素
        pix = page.get_pixmap(dpi=dpi, clip=clip, alpha=False)
        png.parent.mkdir(parents=True, exist_ok=True)
        pix.save(str(png))
        return {"page": p + 1, "rect": [round(clip.x0, 2), round(clip.y0, 2), round(clip.x1, 2), round(clip.y1, 2)]}


def norm_cjk(s: str) -> str:
    return re.sub(r"[^一-鿿]", "", s)


# ── 一份卷 ────────────────────────────────────────────────────────────────
def build(task: dict, src: Path, out: Path, profile: Path) -> dict:
    doc = meta(task)
    pid = doc["id"]
    work = out / "_work" / pid
    docx, pdf = convert(src, work, profile)
    rows = list(docx_stream.stream(docx))
    images: list[dict] = []
    for r in rows:                                      # [圖k]（段內編號）→ ⟦IMG:全卷編號⟧
        local = r.get("images") or []

        def tok(m: re.Match, local=local) -> str:
            k = int(m.group(1)) - 1
            if k >= len(local):
                return ""
            images.append(local[k])
            return f"⟦IMG:{len(images) - 1}⟧"

        r["text"] = re.sub(r"\[圖(\d+)\]", tok, r["text"])
        r["images"] = []
    parsed = wp.parse(rows)
    chem = doc["subject"] == "自然"

    figs = PdfFigures(pdf)
    sections, questions, shared, dropped = [], [], [], []
    group_info: dict[int, dict] = {}
    for gi, g in enumerate(parsed["groups"]):
        raw = "\n".join(x["text"] for x in g["rows"] if not (x["kind"] == "table" and is_layout_table(x["text"])))
        g_imgs = [int(i) for x in g["rows"] for i in TOKEN.findall(x["text"])]
        group_info[id(g)] = {"text": clean(TOKEN.sub("", raw), chem), "imgs": g_imgs, "g": g,
                             "head": f"第{g['from']}～{g['to']}題為題組"}

    sec_index = {}
    for q in parsed["questions"]:
        sec = parsed["sections"][q["section"] - 1]
        sname = clean(sec["name"]) or "選擇題"
        sec_type = wp.section_type(sname)
        num = q["number"]
        qid = f"{pid}_s{q['section']}_{num}"
        reason = None
        parts = []
        for x in q["rows"]:
            if x["kind"] == "table" and is_layout_table(x["text"]):
                parts.append(" ".join(f"⟦IMG:{i}⟧" for i in TOKEN.findall(x["text"])))
            else:
                parts.append(x["text"])
        raw = "\n".join(parts)
        raw_all = raw
        stem_raw, opts_raw = wp.split_options(raw)
        if opts_raw and "\n" in opts_raw[-1]["content"].strip():
            # 最後一個選項只到它那一段；後面的段落（附圖、表格、浮動圖的錨點）屬於題目本身
            head, tail = opts_raw[-1]["content"].strip().split("\n", 1)
            opts_raw[-1]["content"] = head
            stem_raw = stem_raw + "\n" + tail
        topts = None
        if not opts_raw:
            topts = table_options(raw)
        stem_imgs = [int(i) for i in TOKEN.findall(stem_raw)]
        stem = clean(TOKEN.sub("", stem_raw), chem)
        options = []
        for o in opts_raw:
            imgs = [int(i) for i in TOKEN.findall(o["content"])]
            content = clean(TOKEN.sub("", o["content"]), chem)
            options.append({"label": o["label"], "content": content, "_imgs": imgs})
        if topts:
            options = [{**o, "_imgs": []} for o in topts]
        # 最後一個選項尾巴的句號（翰林「(D)甲丙。」）
        if options and options[-1]["content"].endswith("。") and not any(o["content"].endswith("。") for o in options[:-1]):
            options[-1]["content"] = options[-1]["content"][:-1].rstrip()
        # 題型
        if len(options) >= 2:
            qtype = "multiple" if (sec_type == "multiple" or re.search(r"多選|複選|應選\s*[二兩三2-5]\s*項|選出\s*[二兩三2-5]\s*項", stem)) else "single"
        else:
            options = []
            qtype = "fill" if (sec_type == "fill" or re.search(r"＿{2,}|_{3,}", stem)) else "calc"
        rec = {"id": qid, "section": q["section"], "number": num, "type": qtype, "page": None, "stem": stem}
        if options:
            rec["options"] = [{"label": o["label"], "content": o["content"]} for o in options]
        gi = group_info.get(id(q.get("group"))) if q.get("group") else None
        if gi:
            rec["group_stem"] = gi["text"] or gi["head"]
        for t in [stem] + [o["content"] for o in options] + ([rec.get("group_stem", "")]):
            reason = reason or bad_text(t)
        # 安全網：這題的內容裡又出現像下一題的題號開頭，表示切題切錯了（例如原卷漏打題號）
        for line in raw.split("\n")[1:]:
            m = re.match(r"^\s*(?:[（(][\s　]*[）)]\s*)?(\d{1,3})\s*[.．、]?[\s　]*(?:[（(][\s　]*[）)])?\s*[\u4e00-\u9fff「]", wp.plain(line))
            if m and num < int(m.group(1)) <= num + 5 and len(wp.plain(line)) > 15:
                reason = reason or f"題目切割可疑：內容裡出現「{m.group(1)}」開頭的段落"
        rec.update({"_stem_imgs": stem_imgs, "_opt_imgs": [(o["label"], o["_imgs"]) for o in options], "_gi": gi})
        rec["_reason"] = reason
        rec["_q"] = q
        sec_index.setdefault(q["section"], sname)
        questions.append(rec)

    place_figures(questions, images, figs, shared, pid, out)
    attach_answers(parsed, questions)
    attach_explanations(parsed, questions, chem)
    sections = [{"ord": k, "name": v, "count": 0} for k, v in sorted(sec_index.items())]
    doc["sections"] = sections
    scope = next((clean(wp.plain(r["text"])) for r in parsed["head"] if re.search(r"範\s*圍", wp.plain(r["text"]))), None)
    if scope:
        doc["scope_note"] = re.sub(r"^.*?範\s*圍\s*[:：]?\s*", "", scope)[:120] or scope[:120]
    return {"document": doc, "questions": questions, "shared_assets": shared, "pdf": pdf,
            "has_answer_zone": parsed["has_answer_zone"]}


def cjk_key(text: str) -> str | None:
    """題幹開頭一段連續的中文（6～8 字），拿去原卷 PDF 找位置。"""
    for run in re.findall(r"[\u4e00-\u9fff]{6,}", text[:200]):
        return run[:8]
    run = re.findall(r"[\u4e00-\u9fff]{4,}", text[:200])
    return run[0][:8] if run else None


def place_figures(qs: list[dict], images: list[dict], figs: PdfFigures, shared: list[dict], pid: str, out: Path) -> None:
    """附圖歸屬看 PDF 位置，不看 Word 段落（浮動圖常錨定在下一題或下一個題組標題，再往上移到這一題旁邊）。
    ① 每題、每個題組文章在 PDF 的起點 ② 每張圖在 PDF 的位置 ③ 依位置決定歸屬 ④ 提到圖卻沒圖的題向鄰題借 ⑤ 裁圖。"""
    import check_bank_yaml as cbk
    # ① 位置
    cursor = 0
    for q in qs:
        q["_pos"], q["_ci"] = None, cursor
        hit = figs.find(q["stem"], cursor) or figs.find((q["_gi"] or {}).get("text") or "", cursor)
        if hit:
            cursor = hit[0] + 1
            q["_pos"], q["_ci"] = (hit[1], hit[2]), hit[0]
            q["page"] = hit[1] + 1
        elif cursor < len(figs.chars) and len(norm_cjk(q["stem"])) < 12:
            # 題幹幾乎全是公式（中文太少，找不到）：用前一題之後的位置推估，頁碼可能差一頁
            p, y = figs.chars[min(cursor, len(figs.chars) - 1)]
            q["_pos"] = (p, y + 1)
            q["_est"] = True
            q["page"] = p + 1
        else:
            q["_reason"] = q["_reason"] or "在原卷 PDF 找不到這題的位置"
    starts = [(k, q["_pos"]) for k, q in enumerate(qs) if q["_pos"] and not q.get("_est")]   # 推估的位置不拿來分圖

    def owner(p: int, r: "fitz.Rect", k0: int) -> int:
        if qs[k0].get("_est"):
            return k0
        c = figs.center(p, r)
        own = k0
        for k, pos in starts:
            if pos <= c:
                own = k
        same_group = qs[own]["_gi"] is qs[k0]["_gi"]          # 不跨題組搬圖
        return own if abs(own - k0) <= 1 and same_group else k0

    def pages_of(k: int) -> list[int]:
        p0 = qs[k]["_pos"][0] if qs[k]["_pos"] else 0
        return [p0, p0 + 1, p0 - 1]

    # ② ③ 題目的圖
    stem_figs: dict[int, list] = {k: [] for k in range(len(qs))}
    for k, q in enumerate(qs):
        if not q["_pos"]:
            continue
        for i in q["_stem_imgs"]:
            hit = figs.locate(images[i]["size"], pages_of(k))
            if not hit:
                q["_reason"] = q["_reason"] or "附圖在原卷 PDF 找不到位置"
                continue
            stem_figs[owner(hit[0], hit[1], k)].append(hit)
    # ② ③ 題組的圖：落在題組文章開頭之上的，屬於前一題
    group_figs: dict[int, list] = {}
    for k, q in enumerate(qs):
        gi = q["_gi"]
        if not gi or id(gi) in group_figs:
            continue
        hits = []
        gstart = figs.find(gi["text"], qs[k - 1]["_ci"] if k else 0) if gi["text"] else None
        gpos = (gstart[1], gstart[2]) if gstart else q["_pos"]
        for i in gi["imgs"]:
            hit = figs.locate(images[i]["size"], pages_of(k) + [pages_of(k)[0] - 2])
            if not hit:
                gi["fail"] = True
                continue
            c = figs.center(hit[0], hit[1])
            if k and gpos and c < (gpos[0], gpos[1] - 4) and qs[k - 1]["_gi"] is not gi:
                own = max([j for j, pos in starts if pos <= c and j < k] or [k - 1])
                if k - own > 3 or qs[own]["_gi"] is gi:
                    own = k - 1
                stem_figs[own].append(hit)                   # 錨定在題組標題、其實浮在前面題目旁邊
            else:
                hits.append(hit)
        group_figs[id(gi)] = hits
    # ④ 提到圖卻沒有圖的題，向相鄰、同題組、自己沒提到圖的題借圖（浮動圖剛好落在兩題交界）
    mentions = [bool(cbk.FIG_RE.search(q["stem"])) for q in qs]
    for k, q in enumerate(qs):
        if not mentions[k] or stem_figs[k] or (q["_gi"] and group_figs.get(id(q["_gi"]))):
            continue
        for j in (k + 1, k - 1):
            if 0 <= j < len(qs) and stem_figs[j] and not mentions[j] and qs[j]["_gi"] is q["_gi"]:
                stem_figs[k].append(stem_figs[j].pop(0 if j > k else -1))
                break
    # 兩張圖在 PDF 上疊在一起（LibreOffice 排版時擠在同一處）：裁出來會混到另一張，相關題目不收
    placed = [(k, h) for k in stem_figs for h in stem_figs[k]] + \
             [(k, h) for k, q in enumerate(qs) if q["_gi"] for h in group_figs.get(id(q["_gi"]), [])]
    for a in range(len(placed)):
        for b in range(a + 1, len(placed)):
            (ka, (pa, ra)), (kb, (pb, rb)) = placed[a], placed[b]
            if pa == pb and ra is not rb:
                inter = ra & rb
                if not inter.is_empty and inter.get_area() > 0.5 * min(ra.get_area(), rb.get_area()) and tuple(ra) != tuple(rb):
                    for k in (ka, kb):
                        qs[k]["_reason"] = qs[k]["_reason"] or "附圖在原卷 PDF 上和另一張圖重疊，裁不出正確的圖"
    # ⑤ 裁圖
    for k, q in enumerate(qs):
        sec, num, qid = q["section"], q["number"], q["id"]
        hits = sorted(stem_figs[k], key=lambda h: (h[0], h[1].y0, h[1].x0))
        assets = []
        for n, (p, r) in enumerate(hits, 1):
            key_ = f"s{sec}_{num}_f{n}"
            file = f"{pid}/{key_}.png"
            assets.append({"key": key_, "kind": "figure", "file": file, "crop": figs.crop(p, r, out / "assets" / file)})
        if assets:
            q["assets"] = assets
        for o, (lab, imgs) in zip(q.get("options", []), q["_opt_imgs"]):
            if not imgs:
                continue
            hits_o = [figs.locate(images[i]["size"], pages_of(k)) for i in imgs]
            if any(h is None for h in hits_o) or len({h[0] for h in hits_o}) > 1:
                q["_reason"] = q["_reason"] or "選項圖在原卷 PDF 找不到位置"
                continue
            r = hits_o[0][1]
            for h in hits_o[1:]:                              # 一個選項由幾張小圖拼成：合成一張
                r = r | h[1]
            key_ = f"s{sec}_{num}_opt{lab}"
            file = f"{pid}/{key_}.png"
            o["asset"] = {"key": key_, "file": file, "crop": figs.crop(hits_o[0][0], r, out / "assets" / file)}
        gi = q["_gi"]
        if gi and (gi["imgs"]):
            gk = f"g{sec}_{gi['g']['from']}_{gi['g']['to']}"
            if "asset" not in gi:
                hits_g = group_figs.get(id(gi)) or []
                if gi.get("fail") or (hits_g and len({h[0] for h in hits_g}) > 1):
                    gi["asset"] = None
                elif not hits_g:
                    gi["asset"] = False                       # 題組的圖都屬於前一題
                else:
                    r = hits_g[0][1]
                    for h in hits_g[1:]:
                        r = r | h[1]
                    file = f"{pid}/{gk}_f1.png"
                    gi["asset"] = {"key": gk, "label": "題組附圖", "kind": "figure", "file": file, "used_by": [],
                                   "crop": figs.crop(hits_g[0][0], r, out / "assets" / file)}
                    shared.append(gi["asset"])
            if gi["asset"] is None:
                q["_reason"] = q["_reason"] or "題組附圖在原卷 PDF 找不到位置或分在不同頁"
            elif gi["asset"]:
                q["shared_asset"] = gk
                gi["asset"]["used_by"].append(qid)
    for q in qs:
        for key in ("_stem_imgs", "_opt_imgs", "_gi", "_pos", "_est", "_ci"):
            q.pop(key, None)


def entry_lookup(entries: list[dict], qs: list[dict]):
    """題號 → 答案／解析文字。題號全卷不重複就照題號；各大題重新編號的，照大題順序一段對一段。"""
    nums = [q["number"] for q in qs]
    if len(set(nums)) == len(nums):
        by_num: dict[int, str] = {}
        for e in entries:
            by_num.setdefault(e["number"], e["text"])
        return lambda q: by_num.get(q["number"])
    # 各大題重新編號：答案的每一段依序配給「題號都對得上」的下一個大題（答案區可能少了非選題那段）
    secs = list(dict.fromkeys(q["section"] for q in qs))
    nums_of = {sec: {q["number"] for q in qs if q["section"] == sec} for sec in secs}
    table, k = {}, 0
    for b in dict.fromkeys(e["block"] for e in entries):
        got = [e for e in entries if e["block"] == b]
        want = {e["number"] for e in got}
        while k < len(secs) and not want <= nums_of[secs[k]]:
            k += 1
        if k >= len(secs):
            return lambda q: None                          # 對不起來就不填，不猜
        for e in got:
            table.setdefault((secs[k], e["number"]), e["text"])
        k += 1
    return lambda q: table.get((q["section"], q["number"]))


def attach_answers(parsed: dict, qs: list[dict]) -> None:
    get = entry_lookup(parsed["answers"], qs)
    for q in qs:
        raw = get(q)
        if raw is None:
            continue
        first = raw.split("\n")[0].strip()
        if not first or "⟦圖⟧" in first:
            continue
        labels = {o["label"] for o in q.get("options") or []}
        if q["type"] in ("single", "multiple"):
            if re.search(r"送分|皆給分|均給分|全對", first):
                q["answer"] = sorted(labels)
                q["review_note"] = "解答卷：本題送分" if "送分" in first else f"解答卷：{clean(first)}"
            else:
                letters = re.findall(r"([A-E])", re.sub(r"[^A-E或、,，()（）\s]", "", first))
                rest = re.sub(r"[A-E或、,，()（）\s.．]", "", first)
                if not letters or len(rest) > 2 or any(l not in labels for l in letters):
                    continue
                q["answer"] = sorted(set(letters))
                if "或" in first:
                    q["review_note"] = "解答卷：" + "或".join(f"({l})" for l in letters) + "皆給分"
                if q["type"] == "single" and len(q["answer"]) > 1 and "或" not in first:
                    continue
            q["answer_status"] = "verified"
            q["answer_source"] = "answer_key"
        else:
            ans = clean(first)
            if re.fullmatch(r"[（(]?\s*[A-E]\s*[）)]?(\s*[、,，]?\s*[（(]?\s*[A-E]\s*[）)]?)*", ans or ""):
                q["_reason"] = q.get("_reason") or "答案是選項代號，但文字裡找不到選項（選項多半畫在圖裡）"
                continue
            if not re.sub(r"[①-⑳❶-❿\s＿_（）()、,，.．:：答]", "", ans or ""):
                continue                                  # 空白答案卷（只有 ①②、底線）
            if ans and len(ans) <= 80 and not re.search(r"⟦|\[圖", ans):
                q["answer"] = [ans]
                q["answer_status"] = "verified"
                q["answer_source"] = "answer_key"


def attach_explanations(parsed: dict, qs: list[dict], chem: bool = False) -> None:
    get = entry_lookup(parsed["explanations"], qs)
    for q in qs:
        t = get(q)
        if not t or "⟦圖⟧" in t or re.search(r"⟦", t):
            continue
        t = clean(t, chem)
        if len(t) >= 4 and not bad_text(t) and not re.search(r"^\|", t, re.M):
            q["explanation"] = t


# ── 檢查與輸出 ─────────────────────────────────────────────────────────────
def finalize(paper: dict, out: Path) -> dict:
    from apps.api.quality import evaluate
    import check_bank_yaml as cbk
    doc = paper["document"]
    pid = doc["id"]
    total = len(paper["questions"])
    dropped = []
    keep = []
    for q in paper["questions"]:
        reason = q.pop("_reason", None)
        q.pop("_q", None)
        if not reason:
            ok, why = evaluate(q, {"document": doc, "shared_assets": paper["shared_assets"]})
            reason = None if ok else "；".join(why)
        if reason:
            dropped.append({"number": q["number"], "section": q["section"], "reason": reason, "text": qtext(q)})
        else:
            keep.append(q)
    src_pdf = out / "sources" / doc["source_file"]
    src_pdf.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(paper["pdf"], src_pdf)
    ypath = out / "bank" / f"{pid}.yaml"
    ypath.parent.mkdir(parents=True, exist_ok=True)
    for _ in range(3):                                  # 檢查程式點名的題剔除後再檢查一次
        write_yaml(doc, keep, paper["shared_assets"], ypath, out)
        err, warn, n, nbad, issues = cbk.check(ypath, out / "assets", out / "sources", None, dropped_text(dropped))
        paper_level = [i for i in issues if i[0] == pid or not i[0].startswith(pid + "_")]
        bad_ids = {i[0] for i in issues if i[0].startswith(pid + "_")}
        bad_ids |= {e.split("：")[0].strip() for e in err if e.split("：")[0].strip().startswith(pid + "_")}
        if not bad_ids:
            break
        for q in list(keep):
            if q["id"] in bad_ids:
                why = "；".join(f"{c}：{d}" for i, c, d in issues if i == q["id"]) or "；".join(e for e in err if e.startswith(q["id"]))
                dropped.append({"number": q["number"], "section": q["section"], "reason": why[:200], "text": qtext(q)})
                keep.remove(q)
    err, warn, n, nbad, issues = cbk.check(ypath, out / "assets", out / "sources", None, dropped_text(dropped))
    entry = {"id": pid, "source_file": doc["source_file"], "origin": paper.get("origin"),
             "questions": len(keep), "dropped": dropped, "has_answer_key": any(q.get("answer") for q in keep),
             "has_explanation": any(q.get("explanation") for q in keep),
             "pages": len(fitz.open(str(src_pdf)))}
    problem = None
    if total == 0 or len(dropped) > total * DROP_LIMIT:
        problem = f"剔除 {len(dropped)}／{total} 題，超過 10%"
    elif err:
        problem = "檢查程式錯誤：" + "；".join(err[:3])
    elif issues:
        problem = "內容問題：" + "；".join(f"{c}" for _, c, _ in issues[:3])
    if problem:
        reject(out, pid, doc["source_file"])
        entry["skip_reason"] = problem
    return entry


def qtext(q: dict) -> str:
    """剔除題目的原文（題組材料＋題幹＋選項），寫進 manifest 的 dropped，檢查程式算覆蓋率用。"""
    parts = [q.get("group_stem") or "", q.get("stem") or ""] + [o.get("content") or "" for o in q.get("options") or []]
    return "\n".join(p for p in parts if p)


def dropped_text(dropped: list[dict]) -> str:
    return "\n".join(d.get("text") or "" for d in dropped)


def write_yaml(doc: dict, qs: list[dict], shared: list[dict], ypath: Path, out: Path) -> None:
    ids = {q["id"] for q in qs}
    counts: dict[int, int] = {}
    for q in qs:
        counts[q["section"]] = counts.get(q["section"], 0) + 1
    doc = {**doc, "sections": [{**s, "count": counts.get(s["ord"], 0)} for s in doc["sections"] if counts.get(s["ord"])]}
    sh = []
    for a in shared:
        used = [u for u in a["used_by"] if u in ids]
        if used:
            sh.append({**a, "used_by": used})
    keep_sh = {a["key"] for a in sh}
    qs_out = []
    for q in qs:
        q = {k: v for k, v in q.items() if v is not None}
        if q.get("shared_asset") and q["shared_asset"] not in keep_sh:
            q.pop("shared_asset")
        qs_out.append(q)
    d = {"document": doc, "questions": qs_out}
    if sh:
        d["shared_assets"] = sh
    # 沒被引用的圖檔刪掉（剔除的題目留下的）
    used_files = {a["file"] for q in qs_out for a in q.get("assets") or []}
    used_files |= {o["asset"]["file"] for q in qs_out for o in q.get("options") or [] if o.get("asset")}
    used_files |= {a["file"] for a in sh}
    adir = out / "assets" / doc["id"]
    if adir.is_dir():
        for f in adir.glob("*.png"):
            if f"{doc['id']}/{f.name}" not in used_files:
                f.unlink()
    ypath.write_text(yaml.safe_dump(d, allow_unicode=True, sort_keys=False, width=1000), encoding="utf-8")


def reject(out: Path, pid: str, source_file: str) -> None:
    """不交的卷：YAML、附圖、PDF 移到 rejected/，留給人看原因。"""
    rej = out / "rejected"
    for p in (out / "bank" / f"{pid}.yaml", out / "assets" / pid, out / "sources" / source_file):
        if p.exists():
            dst = rej / p.relative_to(out)
            dst.parent.mkdir(parents=True, exist_ok=True)
            if dst.exists():
                shutil.rmtree(dst) if dst.is_dir() else dst.unlink()
            shutil.move(str(p), str(dst))


def env_problems() -> list[str]:
    import ole_math
    out = []
    if not (shutil.which("soffice") or shutil.which("libreoffice")):
        out.append("找不到 LibreOffice（soffice）")
    if (m := ole_math.available()):
        out.append(m)
    try:
        import olefile  # noqa: F401
    except ImportError:
        out.append("Python 缺 olefile（python3 -m pip install olefile）")
    fc = subprocess.run(["fc-list", ":lang=zh"], capture_output=True, text=True) if shutil.which("fc-list") else None
    if fc is not None and not fc.stdout.strip():
        out.append("系統沒有中文字型，LibreOffice 轉出的 PDF 會缺字（apt-get install fonts-noto-cjk）")
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", type=Path, required=True, help="校名來源題庫集中庫的本機資料夾（rel_path 的起點）")
    ap.add_argument("--out", type=Path, default=Path("high_school"))
    ap.add_argument("--tasks", type=Path, default=REPO / "data/high_school/round3_tasks.json")
    ap.add_argument("--subject", help="只做這一科（任務清單的 subject，例如 數學、物理、公民與社會）")
    ap.add_argument("--ids", help="只做這幾份（逗號分隔的卷 id）")
    ap.add_argument("--offset", type=int, default=0)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--keep-work", action="store_true", help="保留 _work/ 的中間檔（除錯用）")
    a = ap.parse_args()
    miss = env_problems()
    if miss:
        print("環境還沒裝好：\n  " + "\n  ".join(miss) + "\n安裝方式見 docs/HIGH_SCHOOL_PIPELINE.md 第 1 節")
        return 2
    tasks = json.loads(a.tasks.read_text(encoding="utf-8"))["papers"]
    if a.subject:
        tasks = [t for t in tasks if t["subject"] == a.subject]
    if a.ids:
        want = set(a.ids.split(","))
        tasks = [t for t in tasks if t["id"] in want]
    tasks = tasks[a.offset:]
    if a.limit:
        tasks = tasks[:a.limit]
    a.out.mkdir(parents=True, exist_ok=True)
    mpath = a.out / "manifest.json"
    manifest = json.loads(mpath.read_text(encoding="utf-8")) if mpath.is_file() else {
        "produced_by": "scripts/word_pipeline.py", "extractor": "programmatic:word_pipeline", "papers": [], "skipped": []}
    manifest["produced_at"] = datetime.date.today().isoformat()
    profile = Path(tempfile.mkdtemp(prefix="lo_profile_"))
    done = {p["id"] for p in manifest["papers"]} | {s.get("id") for s in manifest["skipped"]}
    for k, t in enumerate(tasks, 1):
        if t["id"] in done:
            print(f"[{k}/{len(tasks)}] {t['id']}：已做過，略過")
            continue
        src = a.root / t["rel_path"]
        print(f"[{k}/{len(tasks)}] {t['id']}", flush=True)
        if not src.is_file():
            manifest["skipped"].append({"id": t["id"], "file": t["rel_path"], "reason": "找不到原始檔"})
            continue
        try:
            paper = build(t, src, a.out, profile)
            paper["origin"] = f"校名來源題庫集中庫/{t['rel_path']}"
            entry = finalize(paper, a.out)
        except Exception as e:                          # noqa: BLE001
            traceback.print_exc()
            reject(a.out, t["id"], "")
            manifest["skipped"].append({"id": t["id"], "file": t["rel_path"], "reason": f"程式錯誤：{e}"[:300]})
        else:
            if entry.get("skip_reason"):
                manifest["skipped"].append({"id": t["id"], "file": t["rel_path"], "reason": entry["skip_reason"],
                                            "dropped": entry["dropped"]})
                print(f"    ✗ 不交：{entry['skip_reason']}")
            else:
                manifest["papers"].append(entry)
                print(f"    ✓ {entry['questions']} 題，剔除 {len(entry['dropped'])} 題")
        if not a.keep_work:
            shutil.rmtree(a.out / "_work" / t["id"], ignore_errors=True)
        mpath.write_text(json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")
    ok = len(manifest["papers"])
    print(f"\n完成：交件 {ok} 份，不交 {len(manifest['skipped'])} 份（見 {mpath}）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
