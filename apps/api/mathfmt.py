"""把題幹裡的 Markdown + 行內 LaTeX 轉成 HTML。

刻意不引入 KaTeX/MathJax：
  - 這個環境不允許外部 CDN，內嵌 KaTeX 要多帶約 300 KB 的 JS 與字型；
  - 國中考卷實際用到的數學符號是很小的子集 —— 指數、下標、分數、根號、
    絕對值、線段上標、乘除號。實測兩份真實考卷（29 + 35 題）全部涵蓋。

若日後要出現矩陣、積分、多行對齊，再換成 KaTeX；那時這個模組的介面不變。
"""

from __future__ import annotations

import html
import re

# \times 這類命令直接對應到 Unicode 字元，交給字型處理最穩
COMMANDS = {
    r"\times": "×", r"\div": "÷", r"\pm": "±", r"\mp": "∓",
    r"\cdot": "·", r"\leq": "≤", r"\geq": "≥", r"\neq": "≠",
    r"\approx": "≈", r"\infty": "∞", r"\degree": "°", r"\circ": "∘",
    r"\alpha": "α", r"\beta": "β", r"\theta": "θ", r"\pi": "π",
    r"\angle": "∠", r"\triangle": "△", r"\parallel": "∥", r"\perp": "⊥",
    r"\rightarrow": "→", r"\Rightarrow": "⇒", r"\ldots": "…", r"\cdots": "⋯",
    r"\%": "%", r"\,": " ", r"\ ": " ", r"\!": "",
}


def _group(s: str, i: int) -> tuple[str, int]:
    """讀取 { … } 或單一字元，回傳 (內容, 下一個位置)。"""
    if i >= len(s):
        return "", i
    if s[i] != "{":
        return s[i], i + 1
    depth, j = 1, i + 1
    while j < len(s) and depth:
        if s[j] == "{":
            depth += 1
        elif s[j] == "}":
            depth -= 1
        j += 1
    return s[i + 1:j - 1], j


def latex_to_html(tex: str) -> str:
    """把一段行內 LaTeX 轉成 HTML。未知命令原樣保留，不靜默丟棄。"""
    out: list[str] = []
    i = 0
    while i < len(tex):
        c = tex[i]

        if c == "\\":
            m = re.match(r"\\[a-zA-Z]+|\\.", tex[i:])
            cmd = m.group(0) if m else "\\"

            if cmd in (r"\frac", r"\dfrac", r"\tfrac"):
                num, j = _group(tex, i + len(cmd))
                den, j = _group(tex, j)
                out.append('<span class="frac">'
                           f'<span class="num">{latex_to_html(num)}</span>'
                           f'<span class="den">{latex_to_html(den)}</span></span>')
                i = j
                continue
            if cmd == r"\sqrt":
                body, j = _group(tex, i + len(cmd))
                out.append(f'√<span class="sqrt">{latex_to_html(body)}</span>')
                i = j
                continue
            if cmd == r"\overline":
                body, j = _group(tex, i + len(cmd))
                out.append(f'<span class="ovl">{latex_to_html(body)}</span>')
                i = j
                continue
            if cmd == r"\text":
                body, j = _group(tex, i + len(cmd))
                out.append(html.escape(body))
                i = j
                continue
            if cmd == r"\left" or cmd == r"\right":
                i += len(cmd)
                continue
            if cmd in COMMANDS:
                out.append(COMMANDS[cmd])
                i += len(cmd)
                continue
            # 未知命令：保留原樣，讓校對員看得見而不是被吃掉
            out.append(html.escape(cmd))
            i += len(cmd)
            continue

        if c == "^":
            body, j = _group(tex, i + 1)
            out.append(f"<sup>{latex_to_html(body)}</sup>")
            i = j
            continue
        if c == "_":
            body, j = _group(tex, i + 1)
            out.append(f"<sub>{latex_to_html(body)}</sub>")
            i = j
            continue
        if c in "{}":
            i += 1
            continue

        out.append(html.escape(c))
        i += 1

    return "".join(out)


def render(md: str | None) -> str:
    """Markdown（粗體）+ 行內 $…$ 公式 → HTML。非公式部分一律逸出。"""
    if not md:
        return ""
    parts: list[str] = []
    for i, chunk in enumerate(re.split(r"\$([^$]*)\$", md)):
        if i % 2:                                  # 奇數段是公式
            parts.append(f'<span class="math">{latex_to_html(chunk)}</span>')
        else:
            esc = html.escape(chunk, quote=False)
            esc = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", esc)
            parts.append(esc.replace("\n", "<br>"))
    return "".join(parts)


MATH_CSS = """
.math{font-family:"Cambria Math","Latin Modern Math",Georgia,serif;font-style:italic}
.math sup,.math sub{font-style:normal;font-size:.72em}
.frac{display:inline-flex;flex-direction:column;vertical-align:-0.55em;
      text-align:center;font-size:.92em;margin:0 .15em}
.frac .num{border-bottom:1px solid currentColor;padding:0 .3em}
.frac .den{padding:0 .3em}
.ovl{border-top:1px solid currentColor;padding-top:1px}
.sqrt{border-top:1px solid currentColor;padding:0 .15em}
"""
