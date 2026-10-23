#!/usr/bin/env python3
"""把 group_triage.py 分好的新卷整理成擷取管線吃的 PDF：一份卷一個檔，放在
「<根目錄>/國X/<科目>/<學年度>/<學期>-<段考>/<縣市>/<學校>.pdf」（出處由路徑決定，見 extract.parse_path）。

    python scripts/stage_triage_papers.py <原始資料夾> out/triage_papers.json --od <考卷根目錄> \
        --budget-twd 9371 [--dry-run]

- 只收狀態是「新卷」的；頁面依分卷結果從原檔取出（PDF 頁直接複製，照片轉成 PDF 頁）。
- 掃描時整頁轉了 90 度的頁面要轉正，不然擷取與裁圖都會歪：先用投影粗篩（文字列方向），
  可疑的頁再請小模型判斷要轉幾度（直書的國文不算轉向）。轉正的頁改成影像頁。
- 自然的子科（理化、生物、地科）放在同名資料夾，社會的子科也是；分不出子科的放「自然」「社會」。
- 清單寫到 <根目錄>/_新卷_清單.txt（相對路徑，一行一份），給 batch_extract.py --list 用。
"""

from __future__ import annotations

import argparse
import base64
import io
import json
import re
import sys
from pathlib import Path

import fitz
import numpy as np
from PIL import Image, ImageOps

sys.path.insert(0, str(Path(__file__).resolve().parent))
from batch_api import Ledger  # noqa: E402
from generate_answers import load_env_file  # noqa: E402
from vlm_extract import call_model  # noqa: E402

GRADE_DIR = {7: "國一", 8: "國二", 9: "國三"}
MODEL = "gemini-3.7-flash"           # 小模型判斷方向常錯（轉反或看不出來），改用 flash 比較四個方向
PHASE = "triage"
MAX_SIDE = 2400                     # 照片長邊上限（像素），約 200 DPI 的 A4
ROTATE_PROMPT = """圖中 A、B、C、D 是同一頁考卷轉成四個方向的縮圖。哪一張是正常的閱讀方向（文字是正的，不是側躺或倒過來）？
注意：國文考卷常用直書（字是正的、由上而下排），那也是正常方向。只回答 A、B、C、D 之一。"""
ROTATE_SCHEMA = {"type": "OBJECT", "properties": {"upright": {"type": "STRING", "enum": ["A", "B", "C", "D"]}},
                 "required": ["upright"]}
TURNS = {"A": 0, "B": 90, "C": 180, "D": 270}       # 各縮圖相對原頁順時針轉了幾度


def subject_dir(p: dict) -> str:
    return p.get("sub_subject") or p["subject"].replace("英語", "英文")


def target(p: dict) -> str:
    return (f"{GRADE_DIR[p['grade']]}/{subject_dir(p)}/{p['year_roc']}/{p['semester']}-{p['exam_seq']}/"
            f"{p['city']}/{p['school']}.pdf")


def page_image(src: Path, no: int) -> Image.Image:
    if src.suffix.lower() == ".pdf":
        pg = fitz.open(src)[no - 1]
        pix = pg.get_pixmap(dpi=200)
        return Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
    im = Image.open(src)
    im.seek(0)
    im = ImageOps.exif_transpose(im).convert("RGB")
    im.thumbnail((MAX_SIDE, MAX_SIDE))
    return im


def sideways(im: Image.Image) -> bool:
    """文字列是直的（投影的起伏在欄方向比列方向大）就可能轉了 90 度；直書國文也會中，交給模型判斷。"""
    g = np.asarray(im.convert("L").resize((max(1, im.width // 5), max(1, im.height // 5)))) < 128
    f = lambda v: np.abs(np.diff(v)).mean() / (v.mean() + 1e-6)  # noqa: E731
    return f(g.mean(0)) > f(g.mean(1)) * 1.05


def ask_rotation(im: Image.Image) -> tuple[int, float]:
    """回傳要順時針轉幾度。四個方向的縮圖拼成一張，請模型挑正的那張。"""
    small = im.copy()
    small.thumbnail((700, 700))
    side = max(small.size) + 40
    sheet = Image.new("RGB", (side * 2, side * 2), "white")
    from PIL import ImageDraw
    draw = ImageDraw.Draw(sheet)
    for i, (lab, turn) in enumerate(TURNS.items()):
        t = small.rotate(-turn, expand=True)
        x, y = (i % 2) * side, (i // 2) * side
        sheet.paste(t, (x + 30, y + 30))
        draw.rectangle([x, y, x + 28, y + 28], fill="black")
        draw.text((x + 9, y + 7), lab, fill="white")
    buf = io.BytesIO()
    sheet.save(buf, "JPEG", quality=80)
    out, _, usd = call_model([{"inline_data": {"mime_type": "image/jpeg",
                                               "data": base64.b64encode(buf.getvalue()).decode()}},
                              {"text": ROTATE_PROMPT}], MODEL, schema=ROTATE_SCHEMA,
                             fallback=[MODEL, "gemini-3.6-flash", "gemini-3.8-flash"], thinking="low")
    return TURNS.get(out.get("upright"), 0), usd


def add_image_page(doc: fitz.Document, im: Image.Image, dpi: int = 200) -> None:
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=85)
    w, h = im.width * 72 / dpi, im.height * 72 / dpi
    pg = doc.new_page(width=w, height=h)
    pg.insert_image(pg.rect, stream=buf.getvalue())


def reorient(od: Path, listing: list[str], recheck: set[tuple[str, int]], budget: float) -> None:
    """整理好的卷再檢查一次方向（換判斷方法後用）：可疑頁與先前轉過的頁重新判斷，要轉的改成影像頁。"""
    usd, fixed = 0.0, []
    for t in listing:
        src = fitz.open(od / t)
        turns = []
        for i in range(len(src)):
            im = page_image(od / t, i + 1)
            turn = 0
            if (sideways(im) or (t, i + 1) in recheck) and Ledger.twd() + usd * 32.5 < budget:
                turn, c = ask_rotation(im)
                usd += c
            turns.append((turn, im))
        if not any(tn for tn, _ in turns):
            continue
        out = fitz.open()
        for i, (turn, im) in enumerate(turns):
            if turn:
                fixed.append(f"{t} 第{i + 1}頁 轉{turn}度")
                add_image_page(out, im.rotate(-turn, expand=True))
            else:
                out.insert_pdf(src, from_page=i, to_page=i)
        src.close()
        out.save(od / t, garbage=3, deflate=True)
    Ledger.add(PHASE, usd)
    print(f"重新轉正 {len(fixed)} 頁，判斷費 US${usd:.3f}，累計 NT${Ledger.twd():.0f}")
    for r in fixed:
        print("  " + r)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("root", type=Path)
    ap.add_argument("papers", type=Path)
    ap.add_argument("--od", type=Path, required=True)
    ap.add_argument("--budget-twd", type=float, required=True)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--reorient", type=Path, help="只重新檢查方向；參數是先前的執行紀錄（列出轉過的頁）")
    args = ap.parse_args()
    load_env_file()
    if args.reorient:
        listing = (args.od / "_新卷_清單.txt").read_text(encoding="utf-8").split()
        recheck = {(m.group(1), int(m.group(2))) for m in
                   re.finditer(r"^\s+(\S+\.pdf) 第(\d+)頁", args.reorient.read_text(encoding="utf-8"), re.M)}
        reorient(args.od, listing, recheck, args.budget_twd)
        return 0
    papers = [p for p in json.loads(args.papers.read_text(encoding="utf-8")) if p["status"] == "新卷"]
    targets = [target(p) for p in papers]
    clash = {t for t in targets if targets.count(t) > 1 or (args.od / t).exists()}
    if clash:
        print("目的地重複或已存在：\n  " + "\n  ".join(sorted(clash)))
        return 1
    if args.dry_run:
        print("\n".join(targets[:20]))
        return 0
    usd, rotated, listing = 0.0, [], []
    for p, t in zip(papers, targets):
        out = fitz.open()
        for file, no in p["pages"]:
            src = args.root / file
            im = page_image(src, no)
            turn = 0
            if sideways(im) and Ledger.twd() + usd * 32.5 < args.budget_twd:
                turn, c = ask_rotation(im)
                usd += c
            if turn:
                rotated.append(f"{t} 第{len(out) + 1}頁 轉{turn}度")
                add_image_page(out, im.rotate(-turn, expand=True))
            elif src.suffix.lower() == ".pdf":
                out.insert_pdf(fitz.open(src), from_page=no - 1, to_page=no - 1)
            else:
                add_image_page(out, im)
        dest = args.od / t
        dest.parent.mkdir(parents=True, exist_ok=True)
        out.save(dest, garbage=3, deflate=True)
        listing.append(t)
        p["staged"] = t
    Ledger.add(PHASE, usd)
    (args.od / "_新卷_清單.txt").write_text("\n".join(listing) + "\n", encoding="utf-8")
    staged = {p["unit"] + str(p["pages"][0]): p["staged"] for p in papers}
    allp = json.loads(args.papers.read_text(encoding="utf-8"))
    for p in allp:
        if (k := p["unit"] + str(p["pages"][0])) in staged:
            p["staged"] = staged[k]
    args.papers.write_text(json.dumps(allp, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"整理 {len(listing)} 份卷；轉正 {len(rotated)} 頁，判斷費 US${usd:.3f}，累計 NT${Ledger.twd():.0f}")
    for r in rotated:
        print("  " + r)
    return 0


if __name__ == "__main__":
    sys.exit(main())
