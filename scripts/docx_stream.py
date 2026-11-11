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
import sys
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

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


def eq_field(m: re.Match) -> str:
    try:
        return "$" + eq_latex(m.group(1).strip()).replace("\u3000", " ").strip() + "$"
    except (EqError, RecursionError):
        return m.group(0)


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
            yield (node.text or "").replace("​", ""), under, script
        elif tag == W + "tab":
            yield "　", False, ""
        elif tag in (W + "br", W + "cr"):
            yield ("\n" if node.get(W + "type") == "page" else " "), False, ""
        elif tag in (W + "drawing", W + "pict", W + "object") or tag.endswith("}AlternateContent"):
            for blip in node.iter():
                rid = blip.get(R + "embed") or blip.get(R + "id")
                if rid and rid in rel and "media/" in rel[rid]:
                    yield ("\x00IMG:" + rel[rid] + "\x00"), False, ""
                    break


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
    s = LIT.sub(eq_field, s)
    return LABEL.sub(r"(\1)", s)


def table(tbl, rel) -> str:
    rows = []
    for tr in tbl.findall(W + "tr"):
        cells = []
        for tc in tr.findall(W + "tc"):
            cells.append(" ".join(paragraph(p, rel) for p in tc.findall(W + "p")).replace("|", "｜").strip())
        rows.append(cells)
    if not rows:
        return ""
    n = max(len(r) for r in rows)
    rows = [r + [""] * (n - len(r)) for r in rows]
    lines = ["| " + " | ".join(rows[0]) + " |", "|" + " --- |" * n]
    lines += ["| " + " | ".join(r) + " |" for r in rows[1:]]
    return "\n".join(lines)


def stream(path: Path):
    with zipfile.ZipFile(path) as z:
        rel = rels(z)
        body = ET.fromstring(z.read("word/document.xml")).find(W + "body")
    i = 0
    for el in body:
        if el.tag == W + "p":
            kind, text = "p", paragraph(el, rel)
        elif el.tag == W + "tbl":
            kind, text = "table", table(el, rel)
        else:
            continue
        images = re.findall(r"\x00IMG:([^\x00]+)\x00", text)
        n = iter(range(1, len(images) + 1))
        text = re.sub(r"\x00IMG:[^\x00]+\x00", lambda _: f"[圖{next(n)}]", text)
        if text.strip() or images:
            yield {"i": i, "kind": kind, "text": text, "images": images}
        i += 1


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__)
        return 2
    for row in stream(Path(sys.argv[1])):
        print(json.dumps(row, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
