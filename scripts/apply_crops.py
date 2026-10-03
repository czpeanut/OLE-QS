#!/usr/bin/env python3
"""套用審題網頁上的「調整邊緣」：從原卷 PDF 依新範圍重新裁圖。

審題網頁把調整存在各站資料庫的 crops 集合（每筆：file、doc、page、rect）。
先把集合匯出成 JSON 檔（每筆一個檔），再執行本程式：

    python scripts/apply_crops.py <匯出目錄> --bank data/bank --assets data/assets \\
        --pdf-root <考卷根目錄>

會覆寫 data/assets 裡的圖檔（220 DPI，與擷取時相同），並更新題庫中該圖的 crop 位置，
加註 crop_by: human。之後重建審題網頁即可看到新圖。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import fitz
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from recover_crops import assets_of  # noqa: E402
from vlm_extract import CROP_DPI  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("exported", type=Path, help="crops 集合匯出的目錄（遞迴找 *.json）")
    ap.add_argument("--bank", type=Path, default=Path("data/bank"))
    ap.add_argument("--assets", type=Path, default=Path("data/assets"))
    ap.add_argument("--pdf-root", type=Path, required=True)
    args = ap.parse_args()

    edits = []
    for f in sorted(args.exported.rglob("*.json")):
        x = json.loads(f.read_text(encoding="utf-8"))
        x = x.get("data", x)
        if x.get("file") and x.get("rect") and x.get("page"):
            edits.append(x)
    by_doc: dict[str, list[dict]] = {}
    for e in edits:
        by_doc.setdefault(e["file"].split("/")[0], []).append(e)

    done = 0
    for doc_id, es in by_doc.items():
        yp = args.bank / f"{doc_id}.yaml"
        if not yp.is_file():
            print(f"找不到題庫檔 {doc_id}")
            continue
        d = yaml.safe_load(yp.read_text(encoding="utf-8"))
        pdf = fitz.open(args.pdf_root / d["document"]["source_file"])
        assets = {a.get("file"): a for a, _ in assets_of(d)}
        for e in es:
            a = assets.get(e["file"])
            if not a:
                print(f"題庫中沒有這張圖：{e['file']}")
                continue
            page = pdf[e["page"] - 1]
            clip = fitz.Rect(*e["rect"]) & page.rect
            if clip.is_empty:
                continue
            dest = args.assets / e["file"]
            dest.parent.mkdir(parents=True, exist_ok=True)
            page.get_pixmap(dpi=CROP_DPI, clip=clip).save(dest)
            a["crop"] = {"page": e["page"], "rect": [round(v, 2) for v in (clip.x0, clip.y0, clip.x1, clip.y1)]}
            a["crop_by"] = "human"
            done += 1
        pdf.close()
        yp.write_text(yaml.safe_dump(d, allow_unicode=True, sort_keys=False), encoding="utf-8")
    print(f"重新裁切 {done} 張（共 {len(edits)} 筆調整）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
