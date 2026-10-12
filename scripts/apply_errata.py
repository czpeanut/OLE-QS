#!/usr/bin/env python3
"""把 AI 初審找到、並經複核證實的「原卷印錯」寫回題庫，題目最後加註「（原卷有誤，已修正）」。

只套用 sure＝true 且已證實（原卷文字層確實這樣印，或模型照抄原卷也是這樣）的 errata。
同時改題庫 YAML、data/oleqs.db 與 Supabase；每筆改動記在 out/ai_review/errata_applied.json，可據以還原。

    python scripts/apply_errata.py --codes-root <站點輸出根目錄>            # 只列出會改什麼
    python scripts/apply_errata.py --codes-root <站點輸出根目錄> --apply    # 寫回
"""

from __future__ import annotations

import argparse
import difflib
import json
import re
import sqlite3
import sys
import time
import unicodedata
from pathlib import Path

import requests
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from ai_review import RESULTS, load_codes  # noqa: E402
from batch_api import _env  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
BANK = REPO / "data" / "bank"
DB = REPO / "data" / "oleqs.db"
LOG = REPO / "out" / "ai_review" / "errata_applied.json"
MARK = "（原卷有誤，已修正）"
SKIP = set(" \t\r\n　$_＿")


def index_norm(s: str) -> tuple[str, list[int]]:
    """正規化後的字串，以及每個字對應原字串的位置（比對時忽略空白、$、底線與全半形差異）。"""
    out, pos = [], []
    for i, ch in enumerate(s):
        if ch in SKIP:
            continue
        for c in unicodedata.normalize("NFKC", ch):
            if c not in SKIP:
                out.append(c)
                pos.append(i)
    return "".join(out), pos


def edits(got: str, fix: str) -> list[tuple[str, str, str, str]]:
    """got→fix 的最小改動：[(左文, 舊字, 新字, 右文)]，左右各帶 4 字上下文用來定位。"""
    # 引文跨選項時會帶「(B)」這類標號，題庫的選項內容裡沒有，先拿掉
    g, _ = index_norm(re.sub(r"\([A-E]\)", "", got))
    f, _ = index_norm(re.sub(r"\([A-E]\)", "", fix))
    out = []
    for op, i1, i2, j1, j2 in difflib.SequenceMatcher(None, g, f).get_opcodes():
        if op != "equal":
            out.append((g[max(0, i1 - 4):i1], g[i1:i2], f[j1:j2], g[i2:i2 + 4]))
    return out


def replace_in(text: str, left: str, old: str, new: str, right: str) -> str | None:
    """在 text 裡找唯一一處「左文＋舊字＋右文」，把舊字換成新字；找不到或不只一處回傳 None。"""
    n, pos = index_norm(text)
    key = left + old + right
    at = n.find(key)
    if at < 0 or n.find(key, at + 1) >= 0:
        return None
    s = at + len(left)
    if old:
        a, b = pos[s], pos[s + len(old) - 1] + 1
    else:                                         # 純插入：插在左文最後一字之後
        a = b = pos[s - 1] + 1 if s > 0 else pos[s]
    return text[:a] + new + text[b:]


def fields(q: dict) -> list[tuple[str, int | None]]:
    return [("stem", None), ("group_stem", None)] + [("option", i) for i in range(len(q.get("options") or []))]


def get(q, f, i):
    return (q["options"][i].get("content") or "") if f == "option" else (q.get(f) or "")


def put(q, f, i, v):
    if f == "option":
        q["options"][i]["content"] = v
    else:
        q[f] = v


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--codes-root", type=Path, required=True)
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()
    q2c, _ = load_codes(args.codes_root)
    c2q = {c: q for q, c in q2c.items()}
    done = json.loads(LOG.read_text(encoding="utf-8")) if LOG.is_file() else []
    seen = {(x["qid"], x["from"]) for x in done}

    todo: dict[str, list[tuple[Path, dict]]] = {}
    for f in sorted(RESULTS.glob("*.json")):
        r = json.loads(f.read_text(encoding="utf-8"))
        for it in r.get("errata") or []:
            if it.get("drop") or not it.get("sure") or not it.get("checked") or it.get("applied"):
                continue
            todo.setdefault(r["doc"], []).append((f, it))

    changes, failed = [], []
    for doc, items in todo.items():
        yp = BANK / f"{doc}.yaml"
        d = yaml.safe_load(yp.read_text(encoding="utf-8"))
        by_id = {q["id"]: q for q in d.get("questions") or []}
        touched = False
        for f, it in items:
            qid = c2q.get(it["code"].strip().strip("[]"))
            q = by_id.get(qid)
            if q is None or (qid, it["got"]) in seen:
                continue
            before = {k: get(q, k, i) for k, i in fields(q) if k != "option"}
            before["options"] = [o.get("content") for o in q.get("options") or []]
            todo_edits = edits(it["got"], it["fix"])
            ok = bool(todo_edits)                     # 只差在選項標號之類題庫不存的地方，就不動
            for left, old, new, right in todo_edits:
                hit = None
                # 上下文可能跨到相鄰選項，定位不到就縮短上下文再試（仍要求唯一）
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
                    ok = False
                    break
                k, i, v = hit
                if k == "group_stem":                 # 題組說明是同組各題共用的，一起改
                    old_gs = q.get("group_stem")
                    for other in by_id.values():
                        if other.get("group_stem") == old_gs:
                            other["group_stem"] = v
                else:
                    put(q, k, i, v)
            if not ok:
                failed.append((qid, it["got"], it["fix"]))
                continue
            if not (q.get("stem") or "").rstrip().endswith(MARK):
                q["stem"] = (q.get("stem") or "").rstrip() + MARK
            q.setdefault("errata", []).append({"from": it["got"], "to": it["fix"], "note": it.get("note", ""),
                                               "by": "ai_review"})
            changes.append({"doc": doc, "qid": qid, "code": it["code"], "from": it["got"], "to": it["fix"],
                            "note": it.get("note", ""), "before": before, "at": time.strftime("%Y-%m-%d %H:%M:%S"),
                            "after": {"stem": q.get("stem"), "group_stem": q.get("group_stem"),
                                      "options": [o.get("content") for o in q.get("options") or []]}})
            it["applied"] = True
            touched = True
            print(f"  {it['code']}  {it['got'][:30]} → {it['fix'][:30]}")
        if touched and args.apply:
            yp.write_text(yaml.safe_dump(d, allow_unicode=True, sort_keys=False), encoding="utf-8")
            for f in {f for f, _ in items}:
                r = json.loads(f.read_text(encoding="utf-8"))
                applied = {(x["code"], x["got"]) for _, x in items if x.get("applied")}
                for x in r.get("errata") or []:
                    if (x["code"], x["got"]) in applied:
                        x["applied"] = True
                f.write_text(json.dumps(r, ensure_ascii=False, indent=1), encoding="utf-8")

    print(f"可修正 {len(changes)} 題；定位不到 {len(failed)} 筆")
    for x in failed[:20]:
        print("  定位不到：", x)
    if not args.apply or not changes:
        return 0

    # 資料庫與 Supabase：題幹、題組說明（同組各題）、選項、審題備註
    con = sqlite3.connect(DB)
    url, key = _env("SUPABASE_URL").rstrip("/"), _env("SUPABASE_SERVICE_ROLE_KEY")
    H = {"apikey": key, "Authorization": f"Bearer {key}", "Content-Type": "application/json", "Prefer": "return=minimal"}
    s = requests.Session()
    for c in changes:
        a, b = c["before"], c["after"]
        note = f"原卷有誤已修正：{c['from']} → {c['to']}"
        row = {"stem_md": b["stem"], "review_note": note}
        con.execute("update question set stem_md=?, review_note=? where id=?", (b["stem"], note, c["qid"]))
        s.patch(f"{url}/rest/v1/question", params={"id": f"eq.{c['qid']}"}, headers=H, json=row,
                timeout=60).raise_for_status()
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
    LOG.parent.mkdir(parents=True, exist_ok=True)
    LOG.write_text(json.dumps(done + changes, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"已寫回 {len(changes)} 題（YAML、oleqs.db、Supabase）；紀錄在 {LOG.relative_to(REPO)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
