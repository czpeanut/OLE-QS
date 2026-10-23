#!/usr/bin/env python3
"""雜亂來源（檔名不規則、多科合併、夾解答、照片頁）的考卷先逐頁辨識：是哪校、哪次段考、哪一科、哪一頁。

    python scripts/triage_pages.py <資料夾> -o out/triage.json --budget-twd 8300

資料夾裡的 PDF 逐頁、JPG/PNG/TIFF 各算一頁，用小模型看頁面影像回答：
種類（試題、解答、答案卷、封面、其他）、國中或高中、學校、學年度、學期、第幾次段考、年級、科目、
這頁是不是一份卷的第一頁（卷頭有校名與科目）。結果供 group_triage 分卷、比對重複用。
已辨識過的頁（輸出檔裡有）會跳過，可中斷續跑。
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import io
import json
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import fitz
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent))
from batch_api import Ledger  # noqa: E402
from generate_answers import load_env_file  # noqa: E402
from vlm_extract import call_model  # noqa: E402

MODEL = "gemini-3.1-flash-lite"
PHASE = "triage"
IMAGES = {".jpg", ".jpeg", ".png", ".tif", ".tiff"}

PROMPT = """這是一頁台灣的考卷相關文件。請看頁面（特別是卷頭）回答，看不出來的欄位填 null：
- kind：exam（試題頁）、answer_key（解答、答案、詳解）、answer_sheet（空白答案卷／答案卡）、cover（封面、說明）、other
- level：junior（國中）、senior（高中、高職）、unknown
- school：學校名稱（例如「臺中市立大甲國民中學」照卷上寫的）
- year_roc：學年度（民國，例如 113）
- semester：1＝上學期、2＝下學期
- exam_seq：第幾次段考（1、2、3）；「期末考」填 3
- grade：國中 7、8、9（國一＝7、國二＝8、國三＝9）；高中 10、11、12
- subject：國文、英語、數學、自然、社會、其他之一；sub_subject：理化、生物、地科、歷史、地理、公民（自然、社會才填）
- first_page：這頁是不是一份考卷的第一頁（有卷頭：校名、科目、年級）
- handwriting：頁面上有沒有學生手寫的作答痕跡"""

SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "kind": {"type": "STRING", "enum": ["exam", "answer_key", "answer_sheet", "cover", "other"]},
        "level": {"type": "STRING", "enum": ["junior", "senior", "unknown"]},
        "school": {"type": "STRING", "nullable": True},
        "year_roc": {"type": "INTEGER", "nullable": True},
        "semester": {"type": "INTEGER", "nullable": True},
        "exam_seq": {"type": "INTEGER", "nullable": True},
        "grade": {"type": "INTEGER", "nullable": True},
        "subject": {"type": "STRING", "nullable": True},
        "sub_subject": {"type": "STRING", "nullable": True},
        "first_page": {"type": "BOOLEAN"},
        "handwriting": {"type": "BOOLEAN"},
    },
    "required": ["kind", "level", "first_page", "handwriting"],
}


def pages(root: Path):
    """逐頁產生 (key, 檔案相對路徑, 頁碼, JPEG bytes)；完全相同的檔案只算一次。"""
    seen = set()
    for p in sorted(root.rglob("*")):
        if not p.is_file():
            continue
        ext = p.suffix.lower()
        if ext != ".pdf" and ext not in IMAGES:
            continue
        h = hashlib.md5(p.read_bytes()).hexdigest()
        if h in seen:
            continue
        seen.add(h)
        rel = str(p.relative_to(root))
        if ext == ".pdf":
            try:
                doc = fitz.open(p)
            except Exception:  # noqa: BLE001
                continue
            for i, pg in enumerate(doc, start=1):
                yield f"{h}:{i}", rel, i, lambda pg=pg: pg.get_pixmap(dpi=90).tobytes("jpeg", jpg_quality=75)
        else:
            def render(p=p):
                im = Image.open(p)
                im.seek(0)
                im = im.convert("RGB")
                im.thumbnail((1100, 1100))
                buf = io.BytesIO()
                im.save(buf, "JPEG", quality=75)
                return buf.getvalue()
            yield f"{h}:1", rel, 1, render


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("root", type=Path)
    ap.add_argument("-o", "--out", type=Path, required=True)
    ap.add_argument("--budget-twd", type=float, required=True)
    ap.add_argument("--workers", type=int, default=8)
    args = ap.parse_args()
    load_env_file()
    done = json.loads(args.out.read_text(encoding="utf-8")) if args.out.is_file() else {}
    todo = [x for x in pages(args.root) if x[0] not in done]
    print(f"待辨識 {len(todo)} 頁（已完成 {len(done)}）", flush=True)

    def one(x):
        key, rel, no, render = x
        if Ledger.twd() > args.budget_twd:
            return key, None, 0.0
        img = base64.b64encode(render()).decode()
        try:
            out, _, usd = call_model([{"inline_data": {"mime_type": "image/jpeg", "data": img}}, {"text": PROMPT}],
                                     MODEL, schema=SCHEMA, fallback=[MODEL, "gemini-3.7-flash"], thinking="minimal")
        except Exception as exc:  # noqa: BLE001
            return key, {"error": str(exc)[:200], "file": rel, "page": no}, 0.0
        return key, {**out, "file": rel, "page": no}, usd

    usd = 0.0
    with ThreadPoolExecutor(args.workers) as pool:
        for n, (key, res, c) in enumerate(pool.map(one, todo), start=1):
            usd += c
            if res is not None:
                done[key] = res
            if n % 100 == 0:
                args.out.write_text(json.dumps(done, ensure_ascii=False), encoding="utf-8")
                print(f"  {n}/{len(todo)} 頁，US${usd:.3f}", flush=True)
    args.out.write_text(json.dumps(done, ensure_ascii=False), encoding="utf-8")
    Ledger.add(PHASE, usd)
    print(f"完成 {len(done)} 頁，這次 US${usd:.3f}，累計 NT${Ledger.twd():.0f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
