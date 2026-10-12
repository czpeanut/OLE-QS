#!/usr/bin/env python3
"""重繪被浮水印（校徽等整頁底圖）干擾的附圖：拿掉浮水印後，依原裁切範圍從原卷 PDF 重新繪出。

有些學校的考卷每頁鋪一張彩色校徽大圖當底，題目文字與附圖畫在上面，裁出來的附圖背景就是一片色塊。
這類 PDF 的浮水印是單獨一張「大圖」，把它從頁面拿掉再重新繪製，附圖內容（向量線條、文字）完全不變。

判定浮水印：面積超過版面 25%、上面還壓著文字或向量圖形的圖片，且同樣尺寸的圖在這份卷超過一半的頁面出現
（只有一頁的卷例外）。重繪後若附圖幾乎全白（表示那張大圖其實就是附圖本身）就不換。

    python scripts/redraw_figures.py --pdf-root <原卷根目錄> [--docs a,b]   # 試算，清單寫到 out/redraw_preview.json
    python scripts/redraw_figures.py --pdf-root <原卷根目錄> --apply                              # 寫回

--apply 會覆寫 data/assets 的圖檔（原檔備份在 out/redraw_backup/），在題庫該圖加註 redrawn，
並把換過的圖列在 out/redraw_list.txt，供 upload_assets_supabase.py --force-list 重傳。
"""

from __future__ import annotations

import argparse
import collections
import json
import shutil
import sys
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import fitz
import numpy as np
import yaml
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent))
from recover_crops import assets_of  # noqa: E402
from vlm_extract import CROP_DPI  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
BANK = REPO / "data" / "bank"
ASSETS = REPO / "data" / "assets"
BACKUP = REPO / "out" / "redraw_backup"
LIST = REPO / "out" / "redraw_list.txt"


def watermarks(pdf) -> dict[int, list[int]]:
    """每頁要拿掉的浮水印圖片 xref。"""
    cand: dict[int, list[tuple[int, tuple]]] = {}
    sizes = collections.Counter()
    for i, pg in enumerate(pdf):
        area = pg.rect.get_area()
        content = len(pg.get_text("words")) + len(pg.get_drawings())
        if content <= 30:                       # 掃描卷：整頁就是一張圖，不是浮水印
            continue
        for im in pg.get_image_info(xrefs=True):
            if im["xref"] and fitz.Rect(im["bbox"]).get_area() > 0.25 * area:
                key = (im["width"], im["height"])
                cand.setdefault(i, []).append((im["xref"], key))
                sizes[key] += 1
    need = 1 if len(pdf) == 1 else max(2, (len(pdf) + 1) // 2)
    return {i: [x for x, k in v if sizes[k] >= need] for i, v in cand.items() if any(sizes[k] >= need for _, k in v)}


def blank(im: Image.Image) -> float:
    a = np.asarray(im.convert("L"))
    return float((a >= 235).mean())


def process(args) -> tuple[str, list[dict], str | None]:
    doc_id, pdf_root, apply = args
    yp = BANK / f"{doc_id}.yaml"
    d = yaml.safe_load(yp.read_text(encoding="utf-8"))
    src = d["document"].get("source_file")
    if not src or not (Path(pdf_root) / src).is_file():
        return doc_id, [], "找不到原卷"
    pdf = fitz.open(Path(pdf_root) / src)
    wm = watermarks(pdf)
    if not wm:
        return doc_id, [], None
    for i, xrefs in wm.items():
        for x in set(xrefs):
            pdf[i].delete_image(x)
    out = []
    for a, _ in assets_of(d):
        c, f = a.get("crop"), a.get("file")
        if not c or not f or (c["page"] - 1) not in wm or a.get("redrawn"):
            continue
        old_p = ASSETS / f
        if not old_p.is_file():
            continue
        pix = pdf[c["page"] - 1].get_pixmap(dpi=CROP_DPI, clip=fitz.Rect(*c["rect"]))
        new = Image.frombytes("RGB" if pix.n < 4 else "RGBA", (pix.width, pix.height), pix.samples)
        old = Image.open(old_p)
        if blank(new) > 0.995 and blank(old) < 0.98:
            out.append({"file": f, "skip": "重繪後全白，那張大圖就是附圖本身"})
            continue
        diff = float(np.abs(np.asarray(old.convert("L").resize(new.size), dtype=np.int16)
                            - np.asarray(new.convert("L"), dtype=np.int16)).mean())
        if diff < 1.0:                           # 浮水印沒壓到這張圖
            continue
        out.append({"file": f, "diff": round(diff, 1)})
        if apply:
            bk = BACKUP / f
            bk.parent.mkdir(parents=True, exist_ok=True)
            if not bk.exists():
                shutil.copy2(old_p, bk)
            pix.save(old_p)
            a["redrawn"] = "去除浮水印"
    if apply and any("diff" in x for x in out):
        yp.write_text(yaml.safe_dump(d, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return doc_id, out, None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--pdf-root", type=Path, required=True)
    ap.add_argument("--docs", default="")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--workers", type=int, default=3)
    args = ap.parse_args()
    docs = args.docs.split(",") if args.docs else sorted(p.stem for p in BANK.glob("*.yaml"))
    n_docs, changed, skipped = 0, [], []
    with ProcessPoolExecutor(args.workers) as pool:
        for doc_id, out, err in pool.map(process, [(d, str(args.pdf_root), args.apply) for d in docs], chunksize=8):
            if err or not out:
                continue
            n_docs += 1
            changed += [x["file"] for x in out if "diff" in x]
            skipped += [x for x in out if "skip" in x]
    print(f"有浮水印的卷 {n_docs} 份；重繪附圖 {len(changed)} 張；略過 {len(skipped)} 張")
    for x in skipped[:10]:
        print("  略過", x["file"], x["skip"])
    if args.apply:
        # 從題庫重新彙整（中斷後重跑時，前一次換過的圖也要列進去）
        files = sorted(a["file"] for p in BANK.glob("*.yaml")
                       for a, _ in assets_of(yaml.safe_load(p.read_text(encoding="utf-8")))
                       if a.get("redrawn") and a.get("file"))
        LIST.write_text("\n".join(files) + "\n", encoding="utf-8")
        print(f"重繪過的圖共 {len(files)} 張，列在 {LIST.relative_to(REPO)}")
    else:
        (REPO / "out" / "redraw_preview.json").write_text(json.dumps(changed, ensure_ascii=False), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
