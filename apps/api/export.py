"""把組好的卷輸出成可列印的 HTML（瀏覽器直接列印即為 PDF）。

三份輸出共用同一份渲染邏輯，避免「畫面看到的」與「印出來的」不一致：
    exam    試題卷
    answer  答案卷
    key     教師解答卷（題目 + 答案 + 詳解）

**來源標註在這裡強制執行。** 依授權條件，公開試題可用但須保留出處，
所以出處字串由 Document 直接產生，不接受呼叫端覆寫或關閉。
"""

from __future__ import annotations

import html
from collections import OrderedDict

from .mathfmt import MATH_CSS, render as md
from .models import Paper, Question

PRINT_CSS = """
@page { size: A4; margin: 14mm 12mm 16mm 12mm; }
* { box-sizing: border-box; }
body { font-family: "Noto Serif TC", "Songti TC", serif; font-size: 11.5pt;
       line-height: 1.7; color: #000; margin: 0; }
.head { text-align: center; border-bottom: 2px solid #000; padding-bottom: 6px;
        margin-bottom: 10px; }
.head h1 { font-size: 15pt; margin: 0 0 4px; }
.meta { font-size: 9.5pt; color: #333; }
.fields { display: flex; gap: 18px; justify-content: flex-end;
          font-size: 10pt; margin: 6px 0 12px; }
.section-title { font-weight: 700; font-size: 12pt; margin: 14px 0 6px;
                 border-left: 4px solid #000; padding-left: 6px; }
/* 題目不可跨頁斷裂 —— 老師最在意的排版細節 */
.q { break-inside: avoid; page-break-inside: avoid; margin: 0 0 11px; }
.q-head { display: flex; gap: 6px; }
.q-no { font-weight: 700; min-width: 2.2em; }
.q-body { flex: 1; }
.opts { display: grid; grid-template-columns: repeat(2, 1fr); gap: 2px 14px;
        margin-top: 3px; }
.opts.wide { grid-template-columns: 1fr; }
.opt { display: flex; gap: 4px; }
.group-stem { background: #f4f4f4; padding: 6px 8px; margin: 8px 0 6px;
              border-left: 3px solid #888; break-inside: avoid; }
.passage { background: #f8f8f8; padding: 8px 10px; margin: 8px 0;
           border: 1px solid #ddd; text-indent: 2em; break-inside: avoid; }
figure { margin: 6px 0; text-align: center; break-inside: avoid; }
figure img { max-width: 100%; }
figcaption { font-size: 9pt; color: #555; }
table.asset { border-collapse: collapse; margin: 6px auto; font-size: 10.5pt; }
table.asset td, table.asset th { border: 1px solid #333; padding: 2px 10px;
                                 text-align: center; }
.cite { font-size: 8.5pt; color: #666; margin-top: 2px; }
.ans { color: #b00; font-weight: 700; }
.expl { font-size: 10pt; color: #333; background: #fafafa; padding: 4px 8px;
        margin-top: 3px; border-left: 3px solid #ccc; }
.answer-grid { display: grid; grid-template-columns: repeat(10, 1fr);
               border: 1px solid #000; }
.answer-grid div { border: 1px solid #999; padding: 5px 2px; text-align: center;
                   font-size: 10pt; min-height: 2.1em; }
.answer-grid .n { background: #eee; font-weight: 700; }
.footer-note { margin-top: 16px; padding-top: 6px; border-top: 1px solid #999;
               font-size: 8.5pt; color: #555; }
""" + MATH_CSS


def esc(s: str | None) -> str:
    return html.escape(s or "", quote=False)


def render_options(q: Question) -> str:
    if not q.options:
        return ""
    # 選項短就排兩欄，長就排一欄
    longest = max((len(o.content_md) for o in q.options), default=0)
    cls = "opts" if longest <= 18 else "opts wide"
    cells = []
    for o in q.options:
        body = md(o.content_md)
        if o.asset_file:
            body += f'<img src="/assets/{esc(o.asset_file)}" alt="選項{esc(o.label)}">'
        cells.append(f'<div class="opt"><span>({esc(o.label)})</span>'
                     f'<span>{body}</span></div>')
    return f'<div class="{cls}">{"".join(cells)}</div>'


def render_assets(q: Question) -> str:
    out = []
    for a in sorted(q.assets, key=lambda x: x.key):
        if a.kind.value == "table" and a.markdown:
            out.append(md_table(a.markdown, a.label))
        elif a.file:
            cap = f"<figcaption>{esc(a.label)}</figcaption>" if a.label else ""
            out.append(f'<figure><img src="/assets/{esc(a.file)}" '
                       f'alt="{esc(a.alt)}">{cap}</figure>')
        elif a.pending:
            out.append('<figure style="border:1px dashed #c00;padding:8px;color:#c00">'
                       f'⚠ 此題有圖尚未產生檔案（{esc(a.key)}）</figure>')
    return "".join(out)


def md_table(markdown: str, label: str | None = None) -> str:
    """把 Markdown 表格轉成 HTML。只支援管線式表格，考卷用不到更複雜的語法。"""
    rows = [r.strip() for r in markdown.strip().splitlines() if r.strip()]
    rows = [r for r in rows if not set(r.replace("|", "").replace(" ", "")) <= set(":-")]
    cells = [[c.strip() for c in r.strip("|").split("|")] for r in rows]
    if not cells:
        return ""
    head = "".join(f"<th>{esc(c)}</th>" for c in cells[0])
    body = "".join("<tr>" + "".join(f"<td>{esc(c)}</td>" for c in r) + "</tr>"
                   for r in cells[1:])
    cap = f"<caption>{esc(label)}</caption>" if label else ""
    return f'<table class="asset">{cap}<tr>{head}</tr>{body}</table>'


def render_question(item, n: int, mode: str, seen_shared: set) -> str:
    q: Question = item.question
    out = []

    # 共用素材（多題共用的圖或閱讀短文）只在該組第一題前印一次
    if q.shared_asset_key and q.shared_asset_key not in seen_shared:
        seen_shared.add(q.shared_asset_key)
        shared = next((a for a in q.document.assets
                       if a.key == q.shared_asset_key), None)
        if shared:
            if shared.text:
                paras = "".join(f"<p>{esc(p)}</p>"
                                for p in shared.text.split("\n\n") if p.strip())
                out.append(f'<div class="passage">{paras}</div>')
            elif shared.file:
                cap = f"<figcaption>{esc(shared.label)}</figcaption>"
                out.append(f'<figure><img src="/assets/{esc(shared.file)}" '
                           f'alt="{esc(shared.alt)}">{cap}</figure>')

    if q.group_stem:
        out.append(f'<div class="group-stem">{md(q.group_stem)}</div>')

    body = [f'<div class="q-body">{md(q.stem_md)}']
    body.append(render_assets(q))
    body.append(render_options(q))

    if mode == "key":
        ans = "、".join(str(a) for a in (q.answer or [])) or "（無答案）"
        # 沒被答案卷或人工確認過的答案必須看得出來。教師解答卷是拿來改分的，
        # 把 AI 的作答印得跟正解一模一樣，等於把「不知道錯在哪」交給老師。
        note = {"ai_generated": "　⚠ AI 作答，未經確認",
                "disputed": "　⚠ 模型答案不一致，待判定"}.get(
                    getattr(q.answer_status, "value", q.answer_status), "")
        body.append(f'<div class="ans">答：{esc(ans)}{esc(note)}</div>')
        if q.explanation_md:
            body.append(f'<div class="expl">{esc(q.explanation_md)}</div>')

    # 來源標註：授權條件要求保留出處，因此固定輸出，不提供關閉選項。
    # 出處取自題目自身（可能有多個），與這份卷混用了幾份來源無關。
    body.append(f'<div class="cite">出處：{esc(q.citation)}</div>')
    body.append("</div>")

    score = f"（{item.score:g} 分）" if item.score else ""
    out.append(f'<div class="q"><div class="q-head">'
               f'<span class="q-no">{n}.</span>{"".join(body)}</div>'
               f'<span style="font-size:9.5pt;color:#666">{score}</span></div>')
    return "".join(out)


def render_paper(paper: Paper, mode: str = "exam") -> str:
    title_suffix = {"exam": "", "answer": "　答案卷", "key": "　教師解答卷"}[mode]

    sources = OrderedDict()
    for item in paper.items:
        for c in item.question.citations:
            sources.setdefault(c, None)

    parts = [f'<div class="head"><h1>{esc(paper.title)}{title_suffix}</h1>'
             f'<div class="meta">共 {len(paper.items)} 題　'
             f'總分 {paper.total_score:g} 分</div></div>',
             '<div class="fields"><span>班級：________</span>'
             '<span>座號：______</span><span>姓名：____________</span></div>']

    if mode == "answer":
        n = len(paper.items)
        nums = "".join(f'<div class="n">{i}</div>' for i in range(1, n + 1))
        blanks = "".join('<div></div>' for _ in range(n))
        # 每十題一列，題號列與作答列交錯
        grid = []
        for start in range(0, n, 10):
            end = min(start + 10, n)
            grid.append("".join(f'<div class="n">{i}</div>'
                                for i in range(start + 1, end + 1)))
            grid.append("".join('<div></div>' for _ in range(start, end)))
        parts.append(f'<div class="answer-grid">{"".join(grid)}</div>')
    else:
        current = None
        seen_shared: set = set()
        for n, item in enumerate(paper.items, start=1):
            if item.section_name and item.section_name != current:
                current = item.section_name
                parts.append(f'<div class="section-title">{esc(current)}</div>')
            parts.append(render_question(item, n, mode, seen_shared))

    parts.append('<div class="footer-note">本卷題目取自下列公開試題，'
                 '著作權屬原命題單位所有：<br>' +
                 "<br>".join(esc(s) for s in sources) + "</div>")

    return (f'<!doctype html><html lang="zh-Hant"><head><meta charset="utf-8">'
            f'<title>{esc(paper.title)}{title_suffix}</title>'
            f'<style>{PRINT_CSS}</style></head><body>{"".join(parts)}</body></html>')
