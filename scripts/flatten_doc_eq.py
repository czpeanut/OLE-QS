#!/usr/bin/env python3
"""把 Word 97 .doc 裡的 EQ 欄位就地換成「它顯示出來的文字」，再交給 LibreOffice 轉 .docx／PDF。

為什麼需要：LibreOffice 匯入 .doc 時會把 EQ 欄位（翰林、南一卷的題號、選項代號、加括號代號、
「第 22.、23. 題為題組」）換成空白的 FILLIN 欄位，轉出的 .docx 與 PDF 就看不到這些字；
而底線、表格、圖片又只有 .docx 讀得到。先用本程式改寫 .doc，轉出的 .docx 就同時保有全部資訊，
解析器只要讀 .docx 一個來源。

    python scripts/flatten_doc_eq.py 原卷.doc 輸出.doc            # 單檔
    python scripts/flatten_doc_eq.py --dir 原卷資料夾 輸出資料夾    # 整批
    soffice --headless --convert-to docx --outdir 輸出資料夾 輸出.doc

改寫方式（需要 olefile：python3 -m pip install olefile）：
1. 照片段表（piece table）讀出全文，EQ 欄位「\\x13 EQ … \\x15」換成同樣長度的顯示文字，
   不足的長度補零寬空白 U+200B（docx_stream.py 會刪掉）；
2. 各欄位表（PlcFld）只刪掉改寫過的那些 EQ 欄位的記錄，讓 LibreOffice 把那段當普通文字讀。
長度不變，所以段落、字元格式（含底線）、圖片位置都不動；MathType（EMBED Equation）、頁碼等其他欄位原樣保留。
規則：
  eq \\o\\al(　,12.)              → 12.          題號
  EQ \\o(　,\\l((　)),甲)          → （甲）       加括號代號（含選項（A）～（E）），全形括號，docx_stream.py 再轉半形
  EQ \\o(　…,第22.、23.題為題組)   → 第22.、23.題為題組（只有一個非空白參數的 \\o，輸出那個參數）
  EQ \\o\\ac(○,3)                → ③           圈字（●→❸）
  其他 EQ（\\f 分數、\\r 根號、\\s 上下標…） → ⟦EQ \\f(1,2)⟧ 原指令照字面保留，由 docx_stream.py 轉 LaTeX
  其他欄位（EMBED 方程式、HYPERLINK、PAGE…）→ 不動
原檔不會被修改。
"""
from __future__ import annotations

import argparse
import re
import shutil
import struct
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


# FibRgFcLcb97 裡的欄位表 → 所屬的子文件（欄位表裡的 CP 是相對於子文件開頭）
FIB_FLD = {16: "main", 18: "ftn", 17: "hdd", 19: "atn", 48: "edn", 57: "txbx", 59: "hdrtxbx"}
FLT_EQ = 49                                # 欄位類型 EQ
CLX = 33                                   # FibRgFcLcb97 的 fcClx（片段表）


def fib_base(wd: bytes) -> tuple[int, int]:
    """(FibRgLw97 起點, FibRgFcLcb97 起點)。"""
    if wd[:2] != b"\xec\xa5":
        raise ValueError("不是 Word 97 以後的 .doc（FIB 識別碼不對）")
    csw = int.from_bytes(wd[32:34], "little")
    p = 34 + csw * 2
    cslw = int.from_bytes(wd[p:p + 2], "little")
    return p + 2, p + 2 + cslw * 4 + 2


def subdoc_offsets(wd: bytes) -> dict[str, int]:
    lw, _ = fib_base(wd)
    ccp = [struct.unpack_from("<i", wd, lw + 4 * i)[0] for i in range(11)]
    text, ftn, hdd, atn, edn, txbx = ccp[3], ccp[4], ccp[5], ccp[7], ccp[8], ccp[9]
    return {"main": 0, "ftn": text, "hdd": text + ftn, "atn": text + ftn + hdd, "edn": text + ftn + hdd + atn,
            "txbx": text + ftn + hdd + atn + edn, "hdrtxbx": text + ftn + hdd + atn + edn + txbx}


def read_text(wd: bytes, ts: bytes) -> tuple[list[str], list[tuple[int, int]]]:
    """照片段表（piece table）讀出全文：每個字元的 (WordDocument 資料流位元組位置, 寬度 1 或 2)。
    Word 的文字在檔案裡不一定連續（快速存檔、編輯過的檔），只有照片段表讀才不會把欄位切斷。"""
    _, rg = fib_base(wd)
    fc, lcb = struct.unpack_from("<II", wd, rg + CLX * 8)
    clx = ts[fc:fc + lcb]
    k = 0
    while k < len(clx) and clx[k] == 0x01:                 # Prc（格式資料），跳過
        k += 3 + struct.unpack_from("<H", clx, k + 1)[0]
    if k >= len(clx) or clx[k] != 0x02:
        raise ValueError("讀不到片段表")
    size = struct.unpack_from("<I", clx, k + 1)[0]
    plc = clx[k + 5:k + 5 + size]
    n = (size - 4) // 12
    cps = struct.unpack_from(f"<{n + 1}i", plc, 0)
    chars, where = [], []
    for i in range(n):
        raw = struct.unpack_from("<I", plc, 4 * (n + 1) + 8 * i + 2)[0]
        compressed = bool(raw & 0x40000000)
        fcp = raw & 0x3FFFFFFF
        for j in range(cps[i + 1] - cps[i]):
            if compressed:
                at = fcp // 2 + j
                chars.append(bytes(wd[at:at + 1]).decode("cp1252", errors="replace"))
                where.append((at, 1))
            else:
                at = fcp + 2 * j
                chars.append(bytes(wd[at:at + 2]).decode("utf-16-le", errors="replace"))
                where.append((at, 2))
    return chars, where


def encode(ch: str, width: int) -> bytes | None:
    if width == 2:
        return ch.encode("utf-16-le")
    try:
        b = ch.encode("cp1252")
        return b if len(b) == 1 else None
    except UnicodeEncodeError:
        return None


def rewrite_text(wd: bytearray, chars: list[str], where: list[tuple[int, int]]) -> tuple[int, set[int], list[str]]:
    """EQ 欄位（\\x13 EQ … \\x15）換成顯示文字，長度不變（不足補零寬空白；單位元組片段補空白）。
    回傳 (改寫個數, 改寫過的欄位標記 CP, 略過的欄位)。"""
    text = "".join(chars)
    field = re.compile(r"\x13([^\x13\x14\x15]{1,300})\x15")
    done, cps, skipped = 0, set(), []
    for _ in range(4):                                      # 巢狀欄位：由內而外，最多四層
        changed = False
        for m in list(field.finditer(text)):
            instr = m.group(1)
            vec = re.fullmatch(r"\s*eq\s*\\o\s*\(\s*([A-Za-z]{1,2})\s*,\s*\x01\s*\)\s*", instr, re.I)
            if vec:                                         # 字母疊一張小箭頭圖：物理卷的向量
                new = "⟦VEC " + vec.group(1) + "⟧"
            elif re.search(r"[\x00-\x1f\ufffd]", instr):
                continue
            else:
                new = display(instr)
            if new is None:
                continue                                    # 不是 EQ（EMBED、HYPERLINK…）：保留給 LibreOffice
            L = m.end() - m.start()
            narrow = any(where[c][1] == 1 for c in range(m.start(), m.end()))
            pad = " " if narrow else ZW
            if new.startswith("⟦VEC"):
                rep = pad * (L - len(new)) + new
            elif new.startswith("⟦"):
                rep = "⟦" + instr + "⟧"
            elif len(new) <= L:
                rep = pad * (L - len(new)) + new              # 靠右：原本顯示的參數在欄位尾端，沿用那裡的字元格式
            else:
                skipped.append(instr)
                continue
            data = [encode(ch, where[c][1]) for c, ch in zip(range(m.start(), m.end()), rep)]
            if any(d is None for d in data):
                skipped.append(instr)                       # 單位元組片段放不下中文
                continue
            for c, d in zip(range(m.start(), m.end()), data):
                at = where[c][0]
                wd[at:at + len(d)] = d
            cps.update((m.start(), m.end() - 1))
            text = text[:m.start()] + rep + text[m.end():]
            done += 1
            changed = True
        if not changed:
            break
    return done, cps, skipped


def drop_eq_entries(wd: bytearray, ts: bytearray, cps: set[int], chars: list[str]) -> int:
    """欄位表（PlcFld）只刪掉已經改寫成文字的 EQ 欄位的記錄；沒改寫的欄位與其他欄位都保留。"""
    _, rg = fib_base(wd)
    offs = subdoc_offsets(wd)
    removed = 0
    for idx, sub in FIB_FLD.items():
        at = rg + idx * 8
        fc, lcb = struct.unpack_from("<II", wd, at)
        if lcb < 10:
            continue
        n = (lcb - 4) // 6
        cp = list(struct.unpack_from(f"<{n + 1}i", ts, fc))
        flds = [bytes(ts[fc + 4 * (n + 1) + 2 * i: fc + 4 * (n + 1) + 2 * i + 2]) for i in range(n)]
        drop = set()
        for i, f in enumerate(flds):
            a = cp[i] + offs[sub]
            want = {0x13: "\x13", 0x15: "\x15"}.get(f[0] & 0x1F)
            if a in cps and want and a < len(chars) and chars[a] == want:
                drop.add(i)
        if not drop:
            continue
        keep = [i for i in range(n) if i not in drop]
        new = struct.pack(f"<{len(keep) + 1}i", *[cp[i] for i in keep], cp[n]) + b"".join(flds[i] for i in keep)
        ts[fc:fc + len(new)] = new
        struct.pack_into("<I", wd, at + 4, len(new) if keep else 0)
        removed += len(drop)
    return removed


def flatten_file(src: Path, dst: Path) -> tuple[int, int, list[str]]:
    import olefile
    shutil.copyfile(src, dst)
    with olefile.OleFileIO(str(dst), write_mode=True) as ole:
        wd = bytearray(ole.openstream("WordDocument").read())
        table = "1Table" if int.from_bytes(wd[0x0A:0x0C], "little") & 0x0200 else "0Table"
        ts = bytearray(ole.openstream(table).read())
        chars, where = read_text(bytes(wd), bytes(ts))
        n, cps, skipped = rewrite_text(wd, chars, where)
        removed = drop_eq_entries(wd, ts, cps, chars)
        ole.write_stream("WordDocument", bytes(wd))
        ole.write_stream(table, bytes(ts))
    return n, removed, skipped


def run(src: Path, dst: Path) -> int:
    dst.parent.mkdir(parents=True, exist_ok=True)
    n, removed, skipped = flatten_file(src, dst)
    print(f"{src.name}: 改寫 EQ 欄位 {n} 個，欄位表移除 {removed} 筆" + (f"，略過 {len(skipped)} 個：{skipped[:3]}" if skipped else ""))
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
