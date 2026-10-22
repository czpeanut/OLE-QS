#!/usr/bin/env python3
"""把審題台上審題者確認的回報寫回題庫，並在回報上標記「已修正」。

審題者多半是按「採用為回報」，把 AI 初審意見轉成回報；這類回報有原卷與擷取的對照文字，可以直接改：
- 擷取錯誤：把擷取結果改成原卷的寫法（不加註）
- 原卷有誤：改正後題目最後加註「（原卷有誤，已修正）」
缺圖、畫線、符號圖示、題目錯置這類沒辦法用文字替換的，以及審題者自己寫的說明，列成清單另外處理。

同時改題庫 YAML、data/oleqs.db 與 Supabase；回報上加 resolved（時間、改了什麼），審題台據以顯示。
每筆改動記在 out/review_applied.json，可據以還原。

    python scripts/apply_reports.py --codes-root <站點輸出根目錄>            # 只列出會改什麼
    python scripts/apply_reports.py --codes-root <站點輸出根目錄> --apply
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
import time
from pathlib import Path

import requests
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from ai_review import RESULTS, load_codes  # noqa: E402
from apply_errata import MARK, edits, fields, get, index_norm, put, replace_in  # noqa: E402
from batch_api import _env  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
BANK = REPO / "data" / "bank"
DB = REPO / "data" / "oleqs.db"
LOG = REPO / "out" / "review_applied.json"
TEXTUAL = {"錯字或漏字", "選項錯誤", "題組或短文錯誤"}
NOT_TEXT = re.compile(r"底線|畫線|雙線|圖|框|符號|○|●|錯置|順序|大題|題號|漏擷取|漏題|整題|表格")
MAX_CHANGED = 8                     # 一筆修正最多改幾個字；更多的多半是引文有出入，交人工


def supa():
    url, key = _env("SUPABASE_URL").rstrip("/"), _env("SUPABASE_SERVICE_ROLE_KEY")
    return url, {"apikey": key, "Authorization": f"Bearer {key}", "Content-Type": "application/json"}


def load_reports() -> list[dict]:
    url, H = supa()
    rows, off = [], 0
    while True:
        r = requests.get(f"{url}/rest/v1/review_doc", headers=H, timeout=60,
                         params={"select": "site,id,data", "collection": "eq.reports", "order": "id.asc",
                                 "limit": "1000", "offset": str(off)})
        r.raise_for_status()
        rows += r.json()
        if len(r.json()) < 1000:
            return rows
        off += 1000


def ai_items() -> dict[str, list[tuple[str, dict]]]:
    out: dict[str, list[tuple[str, dict]]] = {}
    for f in RESULTS.glob("*.json"):
        r = json.loads(f.read_text(encoding="utf-8"))
        for kind in ("issues", "errata"):
            for e in r.get(kind) or []:
                out.setdefault(e["code"].strip().strip("[]"), []).append((kind, e))
    return out


def plan(rep: dict, items: list[tuple[str, dict]]) -> tuple[list[dict], str | None]:
    """回報 → 要套用的文字修正 [{got, fix, mark}]；做不到時回傳 (空, 原因)。"""
    note = rep.get("note") or ""
    adopted = [(k, e) for k, e in items if e.get("note") and e["note"][:15] in note]
    if not adopted:
        return [], "審題者自寫說明" if note else "沒有說明"
    fixes = []
    for kind, e in adopted:
        if kind == "errata":
            fixes.append({"got": e.get("got", ""), "fix": e.get("fix", ""), "mark": not e.get("extraction")})
        elif (set(e.get("cats") or []) <= TEXTUAL and e.get("orig") and e.get("got")
              and not NOT_TEXT.search(e.get("note", ""))):
            fixes.append({"got": e["got"], "fix": e["orig"], "mark": False})
        else:
            return [], "非文字修正（缺圖、畫線、題目位置等）"
    return fixes, None


def apply_fix(q: dict, by_id: dict, got: str, fix: str) -> bool:
    es = edits(got, fix)
    changed = sum(max(len(o), len(n)) for _, o, n, _ in es)
    if not es or changed > MAX_CHANGED:
        return False
    staged = []
    for left, old, new, right in es:
        hit = None
        for lc, rc in ((left, right), (left[-2:], right[:2]), (left, ""), ("", right)):
            if not (lc or rc):
                continue
            for k, i in fields(q):
                v = replace_in(get(q, k, i), lc, old, new, rc)
                if v is not None:
                    hit = (k, i, v)
                    break
            if hit:
                break
        if hit is None:
            return False
        k, i, v = hit
        if k == "group_stem":
            old_gs = q.get("group_stem")
            for other in by_id.values():
                if other.get("group_stem") == old_gs:
                    other["group_stem"] = v
        else:
            put(q, k, i, v)
        staged.append(k)
    return True


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--codes-root", type=Path, required=True)
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()
    q2c, _ = load_codes(args.codes_root)
    c2q = {c: q for q, c in q2c.items()}
    items = ai_items()
    reports = [r for r in load_reports() if not (r["data"].get("resolved"))]

    by_doc: dict[str, list[tuple[dict, list[dict]]]] = {}
    manual = []
    for r in reports:
        d = r["data"]
        qid = d.get("qid") or c2q.get(d.get("code"))
        fixes, why = plan(d, items.get(d.get("code"), []))
        if why or not qid:
            manual.append((r, why or "找不到題目"))
            continue
        by_doc.setdefault(qid.rsplit("_s", 1)[0], []).append((r, fixes))

    changes, resolved = [], []
    for doc, rs in by_doc.items():
        yp = BANK / f"{doc}.yaml"
        d = yaml.safe_load(yp.read_text(encoding="utf-8"))
        by_id = {q["id"]: q for q in d.get("questions") or []}
        touched = False
        for r, fixes in rs:
            qid = r["data"].get("qid") or c2q.get(r["data"].get("code"))
            q = by_id.get(qid)
            if q is None:
                manual.append((r, "找不到題目"))
                continue
            before = {"stem": q.get("stem"), "group_stem": q.get("group_stem"),
                      "options": [o.get("content") for o in q.get("options") or []]}
            snapshot = json.dumps([q, [x.get("group_stem") for x in by_id.values()]], ensure_ascii=False)
            ok, already = True, True
            for fx in fixes:
                n_fix, _ = index_norm(fx["fix"])
                if n_fix and n_fix in index_norm(" ".join([get(q, k, i) for k, i in fields(q)]))[0]:
                    continue                              # 已經是正確寫法（例如之前的初審已修正）
                already = False
                if not apply_fix(q, by_id, fx["got"], fx["fix"]):
                    ok = False
                    break
            if not ok:                                    # 這筆回報整筆不改，退回原狀
                q_back, gs_back = json.loads(snapshot)
                q.clear()
                q.update(q_back)
                for x, gs in zip(by_id.values(), gs_back):
                    if x is not q:
                        x["group_stem"] = gs
                manual.append((r, "定位不到或改動太多"))
                continue
            if already:
                resolved.append((r, "已是正確寫法"))
                continue
            if any(fx["mark"] for fx in fixes) and not (q.get("stem") or "").rstrip().endswith(MARK):
                q["stem"] = (q.get("stem") or "").rstrip() + MARK
            kind = "原卷有誤" if any(fx["mark"] for fx in fixes) else "擷取錯誤"
            note = "；".join(f"{fx['got']} → {fx['fix']}" for fx in fixes)
            q.setdefault("errata", []).append({"from": fixes[0]["got"], "to": fixes[0]["fix"],
                                               "note": r["data"].get("note", "")[:200], "by": "review",
                                               **({} if kind == "原卷有誤" else {"extraction": True})})
            changes.append({"doc": doc, "qid": qid, "code": r["data"].get("code"), "kind": kind, "note": note,
                            "before": before, "at": time.strftime("%Y-%m-%d %H:%M:%S"),
                            "after": {"stem": q.get("stem"), "group_stem": q.get("group_stem"),
                                      "options": [o.get("content") for o in q.get("options") or []]}})
            resolved.append((r, f"{kind}已修正"))
            touched = True
        if touched and args.apply:
            yp.write_text(yaml.safe_dump(d, allow_unicode=True, sort_keys=False), encoding="utf-8")

    reasons: dict[str, int] = {}
    for _, why in manual:
        reasons[why] = reasons.get(why, 0) + 1
    print(f"回報 {len(reports)} 筆：可修正 {len(changes)} 題，已是正確寫法 {sum(1 for _, w in resolved if w == '已是正確寫法')} 筆，"
          f"交人工 {len(manual)} 筆 {reasons}")
    (REPO / "out" / "review_manual.json").write_text(json.dumps(
        [{"site": r["site"], "id": r["id"], "why": w, **r["data"]} for r, w in manual], ensure_ascii=False, indent=1),
        encoding="utf-8")
    if not args.apply:
        for c in changes[:30]:
            print(f"  {c['code']} {c['kind']}｜{c['note'][:80]}")
        return 0

    url, H = supa()
    H = {**H, "Prefer": "return=minimal"}
    s = requests.Session()
    con = sqlite3.connect(DB)
    for c in changes:
        a, b = c["before"], c["after"]
        rn = f"{c['kind']}已修正（審題回報）：{c['note']}"[:500]
        con.execute("update question set stem_md=?, review_note=? where id=?", (b["stem"], rn, c["qid"]))
        s.patch(f"{url}/rest/v1/question", params={"id": f"eq.{c['qid']}"}, headers=H,
                json={"stem_md": b["stem"], "review_note": rn}, timeout=60).raise_for_status()
        if a["group_stem"] != b["group_stem"]:
            con.execute("update question set group_stem=? where document_id=? and group_stem=?",
                        (b["group_stem"], c["doc"], a["group_stem"]))
            s.patch(f"{url}/rest/v1/question", params={"document_id": f"eq.{c['doc']}",
                                                         "group_stem": f"eq.{a['group_stem']}"},
                    headers=H, json={"group_stem": b["group_stem"]}, timeout=60).raise_for_status()
        opts = con.execute("select id from option where question_id=? order by ord", (c["qid"],)).fetchall()
        for (oid,), old, new in zip(opts, a["options"], b["options"]):
            if old != new:
                con.execute("update option set content_md=? where id=?", (new, oid))
                s.patch(f"{url}/rest/v1/option", params={"id": f"eq.{oid}"}, headers=H,
                        json={"content_md": new}, timeout=60).raise_for_status()
    con.commit()
    now = time.strftime("%Y-%m-%dT%H:%M:%S")
    for r, what in resolved:
        data = {**r["data"], "resolved": {"at": now, "by": "claude", "what": what}}
        s.patch(f"{url}/rest/v1/review_doc", params={"site": f"eq.{r['site']}", "collection": "eq.reports",
                                                       "id": f"eq.{r['id']}"},
                headers=H, json={"data": data}, timeout=60).raise_for_status()
    prev = json.loads(LOG.read_text(encoding="utf-8")) if LOG.is_file() else []
    LOG.write_text(json.dumps(prev + changes, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"已寫回 {len(changes)} 題；{len(resolved)} 筆回報標為已修正；交人工清單在 out/review_manual.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
