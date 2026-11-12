#!/usr/bin/env python3
"""MathType 公式（MTEF 第 5 版，含 MathType 4 存成的第 5 版）→ LaTeX，純 Python，不需要 ruby／pandoc（裁示 #018）。

依 MathType SDK 公開規格「MTEF Storage Format v5」。字元、上下標、函數名、括號的寫法沿用 mtef3.py，
輸出再經 ole_math.normalize() 正規化，和舊的 Ruby gem＋pandoc 路徑結果一致（數學第 1 批 412 個公式逐一比對）。

    python scripts/mtef5.py oleObject1.bin ...      # 測試用：逐個印出 LaTeX

解析不了（不認得的模板、資料截斷）就丟 ValueError，呼叫端把那題列入 dropped。
"""
from __future__ import annotations

import sys

from mtef3 import BIGOP as _BIGOP3, FUNCS, SYMBOL, char_tex, join, mtef_bytes  # noqa: F401

# 選項旗標
OPT_NUDGE = 0x08
OPT_CHAR_EMBELL = 0x01
OPT_CHAR_FUNC_START = 0x02
OPT_CHAR_ENC_CHAR_8 = 0x04
OPT_CHAR_ENC_CHAR_16 = 0x10
OPT_CHAR_ENC_NO_MTCODE = 0x20
OPT_LINE_NULL = 0x01
OPT_LP_RULER = 0x02
OPT_LINE_LSPACE = 0x04
OPT_COLOR_CMYK = 0x01
OPT_COLOR_NAME = 0x04

# 括號類模板：選擇子 → (左, 右)
FENCES = {0: (r"\langle", r"\rangle"), 1: ("(", ")"), 2: (r"\{", r"\}"), 3: ("[", "]"), 4: ("|", "|"),
          5: (r"\Vert", r"\Vert"), 6: (r"\lfloor", r"\rfloor"), 7: (r"\lceil", r"\rceil"), 8: ("[", "[")}
BIGOP = {15: r"\int", 16: r"\sum", 17: r"\prod", 18: r"\coprod", 19: r"\bigcup", 20: r"\bigcap", 21: r"\int", 22: r"\sum"}
# 字元裝飾（EMBELL）：種類 → (LaTeX 指令, 是否接在字後面)
EMBELL = {2: (r"\dot", False), 3: (r"\ddot", False), 4: (r"\dddot", False), 5: ("'", True), 6: ("''", True),
          7: ("'", True), 8: (r"\tilde", False), 9: (r"\hat", False), 11: (r"\overrightarrow", False),
          12: (r"\overleftarrow", False), 13: (r"\overleftrightarrow", False), 14: (r"\overrightarrow", False),
          15: (r"\overleftarrow", False), 16: (r"\overline", False), 17: (r"\overline", False), 18: ("'''", True),
          19: (r"\overparen", False), 20: (r"\overparen", False), 29: (r"\underline", False)}
# MathType 自己的私用區字元（MT Extra 等）
PRIVATE = {0xEB05: r"\cdots ", 0xEC00: r"\cdots ", 0xEB1A: r"\cdots ", 0x2026: r"\ldots ", 0x22EF: r"\cdots ",
           0xEB01: r"\ldots ", 0xEC04: r"\vdots ", 0xEC05: r"\ddots ", 0xEB00: r"\ldots ", 0xEE04: r"\neq ",
           0xF8E7: "-", 0xF8F7: "", 0xEB40: "", 0x2061: "", 0x2062: "", 0x2063: ""}


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

    def cstr(self) -> str:
        out = bytearray()
        while (b := self.byte()):
            out.append(b)
        return out.decode("latin-1")

    def nudge(self, opts: int) -> None:
        if opts & OPT_NUDGE:
            dx, dy = self.byte(), self.byte()
            if dx == 128 and dy == 128:
                self.word(), self.word()

    def nibble_dims(self, count: int) -> None:
        """跳過「尺寸陣列」：每個值是一串 4 位元（單位、數字、小數點、負號），0xF 結尾。"""
        nibs: list[int] = []
        done = 0
        while done < count:
            b = self.byte()
            for n in (b >> 4, b & 0x0F):
                nibs.append(n)
                if n == 0x0F:
                    done += 1
                    if done == count:
                        break


class Parser:
    def __init__(self, data: bytes):
        self.r = Reader(data)

    # 一串記錄到 END 為止 → [(種類, LaTeX)]
    def objects(self) -> list[tuple[str, str]]:
        out: list[tuple[str, str]] = []
        while True:
            tag = self.r.byte()
            if tag == 0:
                return out
            if tag == 1:
                out.append(("line", self.line()))
            elif tag == 2:
                out.append(self.char())
            elif tag == 3:
                out.append(self.tmpl())
            elif tag == 4:
                out.append(("pile", self.pile()))
            elif tag == 5:
                out.append(("matrix", self.matrix()))
            elif tag == 6:                               # 單獨的 EMBELL（不該出現在這裡，略過）
                o = self.r.byte()
                self.r.nudge(o)
                self.r.byte()
            else:
                self.skip(tag)

    def skip(self, tag: int) -> None:
        r = self.r
        if tag == 7:                                     # RULER
            self.ruler_body()
        elif tag == 8:                                   # FONT_STYLE_DEF
            r.byte(), r.byte()
        elif tag == 9:                                   # SIZE
            first = r.byte()
            if first == 101:
                r.word()
            elif first == 100:
                r.byte(), r.word()
            else:
                r.byte()
        elif tag in (10, 11, 12, 13, 14):               # FULL／SUB／SUB2／SYM／SUBSYM
            pass
        elif tag == 15:                                  # COLOR
            r.byte()
        elif tag == 16:                                  # COLOR_DEF
            o = r.byte()
            for _ in range(4 if o & OPT_COLOR_CMYK else 3):
                r.word()
            if o & OPT_COLOR_NAME:
                r.cstr()
        elif tag == 17:                                  # FONT_DEF
            r.byte()
            r.cstr()
        elif tag == 18:                                  # EQN_PREFS
            r.byte()
            n = r.byte()
            r.nibble_dims(n)
            n = r.byte()
            r.nibble_dims(n)
            n = r.byte()
            for _ in range(n):
                if r.byte():
                    r.byte()
        elif tag == 19:                                  # ENCODING_DEF
            r.cstr()
        elif tag >= 100:                                 # FUTURE：長度＋資料
            n = r.word()
            r.p += n
        else:
            raise ValueError(f"不認得的記錄 {tag}")

    def ruler_body(self) -> None:
        for _ in range(self.r.byte()):
            self.r.byte(), self.r.word()

    def line(self) -> str:
        o = self.r.byte()
        self.r.nudge(o)
        if o & OPT_LINE_LSPACE:
            self.r.word()
        if o & OPT_LP_RULER:
            if self.r.byte() != 7:
                raise ValueError("LINE 的 RULER 記錄不對")
            self.ruler_body()
        if o & OPT_LINE_NULL:
            return ""
        return join5(self.objects())

    def char(self) -> tuple[str, str]:
        o = self.r.byte()
        self.r.nudge(o)
        face = self.r.byte() - 128
        code = None
        if not o & OPT_CHAR_ENC_NO_MTCODE:
            code = self.r.word()
        if o & OPT_CHAR_ENC_CHAR_8:
            pos = self.r.byte()
            code = code if code is not None else pos
        if o & OPT_CHAR_ENC_CHAR_16:
            pos = self.r.word()
            code = code if code is not None else pos
        if code is None:
            raise ValueError("字元沒有內容")
        if code in PRIVATE:
            tex = PRIVATE[code]
        elif 0xE000 <= code <= 0xF8FF:
            if 0xEF00 <= code <= 0xEF0F:                  # MathType 的各種間距
                tex = " "
            else:
                # 模板裡的括號、直線字形（行列式兩側的 EC07、EC08）：模板只看格子、不看這些字元，所以先標記，
                # 留到模板外才算轉不出來（北一女數A，裁示 #018）
                return "pua", f"{code:04X}"
        else:
            tex = char_tex(face, chr(code))
        if o & OPT_CHAR_EMBELL:
            while True:
                t = self.r.byte()
                if t == 0:
                    break
                if t != 6:
                    raise ValueError("裝飾清單裡不是 EMBELL")
                eo = self.r.byte()
                self.r.nudge(eo)
                emb = self.r.byte()
                if emb not in EMBELL:
                    raise ValueError(f"不支援的裝飾 {emb}")
                how, after = EMBELL[emb]
                tex = tex + how if after else how + "{" + tex.strip() + "}"
        return ("func" if face == 2 else "char"), tex

    def pile(self) -> str:
        o = self.r.byte()
        self.r.nudge(o)
        self.r.byte(), self.r.byte()                     # halign, valign
        if o & OPT_LP_RULER:
            if self.r.byte() != 7:
                raise ValueError("PILE 的 RULER 記錄不對")
            self.ruler_body()
        rows = [t for k, t in self.objects() if k == "line"]
        # 換行寫成「\\＋換行字元」：ole_math.normalize() 會把「\＋空白」當成間距吃掉
        return r"\begin{matrix}" + " \\\\\n".join(rows) + r"\end{matrix}"

    def matrix(self) -> str:
        o = self.r.byte()
        self.r.nudge(o)
        self.r.byte(), self.r.byte(), self.r.byte()       # valign, h_just, v_just
        rows, cols = self.r.byte(), self.r.byte()
        for n in (rows, cols):
            for _ in range(((n + 1) * 2 + 7) // 8):
                self.r.byte()
        cells = [t for k, t in self.objects() if k == "line"]
        if len(cells) != rows * cols:
            raise ValueError("矩陣格數不對")
        body = " \\\\\n".join(" & ".join(cells[r * cols:(r + 1) * cols]) for r in range(rows))
        return r"\begin{matrix}" + body + r"\end{matrix}"

    def tmpl(self) -> tuple[str, str]:
        o = self.r.byte()
        self.r.nudge(o)
        sel = self.r.byte()
        var = self.r.byte()
        if var & 0x80:
            var = (var & 0x7F) | (self.r.byte() << 8)
        self.r.byte()                                    # 模板自己的選項
        items = self.objects()
        slots = [t for k, t in items if k in ("line", "pile")]
        chars = [t for k, t in items if k in ("char", "func")]
        s = slots + ["", "", ""]
        if sel in FENCES:
            left, right = FENCES[sel]
            if not var & 1:
                left = "."
            if not var & 2:
                right = "."
            body = s[0]
            if "\\begin{matrix}" in body and left == r"\{" and right == ".":
                return "tmpl", body.replace(r"\begin{matrix}", r"\begin{cases}").replace(r"\end{matrix}", r"\end{cases}")
            return "tmpl", r"\left" + left + " " + body + r" \right" + right + " "
        if sel == 9:                                     # 區間：左右各自的括號
            lmap = {0: "(", 1: ")", 2: "[", 3: "]"}
            return "tmpl", r"\left" + lmap.get(var & 3, "(") + " " + s[0] + r" \right" + lmap.get((var >> 4) & 3, ")") + " "
        if sel == 10:
            return "tmpl", (r"\sqrt[" + s[1] + "]{" + s[0] + "}") if var & 1 and s[1] else r"\sqrt{" + s[0] + "}"
        if sel == 11:
            return "tmpl", r"\frac{" + s[0] + "}{" + s[1] + "}"
        if sel == 12:
            return "tmpl", r"\underline{" + s[0] + "}"
        if sel == 13:
            return "tmpl", r"\overline{" + s[0] + "}"
        if sel == 14:                                    # 箭頭上下寫字；變化值 0x10 向左、0x20 向右，兩個都有是雙向
            cmd = r"\xleftarrow" if var & 0x10 and not var & 0x20 else r"\xrightarrow"
            if var & 0x30 == 0x30:
                cmd = r"\xleftrightarrow"
            if var & 0x01:
                cmd = r"\xrightleftharpoons"
            top, bottom = s[0], s[1]
            return "tmpl", cmd + ("[" + bottom + "]" if bottom else "") + "{" + top + "}"
        if sel in BIGOP:
            main, lower, upper = s[0], s[1], s[2]
            op = BIGOP[sel]
            if sel in (15, 21):
                op = {1: r"\int", 2: r"\iint", 3: r"\iiint"}.get(var & 3, r"\int")
                if var & 4:
                    op = r"\oint"
            return "tmpl", op + ("_{" + lower + "}" if lower else "") + ("^{" + upper + "}" if upper else "") + " " + main
        if sel == 23:                                    # 極限：lim 底下寫 n→∞
            main, lower, upper = s[0], s[1], s[2]
            acc = {r"\xrightarrow{}": r"\overrightarrow", r"\xleftarrow{}": r"\overrightarrow",
                   r"\xleftrightarrow{}": r"\overleftrightarrow"}.get(upper.strip())
            if acc and main and not lower:               # 上限放一個空箭頭：向量、直線符號（北一女數A，裁示 #018）
                return "tmpl", acc + "{" + main + "}"
            if main.replace(" ", "") in ("lim", r"\mathrm{lim}", r"\lim", r"\operatorname{lim}"):
                return "tmpl", r"\lim_{" + lower + "}" + ("^{" + upper + "}" if upper else "") + " "
            out = main
            if upper:
                out = r"\overset{" + upper + "}{" + out + "}"
            if lower:
                out = r"\underset{" + lower + "}{" + out + "}"
            return "tmpl", out
        if sel in (24, 25):                              # 橫的大括號、中括號（上或下）
            cmd = (r"\overbrace" if sel == 24 else r"\overbracket") if var & 1 else (r"\underbrace" if sel == 24 else r"\underbracket")
            return "tmpl", cmd + "{" + s[0] + "}" + (("^" if var & 1 else "_") + "{" + s[1] + "}" if s[1] else "")
        if sel in (27, 28, 29):                          # 上下標：格子順序是下標、上標
            sub, sup = (slots + ["", ""])[:2]
            out = ("_{" + sub + "}" if sub else "") + ("^{" + sup + "}" if sup else "")
            return "script", out
        if sel == 31:                                    # 向量箭頭
            cmd = r"\overleftrightarrow" if var & 3 == 3 else (r"\overleftarrow" if var & 1 else r"\overrightarrow")
            if var & 4:
                cmd = cmd.replace("over", "under")
            return "tmpl", cmd + "{" + s[0] + "}"
        if sel == 32:
            return "tmpl", r"\widetilde{" + s[0] + "}"
        if sel == 33:
            return "tmpl", r"\widehat{" + s[0] + "}"
        if sel == 34:
            return "tmpl", r"\overparen{" + s[0] + "}"
        if sel == 36:
            return "tmpl", r"\cancel{" + s[0] + "}"
        if sel == 37:
            return "tmpl", r"\boxed{" + s[0] + "}"
        raise ValueError(f"不支援的模板 {sel}")


def join5(items: list[tuple[str, str]]) -> str:
    """mtef3.join，再處理連續上下標：前一段已經有同一種上下標時把它包起來（a_{n}_{+1} → {a_{n}}_{+1}）。"""
    bad = [t for k, t in items if k == "pua"]
    if bad:
        raise ValueError(f"不認得的私用區字元 {bad[0]}")
    fixed: list[tuple[str, str]] = []
    for kind, tex in items:
        if kind == "script" and fixed:
            pk, pt = fixed[-1]
            same = (tex.startswith("_") and "_{" in pt[-40:] and pt.rstrip().endswith("}") and pk == "script") or \
                   (tex.startswith("^") and "^{" in pt[-40:] and pt.rstrip().endswith("}") and pk == "script")
            if same:
                base = []
                while fixed and fixed[-1][0] in ("script", "char", "func") and not fixed[-1][1].strip() in ("+", "-", "=", ","):
                    base.insert(0, fixed.pop())
                    if base[0][0] != "script":
                        break
                fixed.append(("char", "{" + join(base) + "}"))
        fixed.append((kind, tex))
    return join(fixed)


def header_len(m: bytes) -> int:
    """MTEF 第 5 版標頭：版本、平台、產品、版本、子版本、應用程式名稱（\\0 結尾）、公式選項。"""
    i = m.index(b"\0", 5)
    return i + 2


def to_latex(ole: bytes) -> str:
    m = mtef_bytes(ole)
    if not m or m[0] != 5:
        raise ValueError(f"不是 MTEF 第 5 版（{m[:1].hex()}）")
    p = Parser(m[header_len(m):])
    return join5(p.objects())


def main() -> int:
    for f in sys.argv[1:]:
        try:
            print(f, to_latex(open(f, "rb").read()))
        except Exception as e:                          # noqa: BLE001
            print(f, "✗", e)
    return 0


if __name__ == "__main__":
    sys.exit(main())
