#!/usr/bin/env python3
"""國中「題目有問題」修正交件（REVISED_修正_*.jsonl）驗收後寫回 Supabase。

  python scripts/junior_apply_fixes.py plan  <REVISED 資料夾> --reviews <審題資料夾…>
  python scripts/junior_apply_fixes.py apply <REVISED 資料夾> --reviews <審題資料夾…>

規則（題庫負責人 2026-11-20：請 agent 修，一樣標修正）：
- result＝fixed：題幹、題組、題型、選項、答案、解析照修正版（選項附圖依代號保留）；來源標「(修正)」
  （question_source.relation＝fixed，note＝fix_note）。答案和原本不同，或原本沒答案：answer_status＝ai_generated、answer_source＝agent，
  review_note 記修正說明（和原答案）。修好之後，審題當時被擋下的難度、單元一併補寫。
- result＝unfixable：不動資料，只列清單（是否下架由負責人決定）。
- 寫入前把原值存到 out/junior_fix_rollback.jsonl.gz。
"""
import argparse, collections, concurrent.futures as cf, gzip, json, sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from junior_review_tool import get
from junior_apply_review import read_reviews, load_units, resolve_unit, req, L, OUT


def fetch(ids):
    def chunk(c):
        return get("/rest/v1/question", {
            "select": "id,document_id,type,stem_md,group_stem,answer,answer_status,answer_source,explanation_md,review_note,difficulty,"
                      "unit_publisher,unit_subject,unit_code,unit_title,unit_chapter,"
                      "option(id,label,content_md,asset_file,ord),question_source(id,relation,note),"
                      "document!document_id!inner(grade,semester,subject,sub_subject)",
            "id": "in.(" + ",".join(c) + ")"})
    out = {}
    with cf.ThreadPoolExecutor(8) as ex:
        for res in ex.map(chunk, [ids[i:i + 60] for i in range(0, len(ids), 60)]):
            for q in res:
                out[q["id"]] = q
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["plan", "apply"])
    ap.add_argument("revised", type=Path)
    ap.add_argument("--reviews", nargs="+", type=Path, required=True)
    a = ap.parse_args()
    RV = {}
    for f in sorted(a.revised.rglob("REVISED_*.jsonl")):
        for l in f.read_text(encoding="utf-8").splitlines():
            if l.strip():
                x = json.loads(l)
                RV[x["id"]] = x
    R, _ = read_reviews(a.reviews)
    fixed = sorted(i for i, x in RV.items() if x["result"] == "fixed")
    unf = sorted(i for i, x in RV.items() if x["result"] == "unfixable")
    D = fetch(fixed)
    tab, eng = load_units()
    st = collections.Counter()
    jobs = []
    for i in fixed:
        d, x = D[i], RV[i]
        new = [str(z) for z in L(x["answer"])]
        old = L(d["answer"])
        body = {"type": x["type"], "stem_md": x["stem"], "group_stem": x.get("group_stem") or None}
        if x.get("explanation"):
            body["explanation_md"] = x["explanation"]
        note = x["fix_note"]
        if new != old:
            body.update(answer=new, answer_status="ai_generated", answer_source="agent")
            note = f"修正：{note}" + (f"（原答案 {old}）" if old else "")
            st["答案變動／新增"] += 1
        else:
            note = f"修正：{note}"
        body["review_note"] = note[:500]
        r = R.get(i, {})
        if r.get("difficulty") and d["difficulty"] is None:
            body["difficulty"] = int(r["difficulty"])
            st["補難度"] += 1
        if r.get("unit") == "fix":
            u = resolve_unit(r, d, tab, eng)
            if u and u[2] != d["unit_code"]:
                body.update(unit_publisher=u[0], unit_subject=u[1], unit_code=u[2], unit_title=u[3], unit_chapter=u[4])
                st["補單元"] += 1
        opts = []
        old_asset = {o["label"]: o["asset_file"] for o in d["option"]}
        for k, o in enumerate(x.get("options") or []):
            opts.append({"question_id": i, "ord": k, "label": o["label"], "content_md": o.get("content") or "",
                         "asset_file": old_asset.get(o["label"])})
        jobs.append((i, body, opts, x["fix_note"]))
        st["寫回題數"] += 1
        st["題型_" + x["type"]] += 1
    print(f"fixed {len(fixed):,}、unfixable {len(unf):,}")
    for k, v in sorted(st.items()):
        print(f"  {k}: {v:,}")
    (OUT / "junior_fix_unfixable.json").write_text(json.dumps(
        [{"id": i, "reason": RV[i]["unfixable_reason"]} for i in unf], ensure_ascii=False), encoding="utf-8")
    if a.mode == "plan":
        return 0
    OUT.mkdir(exist_ok=True)
    with gzip.open(OUT / "junior_fix_rollback.jsonl.gz", "wt", encoding="utf-8") as f:
        for i, *_ in jobs:
            d = D[i]
            f.write(json.dumps({k: d[k] for k in d if k != "document"}, ensure_ascii=False) + "\n")
    print("已存回復檔")

    def run(job):
        i, body, opts, note = job
        req("PATCH", "/rest/v1/question", body, {"id": f"eq.{i}"})
        req("DELETE", "/rest/v1/option", params={"question_id": f"eq.{i}"})
        if opts:
            req("POST", "/rest/v1/option", opts)
        req("PATCH", "/rest/v1/question_source", {"relation": "fixed", "note": note[:300]}, {"question_id": f"eq.{i}"})

    done = 0
    with cf.ThreadPoolExecutor(6) as ex:
        for _ in ex.map(run, jobs):
            done += 1
            if done % 500 == 0:
                print(" ", done, flush=True)
    print(f"已寫回 {done:,} 題")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
