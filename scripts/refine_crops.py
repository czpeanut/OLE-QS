#!/usr/bin/env python3
"""自動校正附圖的裁切邊界（影像分析，不呼叫模型）。

擷取時模型給的框加了 4pt 留白，常見兩種錯：
- 框太大：把旁邊的「(A)」標號、下一張圖的頂端、上一行文字的底部框進來
- 框太小：圖的一角或邊上的數字、端點標記被切掉

做法：把框外擴一圈從原卷 PDF 渲染，二值化後把筆畫稍微膨脹，讓同一張圖的線條與標註
連成一塊；每一塊「墨跡」看它有多少比例落在原框內——過半的是這張圖的一部分（被切到的就補回來），
不到一半的是鄰居（框進來的就剔掉）。新框 = 保留塊的外接框加 3pt 留白。

保守規則（寧可不改也不改壞）：
- 保留塊碰到外擴範圍的邊（代表它延伸更遠，多半連到正文），那一側不外擴
- 新框面積與原框差太多（< 0.35 倍或 > 2 倍）就不改
- 審題時人工調整過的（crop_by: human）不動

    python scripts/refine_crops.py data/bank --pdf-root <考卷根目錄> --sample 40 -o out/refine_sample.png
    python scripts/refine_crops.py data/bank --pdf-root <考卷根目錄> --apply [--workers 4]
"""

from __future__ import annotations

import argparse
import json
import random
import re
import sys
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

import cv2
import fitz
import numpy as np
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from recover_crops import assets_of  # noqa: E402
from vlm_extract import CROP_DPI  # noqa: E402

SCALE = CROP_DPI / 72
PAD = 3.0          # 新框留白（點）
JOIN = 2.5         # 筆畫橫向膨脹半徑（點）：同一張圖的線條與標註連成一塊
JOIN_Y = 0.8       # 縱向膨脹半徑（點）
MIN_KEEP = 0.5     # 一塊墨跡至少這個比例落在原框內才算這張圖的
LABEL = re.compile(r"^[(（][A-Da-dＡ-Ｄ甲乙丙丁戊己][)）]$|^[A-DＡ-Ｄ][.．、]$")


def refine(page: fitz.Page, rect: list[float]) -> list[float] | None:
    """回傳校正後的框（PDF 點座標）；判斷不出來或變動太大時回傳 None（維持原框）。"""
    R = fitz.Rect(rect)
    if R.is_empty or R.width < 6 or R.height < 6:
        return None
    m = min(60.0, max(24.0, 0.3 * max(R.width, R.height)))
    W = fitz.Rect(R.x0 - m, R.y0 - m, R.x1 + m, R.y1 + m) & page.rect
    pix = page.get_pixmap(dpi=CROP_DPI, clip=W, colorspace=fitz.csGRAY)
    img = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width)
    # 掃描卷底色偏灰：門檻跟著底色走
    # 照片、滿版底色的圖：底色本身就是圖的一部分，看不出邊界，維持原框
    ry = slice(int((R.y0 - W.y0) * pix.height / W.height), int((R.y1 - W.y0) * pix.height / W.height))
    rxs = slice(int((R.x0 - W.x0) * pix.width / W.width), int((R.x1 - W.x0) * pix.width / W.width))
    if (img[ry, rxs] < 235).mean() > 0.45:
        return None
    bg = int(np.percentile(img, 90))
    ink = img < min(170, bg - 50)
    sx, sy = pix.width / W.width, pix.height / W.height
    # 原生 PDF 有文字層：選項標號「(A)」一律剔除（組卷時標號另外印），
    # 大半落在原框外的字（題幹、鄰題）也先剔除，免得框被拉向正文
    # 以「行」判斷：一行字大半在框外（題幹延伸進圖旁），整行剔除
    def erase(b):
        ink[max(0, int((b[1] - W.y0) * sy) - 1):int((b[3] - W.y0) * sy) + 2,
            max(0, int((b[0] - W.x0) * sx) - 1):int((b[2] - W.x0) * sx) + 2] = False
    inside = lambda b: (fitz.Rect(b) & R).get_area() / max(fitz.Rect(b).get_area(), 1e-6)  # noqa: E731
    for block in page.get_text("dict", clip=W)["blocks"]:
        for line in block.get("lines", []):
            if inside(line["bbox"]) < 0.5:
                erase(line["bbox"])
                continue
            for span in line["spans"]:
                if LABEL.match(span["text"].strip()):
                    erase(span["bbox"])
    for x0, y0, x1, y1, word, *_ in page.get_text("words", clip=W):
        if LABEL.match(word.strip()):
            erase((x0, y0, x1, y1))
    if ink.sum() < 20:
        return None
    # 橫向膨脹多、縱向少：「12」這種標註會跟圖連在一起，但上下相鄰的文字行不會被黏成一大塊
    kx, ky = max(1, int(round(JOIN * SCALE))), max(1, int(round(JOIN_Y * SCALE)))
    joined = cv2.dilate(ink.astype(np.uint8), cv2.getStructuringElement(cv2.MORPH_RECT, (2 * kx + 1, 2 * ky + 1)))
    n, labels = cv2.connectedComponents(joined, connectivity=8)

    rx0, ry0 = int((R.x0 - W.x0) * sx), int((R.y0 - W.y0) * sy)
    rx1, ry1 = int((R.x1 - W.x0) * sx), int((R.y1 - W.y0) * sy)
    inR = np.zeros_like(ink)
    inR[max(0, ry0):ry1, max(0, rx0):rx1] = True

    lab_ink = labels[ink]
    total = np.bincount(lab_ink, minlength=n)
    inside = np.bincount(labels[ink & inR], minlength=n)
    keep = [i for i in range(1, n) if total[i] >= 4 and inside[i] / total[i] >= MIN_KEEP]
    if not keep:
        return None
    mask = ink & np.isin(labels, keep)
    ys, xs = np.nonzero(mask)
    x0, x1, y0, y1 = xs.min(), xs.max() + 1, ys.min(), ys.max() + 1
    h, w = ink.shape
    # 碰到外擴範圍邊緣：這塊延伸到更遠（多半連到正文），那一側維持原框
    if x0 <= 1: x0 = max(0, rx0)
    if y0 <= 1: y0 = max(0, ry0)
    if x1 >= w - 1: x1 = min(w, rx1)
    if y1 >= h - 1: y1 = min(h, ry1)
    new = fitz.Rect(W.x0 + x0 / sx - PAD, W.y0 + y0 / sy - PAD, W.x0 + x1 / sx + PAD, W.y0 + y1 / sy + PAD) & page.rect
    ratio = new.get_area() / max(R.get_area(), 1)
    if new.is_empty or not 0.35 <= ratio <= 2.0:
        return None
    return [round(v, 2) for v in (new.x0, new.y0, new.x1, new.y1)]


def changed(a: list[float], b: list[float], tol: float = 1.5) -> bool:
    return any(abs(x - y) > tol for x, y in zip(a, b))


def process(path: str, pdf_root: str, assets_dir: str, apply: bool) -> tuple[str, int, int, list]:
    p = Path(path)
    d = yaml.safe_load(p.read_text(encoding="utf-8"))
    m = d["document"]
    pdf_path = Path(pdf_root) / (m.get("source_file") or "")
    if not str(m.get("extractor", "")).startswith("vlm") or not pdf_path.is_file():
        return p.name, 0, 0, []
    doc = fitz.open(pdf_path)
    n_seen = n_changed = 0
    log = []
    for a, _ in assets_of(d):
        c = a.get("crop")
        if not a.get("file") or not c or a.get("crop_by") == "human" or not 1 <= c["page"] <= len(doc):
            continue
        n_seen += 1
        page = doc[c["page"] - 1]
        new = refine(page, c["rect"])
        if not new or not changed(new, c["rect"]):
            continue
        n_changed += 1
        log.append({"file": a["file"], "page": c["page"], "old": c["rect"], "new": new})
        if apply:
            dest = Path(assets_dir) / a["file"]
            page.get_pixmap(dpi=CROP_DPI, clip=fitz.Rect(new)).save(dest)
            a["crop"] = {"page": c["page"], "rect": new, "auto_from": c["rect"]}
            a["crop_by"] = "auto"
    doc.close()
    if apply and n_changed:
        p.write_text(yaml.safe_dump(d, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return p.name, n_seen, n_changed, log


def sheet(entries: list[dict], pdf_root: Path, bank: Path, out: Path) -> None:
    """對照圖：紅框＝原本，綠框＝校正後。"""
    from PIL import Image, ImageDraw, ImageFont
    tiles = []
    font = ImageFont.load_default()
    for e in entries:
        doc_id = e["file"].split("/")[0]
        meta = yaml.safe_load((bank / f"{doc_id}.yaml").read_text(encoding="utf-8"))["document"]
        page = fitz.open(pdf_root / meta["source_file"])[e["page"] - 1]
        o, n = e["old"], e["new"]
        V = fitz.Rect(min(o[0], n[0]) - 20, min(o[1], n[1]) - 20, max(o[2], n[2]) + 20, max(o[3], n[3]) + 20) & page.rect
        s = min(3.0, 420 / max(V.width, 1))
        pix = page.get_pixmap(matrix=fitz.Matrix(s, s), clip=V)
        im = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
        dr = ImageDraw.Draw(im)
        f = lambda r: [(r[0] - V.x0) * s, (r[1] - V.y0) * s, (r[2] - V.x0) * s, (r[3] - V.y0) * s]  # noqa: E731
        dr.rectangle(f(o), outline=(220, 0, 0), width=2)
        dr.rectangle(f(n), outline=(0, 170, 0), width=2)
        dr.text((3, 3), e["file"].split("/", 1)[1], fill=(0, 0, 255), font=font)
        tiles.append(im)
    cols = 3
    cw = max(t.width for t in tiles) + 12
    rows = [tiles[i:i + cols] for i in range(0, len(tiles), cols)]
    H = sum(max(t.height for t in r) + 12 for r in rows)
    canvas = Image.new("RGB", (cw * cols, H), "white")
    y = 0
    for r in rows:
        for j, t in enumerate(r):
            canvas.paste(t, (j * cw, y))
        y += max(t.height for t in r) + 12
    canvas.save(out)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("bank", type=Path)
    ap.add_argument("--pdf-root", type=Path, required=True)
    ap.add_argument("--assets", type=Path, default=Path("data/assets"))
    ap.add_argument("--sample", type=int, help="隨機抽這麼多份卷試跑，輸出對照圖（不寫檔）")
    ap.add_argument("-o", "--out", type=Path, default=Path("out/refine_sample.png"))
    ap.add_argument("--apply", action="store_true", help="寫回題庫與圖檔")
    ap.add_argument("--only")
    ap.add_argument("--workers", type=int, default=4)
    args = ap.parse_args()

    paths = sorted(str(p) for p in args.bank.glob("*.yaml") if not args.only or args.only in p.name)
    if args.sample:
        random.seed(7)
        paths = random.sample(paths, min(args.sample, len(paths)))
    seen = chg = 0
    logs: list[dict] = []
    with ProcessPoolExecutor(args.workers) as pool:
        futs = [pool.submit(process, p, str(args.pdf_root), str(args.assets), args.apply) for p in paths]
        for i, f in enumerate(as_completed(futs), 1):
            _, s, c, log = f.result()
            seen += s
            chg += c
            logs += log
            if i % 200 == 0:
                print(f"[{i}/{len(paths)}] 檢查 {seen:,} 張、校正 {chg:,} 張", flush=True)
    print(f"檢查 {seen:,} 張附圖，校正 {chg:,} 張（{chg / max(seen, 1):.0%}）")
    log_path = Path("out/refine_crops_log.jsonl" if args.apply else "out/refine_crops_sample.jsonl")
    log_path.write_text("".join(json.dumps(x, ensure_ascii=False) + "\n" for x in logs), encoding="utf-8")
    if args.sample and logs:
        random.seed(1)
        sheet(random.sample(logs, min(36, len(logs))), args.pdf_root, args.bank, args.out)
        print(f"對照圖 → {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
