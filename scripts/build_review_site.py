#!/usr/bin/env python3
"""把題庫打包成審題網頁的資料檔。

網頁本身是 apps/review/index.html；這支程式產生它讀取的資料：

    <out>/index.html            審題頁面（從 apps/review 複製）
    <out>/data/index.json       段考清單、每份卷的摘要、題號對照
    <out>/data/g/NNN.json       一場段考（年級／科目／學年度／學期-次數）的全部題目

依段考分檔，是因為整個題庫連圖約 150 MB，不能一次載入；審題也是一份卷一份卷看，
選到哪一場才下載哪一場。圖片轉成 WebP 並縮到網頁顯示所需的寬度，直接嵌在該場的
資料檔裡（原 PNG 約 700 MB，轉完約 100 MB）。

每一題有一個 6 碼的**審題編號**，由題目 ID 雜湊而來：同一題不論重建幾次編號都不變，
回報問題時用它指認題目。對照表另存在 <out>/codes.json（不發佈），供回頭查題。

用法:
    python scripts/build_review_site.py data/bank --assets data/assets -o out/review

整個題庫超過單一網頁的容量（256 MB）時，依年級分站：
    python scripts/build_review_site.py data/bank --assets data/assets -o out/review7 \
        --grade 7 --sites 7=<國一網址>,8=<國二網址>,9=<國三網址>
每站只放該年級，但認得其他年級的審題編號，輸入時會連到對的那一站。
再加 --sem 1/2 可以把年級再分上下學期（--sites 7-1=…,7-2=…）。
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import io
import json
import shutil
import sys
from collections import defaultdict
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
from apps.api.quality import evaluate  # noqa: E402

try:
    from PIL import Image
except ImportError:
    sys.exit("需要 Pillow，請先執行：pip install pillow")

CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"   # 去掉 I L O U，唸出來不會混淆
MAX_WIDTH = 800
PAD = 0.08                # 調整邊緣的留邊：裁切範圍長邊的 8%
GRADE_LABEL = {7: "國一", 8: "國二", 9: "國三"}


def code_of(qid: str, length: int = 6) -> str:
    n = int.from_bytes(hashlib.sha1(qid.encode("utf-8")).digest()[:8], "big")
    out = ""
    for _ in range(length):
        out = CROCKFORD[n & 31] + out
        n >>= 5
    return out


def is_gray(im) -> bool:
    small = im.resize((48, 48))
    return max(max(px) - min(px) for px in small.getdata()) < 24


def webp_data_uri(path: Path) -> str | None:
    if not path.is_file():
        return None
    try:
        im = Image.open(path)
        if im.width > MAX_WIDTH:
            im = im.resize((MAX_WIDTH, round(im.height * MAX_WIDTH / im.width)), Image.LANCZOS)
        if im.mode not in ("RGB", "RGBA", "L"):
            im = im.convert("RGBA" if "transparency" in im.info else "RGB")
        if im.mode == "RGB" and is_gray(im):
            im = im.convert("L")                 # 考卷圖多半是黑白線條圖，灰階省下約兩成
        buf = io.BytesIO()
        im.save(buf, "WEBP", quality=65, method=6)
        return "data:image/webp;base64," + base64.b64encode(buf.getvalue()).decode()
    except Exception:
        return None


def encode(im) -> str:
    if im.mode == "RGB" and is_gray(im):
        im = im.convert("L")
    buf = io.BytesIO()
    im.save(buf, "WEBP", quality=65, method=6)
    return "data:image/webp;base64," + base64.b64encode(buf.getvalue()).decode()


def padded(pdf, crop: dict) -> tuple[str, dict] | None:
    """從 PDF 渲染「裁切範圍四周多留 PAD」的圖，供審題網頁拖曳調整邊緣。"""
    import fitz
    pno, (x0, y0, x1, y1) = crop["page"], crop["rect"]
    if not (1 <= pno <= len(pdf)):
        return None
    page = pdf[pno - 1]
    pad = max(8.0, PAD * max(x1 - x0, y1 - y0))
    R = fitz.Rect(x0 - pad, y0 - pad, x1 + pad, y1 + pad) & page.rect
    dpi = min(200, MAX_WIDTH / (R.width / 72))
    pix = page.get_pixmap(dpi=dpi, clip=R)
    im = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
    meta = {"pg": pno, "R": [round(v, 2) for v in (R.x0, R.y0, R.x1, R.y1)],
            "r": [x0, y0, x1, y1], "px": pix.width}
    return encode(im), meta


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("bank", type=Path)
    ap.add_argument("--assets", type=Path, required=True)
    ap.add_argument("-o", "--out", type=Path, required=True)
    ap.add_argument("--grade", type=int, help="只放這個年級（7、8、9）")
    ap.add_argument("--sem", type=int, help="再只放這個學期（1、2）；--sites 的鍵改用 7-1 這種格式")
    ap.add_argument("--sites", default="", help="各年級網址，如 7=https://…,8=https://…")
    ap.add_argument("--only", help="只放檔名含這段文字的卷（測試用）")
    ap.add_argument("--pdf-root", type=Path, help="考卷 PDF 根目錄；有的話附圖改從 PDF 渲染並留邊，可在網頁調整邊緣")
    args = ap.parse_args()
    sites = dict(kv.split("=", 1) for kv in args.sites.split(",") if "=" in kv)

    out = args.out
    (out / "data" / "g").mkdir(parents=True, exist_ok=True)
    shutil.copy(REPO / "apps" / "review" / "index.html", out / "index.html")

    groups: dict[tuple, list] = defaultdict(list)
    for p in sorted(args.bank.glob("*.yaml")):
        if args.only and args.only not in p.name:
            continue
        d = yaml.safe_load(p.read_text(encoding="utf-8"))
        m = d.get("document") or {}
        key = (m.get("grade"), m.get("subject"), m.get("sub_subject") or "",
               m.get("academic_year_roc"), m.get("semester"), m.get("exam_seq"))
        groups[key].append(d)

    elsewhere: dict[str, str] = {}      # 其他站的審題編號 → 站名（"7" 或 "7-1"）
    if args.grade:
        def mine(k) -> bool:
            return k[0] == args.grade and (not args.sem or k[4] == args.sem)
        for key in [k for k in groups if not mine(k)]:
            site = f"{key[0]}-{key[4]}" if args.sem else str(key[0])
            for d in groups.pop(key):
                for q in d.get("questions") or []:
                    elsewhere[code_of(q["id"])] = site
    codes: dict[str, str] = {}          # 審題編號 → 題目 ID
    code_group: dict[str, int] = {}     # 審題編號 → 段考檔序號
    index_groups = []
    missing_images = 0

    for gi, key in enumerate(sorted(groups, key=lambda k: tuple(str(x) for x in k))):
        grade, subject, sub, year, sem, exam = key
        papers_out, summary = [], []
        for d in sorted(groups[key], key=lambda d: (d["document"].get("city") or "",
                                                     d["document"].get("school_short") or "")):
            m = d["document"]
            images: dict[str, str | None] = {}
            img_ids: dict[str, str] = {}
            crop_meta: dict[str, dict] = {}
            pdf_path = args.pdf_root / m["source_file"] if args.pdf_root and m.get("source_file") else None
            pdf_doc = None

            def img(asset: dict | None) -> str | None:
                nonlocal missing_images, pdf_doc
                rel = (asset or {}).get("file")
                if not rel:
                    return None
                if rel not in img_ids:
                    iid = f"i{len(img_ids) + 1}"
                    img_ids[rel] = iid
                    done = None
                    if asset.get("crop") and pdf_path and pdf_path.is_file():
                        try:
                            if pdf_doc is None:
                                import fitz
                                pdf_doc = fitz.open(pdf_path)
                            done = padded(pdf_doc, asset["crop"])
                        except Exception:  # noqa: BLE001
                            done = None
                    if done:
                        images[iid] = done[0]
                        crop_meta[iid] = {"f": rel, **done[1]}
                    else:
                        images[iid] = webp_data_uri(args.assets / rel)
                    if images[iid] is None:
                        missing_images += 1
                return img_ids[rel]

            shared = {a["key"]: a for a in d.get("shared_assets") or []}
            passages: dict[str, str] = {}
            pass_ids: dict[str, str] = {}
            qs = []
            for q in d.get("questions") or []:
                c = code_of(q["id"])
                while c in codes and codes[c] != q["id"]:
                    c = code_of(q["id"], len(c) + 1)     # 雜湊碰撞時加長一碼
                codes[c] = q["id"]
                code_group[c] = gi

                ok, why = evaluate(q, d)
                gs = (q.get("group_stem") or "").strip()
                gid = None
                if gs:
                    if gs not in pass_ids:
                        pass_ids[gs] = f"p{len(pass_ids) + 1}"
                        passages[pass_ids[gs]] = gs
                    gid = pass_ids[gs]
                sa = shared.get(q.get("shared_asset") or "")
                item = {
                    "c": c, "id": q["id"], "s": q.get("section"), "n": q.get("number"),
                    "t": q.get("type"), "stem": q.get("stem") or "",
                    "opts": [{"l": o.get("label"), "c": o.get("content") or "",
                              "img": img(o.get("asset"))}
                             for o in q.get("options") or []],
                    "imgs": [i for a in q.get("assets") or [] if (i := img(a))],
                    "sa": img(sa) if sa else None,
                    "g": gid, "ok": ok, "why": why,
                }
                if q.get("answer"):
                    item["ans"] = q["answer"]
                    item["as"] = q.get("answer_status") or "verified"
                if q.get("answer_status") == "disputed":
                    item["as"] = "disputed"
                    if q.get("ai_answers"):
                        item["votes"] = q["ai_answers"]
                    if q.get("review_note") and "答案卷" in str(q["review_note"]):
                        item["note"] = q["review_note"]
                if str(q.get("answer_source", "")).startswith("ai:"):
                    item["src"] = q["answer_source"]
                if q.get("source_ocr") or str(q.get("review_note", "")).startswith("掃描件"):
                    item["ocr"] = True
                tag = (q.get("tags") or {}).get("textbook")
                if tag:
                    item["tag"] = tag
                qs.append(item)

            ext = str(m.get("extractor") or "rule")
            if pdf_doc is not None:
                pdf_doc.close()
            papers_out.append({
                "doc": m.get("id"), "school": m.get("school"), "short": m.get("school_short"),
                "city": m.get("city"), "title": m.get("title"),
                "ext": "vlm" if ext.startswith("vlm") else "rule",
                "scope": m.get("scope_note"),
                "sections": [{"o": s.get("ord"), "name": s.get("name")}
                             for s in m.get("sections") or []],
                "passages": passages, "images": images, "cm": crop_meta, "q": qs,
            })
            summary.append({"doc": m.get("id"), "short": m.get("school_short"),
                            "city": m.get("city"), "n": len(qs),
                            "ok": sum(1 for x in qs if x["ok"]),
                            "ocr": any(x.get("ocr") for x in qs),
                            "ext": papers_out[-1]["ext"]})

        fname = f"g/{gi:03d}.json"
        (out / "data" / fname).write_text(
            json.dumps({"papers": papers_out}, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8")
        index_groups.append({"f": fname, "grade": grade, "subject": subject, "sub": sub,
                             "year": year, "sem": sem, "exam": exam, "papers": summary})

    (out / "data" / "index.json").write_text(json.dumps(
        {"groups": index_groups, "codes": code_group, "elsewhere": elsewhere,
         "sites": sites,
         "label": (GRADE_LABEL.get(args.grade, "") + ("上" if args.sem == 1 else "下" if args.sem == 2 else ""))
                  or None}, ensure_ascii=False,
        separators=(",", ":")), encoding="utf-8")
    (out / "codes.json").write_text(json.dumps(codes, ensure_ascii=False, indent=0),
                                    encoding="utf-8")

    sizes = [f.stat().st_size for f in (out / "data" / "g").glob("*.json")]
    total = sum(f.stat().st_size for f in out.rglob("*") if f.is_file() and f.name != "codes.json")
    print(f"{len(index_groups)} 場段考、{sum(len(g['papers']) for g in index_groups)} 份卷、"
          f"{len(codes)} 題；遺失圖片 {missing_images} 張")
    print(f"資料檔 {len(sizes)} 個，最大 {max(sizes)/1e6:.1f} MB；發佈總量 {total/1e6:.0f} MB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
