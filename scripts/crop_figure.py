#!/usr/bin/env python3
"""從原卷 PDF 裁出附圖 PNG，並印出 YAML 要寫的 crop（docs/HIGH_SCHOOL_HANDOFF.md 第 5 節）。

位置可以用三種單位給，輸出的 crop.rect 一律是 PDF point（1/72 吋，原點在轉正後頁面的左上角）：

    # PDF point（x0 y0 x1 y1）
    python scripts/crop_figure.py 原卷.pdf --page 1 --rect 72 310.5 290.2 455 -o s1_3_f1.png
    # 以某個 DPI 渲染時的像素框（x0 y0 x1 y1）：自動換算成 point
    python scripts/crop_figure.py 原卷.pdf --page 1 --px 200 862 806 1264 --px-dpi 200 -o s1_3_f1.png
    # 模型常用的 0～1000 正規化框（ymin xmin ymax xmax）
    python scripts/crop_figure.py 原卷.pdf --page 1 --box1000 210 85 380 520 -o s1_3_f1.png

--dpi 是輸出 PNG 的解析度（預設 220，規格要求 200 以上）；--rotate 90/180/270 可把裁下的圖順時針轉正。
需要 PyMuPDF（pip install pymupdf）。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("pdf", type=Path)
    ap.add_argument("--page", type=int, required=True, help="第幾頁（1 起）")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--rect", type=float, nargs=4, metavar=("X0", "Y0", "X1", "Y1"), help="PDF point")
    g.add_argument("--px", type=float, nargs=4, metavar=("X0", "Y0", "X1", "Y1"), help="像素框（配合 --px-dpi）")
    g.add_argument("--box1000", type=float, nargs=4, metavar=("YMIN", "XMIN", "YMAX", "XMAX"),
                   help="0～1000 正規化框")
    ap.add_argument("--px-dpi", type=float, default=200, help="--px 的像素是以多少 DPI 渲染的（預設 200）")
    ap.add_argument("--pad", type=float, default=3, help="四周多留幾 point（預設 3）")
    ap.add_argument("--dpi", type=int, default=220)
    ap.add_argument("--rotate", type=int, default=0, choices=[0, 90, 180, 270])
    ap.add_argument("-o", "--out", type=Path, required=True)
    args = ap.parse_args()

    try:
        import pymupdf as fitz  # PyMuPDF 1.24 以後的名稱
    except ImportError:
        import fitz

    doc = fitz.open(args.pdf)
    if not 1 <= args.page <= len(doc):
        print(f"頁碼超出範圍：這份 PDF 只有 {len(doc)} 頁", file=sys.stderr)
        return 1
    page = doc[args.page - 1]
    W, H = page.rect.width, page.rect.height
    if args.rect:
        x0, y0, x1, y1 = args.rect
    elif args.px:
        k = 72 / args.px_dpi
        x0, y0, x1, y1 = (v * k for v in args.px)
    else:
        ymin, xmin, ymax, xmax = args.box1000
        x0, y0, x1, y1 = xmin / 1000 * W, ymin / 1000 * H, xmax / 1000 * W, ymax / 1000 * H
    r = fitz.Rect(x0 - args.pad, y0 - args.pad, x1 + args.pad, y1 + args.pad) & page.rect
    if r.is_empty or r.width < 4 or r.height < 4:
        print("裁切範圍是空的，請檢查座標", file=sys.stderr)
        return 1
    pix = page.get_pixmap(dpi=args.dpi, clip=r, alpha=False)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    if args.rotate:
        from PIL import Image
        im = Image.frombytes("RGB", (pix.width, pix.height), pix.samples).rotate(-args.rotate, expand=True)
        im.save(args.out)
        size = im.size
    else:
        pix.save(args.out)
        size = (pix.width, pix.height)
    rect = [round(v, 2) for v in (r.x0, r.y0, r.x1, r.y1)]
    print(json.dumps({"file": str(args.out), "size_px": size, "crop": {"page": args.page, "rect": rect}},
                     ensure_ascii=False))
    if size[0] < 300:
        print("提醒：圖寬不到 300 像素，可以加大 --dpi", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
