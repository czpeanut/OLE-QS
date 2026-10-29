#!/usr/bin/env python3
"""把寫回題庫的修正直接更新到審題台已發佈的資料（不重建整站）。

重建審題台要全部原卷與附圖；只修了少數題目時，用這支把那些題目所在的卷的題目文字、題組短文、
品管結果從題庫 YAML 重新帶入，改過附圖的題目再從原卷重裁附圖。只改有動到的段考檔，直接上傳。

    python scripts/patch_review_sites.py --since 2026-10-29 --pdf-root <考卷根目錄> [--dry-run]

--since：out/review_applied.json 裡這個時間之後寫回的題目才更新。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import requests
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from apps.api.quality import evaluate  # noqa: E402
from batch_api import _env  # noqa: E402
from build_review_site import padded, webp_data_uri  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
BANK = REPO / "data" / "bank"
ASSETS = REPO / "data" / "assets"
LOG = REPO / "out" / "review_applied.json"
FIXES = REPO / "out" / "fix_reports"
BUCKET = "review"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--since", required=True)
    ap.add_argument("--pdf-root", type=Path)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    url, key = _env("SUPABASE_URL").rstrip("/"), _env("SUPABASE_SERVICE_ROLE_KEY")
    H = {"apikey": key, "Authorization": f"Bearer {key}"}
    s = requests.Session()

    log = [c for c in json.loads(LOG.read_text(encoding="utf-8")) if c.get("at", "") >= args.since]
    docs = {c["doc"] for c in log}
    fig_q = set()                                   # 附圖重裁過的題目：要重新產生附圖
    for f in FIXES.glob("*.json"):
        r = json.loads(f.read_text(encoding="utf-8"))
        if r.get("_applied") == "fixed" and r.get("figures_changed"):
            fig_q.add(r["_qid"])
    print(f"要更新 {len(docs)} 份卷（{len(log)} 題，其中 {len(fig_q)} 題改了附圖）")

    by_site: dict[str, set[str]] = {}
    for doc in docs:
        m = yaml.safe_load((BANK / f"{doc}.yaml").read_text(encoding="utf-8"))["document"]
        by_site.setdefault(f"{m['grade']}-{m['semester']}", set()).add(doc)

    n_q = n_files = 0
    for site, site_docs in sorted(by_site.items()):
        idx = s.get(f"{url}/storage/v1/object/{BUCKET}/{site}/data/index.json", headers=H, timeout=120).json()
        files = {}
        for g in idx["groups"]:
            for p in g["papers"]:
                if p["doc"] in site_docs:
                    files.setdefault(g["f"], set()).add(p["doc"])
        for gf, want in sorted(files.items()):
            data = s.get(f"{url}/storage/v1/object/{BUCKET}/{site}/data/{gf}", headers=H, timeout=300).json()
            for paper in data["papers"]:
                if paper["doc"] not in want:
                    continue
                d = yaml.safe_load((BANK / f"{paper['doc']}.yaml").read_text(encoding="utf-8"))
                by_id = {q["id"]: q for q in d.get("questions") or []}
                pdf = None
                if args.pdf_root and d["document"].get("source_file"):
                    pp = args.pdf_root / d["document"]["source_file"]
                    if pp.is_file():
                        import fitz
                        pdf = fitz.open(pp)

                def new_img(asset: dict | None) -> str | None:
                    if not asset or not asset.get("file"):
                        return None
                    iid = f"r{len(paper['images']) + 1}"
                    done = padded(pdf, asset["crop"]) if pdf is not None and asset.get("crop") else None
                    if done:
                        paper["images"][iid] = done[0]
                        paper.setdefault("cm", {})[iid] = {"f": asset["file"], **done[1]}
                    else:
                        paper["images"][iid] = webp_data_uri(ASSETS / asset["file"])
                    return iid

                # 題組短文依題庫重新編號（短文改過、題目移出題組都要反映）
                passages, pass_ids = {}, {}
                for q in d.get("questions") or []:
                    gs = (q.get("group_stem") or "").strip()
                    if gs and gs not in pass_ids:
                        pass_ids[gs] = f"p{len(pass_ids) + 1}"
                        passages[pass_ids[gs]] = gs
                paper["passages"] = passages
                shared = {a["key"]: a for a in d.get("shared_assets") or []}
                for item in paper["q"]:
                    q = by_id.get(item["id"])
                    if q is None:
                        continue
                    gs = (q.get("group_stem") or "").strip()
                    item["g"] = pass_ids.get(gs) if gs else None
                    item["stem"] = q.get("stem") or ""
                    item["ok"], item["why"] = evaluate(q, d)
                    old_opts = {o["l"]: o.get("img") for o in item.get("opts") or []}
                    if q["id"] in fig_q:
                        item["imgs"] = [i for a in q.get("assets") or [] if (i := new_img(a))]
                        item["opts"] = [{"l": o.get("label"), "c": o.get("content") or "", "img": new_img(o.get("asset"))}
                                        for o in q.get("options") or []]
                        sa = shared.get(q.get("shared_asset") or "")
                        if sa and item.get("sa"):
                            fresh = new_img(sa)
                            if fresh:                       # 共用圖：原 id 換成新圖，同題組各題一起更新
                                paper["images"][item["sa"]] = paper["images"].pop(fresh)
                                if fresh in paper.get("cm", {}):
                                    paper["cm"][item["sa"]] = paper["cm"].pop(fresh)
                        elif not sa:
                            item["sa"] = None
                    else:
                        item["opts"] = [{"l": o.get("label"), "c": o.get("content") or "",
                                         "img": old_opts.get(o.get("label"))} for o in q.get("options") or []]
                    n_q += 1
                if pdf is not None:
                    pdf.close()
            n_files += 1
            if not args.dry_run:
                body = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
                r = s.post(f"{url}/storage/v1/object/{BUCKET}/{site}/data/{gf}", data=body, timeout=300,
                           headers={**H, "Content-Type": "application/json", "x-upsert": "true"})
                r.raise_for_status()
            print(f"  {site} {gf}：{len(want)} 份卷", flush=True)
    print(f"{'（試算）' if args.dry_run else ''}更新 {n_files} 個段考檔、{n_q} 題")
    return 0


if __name__ == "__main__":
    sys.exit(main())
