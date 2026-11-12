#!/usr/bin/env python3
"""Microsoft 方程式編輯器 3.0（MTEF 第 3 版）→ LaTeX。

舊的 Word 卷（多數物理、化學卷）用的是 Office 內建的「方程式編輯器 3.0」，
公開的 Ruby gem 只支援 MathType 5 以後的 MTEF 第 5 版，所以第 3 版由本程式依公開規格解析：
MathType「MTEF Version 3」說明（rtf2latex2e 網站存檔 https://rtf2latex2e.sourceforge.net/MTEF3.html）。

    python scripts/mtef3.py oleObject1.bin ...      # 測試用：逐個印出 LaTeX

解析不了（不認得的模板、資料截斷）就丟 ValueError，呼叫端把那題列入 dropped。
"""
from __future__ import annotations

import sys

GREEK = {
    "α": "alpha", "β": "beta", "γ": "gamma", "δ": "delta", "ε": "varepsilon", "ϵ": "epsilon", "ζ": "zeta",
    "η": "eta", "θ": "theta", "ϑ": "theta", "ι": "iota", "κ": "kappa", "λ": "lambda", "μ": "mu", "ν": "nu",
    "ξ": "xi", "π": "pi", "ρ": "rho", "σ": "sigma", "ς": "sigma", "τ": "tau", "υ": "upsilon", "φ": "phi",
    "ϕ": "varphi", "χ": "chi", "ψ": "psi", "ω": "omega", "Γ": "Gamma", "Δ": "Delta", "Θ": "Theta",
    "Λ": "Lambda", "Ξ": "Xi", "Π": "Pi", "Σ": "Sigma", "Φ": "Phi", "Ψ": "Psi", "Ω": "Omega",
}
SYMBOL = {
    "⋅": r"\cdot ", "·": r"\cdot ", "∙": r"\cdot ", "×": r"\times ", "÷": r"\div ", "±": r"\pm ", "∓": r"\mp ",
    "≤": r"\leq ", "≥": r"\geq ", "≠": r"\neq ", "≈": r"\approx ", "≅": r"\cong ", "∼": r"\sim ", "≡": r"\equiv ",
    "∝": r"\propto ", "→": r"\rightarrow ", "←": r"\leftarrow ", "↔": r"\leftrightarrow ", "⇒": r"\Rightarrow ",
    "⇔": r"\Leftrightarrow ", "⇌": r"\rightleftharpoons ", "↑": r"\uparrow ", "↓": r"\downarrow ",
    "∞": r"\infty ", "∂": r"\partial ", "∇": r"\nabla ", "∠": r"\angle ", "△": r"\triangle ", "⊥": r"\perp ",
    "∥": r"\parallel ", "∴": r"\therefore ", "∵": r"\because ", "∈": r"\in ", "∉": r"\notin ", "⊂": r"\subset ",
    "⊆": r"\subseteq ", "⊃": r"\supset ", "∪": r"\cup ", "∩": r"\cap ", "∅": r"\emptyset ", "∀": r"\forall ",
    "∃": r"\exists ", "°": r"^{\circ}", "′": "'", "″": "''", "ℓ": r"\ell ", "−": "-", "–": "-", "…": r"\ldots ",
    "⋯": r"\cdots ", "∑": r"\sum ", "∏": r"\prod ", "∫": r"\int ", "√": r"\surd ", "〈": r"\langle ",
    "〉": r"\rangle ", "⟨": r"\langle ", "⟩": r"\rangle ", "{": r"\{", "}": r"\}", "%": r"\%", "#": r"\#",
    "&": r"\&", "_": r"\_", "~": r"\sim ", " ": " ", " ": " ", " ": " ", " ": " ", "∗": "*",
    "□": r"\square ", "○": r"\bigcirc ", "★": r"\bigstar ", "⊕": r"\oplus ", "⊗": r"\otimes ", "⊙": r"\odot ",
}
FENCES = {0: (r"\langle ", r"\rangle "), 1: ("(", ")"), 2: (r"\{", r"\}"), 3: ("[", "]"), 4: ("|", "|"),
          5: (r"\Vert ", r"\Vert "), 6: ("[", "]"), 7: ("[", "]"), 8: ("[", "["), 9: ("]", "]"),
          10: ("]", "["), 11: ("[", ")"), 12: ("(", "]")}
BIGOP = {21: r"\int", 22: r"\iint", 23: r"\iint", 24: r"\int", 25: r"\iint", 26: r"\iint", 29: r"\sum", 30: r"\sum",
         31: r"\prod", 32: r"\prod", 33: r"\prod", 34: r"\prod", 35: r"\cup", 36: r"\cup", 37: r"\cap", 38: r"\cap",
         42: r"\int", 43: r"\sum"}
ACCENT = {"\u20d1": r"\overrightarrow", "\u20d7": r"\overrightarrow", "→": r"\overrightarrow", r"\rightarrow": r"\overrightarrow",
          "\u20e1": r"\overleftrightarrow", "↔": r"\overleftrightarrow", r"\leftrightarrow": r"\overleftrightarrow",
          "¯": r"\overline", "\u0305": r"\overline", "⌒": r"\overparen", "︵": r"\overparen", "\u0311": r"\overparen"}
FUNCS = {"sin", "cos", "tan", "cot", "sec", "csc", "log", "ln", "lim", "max", "min", "exp"}
EMBELL = {2: ("dot", None), 3: ("ddot", None), 5: ("prime", "'"), 6: ("dprime", "''"), 18: ("tprime", "'''"),
          9: ("hat", r"\widehat"), 11: ("rarrow", r"\vec"), 13: ("barrow", r"\overleftrightarrow"),
          17: ("obar", r"\overline"), 19: ("frown", r"\overparen"), 8: ("tilde", None), 10: ("not", None)}


class Reader:
    def __init__(self, data: bytes):
        self.d, self.p = data, 0

    def byte(self) -> int:
        if self.p >= len(self.d):
            raise ValueError("資料截斷")
        self.p += 1
        return self.d[self.p - 1]

    def word(self) -> int:
        return self.byte() | (self.byte() << 8)

    def nudge(self, tag: int) -> None:
        if tag & 0x80:                                 # xfLMOVE
            dx, dy = self.byte(), self.byte()
            if dx == 128 and dy == 128:
                self.word(), self.word()


def char_tex(face: int, ch: str) -> str:
    if ch in GREEK:
        return "\\" + GREEK[ch] + " "
    if ch in SYMBOL:
        return SYMBOL[ch]
    if face == 1 and ord(ch) > 0x2E80:                 # 文字字型裡的中文
        return r"\text{" + ch + "}"
    return ch


class Parser:
    def __init__(self, data: bytes):
        self.r = Reader(data)

    def objects(self) -> list[tuple[str, str]]:
        """讀一串記錄到 END 為止，回傳 [(種類, LaTeX)]；種類 char／tmpl／line／pile／matrix／script。"""
        out: list[tuple[str, str]] = []
        while True:
            tag = self.r.byte()
            kind = tag & 0x0F
            if kind == 0:                                  # END
                return out
            if kind == 1:
                out.append(("line", self.line(tag)))
            elif kind == 2:
                out.append(self.char(tag))
            elif kind == 3:
                out.append(self.tmpl(tag))
            elif kind == 4:
                out.append(("pile", self.pile(tag)))
            elif kind == 5:
                out.append(("matrix", self.matrix(tag)))
            elif kind == 6:
                self.r.nudge(tag)
                self.r.byte()
            elif kind == 7:
                self.ruler()
            elif kind == 8:
                self.r.byte(), self.r.byte()
                while self.r.byte():
                    pass
            elif kind == 9:
                first = self.r.byte()
                if first == 101:
                    self.r.word()
                elif first == 100:
                    self.r.byte(), self.r.word()
                else:
                    self.r.byte()
            elif kind in (10, 11, 12, 13, 14):
                pass
            else:
                raise ValueError(f"不認得的記錄 {kind}")

    def ruler(self) -> None:
        for _ in range(self.r.byte()):
            self.r.byte(), self.r.word()

    def line(self, tag: int) -> str:
        self.r.nudge(tag)
        if tag & 0x10:                                   # xfNULL：空的格子
            return ""
        if tag & 0x40:                                   # xfLSPACE
            self.r.word()
        if tag & 0x20:                                   # xfRULER
            self.r.byte()
            self.ruler()
        return join(self.objects())

    def char(self, tag: int) -> tuple[str, str]:
        self.r.nudge(tag)
        face = self.r.byte() - 128
        ch = chr(self.r.word())
        tex = char_tex(face, ch)
        if tag & 0x20:                                   # xfEMBELL：裝飾（上點、撇號、向量箭頭…）
            while True:
                t = self.r.byte()
                if t & 0x0F == 0:
                    break
                self.r.nudge(t)
                emb = self.r.byte()
                name, how = EMBELL.get(emb, (None, None))
                if how is None:
                    raise ValueError(f"不支援的裝飾 {emb}")
                tex = tex + how if how.startswith("'") else how + "{" + tex.strip() + "}"
        return ("func" if face == 2 else "char"), tex

    def pile(self, tag: int) -> str:
        self.r.nudge(tag)
        self.r.byte(), self.r.byte()
        if tag & 0x20:
            self.r.byte()
            self.ruler()
        rows = [t for k, t in self.objects() if k == "line"]
        return r"\begin{matrix}" + r" \\ ".join(rows) + r"\end{matrix}"

    def matrix(self, tag: int) -> str:
        self.r.nudge(tag)
        self.r.byte(), self.r.byte(), self.r.byte()
        rows, cols = self.r.byte(), self.r.byte()
        for n in (rows, cols):
            for _ in range(((n + 1) * 2 + 7) // 8):
                self.r.byte()
        cells = [t for k, t in self.objects() if k == "line"]
        if len(cells) != rows * cols:
            raise ValueError("矩陣格數不對")
        body = r" \\ ".join(" & ".join(cells[r * cols:(r + 1) * cols]) for r in range(rows))
        return r"\begin{matrix}" + body + r"\end{matrix}"

    def tmpl(self, tag: int) -> tuple[str, str]:
        self.r.nudge(tag)
        sel, var = self.r.byte(), self.r.byte()
        self.r.byte()                                    # options
        items = self.objects()
        slots = [t for k, t in items if k in ("line", "pile")]
        chars = [t for k, t in items if k in ("char", "func")]
        s = slots + ["", "", ""]
        if sel in FENCES:
            left, right = FENCES[sel]
            if sel <= 5:
                left = left if var in (0, 1) else "."
                right = right if var in (0, 2) else "."
            body = s[0]
            if "\\begin{matrix}" in body and left == r"\{" and right == ".":
                return "tmpl", body.replace(r"\begin{matrix}", r"\begin{cases}").replace(r"\end{matrix}", r"\end{cases}")
            return "tmpl", r"\left" + left.strip() + " " + body + r" \right" + right.strip() + " "
        if sel == 13:
            return "tmpl", (r"\sqrt[" + s[1] + "]{" + s[0] + "}") if var == 1 and s[1] else r"\sqrt{" + s[0] + "}"
        if sel in (14, 41):
            return "tmpl", r"\frac{" + s[0] + "}{" + s[1] + "}"
        if sel in (15, 44):                                # 上下標：格子順序是下標、上標
            sub, sup = (slots + ["", ""])[:2]
            out = ("_{" + sub + "}" if sub else "") + ("^{" + sup + "}" if sup else "")
            return "script", out
        if sel == 16:
            return "tmpl", r"\underline{" + s[0] + "}"
        if sel == 17:
            return "tmpl", r"\overline{" + s[0] + "}"
        if sel in (18, 19, 20):
            cmd = {18: r"\xleftarrow", 19: r"\xrightarrow", 20: r"\xrightleftharpoons"}[sel]
            text = s[0] if len(slots) else ""
            return "tmpl", (cmd + "[" + text + "]{}") if var == 1 else (cmd + "{" + text + "}")
        if sel in BIGOP:
            main, upper, lower = s[0], s[1], s[2]
            return "tmpl", BIGOP[sel] + ("_{" + lower + "}" if lower else "") + ("^{" + upper + "}" if upper else "") + " " + main
        if sel == 39:                                      # 上下限：原本給 lim 用，也常被拿來在字上加箭頭
            main, lower, upper = s[0], s[1], s[2]
            for mark in (upper, lower):
                acc = ACCENT.get(mark.strip())
                if acc and main:
                    return "tmpl", acc + "{" + main + "}"
            if main.replace(" ", "") in ("lim", r"\mathrm{lim}", r"\lim"):
                return "tmpl", r"\lim_{" + lower + "}" + ("^{" + upper + "}" if upper else "") + " "
            out = main
            if upper:
                out = r"\overset{" + upper + "}{" + out + "}"
            if lower:
                out = r"\underset{" + lower + "}{" + out + "}"
            return "tmpl", out
        if sel in (46, 47):
            return "tmpl", {0: r"\overleftarrow", 1: r"\overrightarrow", 2: r"\overleftrightarrow"}.get(var, r"\vec") + "{" + s[0] + "}"
        if sel == 48:
            return "tmpl", r"\overparen{" + s[0] + "}"
        raise ValueError(f"不支援的模板 {sel}")


def join(items: list[tuple[str, str]]) -> str:
    """把一串字元與模板接起來；連續的函數字型字母合成 \\sin 這類函數名。"""
    out, func = [], ""
    for kind, tex in items + [("end", "")]:
        if kind == "func" and len(tex) == 1 and tex.isalpha():
            func += tex
            continue
        if func:
            out.append("\\" + func + " " if func in FUNCS else r"\mathrm{" + func + "}")
            func = ""
        out.append(tex)
    return "".join(out).strip()


def mtef_bytes(ole: bytes) -> bytes:
    """OLE 物件裡的 MTEF 資料。公式外面又包了一層 Word 文件的，找 ObjectPool 裡唯一那一個。"""
    import io
    import olefile
    with olefile.OleFileIO(io.BytesIO(ole)) as o:
        hits = [p for p in o.listdir() if p[-1] == "Equation Native"]
        if len(hits) != 1:
            raise ValueError(f"找到 {len(hits)} 個 Equation Native")
        data = o.openstream(hits[0]).read()
    return data[int.from_bytes(data[:2], "little"):]      # 前面是 28 位元組的標頭


def to_latex(ole: bytes) -> str:
    m = mtef_bytes(ole)
    if not m or m[0] != 3:
        raise ValueError(f"不是 MTEF 第 3 版（{m[:1].hex()}）")
    p = Parser(m[5:])                                      # 跳過 5 位元組的 MTEF 標頭
    return join(p.objects())


def main() -> int:
    for f in sys.argv[1:]:
        try:
            print(f, to_latex(open(f, "rb").read()))
        except Exception as e:                          # noqa: BLE001
            print(f, "✗", e)
    return 0


if __name__ == "__main__":
    sys.exit(main())
