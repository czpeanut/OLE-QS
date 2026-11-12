#!/usr/bin/env python3
"""讀 flatten_doc_eq.py → LibreOffice 轉出的 .docx，輸出解析器要用的段落流（JSON Lines，一段一行）。

    python scripts/flatten_doc_eq.py 原卷.doc work/卷.doc
    soffice --headless --convert-to docx --outdir work work/卷.doc
    soffice --headless --convert-to pdf  --outdir work work/卷.doc      # 原卷 PDF 也用這份（有題號與代號）
    python scripts/docx_stream.py work/卷.docx > work/卷.jsonl

每段一個 JSON：{"i": 段落序號, "kind": "p" 或 "table", "text": …, "images": [word/media/… 檔名]}
- 已套用規則 A：EQ 代號（甲）（A）（二）→ (甲) (A) (二)；題號 EQ → 「12.」。
- 已套用規則 B：底線且非空白的字 → <u>…</u>（相鄰的合併）；空白底線（作答格）→ ＿＿＿。
- 已套用規則 E：段落內的軟換行（w:br textWrapping）→ 空白，不換行。
- 上標、下標 → ^{…}、_{…}；圖片（含方程式預覽圖）→ 文中位置寫 [圖n]，n 對應 images 的順序。
- 其他 EQ 欄位（flatten_doc_eq.py 留下的 ⟦EQ …⟧）：\\f、\\r、\\s\\up／\\do、\\b、\\x、\\a 轉成 $LaTeX$；
  看不懂的照原樣留著，檢查程式會以「Word欄位碼」攔下，請補進本程式，不要手改 YAML。
- 表格整個輸出成一段 Markdown 表格（kind = "table"），儲存格內的換行改成空白。
切題、選項、題組、答案由各自的解析器處理；本程式只負責把 Word 的格式忠實轉成文字。只用標準函式庫。
"""
from __future__ import annotations

import json
import re
import struct
import sys
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

sys.path.insert(0, str(Path(__file__).resolve().parent))

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
R = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
LABEL = re.compile(r"（([A-E甲乙丙丁戊己庚辛壬癸子丑寅卯辰巳午未申酉戌亥一二三四五六七八九十ㄅ-ㄩ]|[0-9]{1,2}|十[一二三四五六七八九])）")


LIT = re.compile(r"⟦\s*eq\s*(.*?)⟧", re.I | re.S)


EQ_CMD = re.compile(r"\\([a-zA-Z])((?:\\(?:lc|rc|bc)\\?.|\\[a-zA-Z]+\s*-?\d*)*)\s*")   # \\s\\up8( … ) 的 \\up8、\\b\\lc\\{ 的 \\lc\\{ 是選項


class EqError(ValueError):
    pass


def eq_latex(src: str) -> str:
    """Word EQ 指令轉 LaTeX：\\f 分數、\\r 根號、\\s\\up／\\do 上下標、\\b 括號、\\x 方框、\\a 陣列、\\o 疊字。
    看不懂的指令丟 EqError，呼叫端保留原文，交給檢查程式攔下（Word欄位碼）。"""
    pos = 0

    def args() -> list[str]:
        nonlocal pos
        if pos >= len(src) or src[pos] != "(":
            raise EqError(src)
        depth, cur, out = 0, "", []
        start = pos + 1
        i = start
        while i < len(src):
            ch = src[i]
            if ch == "\\" and i + 1 < len(src) and src[i + 1] in "(),\\":
                cur += src[i:i + 2]; i += 2; continue
            if ch == "(":
                depth += 1
            elif ch == ")":
                if depth == 0:
                    out.append(cur); pos = i + 1
                    return [eq_latex(a) for a in out]
                depth -= 1
            elif ch == "," and depth == 0:
                out.append(cur); cur = ""; i += 1; continue
            cur += ch
            i += 1
        raise EqError(src)

    out = ""
    while pos < len(src):
        ch = src[pos]
        if ch != "\\":
            out += ch; pos += 1; continue
        m = EQ_CMD.match(src, pos)
        if not m:
            out += src[pos + 1:pos + 2]; pos += 2; continue           # \, \( 這類跳脫字元
        cmd, opts = m.group(1).lower(), m.group(2)
        pos = m.end()
        a = args()
        if cmd == "f" and len(a) == 2:
            out += rf"\frac{{{a[0]}}}{{{a[1]}}}"
        elif cmd == "r" and len(a) in (1, 2):
            out += rf"\sqrt{{{a[-1]}}}" if len(a) == 1 or not a[0].strip() else rf"\sqrt[{a[0]}]{{{a[1]}}}"
        elif cmd == "s":
            body = "".join(a)
            out += f"^{{{body}}}" if "\\up" in opts.lower() else f"_{{{body}}}" if "\\do" in opts.lower() else body
        elif cmd == "b":
            lc = re.search(r"\\lc\\?(.)", opts); rc = re.search(r"\\rc\\?(.)", opts)
            bc = re.search(r"\\bc\\?(.)", opts)
            l = (lc or bc).group(1) if (lc or bc) else "("
            r = rc.group(1) if rc else {"(": ")", "[": "]", "{": "}", "|": "|"}.get(l, ")") if (bc or not lc) else "."
            esc = lambda c: "\\" + c if c in "{}" else c
            out += rf"\left{esc(l)}{''.join(a)}\right{esc(r)}"
        elif cmd == "x":
            out += rf"\boxed{{{''.join(a)}}}"
        elif cmd == "a":
            out += r"\begin{matrix}" + r" \\ ".join(a) + r"\end{matrix}"
        elif cmd == "o":
            nb = [x for x in a if x.strip("\u3000 ")]
            out += nb[-1] if nb else ""
        else:
            raise EqError(src)
    return out


INNER = re.compile(r"⟦\s*eq\s*([^⟦⟧]*)⟧", re.I)


def eq_fields(s: str) -> str:
    """⟦EQ …⟧ 由內而外轉 LaTeX（根號裡包分數這種巢狀欄位），最外層包 $…$；轉不了的整段照原樣留著。
    ⟦VEC f⟧（字母疊箭頭圖）→ $\\vec{f}$。"""
    s = re.sub(r"⟦VEC ([A-Za-z]{1,2})⟧", r"$\\vec{\1}$", s)
    store: list[str] = []
    for _ in range(5):
        def conv(m: re.Match) -> str:
            body = re.sub(r"\x02(\d+)\x03", lambda k: "\x04" + k.group(1) + "\x05", m.group(1).strip())
            try:
                tex = eq_latex(body).replace("\u3000", " ").strip()
            except (EqError, RecursionError):
                raise EqError(m.group(0))
            store.append(tex)
            return f"\x02{len(store) - 1}\x03"
        try:
            new = INNER.sub(conv, s)
        except EqError:
            return s
        if new == s:
            break
        s = new

    def restore(t: str) -> str:
        for _ in range(6):
            t = re.sub(r"\x04(\d+)\x05", lambda k: store[int(k.group(1))], t)
        return t

    store[:] = [restore(t) for t in store]
    return re.sub(r"\x02(\d+)\x03", lambda k: "$" + store[int(k.group(1))] + "$", s)


def rels(z: zipfile.ZipFile) -> dict[str, str]:
    x = ET.fromstring(z.read("word/_rels/document.xml.rels"))
    return {r.get("Id"): "word/" + r.get("Target").lstrip("/").removeprefix("word/") for r in x}


def run_parts(run, rel):
    """一個 w:r 拆成 (文字, 格式) 片段；格式 = (底線, 上下標)。"""
    pr = run.find(W + "rPr")
    u = pr.find(W + "u") if pr is not None else None
    under = u is not None and u.get(W + "val", "single") not in ("none", "0", "false")
    va = pr.find(W + "vertAlign") if pr is not None else None
    script = va.get(W + "val") if va is not None else ""
    if pr is not None and pr.find(W + "vanish") is not None:
        return
    for node in run:
        tag = node.tag
        if tag == W + "t":
            t = (node.text or "").replace("\u200b", "")
            if any("\uf000" <= ch <= "\uf0ff" for ch in t):           # 符號字型打的私用區字元
                font = ""
                if pr is not None and pr.find(W + "rFonts") is not None:
                    f = pr.find(W + "rFonts")
                    font = f.get(W + "ascii") or f.get(W + "hAnsi") or ""
                t = "".join(sym_char(font, ord(ch)) if "\uf000" <= ch <= "\uf0ff" else ch for ch in t)
            yield t, under, script
        elif tag == W + "sym":                                  # 符號字型（Symbol、Wingdings）的字
            yield sym_char(node.get(W + "font") or "", int(node.get(W + "char") or "0", 16)), under, script
        elif tag == W + "tab":
            yield "　", False, ""
        elif tag in (W + "br", W + "cr"):
            yield ("\n" if node.get(W + "type") == "page" else " "), False, ""
        elif tag == W + "object":
            ole = next((e.get(R + "id") for e in node.iter() if e.tag.endswith("}OLEObject")), None)
            prev = next((e.get(R + "id") for e in node.iter() if e.tag.endswith("}imagedata")), None)
            if ole in rel:
                yield f"\x00OLE:{rel[ole]}\x01{rel.get(prev, '')}\x01{size(node)}\x00", False, ""
            elif prev in rel:
                yield f"\x00IMG:{rel[prev]}\x01{size(node)}\x00", False, ""
        elif tag in (W + "drawing", W + "pict") or tag.endswith("}AlternateContent"):
            media = next((rel[r] for e in node.iter() for r in (e.get(R + "embed"), e.get(R + "id"))
                          if r in rel and "media/" in rel[r]), "")
            boxed = textbox_content(node, rel)
            if boxed:
                yield "\n" + boxed + "\n", False, ""          # 文字方塊裡的表格或整段文字：照文字輸出
                continue
            yield f"\x00IMG:{media or 'shape'}\x01{size(node)}\x00", False, ""     # 沒有圖檔的是向量圖形（線條、文字方塊組成的圖）


SYMBOL_FONT = {0x41 + i: c for i, c in enumerate("ΑΒΧΔΕΦΓΗΙϑΚΛΜΝΟΠΘΡΣΤΥςΩΞΨΖ")}
SYMBOL_FONT.update({0x61 + i: c for i, c in enumerate("αβχδεφγηιϕκλμνοπθρστυϖωξψζ")})
SYMBOL_FONT.update({0xB4: "×", 0xB1: "±", 0xA3: "≤", 0xB3: "≥", 0xB9: "≠", 0xBB: "≈", 0xAE: "→", 0xAC: "←",
                    0xAB: "↔", 0xB0: "°", 0xD0: "∠", 0x5E: "⊥", 0x2D: "−", 0xB7: "•", 0xD7: "⋅", 0xA5: "∞",
                    0xB6: "∂", 0xD6: "√", 0xC6: "∅", 0xCE: "∈", 0xC7: "∩", 0xC8: "∪", 0xBA: "≡", 0x40: "≅",
                    0x7E: "∼", 0xA2: "′", 0xB2: "″", 0xDE: "⇒", 0xDB: "⇔", 0xBE: "—", 0x5C: "∴", 0xA4: "/"})


def sym_char(font: str, code: int) -> str:
    """Word 的 w:sym 與私用區字元（U+F0xx）→ Unicode。認不出的字型寫 ⟦符號⟧，交給品管擋下。"""
    c = code - 0xF000 if code >= 0xF000 else code
    if "symbol" in font.lower():
        return SYMBOL_FONT.get(c, chr(c) if 0x20 <= c < 0x7F else "⟦符號⟧")
    if not font or "wingdings" not in font.lower() and "webdings" not in font.lower():
        return chr(c) if 0x20 <= c < 0x7F else "⟦符號⟧"
    return {0x6C: "●", 0x6E: "■", 0xA8: "□", 0xFC: "✓", 0xFB: "✗", 0x81: "①", 0x82: "②", 0x83: "③",
            0x84: "④", 0x85: "⑤", 0x8C: "①", 0x8D: "②", 0x8E: "③", 0x8F: "④", 0x90: "⑤"}.get(c, "⟦符號⟧")


def textbox_content(node, rel) -> str:
    """圖形裡的文字方塊：有表格就回傳 Markdown 表格；是一整段文字（中文 20 字以上，可以夾著圖）就照順序回傳文字與圖；
    只有短標註（圖上的甲、乙、A、B）回傳空字串，整個圖形當成圖。AlternateContent 只看第一個版本。"""
    boxes = [e for e in node.iter() if e.tag == W + "txbxContent"]
    if not boxes:
        return ""
    seen, parts = set(), []
    for box in boxes[:1] if node.tag.endswith("}AlternateContent") else boxes:
        for el in box:
            if el.tag == W + "tbl":
                parts.append(table(el, rel))
            elif el.tag == W + "p":
                t = paragraph(el, rel)
                if t.strip() and t not in seen:
                    seen.add(t)
                    parts.append(t)
    text = "\n".join(p for p in parts if p.strip())
    if any(p.lstrip().startswith("|") for p in parts):
        return text
    return text if len(re.findall(r"[\u4e00-\u9fff]", re.sub(r"\x00[^\x00]*\x00", "", text))) >= 20 else ""


def metafile_text(b: bytes) -> list[str]:
    """EMF／WMF 裡的文字紀錄，依繪製順序。選項畫在圖裡時，用來讀圖中的 (A)(B)(C) 代號（裁示 #015）。"""
    out = []
    if b[:4] == b"\x01\x00\x00\x00":                         # EMF
        i = 0
        while i + 8 <= len(b):
            typ, size = struct.unpack_from("<II", b, i)
            if size < 8:
                break
            if typ in (83, 84) and size >= 76:               # EMR_EXTTEXTOUTA / W
                nchars, offstr = struct.unpack_from("<II", b, i + 36 + 8)
                raw = b[i + offstr: i + offstr + nchars * (2 if typ == 84 else 1)]
                out.append(raw.decode("utf-16-le" if typ == 84 else "cp950", "ignore"))
            i += size
        return out
    i = 22 if b[:4] == b"\xd7\xcd\xc6\x9a" else 0           # 可置放 WMF 檔頭
    if len(b) < i + 18:
        return out
    hsize = struct.unpack_from("<H", b, i + 2)[0] * 2
    i += hsize
    while i + 6 <= len(b):
        size, fn = struct.unpack_from("<IH", b, i)
        if size < 3:
            break
        if fn == 0x0A32:                                     # META_EXTTEXTOUT
            y, x, n, opt = struct.unpack_from("<hhhH", b, i + 6)
            p = i + 14 + (8 if opt & 0x6 else 0)
            out.append(b[p:p + n].decode("cp950", "ignore"))
        elif fn == 0x0521:                                   # META_TEXTOUT
            n = struct.unpack_from("<h", b, i + 6)[0]
            out.append(b[i + 8:i + 8 + n].decode("cp950", "ignore"))
        i += size * 2
    return out


def size(node) -> str:
    """圖在版面上的寬高（point），附圖對位用。"""
    for e in node.iter():
        if e.tag.endswith("}extent") and e.get("cx"):
            return f"{int(e.get('cx')) / 12700:.1f}x{int(e.get('cy')) / 12700:.1f}"
        st = e.get("style") or ""
        w = re.search(r"width:([\d.]+)pt", st)
        h = re.search(r"height:([\d.]+)pt", st)
        if w and h:
            return f"{float(w.group(1)):.1f}x{float(h.group(1)):.1f}"
    return ""


def runs(el):
    """段落裡的 w:r（含超連結、修訂內的），但不進文字方塊：圖上的標註不是題目文字。"""
    for c in el:
        if c.tag == W + "r":
            yield c
        elif c.tag not in (W + "txbxContent", W + "pPr"):
            yield from runs(c)


def paragraph(p, rel) -> str:
    out, buf, state = [], "", (False, "")

    def flush():
        nonlocal buf
        if not buf:
            return
        under, script = state
        s = buf
        if script == "superscript":
            s = "^{" + s + "}"
        elif script == "subscript":
            s = "_{" + s + "}"
        if under:
            s = "<u>" + s + "</u>" if s.strip("　 ") else "＿" * max(3, len(s))
        out.append(s)
        buf = ""

    for r in runs(p):
        for text, under, script in run_parts(r, rel):
            fmt = (under, script if script in ("superscript", "subscript") else "")
            if fmt != state:
                flush()
                state = fmt
            buf += text
    flush()
    s = "".join(out).replace("</u><u>", "")
    s = eq_fields(s)
    return LABEL.sub(r"(\1)", s)


def table(tbl, rel) -> str:
    rows = []
    for tr in tbl.findall(W + "tr"):
        cells = []
        gb = tr.find(f"{W}trPr/{W}gridBefore")
        cells += [""] * int(gb.get(W + "val") or 0) if gb is not None else []
        for tc in tr.findall(W + "tc"):
            cells.append(" ".join(paragraph(p, rel) for p in tc.findall(W + "p")).replace("|", "｜").strip())
            span = tc.find(f"{W}tcPr/{W}gridSpan")
            # 橫向合併的格子（表頭「出發資訊」跨 3 欄）：後面補空格，欄位才對得齊（師大附中地理第 2 題，裁示 #018）
            cells += [""] * (int(span.get(W + "val") or 1) - 1) if span is not None else []
        rows.append(cells)
    if not rows:
        return ""
    n = max(len(r) for r in rows)
    rows = [r + [""] * (n - len(r)) for r in rows]
    lines = ["| " + " | ".join(rows[0]) + " |", "|" + " --- |" * n]
    lines += ["| " + " | ".join(r) + " |" for r in rows[1:]]
    return "\n".join(lines)


def unwrap(body):
    """body 的段落與表格；格子裡又放表格的外框表格（排版用）拆開，裡面的段落、表格依序輸出（台中二中公民答案公佈表）。"""
    for el in body:
        cells = el.findall(f"{W}tr/{W}tc") if el.tag == W + "tbl" else []
        boxed_tbl = el.tag == W + "tbl" and any(c.find(f".//{W}txbxContent/{W}tbl") is not None for c in cells)
        if el.tag == W + "tbl" and (el.find(f"{W}tr/{W}tc/{W}tbl") is not None or len(cells) == 1 or boxed_tbl):
            # 格子裡的段落夾著「文字方塊裡的表格」：LibreOffice 7.4 把兩欄文章排成這種外框（台中二中公民第 30～31 題，裁示 #018）
            # 格子裡又放表格的外框表格，或只有一格的表格（加框的文章，嘉義高中公民題組）：拆開
            for tr in el.findall(W + "tr"):
                for tc in tr.findall(W + "tc"):
                    yield from unwrap(tc)
        elif el.tag in (W + "p", W + "tbl"):
            yield el


def numbered_styles(z: zipfile.ZipFile) -> set[str]:
    """段落樣式本身帶 Word 自動編號的 styleId（含 basedOn 繼承）。"""
    if "word/styles.xml" not in z.namelist():
        return set()
    root = ET.fromstring(z.read("word/styles.xml"))
    base, own = {}, set()
    for st in root.iter(W + "style"):
        sid = st.get(W + "styleId")
        b = st.find(W + "basedOn")
        base[sid] = b.get(W + "val") if b is not None else None
        n = st.find(f"{W}pPr/{W}numPr/{W}numId")
        if n is not None and n.get(W + "val") not in (None, "0"):
            own.add(sid)
    out = set()
    for sid in base:
        cur, seen = sid, set()
        while cur and cur not in seen:
            if cur in own:
                out.add(sid)
                break
            seen.add(cur)
            cur = base.get(cur)
    return out


def is_numbered(p, styles: set[str]) -> bool:
    n = p.find(f"{W}pPr/{W}numPr/{W}numId")
    if n is not None:
        return n.get(W + "val") not in (None, "0")
    st = p.find(f"{W}pPr/{W}pStyle")
    return st is not None and st.get(W + "val") in styles


_KEEP = re.compile(r"[\u4e00-\u9fffA-Za-z0-9]")


def _filt(s: str) -> str:
    s = re.sub(r"\x00[^\x00]*\x00|\[圖\d+\]|</?u>", "", s)
    return "".join(_KEEP.findall(s))


class ListLabels:
    """Word 自動編號的題號／代號：到 LibreOffice 純文字輸出（同一份文件）依序找同一段，取段落前面的編號。
    編號完全照 LibreOffice 排版（與原卷 PDF 一致），不自己推算 Word 的編號規則。"""

    def __init__(self, txt: Path | None):
        self.lines = []
        if txt and Path(txt).is_file():
            self.lines = Path(txt).read_text(encoding="utf-8-sig", errors="replace").splitlines()
        self.j = 0

    def label(self, text: str) -> str:
        key = _filt(text)[:6]
        if len(key) < 2 or not self.lines:
            return ""
        for k in range(self.j, min(len(self.lines), self.j + 400)):
            raw = self.lines[k]
            f = _filt(raw)
            idx = f.find(key)
            if idx < 0:
                continue
            self.j = k + 1
            if idx == 0 or idx > 10:
                return ""
            n = 0                                        # 第 idx 個保留字元在原行的位置
            for pos, ch in enumerate(raw):
                if _KEEP.match(ch):
                    if n == idx:
                        return raw[:pos].strip()
                    n += 1
            return ""
        return ""


def stream(path: Path, math: bool = True, txt: Path | None = None):
    """段落流。math=True 時把 MathType 物件轉成 $LaTeX$（需要 ole_math.py 的工具），轉不出來的寫 ⟦公式無法轉換⟧。"""
    with zipfile.ZipFile(path) as z:
        rel = rels(z)
        body = ET.fromstring(z.read("word/document.xml")).find(W + "body")
        styles = numbered_styles(z)
        labels = ListLabels(txt if txt is not None else Path(path).with_suffix(".txt"))
        rows, i = [], 0
        for el in unwrap(body):
            if el.tag == W + "p":
                kind, text = "p", paragraph(el, rel)
                if is_numbered(el, styles):
                    lab = labels.label(text)
                    if lab:
                        text = lab + " " + text
            elif el.tag == W + "tbl":
                kind, text = "table", table(el, rel)
            else:
                continue
            if kind == "p" and re.search(r"(?m)^\|", text):
                # 段落裡夾著文字方塊表格（浮動表格）：拆成「段落、表格、段落」，表格自成一段（LibreOffice 7.4 的答案表，裁示 #018）
                for seg in re.split(r"((?:^\|.*(?:\n|$))+)", text, flags=re.M):
                    if seg.strip():
                        tbl = seg.lstrip().startswith("|")
                        rows.append({"i": i, "kind": "table" if tbl else "p", "text": seg.strip("\n") if tbl else seg.strip()})
                        i += 1
                continue
            rows.append({"i": i, "kind": kind, "text": text})
            i += 1
        oles = sorted({m for r in rows for m in re.findall(r"\x00OLE:([^\x01\x00]+)\x01", r["text"])})
        latex = {}
        if math and oles:
            import ole_math
            latex = ole_math.convert({o: z.read(o) for o in oles if o in z.namelist()})

    def ole(m: re.Match) -> str:
        target, preview, sz = m.group(1), m.group(2), m.group(3)
        tex = latex.get(target)
        if tex:
            return f"${tex}$"
        if not math:
            return "⟦公式⟧"
        data_is_eq = target in latex                     # 是方程式但轉不出來
        return "⟦公式無法轉換⟧" if data_is_eq else f"\x00IMG:{preview or 'shape'}\x01{sz}\x00"

    for r in rows:
        text = re.sub(r"\x00OLE:([^\x01\x00]+)\x01([^\x01\x00]*)\x01([^\x00]*)\x00", ole, r["text"])
        images = []

        def img(m: re.Match) -> str:
            media, _, sz = m.group(1).partition("\x01")
            images.append({"media": media, "size": sz})
            return f"[圖{len(images)}]"

        text = re.sub(r"\x00IMG:([^\x00]*)\x00", img, text)
        text = re.sub(r"\$\s*\$", "", text)
        if text.strip() or images:
            yield {**r, "text": text, "images": images}


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__)
        return 2
    for row in stream(Path(sys.argv[1])):
        print(json.dumps(row, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
