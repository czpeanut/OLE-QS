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
1. 文字流裡的 EQ 欄位「\\x13 EQ … \\x15」換成同樣長度的顯示文字，不足的長度補零寬空白 U+200B（docx_stream.py 會刪掉）；
2. 各欄位表（PlcFld）只移除 EQ 欄位的記錄，讓 LibreOffice 把那段當普通文字讀。
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


FIB_FLD = (16, 17, 18, 19, 48, 57, 59)   # FibRgFcLcb97 裡的欄位表：主文、頁首、註腳、註解、尾註、文字方塊、頁首文字方塊
FLT_EQ = 49                                # 欄位類型 EQ


def fib_base(wd: bytes) -> int:
    """FibRgFcLcb97 在 WordDocument 資料流裡的起點。"""
    if wd[:2] != b"\xec\xa5":
        raise ValueError("不是 Word 97 以後的 .doc（FIB 識別碼不對）")
    csw = int.from_bytes(wd[32:34], "little")
    p = 34 + csw * 2
    cslw = int.from_bytes(wd[p:p + 2], "little")
    return p + 2 + cslw * 4 + 2


def drop_eq_entries(wd: bytearray, ts: bytearray) -> int:
    """從每張欄位表（PlcFld）移除 EQ 欄位的開始、分隔、結束三種記錄，其他欄位（EMBED 方程式、頁碼…）保留。"""
    base, removed = fib_base(wd), 0
    for idx in FIB_FLD:
        at = base + idx * 8
        fc, lcb = struct.unpack_from("<II", wd, at)
        if lcb < 10:
            continue
        n = (lcb - 4) // 6
        cps = list(struct.unpack_from(f"<{n + 1}i", ts, fc))
        flds = [bytes(ts[fc + 4 * (n + 1) + 2 * i: fc + 4 * (n + 1) + 2 * i + 2]) for i in range(n)]
        drop, stack = set(), []
        for i, f in enumerate(flds):
            ch = f[0] & 0x1F
            if ch == 0x13:
                stack.append((i, f[1] == FLT_EQ))
            elif ch == 0x14 and stack and stack[-1][1]:
                drop.add(i)
            elif ch == 0x15 and stack:
                b, is_eq = stack.pop()
                if is_eq:
                    drop.update((b, i))
        if not drop:
            continue
        keep = [i for i in range(n) if i not in drop]
        new = struct.pack(f"<{len(keep) + 1}i", *[cps[i] for i in keep], cps[n]) + b"".join(flds[i] for i in keep)
        ts[fc:fc + len(new)] = new
        struct.pack_into("<I", wd, at + 4, len(new) if keep else 0)
        removed += len(drop)
    return removed


def rewrite_text(wd: bytearray) -> tuple[int, list[str]]:
    """把文字流裡的 EQ 欄位（\\x13 EQ … \\x15）換成顯示文字＋零寬空白，長度不變。"""
    n, skipped = 0, []
    field = re.compile(r"\x13([^\x13\x14\x15]{1,300})\x15")
    for off in (0, 0, 0, 1, 1, 1):                      # 文字可能從奇數位元組開始；重跑三次處理巢狀欄位
        text = bytes(wd[off:]).decode("utf-16-le", errors="replace")
        for m in field.finditer(text):
            instr = m.group(1)
            if re.search(r"[\x00-\x1f\ufffd]", instr):
                continue                                # 二進位資料剛好含這些位元組，不是欄位
            new = display(instr)
            if new is None:
                continue                                # 不是 EQ（EMBED、HYPERLINK…）：保留給 LibreOffice
            if new.startswith("⟦"):
                rep_s = "⟦" + m.group(0)[1:-1] + "⟧"
            elif len(new) <= len(m.group(0)):
                rep_s = ZW * (len(m.group(0)) - len(new)) + new     # 靠右：原本顯示的參數在欄位尾端，沿用那裡的字元格式
            else:
                skipped.append(instr)
                continue
            a = off + 2 * m.start()
            wd[a:a + 2 * len(rep_s)] = rep_s.encode("utf-16-le")
            n += 1
    return n, skipped


def flatten_file(src: Path, dst: Path) -> tuple[int, int, list[str]]:
    import olefile
    shutil.copyfile(src, dst)
    with olefile.OleFileIO(str(dst), write_mode=True) as ole:
        wd = bytearray(ole.openstream("WordDocument").read())
        table = "1Table" if int.from_bytes(wd[0x0A:0x0C], "little") & 0x0200 else "0Table"
        ts = bytearray(ole.openstream(table).read())
        n, skipped = rewrite_text(wd)
        removed = drop_eq_entries(wd, ts)
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
