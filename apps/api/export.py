"""把選好的題目排成考卷：HTML（預覽、列印）與 PDF（headless Chromium）。

三種輸出共用同一份渲染邏輯，避免「畫面看到的」與「印出來的」不一致：
    exam    試題卷（可選卷末附答案）
    answer  答案卷（作答格）
    key     教師解答卷（題目 + 答案 + 詳解）

**來源標註在這裡強制執行。** 依授權條件，公開試題可用但須保留出處，
所以每題底下的出處與卷末來源列表固定輸出，不提供關閉選項。
"""

from __future__ import annotations

import html
import os
import re
import struct
from collections import OrderedDict
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from .mathfmt import MATH_CSS, render as md
from .models import Question
from .storage import asset_path

TYPE_ORDER = ["single", "multiple", "tf", "fill", "matching", "calc", "essay", "group"]
TYPE_NAME = {"single": "單選題", "multiple": "多選題", "tf": "是非題", "fill": "填充題",
             "matching": "配合題", "calc": "計算題", "essay": "非選擇題", "group": "題組"}
GROUP_NAME = {"英語": "閱讀測驗", "國文": "閱讀測驗"}
CHOICE = {"single", "multiple", "tf"}
NUM = "一二三四五六七八九十"
CROP_DPI = 220            # 擷取管線裁圖的解析度；據此把圖印回原卷上的實際大小
# 原卷題幹開頭的作答括號與題號（「( )16.」）：組卷後題號重編，留著會兩個題號並列
# 選項圖：每張都至少這麼高（像素）才算「圖片選項」，統一成同一高度；更矮的多半是「甲→丙→乙」這類文字式圖
PICTURE_PX = 100
# 題目在比大小（顯微鏡倍率、比例尺、面積…）時，選項圖的大小本身就是答案，不能統一
KEEP_SCALE = re.compile(r"倍|放大|縮小|比例|大小|面積|長度|尺寸")
LEAD = re.compile(r"^\s*(?:[(（]\s*[)）]\s*)?(?:\d{1,3}\s*[.．、](?!\d)\s*)?")


def clean_stem(stem: str | None) -> str:
    return LEAD.sub("", stem or "", count=1)

DEFAULTS = {
    "subtitle": "",            # 副標題，例如「七年級數學 第一次段考 複習卷」
    "header_fields": True,     # 班級／座號／姓名
    "auto_sections": True,     # 依題型自動分大題
    "columns": 1,              # 版面分欄（1 或 2）
    "font_size": 11.5,         # pt
    "answer_lines": 4,         # 計算、非選擇題留幾行作答空間
    "show_score": True,
    "answer_appendix": False,  # 試題卷卷末附答案
    "choice_paren": True,      # 選擇題前印作答括號「(　　)」
    "uniform_option_images": True,  # 選項圖統一大小（題目在比大小時自動維持原卷比例）
}


@dataclass
class Item:
    question: Question
    score: float | None = None
    section_name: str | None = None


def esc(s: str | None) -> str:
    return html.escape(s or "", quote=False)


def _mm(px: float) -> float:
    return px / CROP_DPI * 25.4


@lru_cache(maxsize=20000)
def _png_size(path: str) -> tuple[int, int] | None:
    try:
        with open(path, "rb") as f:
            head = f.read(24)
        if head[:8] == b"\x89PNG\r\n\x1a\n":
            return struct.unpack(">II", head[16:24])
    except OSError:
        pass
    return None


class Renderer:
    def __init__(self, asset_root: Path, asset_url: str = "/assets/", columns: int = 1, uniform: bool = True):
        self.root = asset_root
        self.url = asset_url
        self.narrow = columns == 2
        self.uniform = uniform

    def size(self, file: str) -> tuple[int, int] | None:
        local = asset_path(file)
        return _png_size(str(local)) if local else None

    def img(self, file: str, alt: str = "", style: str | None = None) -> str:
        if style is None:
            d = self.size(file)
            style = f"width:{_mm(d[0]):.1f}mm" if d else ""
        return f'<img src="{self.url}{esc(file)}" alt="{esc(alt)}"' + (f' style="{style}"' if style else "") + ">"

    def option_images(self, q: Question) -> tuple[int, dict[str, str]] | None:
        """同一題的選項圖排成一致的大小（原卷各選項的圖常常裁得大小不一）。與 web/lib/render.ts 同規則。"""
        files = [o.asset_file for o in q.options if o.asset_file]
        ds = [self.size(f) for f in files]
        if not files or any(d is None for d in ds):
            return None
        if self.uniform and not KEEP_SCALE.search(q.stem_md or "") and min(h for _, h in ds) >= PICTURE_PX:
            # 圖片選項：同一高度（取中位數，限制在 18–38mm，雙欄時 15–28mm），太寬的等比縮進格子
            lo, hi = (15, 28) if self.narrow else (18, 38)
            H = min(hi, max(lo, _mm(sorted(h for _, h in ds)[len(ds) // 2])))
            style = {f: f"height:{H:.1f}mm;width:auto;max-width:100%;object-fit:contain" for f in files}
            shown = [H * w / h for w, h in ds]
        else:
            # 文字式小圖、比大小的題目或關閉統一時：維持原卷比例；格子放不下時整題一起等比縮小
            max_w = max(w for w, _ in ds)
            style = {f: f"width:calc(min(100%, {_mm(max_w):.1f}mm) * {w / max_w:.3f})" for f, (w, _) in zip(files, ds)}
            shown = [_mm(w) for w, _ in ds]
        cols = 4 if len(files) == len(q.options) and max(shown) <= (17 if self.narrow else 36) else 2
        return cols, style

    # ── 題目元件 ──
    def options(self, q: Question) -> str:
        if not q.options:
            return ""
        has_img = any(o.asset_file for o in q.options)
        longest = max((len(o.content_md) for o in q.options), default=0)
        # 選項短就並排；雙欄版面每欄只有一半寬，門檻跟著減半
        four, two = (4, 10) if self.narrow else (7, 20)
        pics = self.option_images(q) if has_img else None
        cols = (pics[0] if pics else 2) if has_img else 4 if longest <= four else 2 if longest <= two else 1
        cells = []
        for o in q.options:
            body = md(o.content_md)
            if o.asset_file:
                body += self.img(o.asset_file, f"選項{o.label}", pics[1].get(o.asset_file) if pics else None)
            cells.append(f'<div class="opt"><span class="ol">({esc(o.label)})</span>'
                         f'<span class="oc">{body}</span></div>')
        return f'<div class="opts c{cols}">{"".join(cells)}</div>'

    def assets(self, q: Question) -> str:
        out = []
        for a in sorted(q.assets, key=lambda x: x.key):
            if a.kind.value == "table" and a.markdown:
                out.append(md_table(a.markdown, a.label))
            elif a.file:
                cap = f"<figcaption>{esc(a.label)}</figcaption>" if a.label else ""
                out.append(f"<figure>{self.img(a.file, a.alt or '')}{cap}</figure>")
        return "".join(out)

    def shared(self, q: Question) -> str:
        a = next((x for x in (q.document.assets if q.document else [])
                  if x.key == q.shared_asset_key), None)
        if not a:
            return ""
        if a.text:
            paras = "".join(f"<p>{md(p)}</p>" for p in a.text.split("\n\n") if p.strip())
            return f'<div class="passage">{paras}</div>'
        if a.file:
            cap = f"<figcaption>{esc(a.label)}</figcaption>" if a.label else ""
            return f"<figure>{self.img(a.file, a.alt or '')}{cap}</figure>"
        return ""

    def question(self, item: Item, n: int, mode: str, st: dict, seen: dict) -> str:
        q = item.question
        out = []
        # 共用素材與題組說明只在該組第一題前印一次
        if q.shared_asset_key and (q.document_id, q.shared_asset_key) not in seen["shared"]:
            seen["shared"].add((q.document_id, q.shared_asset_key))
            out.append(self.shared(q))
        if q.group_stem and q.group_stem != seen.get("group"):
            out.append(f'<div class="group-stem">{md(q.group_stem)}</div>')
        seen["group"] = q.group_stem

        t = q.type.value
        paren = '<span class="paren">(　　)</span>' if st["choice_paren"] and mode == "exam" and t in CHOICE else ""
        body = [paren + md(clean_stem(q.stem_md)), self.assets(q), self.options(q)]
        if mode == "exam" and t in ("calc", "essay") and st["answer_lines"]:
            body.append('<div class="lines">' + '<div class="ln"></div>' * int(st["answer_lines"]) + "</div>")
        if mode == "key":
            ans = "、".join(str(a) for a in (q.answer or [])) or "（無答案）"
            # 沒被答案卷或人工確認過的答案必須看得出來：教師解答卷是拿來改分的
            note = {"ai_generated": ("（AI 雙模型一致，未經人工確認）" if q.answer_source == "ai:gemini+qwen"
                                     else "（AI 多數決，未經人工確認）"),
                    "disputed": "（模型答案不一致，待判定）"}.get(q.answer_status.value, "")
            body.append(f'<div class="ans">答：{esc(ans)}<span class="warn">{esc(note)}</span></div>')
            if q.explanation_md:
                body.append(f'<div class="expl">{md(q.explanation_md)}</div>')
        body.append(f'<div class="cite">{esc(q.citation)}</div>')
        score = (f'<span class="sc">（{item.score:g} 分）</span>'
                 if st["show_score"] and item.score else "")
        out.append(f'<div class="q"><span class="qn">{n}.</span>'
                   f'<div class="qb">{"".join(body)}</div>{score}</div>')
        return "".join(out)


def md_table(markdown: str, label: str | None = None) -> str:
    """Markdown 管線式表格 → HTML。"""
    rows = [r.strip() for r in markdown.strip().splitlines() if r.strip()]
    rows = [r for r in rows if not set(r.replace("|", "").replace(" ", "")) <= set(":-")]
    cells = [[c.strip() for c in r.strip("|").split("|")] for r in rows]
    if not cells:
        return ""
    head = "".join(f"<th>{md(c)}</th>" for c in cells[0])
    body = "".join("<tr>" + "".join(f"<td>{md(c)}</td>" for c in r) + "</tr>" for r in cells[1:])
    cap = f"<caption>{esc(label)}</caption>" if label else ""
    return f'<table class="asset">{cap}<tr>{head}</tr>{body}</table>'


def sections(items: list[Item], auto: bool, group_name: str = "題組") -> list[tuple[str | None, list[Item]]]:
    """分大題。自動分組時依題型排序（同題型維持原順序，題組不會被拆開）。"""
    if not auto:
        out: list[tuple[str | None, list[Item]]] = []
        for it in items:
            if not out or out[-1][0] != it.section_name:
                out.append((it.section_name, []))
            out[-1][1].append(it)
        return out
    groups: OrderedDict[str, list[Item]] = OrderedDict((t, []) for t in TYPE_ORDER)
    for it in items:
        q = it.question
        # 閱讀、圖表題組自成一個大題，放在一般題型之後
        key = "group" if (q.group_stem or q.shared_asset_key) else q.type.value
        groups.setdefault(key, []).append(it)
    named = [(group_name if t == "group" else TYPE_NAME.get(t, t), its) for t, its in groups.items() if its]
    return [(f"{NUM[i]}、{name}" if i < len(NUM) else name, its) for i, (name, its) in enumerate(named)]


def section_note(its: list[Item]) -> str:
    scores = [it.score for it in its]
    if not all(scores):
        return ""
    total = sum(scores)
    if len(set(scores)) == 1:
        return f"（每題 {scores[0]:g} 分，共 {total:g} 分）"
    return f"（共 {total:g} 分）"


def render(title: str, items: list[Item], mode: str, settings: dict | None,
           asset_root: Path, asset_url: str = "/assets/", base_href: str | None = None) -> str:
    st = {**DEFAULTS, **(settings or {})}
    cols = 2 if int(st["columns"]) == 2 and mode != "answer" else 1     # 答案卷的作答格不分欄
    r = Renderer(asset_root, asset_url, cols, st["uniform_option_images"] is not False)
    suffix = {"exam": "", "answer": "　答案卷", "key": "　教師解答卷"}[mode]
    total = sum(it.score or 0 for it in items)

    head = [f'<header class="head"><h1>{esc(title)}{suffix}</h1>']
    if st["subtitle"]:
        head.append(f'<div class="sub">{esc(st["subtitle"])}</div>')
    meta = f"共 {len(items)} 題" + (f"　滿分 {total:g} 分" if st["show_score"] and total else "")
    head.append(f'<div class="meta">{meta}</div></header>')
    if st["header_fields"] and mode != "key":
        head.append('<div class="fields"><span>班級：＿＿＿＿</span><span>座號：＿＿＿</span>'
                    '<span>姓名：＿＿＿＿＿＿</span><span>得分：＿＿＿＿</span></div>')

    subjects = {it.question.document.subject for it in items if it.question.document}
    gname = GROUP_NAME.get(next(iter(subjects)), "題組") if len(subjects) == 1 else "題組"
    secs = sections(items, st["auto_sections"], gname)
    body: list[str] = []
    if mode == "answer":
        n = 0
        for name, its in secs:
            if name:
                body.append(f'<div class="sec">{esc(name)}</div>')
            grid = []
            for start in range(0, len(its), 10):
                chunk = range(n + start + 1, n + min(start + 10, len(its)) + 1)
                grid.append("".join(f'<div class="n">{i}</div>' for i in chunk))
                grid.append("".join('<div class="b"></div>' for _ in chunk))
            n += len(its)
            body.append(f'<div class="grid">{"".join(grid)}</div>')
    else:
        n = 0
        seen: dict = {"shared": set()}
        for name, its in secs:
            if name:
                note = section_note(its) if st["show_score"] else ""
                body.append(f'<div class="sec">{esc(name)}<span class="sn">{note}</span></div>')
            for it in its:
                n += 1
                body.append(r.question(it, n, mode, st, seen))
        if mode == "exam" and st["answer_appendix"]:
            n = 0
            cells = []
            for _, its in secs:
                for it in its:
                    n += 1
                    a = "、".join(str(x) for x in (it.question.answer or [])) or "—"
                    cells.append(f"<div><b>{n}.</b> {esc(a)}</div>")
            body.append('<div class="appendix"><div class="sec">參考答案</div>'
                        f'<div class="anslist">{"".join(cells)}</div></div>')

    sources: OrderedDict[str, None] = OrderedDict()
    for it in items:
        for c in it.question.citations:
            sources.setdefault(c, None)
    foot = ('<footer class="src">本卷題目取自下列公開試題，著作權屬原命題單位所有：'
            + "、".join(esc(s) for s in sources) + "</footer>")

    base = f'<base href="{esc(base_href)}">' if base_href else ""
    return (f'<!doctype html><html lang="zh-Hant"><head><meta charset="utf-8">{base}'
            f"<title>{esc(title)}{suffix}</title><style>{PRINT_CSS}"
            f":root{{--fs:{float(st['font_size'])}pt}}</style></head>"
            f'<body><div class="paper">{"".join(head)}'
            f'<main class="cols{cols}">{"".join(body)}</main>{foot}</div></body></html>')


PRINT_CSS = """
@page { size: A4; margin: 15mm 13mm 16mm 13mm; }
* { box-sizing: border-box; }
html { background: #fff; }
/* 英數字先用西文字型：CJK 字型裡的 ’ “ 是全形寬，英文閱讀題會出現怪空格 */
body { font-family: "Times New Roman", "Liberation Serif", "Noto Serif TC", "Noto Serif CJK TC",
       "Songti TC", "PMingLiU", serif;
       font-size: var(--fs); line-height: 1.75; color: #000; margin: 0; }
@media screen { body { background: #e9ebee; padding: 16px 0; }
  .paper { background: #fff; width: 210mm; min-height: 297mm; margin: 0 auto; padding: 15mm 13mm;
           box-shadow: 0 1px 6px rgba(0,0,0,.18); } }
.head { text-align: center; border-bottom: 2px solid #000; padding-bottom: 5px; margin-bottom: 8px; }
.head h1 { font-size: 1.45em; margin: 0; letter-spacing: .08em; }
.head .sub { font-size: 1.02em; margin-top: 2px; }
.head .meta { font-size: .82em; color: #333; }
.fields { display: flex; gap: 1.6em; justify-content: flex-end; font-size: .9em; margin: 4px 0 10px; }
main.cols2 { column-count: 2; column-gap: 9mm; column-rule: 1px solid #999; }
.sec { font-weight: 700; font-size: 1.05em; margin: 12px 0 6px; break-after: avoid; }
.sec .sn { font-weight: 400; font-size: .85em; margin-left: .4em; }
.q { display: flex; gap: .35em; margin: 0 0 .85em; break-inside: avoid; }
.qn { font-weight: 700; min-width: 1.9em; text-align: right; }
.qb { flex: 1; min-width: 0; }
.sc { font-size: .8em; color: #555; white-space: nowrap; }
.opts { display: grid; gap: 1px 1.2em; margin-top: 2px; }
.opts.c4 { grid-template-columns: repeat(4, 1fr); }
.opts.c2 { grid-template-columns: repeat(2, 1fr); }
.opts.c1 { grid-template-columns: 1fr; }
.opt { display: flex; gap: .25em; }
.ol { white-space: nowrap; }
.paren { margin-right: .3em; }
.group-stem { padding: 4px 8px; margin: 6px 0; border-left: 3px solid #555; break-inside: avoid; }
.passage { padding: 6px 10px; margin: 6px 0; border: 1px solid #888; break-inside: avoid; }
.passage p { margin: 0 0 .4em; text-indent: 2em; }
figure { margin: 4px 0; text-align: center; break-inside: avoid; }
figure img { max-width: 100%; height: auto; }
.opt .oc { flex: 1; min-width: 0; } .opt img { display: block; max-width: 100%; margin: 1px 0 3px; }
figcaption { font-size: .8em; }
table.asset { border-collapse: collapse; margin: 5px auto; font-size: .92em; }
table.asset td, table.asset th { border: 1px solid #333; padding: 1px 8px; text-align: center; }
.lines { margin-top: 4px; }
.lines .ln { border-bottom: 1px solid #999; height: 2em; }
.cite { font-size: .68em; color: #777; text-align: right; line-height: 1.3; }
.ans { color: #b00; font-weight: 700; }
.ans .warn { font-weight: 400; font-size: .8em; margin-left: .4em; }
.expl { font-size: .9em; border-left: 3px solid #ccc; padding: 2px 8px; margin-top: 2px; }
.grid { display: grid; grid-template-columns: repeat(10, 1fr); border: 1px solid #000; margin-bottom: 8px; }
.grid div { border: 1px solid #999; text-align: center; min-height: 2.3em; padding-top: .3em; }
.grid .n { background: #eee; font-weight: 700; min-height: 0; padding: 0; }
.appendix { break-before: page; column-span: all; }
.anslist { display: grid; grid-template-columns: repeat(5, 1fr); gap: 2px 10px; }
footer.src { margin-top: 14px; padding-top: 5px; border-top: 1px solid #999; font-size: .68em; color: #555; }
""" + MATH_CSS


def to_pdf(html_text: str) -> bytes:
    """用 headless Chromium 把 HTML 印成 PDF（頁尾印頁碼）。html_text 要帶 <base>，圖才找得到。"""
    from playwright.sync_api import sync_playwright
    # OLEQS_CHROMIUM：指定現成的 Chromium（版本與 playwright 套件不符時用）
    exe = os.environ.get("OLEQS_CHROMIUM") or None
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=exe)
        try:
            page = browser.new_page()
            page.set_content(html_text, wait_until="networkidle")
            return page.pdf(format="A4", print_background=True, prefer_css_page_size=True,
                            display_header_footer=True, header_template="<span></span>",
                            footer_template='<div style="width:100%;text-align:center;font-size:8pt;'
                                            'color:#555">第 <span class="pageNumber"></span> 頁，'
                                            '共 <span class="totalPages"></span> 頁</div>')
        finally:
            browser.close()
