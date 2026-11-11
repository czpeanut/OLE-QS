#!/usr/bin/env python3
"""把 Word 97 .doc 裡的 EQ 欄位就地換成「它顯示出來的文字」，再交給 LibreOffice 轉 .docx／PDF。

為什麼需要：LibreOffice 匯入 .doc 時會把 EQ 欄位（翰林、南一卷的題號、選項代號、加括號代號、
「第 22.、23. 題為題組」）換成空白的 FILLIN 欄位，轉出的 .docx 與 PDF 就看不到這些字；
而底線、表格、圖片又只有 .docx 讀得到。先用本程式改寫 .doc，轉出的 .docx 就同時保有全部資訊，
解析器只要讀 .docx 一個來源。

    python scripts/flatten_doc_eq.py 原卷.doc 輸出.doc            # 單檔
    python scripts/flatten_doc_eq.py --dir 原卷資料夾 輸出資料夾    # 整批
    soffice --headless --convert-to docx --outdir 輸出資料夾 輸出.doc

改寫方式：在檔案位元組中找到 UTF-16 的「\\x13 … \\x15」欄位，換成同樣長度的文字，不足的長度補零寬空白
U+200B（docx_stream.py 會刪掉）。長度不變，所以 Word 的段落、字元格式（含底線）、圖片位置全部不動。
再把 .doc 的主文與文字方塊欄位表長度設成 0，讓 LibreOffice 照字面讀。
規則：
  eq \\o\\al(　,12.)              → 12.          題號
  EQ \\o(　,\\l((　)),甲)          → （甲）       加括號代號（含選項（A）～（E）），全形括號，docx_stream.py 再轉半形
  EQ \\o(　…,第22.、23.題為題組)   → 第22.、23.題為題組（只有一個非空白參數的 \\o，輸出那個參數）
  EQ \\o\\ac(○,3)                → ③           圈字（●→❸）
  其他 EQ（\\f 分數、\\r 根號、\\s 上下標…） → ⟦EQ \\f(1,2)⟧ 原指令照字面保留，由 docx_stream.py 轉 LaTeX
  其他欄位（EMBED 方程式、HYPERLINK…）  → 只留欄位結果（方程式會變成文中的預覽圖）
原檔不會被修改。只用標準函式庫。
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ZW = "\u200b"
PAREN = re.compile(r"\\o\s*\([^,]*,\s*\\l\(\([^)]*\)\)\s*,\s*[\u3000 ]*([^)]*?)[\u3000 ]*\)\s*$", re.I)
ARGS = re.compile(r"\\o(?:\\a[lrc])?\s*\((.*)\)\s*$", re.I | re.S)


def circled(mark: str, n: str) -> str | None:
    if not n.isdigit() or not 1 <= int(n) <= 20:
        return None
    if mark in "○◯〇":
        return chr(0x2460 + int(n) - 1)
    if mark in "●" and int(n) <= 10:
        return chr(0x2776 + int(n) - 1)
    return None


def display(instr: str) -> str | None:
    """EQ 欄位要換成的文字；不是 EQ 欄位回傳 None。"""
    if not re.match(r"\s*eq\s*\\", instr, re.I):
        return None
    m = PAREN.search(instr)
    if m:
        return f"（{m.group(1)}）"                   # 全形括號：半形括號會被 LibreOffice 當成 EQ 語法
    m = ARGS.search(instr)
    if m and not re.search(r"\\[a-z]", m.group(1).replace("\\l((", ""), re.I):
        args = [a.replace(ZW, "").strip("\u3000 ") for a in m.group(1).split(",")]
        args = [a for a in args if a]
        if len(args) <= 1:
            return args[0] if args else "\u3000"
        if len(args) == 2:
            c = circled(args[0], args[1]) or circled(args[1], args[0])
            if c:
                return c
    return "⟦" + instr.replace(ZW, "") + "⟧"     # 其他 EQ：原指令照字面留給 docx_stream.py


FIB_FLD = (16, 57)       # FibRgFcLcb97 的 fcPlcfFldMom（主文欄位表）、fcPlcfFldTxbx（文字方塊欄位表）


def fib_offsets(buf: bytes) -> list[int]:
    """WordDocument 資料流開頭的 FIB（wIdent 0xA5EC，在磁區邊界上），回傳欄位表 lcb 的位元組位置。"""
    for off in range(0, len(buf), 512):
        if buf[off:off + 2] == b"\xec\xa5" and buf[off + 32:off + 34] == b"\x0e\x00":
            csw = int.from_bytes(buf[off + 32:off + 34], "little")
            p = off + 34 + csw * 2
            cslw = int.from_bytes(buf[p:p + 2], "little")
            base = p + 2 + cslw * 4 + 2
            return [base + i * 8 + 4 for i in FIB_FLD]
    raise ValueError("找不到 Word 97 FIB，可能不是 .doc 格式")


def blank(field: str) -> str:
    """非 EQ 欄位（EMBED 方程式、超連結…）：欄位標記與指令換成零寬空白，保留結果（\\x14 之後，例如方程式物件 \\x01）。"""
    if "\x14" in field:
        instr, result = field[1:-1].split("\x14", 1)
        return ZW * (len(instr) + 2) + result + ZW
    return ZW * len(field)


def flatten(data: bytes) -> tuple[bytes, int, list[str]]:
    """改寫主文與文字方塊裡的所有欄位，並把這兩個欄位表的長度設成 0，讓 LibreOffice 照字面讀。"""
    buf = bytearray(data)
    n, skipped = 0, []
    field = re.compile(r"\x13([^\x13\x14\x15]{1,200})(?:\x14([^\x13\x14\x15]{0,200}))?\x15")
    for off in (0, 0, 0, 1, 1, 1):                      # 文字流可能從奇數位元組開始；重跑三次處理巢狀欄位
        text = bytes(buf[off:]).decode("utf-16-le", errors="replace")
        for m in field.finditer(text):
            instr = m.group(1)
            if re.match(r"\s*(PAGE|NUMPAGES)\b", instr):    # 頁首頁尾的頁碼欄位用另一張表，不動
                continue
            if not re.match(r"\s*[A-Za-z]{2,}[ \\]", instr) or re.search(r"[\x00-\x1f\ufffd]", instr):
                continue                                # 不是欄位（圖片、表格的二進位資料剛好含這些位元組）
            new = display(instr)
            if new is None:
                rep_s = blank(m.group(0))
            elif new.startswith("⟦"):
                rep_s = "⟦" + m.group(0)[1:-1] + "⟧"              # 長度剛好相同
            elif len(new) <= len(m.group(0)):
                rep_s = ZW * (len(m.group(0)) - len(new)) + new     # 靠右：原本顯示的參數在欄位尾端，沿用那裡的字元格式
            else:
                skipped.append(instr)
                continue
            rep = rep_s.encode("utf-16-le")
            a = off + 2 * m.start()
            if bytes(buf[a:a + len(rep)]) != m.group(0).encode("utf-16-le"):
                skipped.append(instr)                   # 欄位跨越檔案磁區，不安全，略過
                continue
            buf[a:a + len(rep)] = rep
            n += new is not None
    for p in fib_offsets(buf):
        buf[p:p + 4] = b"\0\0\0\0"
    return bytes(buf), n, skipped


def run(src: Path, dst: Path) -> int:
    out, n, skipped = flatten(src.read_bytes())
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_bytes(out)
    print(f"{src.name}: 改寫 EQ 欄位 {n} 個" + (f"，略過 {len(skipped)} 個：{skipped[:3]}" if skipped else ""))
    return len(skipped)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("src", type=Path)
    ap.add_argument("dst", type=Path)
    ap.add_argument("--dir", action="store_true", help="src、dst 都是資料夾")
    a = ap.parse_args()
    if not a.dir:
        return 1 if run(a.src, a.dst) else 0
    bad = 0
    for f in sorted(a.src.rglob("*")):
        if f.suffix.lower() == ".doc":
            bad += run(f, a.dst / f.relative_to(a.src)) > 0
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
