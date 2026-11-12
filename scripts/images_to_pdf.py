#!/usr/bin/env python3
"""把照片卷（JPG、PNG、TIFF…）依頁序合成一份 PDF（docs/HIGH_SCHOOL_HANDOFF.md 第 1 節）。

照片卷先合成 PDF 放進 sources/，YAML 的 source_file 指向它，附圖的 crop 也以這份 PDF 為準。

    python scripts/images_to_pdf.py P1.jpg P2.jpg P3.jpg -o sources/高一/數學/113/1-1/台中/台中一中.pdf
    python scripts/images_to_pdf.py 照片資料夾/ -o 卷.pdf             # 資料夾裡的圖依檔名排序（P2 在 P10 前）
    python scripts/images_to_pdf.py 照片資料夾/ -o 卷.pdf --rotate 2:90  # 第 2 頁順時針轉 90 度

- 依照片的 EXIF 方向自動轉正；整頁橫掃的再用 --rotate 頁碼:角度 指定（可重複）。
- 長邊縮到 --max-side（預設 2400 像素，約 A4 200 DPI），頁面大小依 200 DPI 換算。
需要 PyMuPDF 與 Pillow（pip install pymupdf pillow）；iPhone 的 HEIC 照片另需 pip install pillow-heif。
"""

from __future__ import annotations

import argparse
import io
import re
import sys
from pathlib import Path

IMAGES = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".bmp", ".webp", ".heic", ".heif"}


def natural(p: Path) -> list:
    """P2.jpg 排在 P10.jpg 前面。"""
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", p.name)]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("inputs", nargs="+", type=Path)
    ap.add_argument("-o", "--out", type=Path, required=True)
    ap.add_argument("--rotate", action="append", default=[], metavar="頁碼:角度",
                    help="某頁順時針轉幾度（90、180、270），可重複")
    ap.add_argument("--max-side", type=int, default=2400)
    args = ap.parse_args()

    try:
        import pymupdf as fitz  # PyMuPDF 1.24 以後的名稱
    except ImportError:
        import fitz
    from PIL import Image, ImageOps

    files: list[Path] = []
    for p in args.inputs:
        if p.is_dir():
            files += sorted((f for f in p.iterdir() if f.suffix.lower() in IMAGES), key=natural)
        elif p.suffix.lower() in IMAGES:
            files.append(p)
    if not files:
        print("沒有找到圖片", file=sys.stderr)
        return 1
    if any(f.suffix.lower() in (".heic", ".heif") for f in files):
        try:
            from pillow_heif import register_heif_opener
            register_heif_opener()
        except ImportError:
            print("有 HEIC 照片：請先 pip install pillow-heif", file=sys.stderr)
            return 1
    turns = {int(a.split(":")[0]): int(a.split(":")[1]) for a in args.rotate}
    doc = fitz.open()
    for i, f in enumerate(files, start=1):
        im = Image.open(f)
        im.seek(0)
        im = ImageOps.exif_transpose(im).convert("RGB")
        if turns.get(i):
            im = im.rotate(-turns[i], expand=True)
        im.thumbnail((args.max_side, args.max_side))
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=88)
        page = doc.new_page(width=im.width * 72 / 200, height=im.height * 72 / 200)
        page.insert_image(page.rect, stream=buf.getvalue())
        print(f"第 {i} 頁：{f.name}")
    args.out.parent.mkdir(parents=True, exist_ok=True)
    doc.save(args.out, garbage=3, deflate=True)
    print(f"→ {args.out}（{len(files)} 頁）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
