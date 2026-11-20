#!/usr/bin/env python3
"""把國中逐題審題結果（REVIEW_*.jsonl）驗收後寫回 Supabase。

  python scripts/junior_apply_review.py plan  <審題資料夾…> --db-cache db.json   # 試跑：只算計畫與統計，不寫
  python scripts/junior_apply_review.py apply <審題資料夾…> --db-cache db.json   # 執行（先存回復檔）

審題資料夾可給多個，後面的覆蓋前面的（同一題以最新一包為準）。--db-cache 是 {題目id: 資料庫列} 的 JSON
（含 option(label)、document 的 grade/semester/subject/sub_subject）；沒給就即時抓。

寫回規則（題庫負責人 2026-11-20 核准）：
- 不寫：text＝issue；選擇題沒有選項；答案格式不合（字母不在選項、單選多答案）。
- 答案：原本沒有答案、agent 補上 → ai_generated／agent。agent 推翻原答案 → disputed／agent_fix，原答案記在 review_note
  （「風險題」：推翻答案卷答案、或作答格數變少——保留原答案，只標 disputed 並把 agent 建議寫在 review_note）。
- 難度：只補資料庫裡沒有難度的題。
- 單元：unit＝fix 且解得出（版本、科目、代號對得到單元表）且與現有不同才改。
- 數學題型：pattern＝fix 且 pattern_fix 在題型目錄裡才改（tag 表 axis＝pattern，labeled_by＝agent）。
"""
from __future__ import annotations

import argparse, collections, concurrent.futures as cf, gzip, json, re, sys, time, urllib.parse, urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from junior_review_tool import env, get  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
OUT = REPO / "out"
TAX = REPO / "data/export/taxonomy"


def L(a):
    return a if isinstance(a, list) else ([] if a is None else [a])


# ---------- 單元對照 ----------
def load_units():
    cur = json.loads((TAX / "curriculum.json").read_text(encoding="utf-8"))
    tab = {}      # (grade, sem, subject, sub, publisher) -> {code: (title, chapter)}
    for g, sems in cur["junior"].items():
        for s, subs in sems.items():
            for subj, bysub in subs.items():
                for sub, pubs in bysub.items():
                    for pub, items in pubs.items():
                        m = {}
                        for u in items:
                            if subj == "國文":
                                m[u["lesson"]] = (u["title"], None)
                            else:
                                m[u["section"]] = (u["title"], f"第{u['chapter']}章 {u['chapter_title']}")
                        tab[(int(g), int(s), subj, sub, pub)] = m
    eng = {t["id"]: (t["title"], t["group"]) for t in cur["english"]}
    return tab, eng


def resolve_unit(r, d, tab, eng):
    """回傳 (publisher, unit_subject, code, title, chapter) 或 None。"""
    doc = d["document"]
    subj = doc["subject"]
    u = str(r.get("unit_fix") or "").strip()
    if not u:
        return None
    if subj == "英語":
        return (None, "英語", u, *eng[u]) if u in eng else None
    g, s = doc["grade"], doc["semester"]
    book = str(r.get("unit_fix_book") or "")
    m = re.match(r"^([789])-([12])", book)
    if m:
        g, s = int(m.group(1)), int(m.group(2))
    pub = d.get("unit_publisher") or "翰林"
    if subj == "國文":
        for p in (pub, "翰林"):
            t = tab.get((g, s, "國文", "", p), {}).get(u)
            if t:
                return (p, "國文", u, t[0], None)
        return None
    if subj == "數學":
        sub = ""
    else:
        sub = r.get("unit_fix_sub") or ""
        if not sub:
            # 書名後面可能帶子科（「8-1 理化」）
            tail = book.split(" ", 1)[1].strip() if " " in book else ""
            sub = tail or doc.get("sub_subject") or d.get("unit_subject") or ""
        if sub == "理化" and (g, s, subj, "理化", pub) not in tab and (g, s, subj, "物理", pub) in tab:
            sub = "物理"
    cands = [sub] if sub else []
    if subj == "自然" and sub == "地科":
        cands += ["地球科學"]
    for sb in cands or [""]:
        for p in (pub, "翰林"):
            t = tab.get((g, s, subj, sb, p), {}).get(u)
            if t:
                return (p, "數學" if subj == "數學" else sb, u, t[0], t[1])
    return None


# ---------- 讀審題 ----------
def read_reviews(dirs):
    R, src = {}, {}
    for d in dirs:
        for f in sorted(Path(d).rglob("REVIEW_*.jsonl")):
            for line in f.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    r = json.loads(line)
                    R[r["id"]] = r
                    src[r["id"]] = f.stem[7:]
    return R, src


def fetch_db(ids):
    def chunk(c):
        return get("/rest/v1/question", {
            "select": "id,document_id,type,answer,answer_status,answer_source,unit_publisher,unit_subject,unit_code,unit_title,unit_chapter,"
                      "difficulty,status,review_note,option(label),document!document_id!inner(grade,semester,subject,sub_subject)",
            "id": "in.(" + ",".join(c) + ")"})
    out = {}
    ids = sorted(ids)
    with cf.ThreadPoolExecutor(8) as ex:
        for res in ex.map(chunk, [ids[i:i + 100] for i in range(0, len(ids), 100)]):
            for q in res:
                out[q["id"]] = q
    return out


# ---------- 計畫 ----------
def build(R, D):
    tab, eng = load_units()
    mp = json.loads((TAX / "math_patterns.json").read_text(encoding="utf-8"))
    risky = {}
    csvp = REPO / "docs/junior_review_round2_risky_answers.csv"
    if csvp.is_file():
        import csv
        for row in csv.DictReader(csvp.open(encoding="utf-8-sig")):
            risky[row["題目id"]] = row["類別"]
    plan = {}          # id -> patch dict
    pat = {}           # id -> pattern value
    st = collections.defaultdict(collections.Counter)      # subject -> counter
    notes = collections.defaultdict(list)

    def S(subj, k, n=1):
        st[subj][k] += n
        st["全部"][k] += n

    for i, r in R.items():
        d = D.get(i)
        if not d:
            S("?", "資料庫找不到")
            continue
        subj = d["document"]["subject"]
        labs = {o["label"] for o in d["option"]}
        choice = d["type"] in ("single", "multiple")
        S(subj, "審題題數")
        # --- 題目文字
        if r["text"] == "issue":
            S(subj, "題目文字有問題")
            if choice and not labs:
                S(subj, "　其中：選擇題缺選項")
        noopt = choice and not labs
        skip_all = r["text"] == "issue" or noopt
        if noopt and r["text"] == "ok":
            S(subj, "缺選項卻標ok")
        # --- 答案
        a = r["answer"]
        S(subj, "答案_" + a)
        p = plan.setdefault(i, {})
        if a == "fix":
            new, old = L(r.get("answer_fix")), L(d["answer"])
            bad = (choice and labs and (any(x not in labs for x in new) or (d["type"] == "single" and len(new) != 1)))
            if not old:
                S(subj, "答案_補缺")
            else:
                S(subj, "答案_訂正")
            if skip_all:
                S(subj, "答案修正_未寫回(題目有問題)")
            elif bad:
                S(subj, "答案修正_未寫回(格式不合)")
            elif not old:
                p.update(answer=new, answer_status="ai_generated", answer_source="agent")
                S(subj, "答案修正_寫回_補缺")
            elif i in risky:
                p.update(answer_status="disputed", review_note=f"審題 agent 建議改為 {new}（未採用，保留原答案）：{r.get('answer_reason', '')}"[:500])
                S(subj, "答案修正_風險題_只標爭議")
            elif new != old:
                p.update(answer=new, answer_status="disputed", answer_source="agent_fix",
                         review_note=f"原答案 {old}，審題 agent 修正為 {new}：{r.get('answer_reason', '')}"[:500])
                S(subj, "答案修正_寫回_訂正")
            else:
                S(subj, "答案修正_與原答案相同")
        # --- 難度
        if not skip_all and r.get("difficulty") and d["difficulty"] is None:
            p["difficulty"] = int(r["difficulty"])
            S(subj, "補難度")
        # --- 單元
        if r["unit"] == "fix":
            if skip_all:
                S(subj, "單元修正_未寫回(題目有問題)")
            else:
                u = resolve_unit(r, d, tab, eng)
                if not u:
                    S(subj, "單元修正_對不到單元表")
                    if len(notes["單元對不到"]) < 30:
                        notes["單元對不到"].append((i, r.get("unit_fix"), r.get("unit_fix_book"), r.get("unit_fix_sub")))
                elif (u[2], u[1]) == (d["unit_code"], d["unit_subject"]) and (u[0] or None) == (d["unit_publisher"] or None) or u[2] == d["unit_code"] and u[1] == d["unit_subject"]:
                    S(subj, "單元修正_與現有相同")
                else:
                    p.update(unit_publisher=u[0], unit_subject=u[1], unit_code=u[2], unit_title=u[3], unit_chapter=u[4])
                    S(subj, "單元修正_寫回")
        # --- 數學題型
        if r.get("pattern") == "fix":
            v = r.get("pattern_fix") or ""
            m = re.match(r"^(.+):(T\d+)$", v)
            if skip_all:
                S(subj, "題型修正_未寫回(題目有問題)")
            elif m and m.group(1) in mp and any(t["code"] == m.group(2) for t in mp[m.group(1)]["types"]):
                pat[i] = v
                S(subj, "題型修正_寫回")
            else:
                S(subj, "題型修正_對不到題型目錄")
        if not p:
            plan.pop(i, None)
    return plan, pat, st, notes


# ---------- 寫入 ----------
def req(method, path, body=None, params=None, prefer="return=minimal"):
    url, key = env()
    q = ("?" + urllib.parse.urlencode(params, safe="(),.*:")) if params else ""
    data = json.dumps(body, ensure_ascii=False).encode() if body is not None else None
    rq = urllib.request.Request(url + path + q, data=data, method=method, headers={
        "apikey": key, "Authorization": f"Bearer {key}", "Content-Type": "application/json", "Prefer": prefer})
    for attempt in range(5):
        try:
            with urllib.request.urlopen(rq, timeout=120) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            if e.code < 500 or attempt == 4:
                raise RuntimeError(f"{method} {path} {e.code} {e.read()[:200]!r}")
            time.sleep(3 * (attempt + 1))
        except Exception:  # noqa: BLE001
            if attempt == 4:
                raise
            time.sleep(3 * (attempt + 1))


def apply(plan, pat, D):
    OUT.mkdir(exist_ok=True)
    before = OUT / "junior_review_rollback.jsonl.gz"
    with gzip.open(before, "wt", encoding="utf-8") as f:
        for i, p in plan.items():
            f.write(json.dumps({"id": i, **{k: D[i].get(k) for k in p}}, ensure_ascii=False) + "\n")
    print("已存回復檔", before)
    # 相同內容的 patch 合併成一次請求（id in (...)）
    groups = collections.defaultdict(list)
    for i, p in plan.items():
        groups[json.dumps(p, ensure_ascii=False, sort_keys=True)].append(i)
    jobs = []
    for body, ids in groups.items():
        for k in range(0, len(ids), 60):
            jobs.append((json.loads(body), ids[k:k + 60]))

    def run(job):
        body, ids = job
        req("PATCH", "/rest/v1/question", body, {"id": "in.(" + ",".join(ids) + ")"})
        return len(ids)

    done = 0
    with cf.ThreadPoolExecutor(6) as ex:
        for n in ex.map(run, jobs):
            done += n
    print(f"question 已更新 {done:,} 題（{len(jobs):,} 次請求）")
    if pat:
        ids = sorted(pat)
        for k in range(0, len(ids), 60):
            c = ids[k:k + 60]
            req("DELETE", "/rest/v1/tag", params={"axis": "eq.pattern", "question_id": "in.(" + ",".join(c) + ")"})
            req("POST", "/rest/v1/tag", [{"question_id": i, "axis": "pattern", "value": pat[i], "is_primary": True,
                                          "labeled_by": "agent"} for i in c])
        print(f"題型 tag 已更新 {len(ids):,} 題")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["plan", "apply"])
    ap.add_argument("reviews", nargs="+", type=Path)
    ap.add_argument("--db-cache", type=Path)
    ap.add_argument("--out", type=Path, default=OUT / "junior_plan.json")
    a = ap.parse_args()
    R, src = read_reviews(a.reviews)
    if a.db_cache and a.db_cache.is_file():
        D = json.loads(a.db_cache.read_text(encoding="utf-8"))
    else:
        D = fetch_db(R)
        if a.db_cache:
            a.db_cache.write_text(json.dumps(D, ensure_ascii=False), encoding="utf-8")
    plan, pat, st, notes = build(R, D)
    OUT.mkdir(exist_ok=True)
    a.out.write_text(json.dumps({"plan": plan, "pattern": pat, "stats": st, "notes": notes}, ensure_ascii=False), encoding="utf-8")
    for subj in ("全部", "數學", "國文", "英語", "社會", "自然"):
        print(f"\n== {subj} ==")
        for k, v in sorted(st[subj].items()):
            print(f"  {k}: {v:,}")
    print(f"\n將改動題目 {len(plan):,} 題、題型 tag {len(pat):,} 題")
    if a.mode == "apply":
        apply(plan, pat, D)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
