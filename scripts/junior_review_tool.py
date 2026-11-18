#!/usr/bin/env python3
"""國中題庫逐題審題工具（給執行 agent；只用 Python 標準函式庫，只讀不寫）。

需要環境變數或同資料夾 .env.local：SUPABASE_URL、SUPABASE_SERVICE_ROLE_KEY。
本工具只發 GET 請求，**不會寫入資料庫**；審題結果交 JSONL 到雲端硬碟，由題庫維護端驗收後寫回。

  python junior_review_tool.py batches                     # 列出全部批次（科目_年級-學期_第N份）與卷數、題數
  python junior_review_tool.py fetch 數學_7-1_1 -o work/    # 取一批題目 → work/數學_7-1_1.jsonl
  python junior_review_tool.py image "doc_…/s1_8_f1.png" -o fig.png   # 取一張附圖
  python junior_review_tool.py check work/數學_7-1_1.jsonl REVIEW_數學_7-1_1.jsonl   # 交件前檢查

批次：同科目、年級、學期的卷依卷 id 排序，每 50 份卷一批，編號從 1 起。
"""
from __future__ import annotations

import argparse, collections, hashlib, json, os, re, sys, time, urllib.parse, urllib.request
from pathlib import Path

PAPERS_PER_BATCH = 50
HERE = Path(__file__).resolve().parent


def env() -> tuple[str, str]:
    for p in (Path.cwd() / ".env.local", HERE / ".env.local", HERE.parent / ".env.local"):
        if p.is_file():
            for line in p.read_text(encoding="utf-8").splitlines():
                m = re.match(r"\s*([A-Z_]+)\s*=\s*(.*?)\s*$", line)
                if m and m.group(1) not in os.environ:
                    os.environ[m.group(1)] = m.group(2).strip('"').strip("'")
    url, key = os.environ.get("SUPABASE_URL"), os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
    if not url or not key:
        sys.exit("缺 SUPABASE_URL／SUPABASE_SERVICE_ROLE_KEY（放環境變數或 .env.local，不要印出來）")
    return url.rstrip("/"), key


def get(path: str, params: dict | None = None, raw: bool = False):
    """唯一的網路出口：只做 GET。"""
    url, key = env()
    q = ("?" + urllib.parse.urlencode(params, safe="(),.*:")) if params else ""
    req = urllib.request.Request(url + path + q, method="GET", headers={"apikey": key, "Authorization": f"Bearer {key}"})
    for attempt in range(5):
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                b = r.read()
                return b if raw else json.loads(b)
        except Exception as e:  # noqa: BLE001
            if attempt == 4:
                raise
            time.sleep(3 * (attempt + 1))


def rows(table: str, select: str, filt: dict, order: str = "id") -> list[dict]:
    out, page = [], 1000
    while True:
        r = get(f"/rest/v1/{table}", {"select": select, "order": f"{order}.asc", "limit": page, "offset": len(out), **filt})
        out += r
        if len(r) < page:
            return out


def documents() -> list[dict]:
    return rows("document", "id,title,school,exam_name,academic_year_roc,semester,exam_seq,grade,subject,sub_subject,scope_note,publisher",
                {"grade": "in.(7,8,9)"})


def batch_map(docs: list[dict]) -> dict[str, list[dict]]:
    by = collections.defaultdict(list)
    for d in sorted(docs, key=lambda d: d["id"]):
        by[f"{d['subject']}_{d['grade']}-{d['semester'] or 0}"].append(d)
    out = {}
    for k, ds in sorted(by.items()):
        for i in range(0, len(ds), PAPERS_PER_BATCH):
            out[f"{k}_{i // PAPERS_PER_BATCH + 1}"] = ds[i:i + PAPERS_PER_BATCH]
    return out


def cmd_batches(_a) -> int:
    bm = batch_map(documents())
    tot = 0
    for k, ds in bm.items():
        print(f"{k}\t{len(ds)} 卷")
        tot += len(ds)
    print(f"合計 {len(bm)} 批、{tot} 卷")
    return 0


def cmd_fetch(a) -> int:
    bm = batch_map(documents())
    if a.batch not in bm:
        sys.exit(f"沒有批次 {a.batch}；先執行 batches 看清單")
    docs = {d["id"]: d for d in bm[a.batch]}
    ids = sorted(docs)
    qs, opts, assets, tags, secs = [], [], [], [], []
    for i in range(0, len(ids), 10):
        f = {"document_id": "in.(" + ",".join(ids[i:i + 10]) + ")"}
        secs += rows("section", "document_id,ord,name", f)
        qs += rows("question", "id,document_id,section_ord,number,type,stem_md,group_stem,answer,answer_status,answer_source,"
                   "explanation_md,difficulty,parent_id,shared_asset_key,unit_publisher,unit_subject,unit_code,unit_title,"
                   "unit_chapter,status,review_note", f)
        assets += rows("asset", "document_id,question_id,key,label,kind,scope,file,markdown,text", f)
    qids = [q["id"] for q in qs]
    for i in range(0, len(qids), 80):
        f = {"question_id": "in.(" + ",".join(qids[i:i + 80]) + ")"}
        opts += rows("option", "question_id,label,content_md,asset_file,ord", f)
        tags += rows("tag", "question_id,axis,value", f)
    sec = {(s["document_id"], s["ord"]): s["name"] for s in secs}
    O, A, T = collections.defaultdict(list), collections.defaultdict(list), collections.defaultdict(lambda: collections.defaultdict(list))
    shared = {}
    for o in opts:
        O[o["question_id"]].append(o)
    for x in assets:
        if x["question_id"]:
            A[x["question_id"]].append(x)
        else:
            shared[(x["document_id"], x["key"])] = x
    for t in tags:
        T[t["question_id"]][t["axis"]].append(t["value"])
    out = Path(a.out) / f"{a.batch}.jsonl"
    out.parent.mkdir(parents=True, exist_ok=True)
    n = 0
    with out.open("w", encoding="utf-8") as w:
        for q in sorted(qs, key=lambda q: (q["document_id"], q["section_ord"] or 0, q["number"] or 0)):
            d = docs[q["document_id"]]
            sa = shared.get((q["document_id"], q["shared_asset_key"])) if q.get("shared_asset_key") else None
            rec = {
                "id": q["id"], "paper_id": d["id"], "subject": d["subject"], "unit_subject": q["unit_subject"],
                "grade": d["grade"], "semester": d["semester"], "exam_seq": d["exam_seq"], "school": d["school"],
                "scope_note": d["scope_note"], "publisher": d["publisher"],
                "section_name": sec.get((q["document_id"], q["section_ord"])), "number": q["number"], "type": q["type"],
                "status": q["status"], "group_id": q["parent_id"], "group_stem": q["group_stem"],
                "shared_text": (sa or {}).get("text"), "stem": q["stem_md"],
                "options": [{"label": o["label"], "text": o["content_md"], "figure": o["asset_file"]}
                            for o in sorted(O[q["id"]], key=lambda o: o["ord"] or 0)],
                "figures": [x["file"] for x in A[q["id"]] if x["file"]] + ([sa["file"]] if sa and sa.get("file") else []),
                "tables": [x["markdown"] for x in A[q["id"]] if x.get("markdown")] + ([sa["markdown"]] if sa and sa.get("markdown") else []),
                "answer": q["answer"], "answer_status": q["answer_status"], "answer_source": q["answer_source"],
                "explanation": q["explanation_md"],
                "unit": ({"publisher": q["unit_publisher"], "code": q["unit_code"], "title": q["unit_title"],
                          "chapter": q["unit_chapter"]} if q["unit_code"] else None),
                "pattern": (T[q["id"]].get("pattern") or [None])[0],
                "difficulty": q["difficulty"], "review_note": q["review_note"],
            }
            w.write(json.dumps(rec, ensure_ascii=False) + "\n")
            n += 1
    print(f"{a.batch}：{len(docs)} 卷、{n} 題 → {out}")
    return 0


def cmd_image(a) -> int:
    f = a.file.replace("\\", "/").lstrip("/")
    h = hashlib.sha1(f.encode("utf-8")).hexdigest()
    b = get(f"/storage/v1/object/authenticated/assets/{h[:2]}/{h[2:26]}.png", raw=True)
    Path(a.out).write_bytes(b)
    print(f"{f} → {a.out}（{len(b):,} bytes）")
    return 0


TEXT = {"ok", "issue"}
ANS = {"agree", "fix", "unsure", "none"}
OKFIX = {"ok", "fix"}


def cmd_check(a) -> int:
    work = {json.loads(l)["id"]: json.loads(l) for l in Path(a.work).read_text(encoding="utf-8").splitlines() if l.strip()}
    seen = collections.Counter()
    err = 0

    def bad(msg):
        nonlocal err
        err += 1
        print("✗", msg)

    stats = collections.Counter()
    for n, line in enumerate(Path(a.review).read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            bad(f"第 {n} 行不是 JSON"); continue
        qid = r.get("id")
        if qid not in work:
            bad(f"第 {n} 行 id 不在這一批：{qid}"); continue
        seen[qid] += 1
        q = work[qid]
        if r.get("text") not in TEXT:
            bad(f"{qid}：text 要是 ok／issue")
        elif r["text"] == "issue" and not r.get("text_issue"):
            bad(f"{qid}：text 是 issue，text_issue 要寫哪裡有問題")
        av = r.get("answer")
        if av not in ANS:
            bad(f"{qid}：answer 要是 agree／fix／unsure／none")
        elif av == "fix" and (not isinstance(r.get("answer_fix"), list) or not r["answer_fix"] or not r.get("answer_reason")):
            bad(f"{qid}：answer 是 fix，要有 answer_fix（陣列）與 answer_reason")
        elif av == "agree" and not q.get("answer"):
            bad(f"{qid}：原本沒有答案，不能 agree；請用 fix 填答案或 unsure")
        uv = r.get("unit")
        if uv not in OKFIX:
            bad(f"{qid}：unit 要是 ok／fix")
        elif uv == "fix" and not re.fullmatch(r"\d-\d+", str(r.get("unit_fix", "")).strip()):
            bad(f"{qid}：unit 是 fix，unit_fix 要寫單元代號（例如 2-3）")
        d = r.get("difficulty")
        if d not in (1, 2, 3, 4, 5):
            bad(f"{qid}：difficulty 要是 1～5 的整數")
        if q["subject"] == "數學":
            pv = r.get("pattern")
            if pv not in OKFIX | {"none"}:
                bad(f"{qid}：數學題 pattern 要是 ok／fix／none")
            elif pv == "fix" and not re.fullmatch(r"\d-\d_\d-\d+:(T\d+|NEW:.+)", str(r.get("pattern_fix", ""))):
                bad(f"{qid}：pattern_fix 格式是 7-1_1-2:T03（沒有合適題型寫 7-1_1-2:NEW:題型名稱）")
        stats[f"answer_{av}"] += 1
        stats[f"text_{r.get('text')}"] += 1
        stats[f"unit_{uv}"] += 1
        stats[f"d{d}"] += 1
    for qid in work:
        if seen[qid] == 0:
            bad(f"{qid}：漏審")
        elif seen[qid] > 1:
            bad(f"{qid}：出現 {seen[qid]} 次")
    print(f"{len(work)} 題；" + "、".join(f"{k} {v}" for k, v in sorted(stats.items())))
    print(f"錯誤 {err} 項")
    return 1 if err else 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sp = ap.add_subparsers(dest="cmd", required=True)
    sp.add_parser("batches")
    f = sp.add_parser("fetch"); f.add_argument("batch"); f.add_argument("-o", "--out", default="work")
    i = sp.add_parser("image"); i.add_argument("file"); i.add_argument("-o", "--out", required=True)
    c = sp.add_parser("check"); c.add_argument("work"); c.add_argument("review")
    a = ap.parse_args()
    return {"batches": cmd_batches, "fetch": cmd_fetch, "image": cmd_image, "check": cmd_check}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
