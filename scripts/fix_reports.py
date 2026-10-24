#!/usr/bin/env python3
"""審題台上沒辦法用文字替換修正的回報（畫線、缺圖、題目錯置、符號…）：請模型對照原卷重新擷取該題。

apply_reports.py 處理完能直接替換文字的回報後，剩下的交給這支。每筆回報送出：原卷該題所在頁的影像、
題庫目前的擷取內容（題幹、題組短文、選項、附圖位置）、回報說明；模型回傳這一題正確的擷取內容，
需要改附圖時給出附圖在頁面上的位置（重新裁圖）。

    python scripts/fix_reports.py run --codes-root <站點輸出根目錄> --pdf-root <考卷根目錄> --budget-twd <上限>
    python scripts/fix_reports.py check                 # 列出模型的修正與自動檢查結果（不寫回）
    python scripts/fix_reports.py apply [--only 編號,…]  # 寫回題庫 YAML、data/oleqs.db、Supabase，回報標為已修正

模型輸出存在 out/fix_reports/<審題編號>.json（已有的不重送）。寫回前的自動檢查：
- 模型判定「已正確」或「改不了」（例如要新增整題、牽涉別題）的不改，列入交人工
- 題幹與選項的改動量過大（相似度太低）又不是題目錯置類的回報，不改，列入交人工
每筆寫回記在 out/review_applied.json（與 apply_reports.py 同一份），可據以還原。
"""

from __future__ import annotations

import argparse
import base64
import difflib
import json
import re
import sqlite3
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import fitz
import requests
from PIL import Image
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from apps.api.storage import BUCKET, storage_key  # noqa: E402
from apply_reports import LOG, load_reports, supa  # noqa: E402
from ai_review import load_codes  # noqa: E402
from apply_errata import MARK  # noqa: E402
from batch_api import Ledger  # noqa: E402
from generate_answers import load_env_file  # noqa: E402
from stage_triage_papers import ask_rotation  # noqa: E402
from vlm_extract import call_model, crop  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
BANK = REPO / "data" / "bank"
ASSETS = REPO / "data" / "assets"
DB = REPO / "data" / "oleqs.db"
OUT = REPO / "out" / "fix_reports"
MODEL = "gemini-3.7-flash"
PHASE = "fix_reports"
ROTATE_NOTE = re.compile(r"旋轉|顛倒|倒過來|轉向|轉正")
STRUCT_NOTE = re.compile(r"錯置|題號|大題|小題|題型|題組|拆|合併|填充題|計算題|單選|非選|多選")
STRUCTURAL = re.compile(r"錯置|順序|題號|漏題|漏擷取|整題|大題|合併|拆|移到|另一題|上一題|下一題")

PROMPT = """你是題庫校對人員。附上的是一份國中段考考卷的頁面影像，以及題庫裡某一題目前的擷取內容；
審題者回報這一題有問題（見【審題回報】）。請對照原卷影像，輸出這一題正確的擷取內容。

# 規則
- 照原卷逐字抄寫，不要改寫、補字、潤飾。回報沒提到、本來就正確的部分**維持原樣**（沿用目前的文字與 LaTeX 寫法）。
- 原卷用底線標出的字詞用 <u>…</u> 包起來；雙底線用 <u class="dbl">…</u>。
  只是留給學生作答的空格底線（＿＿＿、（　　））照原樣寫，不要加 <u>。
- 原卷的特殊符號（○●□■△▲☆★※→⇒∵∴…）寫成對應的 Unicode 字元；公式用行內 LaTeX（前後加 $）。
- 手寫的作答、批改痕跡不是題目內容。
- group_stem 是題組共用的短文或說明；這題不屬於題組就填 null。題組短文有錯也一併修正（會套用到同題組的每一題）。
- options 依原卷順序列出每個選項（label 寫 A、B、C、D…）；沒有選項的題目給空陣列。
- 附圖：只有回報與附圖有關（缺圖、圖裁錯、圖放錯題、圖該是選項圖…）時 figures_changed 才填 true，
  並在 figures 列出這一題**全部**應有的附圖：target 寫 stem（題幹的圖）、group（題組共用圖）或 option:A（選項圖），
  page 是頁碼（附上的頁面依序是第幾頁就填幾），box 是該頁影像上的位置 [ymin, xmin, ymax, xmax]（0～1000），
  框要包含圖內的標號與座標文字；rotate 是裁下來的圖要順時針轉幾度才是正的（通常 0）。
  表格若能轉成 Markdown 表格就寫進題幹，不必當圖。
- verdict：fixed＝已依原卷修正；already_ok＝目前的內容其實正確（回報不成立）；
  cannot＝無法只改這一題（例如原卷漏印、要新增或刪除整題、內容其實屬於別題、影像看不清楚）。
- note 用一句話說明改了什麼，或為什麼不改。"""

SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "verdict": {"type": "STRING", "enum": ["fixed", "already_ok", "cannot"]},
        "stem": {"type": "STRING"},
        "group_stem": {"type": "STRING", "nullable": True},
        "options": {"type": "ARRAY", "items": {"type": "OBJECT", "properties": {
            "label": {"type": "STRING"}, "content": {"type": "STRING"}}, "required": ["label", "content"]}},
        "figures_changed": {"type": "BOOLEAN"},
        "figures": {"type": "ARRAY", "items": {"type": "OBJECT", "properties": {
            "target": {"type": "STRING"}, "page": {"type": "INTEGER"},
            "box": {"type": "ARRAY", "items": {"type": "INTEGER"}},
            "rotate": {"type": "STRING", "enum": ["0", "90", "180", "270"]}}, "required": ["target", "page", "box"]}},
        "note": {"type": "STRING"},
    },
    "required": ["verdict", "stem", "options", "figures_changed", "note"],
}


def pdf_of(doc: dict, pdf_root: Path) -> Path | None:
    src = doc.get("source_file")
    return pdf_root / src if src and (pdf_root / src).is_file() else None


def pages_for(q: dict, by_id: dict, n_pages: int, wide: bool = False) -> list[int]:
    """這題所在頁；題組的題目再加上題組第一題所在頁（短文常在前一頁）。wide：前後頁都附（跨頁的題目）。"""
    ps = {q.get("page") or 1}
    if wide:
        ps |= {(q.get("page") or 1) - 1, (q.get("page") or 1) + 1}
    if q.get("group_stem"):
        firsts = [x.get("page") for x in by_id.values() if x.get("group_stem") == q["group_stem"] and x.get("page")]
        if firsts:
            ps.add(min(firsts))
            ps.add(min(firsts) - 1)                    # 短文常從前一頁開始
    for a in [*(q.get("assets") or []), *[o.get("asset") or {} for o in q.get("options") or []]]:
        if (a.get("crop") or {}).get("page"):
            ps.add(a["crop"]["page"])
    return sorted(p for p in ps if 1 <= p <= n_pages)[-4:]


def current(q: dict, shared: dict) -> dict:
    figs = []
    for a in q.get("assets") or []:
        figs.append({"target": "stem", **({"page": a["crop"]["page"]} if a.get("crop") else {})})
    if q.get("shared_asset") and q["shared_asset"] in shared:
        figs.append({"target": "group"})
    for o in q.get("options") or []:
        if o.get("asset"):
            figs.append({"target": f"option:{o['label']}"})
    return {"stem": q.get("stem"), "group_stem": q.get("group_stem"),
            "options": [{"label": o.get("label"), "content": o.get("content")} for o in q.get("options") or []],
            "figures": figs}


def load_doc(doc_id: str) -> tuple[Path, dict]:
    yp = BANK / f"{doc_id}.yaml"
    return yp, yaml.safe_load(yp.read_text(encoding="utf-8"))


def unresolved(codes_root: Path) -> list[dict]:
    q2c, _ = load_codes(codes_root)
    c2q = {c: q for q, c in q2c.items()}
    out = []
    for r in load_reports():
        d = r["data"]
        if d.get("resolved"):
            continue
        qid = d.get("qid") or c2q.get(d.get("code"))
        if qid:
            out.append({**r, "qid": qid})
    return out


def cmd_run(args) -> int:
    load_env_file()
    OUT.mkdir(parents=True, exist_ok=True)
    reps = unresolved(args.codes_root)
    by_code: dict[str, list[dict]] = {}
    for r in reps:
        by_code.setdefault(r["data"]["code"], []).append(r)
    todo = [c for c in by_code if not (OUT / f"{c}.json").is_file()]
    if args.only:
        todo = [c for c in todo if c in args.only.split(",")]
    if args.limit:                                      # 試跑：各類回報各挑幾題
        todo = sorted(todo, key=lambda c: hash(c) % 97)[: args.limit]
    print(f"未處理回報 {len(reps)} 筆（{len(by_code)} 題），待送 {len(todo)} 題；累計 NT${Ledger.twd():.0f}", flush=True)

    def one(code: str):
        if Ledger.twd() >= args.budget_twd:
            return code, None, 0.0
        rs = by_code[code]
        qid = rs[0]["qid"]
        doc_id = qid.rsplit("_s", 1)[0]
        try:
            _, d = load_doc(doc_id)
        except FileNotFoundError:
            return code, {"verdict": "cannot", "note": "找不到題庫檔"}, 0.0
        by_id = {x["id"]: x for x in d.get("questions") or []}
        q = by_id.get(qid)
        pdf = pdf_of(d["document"], args.pdf_root)
        if q is None or pdf is None:
            return code, {"verdict": "cannot", "note": "找不到題目或原卷"}, 0.0
        doc = fitz.open(pdf)
        pages = pages_for(q, by_id, len(doc), args.wide)
        parts = [{"text": PROMPT}]
        for i, p in enumerate(pages, start=1):
            img = doc[p - 1].get_pixmap(dpi=150).tobytes("jpeg", jpg_quality=82)
            parts += [{"text": f"\n【第{i}頁影像】（原卷第 {p} 頁）"},
                      {"inline_data": {"mime_type": "image/jpeg", "data": base64.b64encode(img).decode()}}]
        shared = {a["key"]: a for a in d.get("shared_assets") or []}
        notes = "\n".join(f"- {r['data'].get('note') or '（沒有說明）'}（分類：{'、'.join(r['data'].get('cats') or [])}）"
                          for r in rs)
        parts.append({"text": f"\n【題號】第 {q.get('number')} 題\n【目前的擷取內容】\n"
                              f"{json.dumps(current(q, shared), ensure_ascii=False, indent=1)}\n【審題回報】\n{notes}"})
        try:
            out, model, usd = call_model(parts, MODEL, schema=SCHEMA, thinking="medium",
                                         fallback=[MODEL, "gemini-3.6-flash", "gemini-3.8-flash"])
        except Exception as exc:  # noqa: BLE001
            print(f"  {code} 失敗：{str(exc)[:120]}", flush=True)
            return code, None, 0.0
        out["_pages"] = pages
        out["_qid"] = qid
        out["_reports"] = [{"site": r["site"], "id": r["id"], "note": r["data"].get("note"),
                            "cats": r["data"].get("cats")} for r in rs]
        return code, out, usd

    spent = 0.0
    with ThreadPoolExecutor(args.workers) as pool:
        for n, (code, out, usd) in enumerate(pool.map(one, todo), start=1):
            spent += usd
            if out is not None:
                (OUT / f"{code}.json").write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
            if n % 50 == 0:
                Ledger.add(PHASE, spent)
                spent = 0.0
                print(f"  {n}/{len(todo)}，累計 NT${Ledger.twd():.0f}", flush=True)
    Ledger.add(PHASE, spent)
    print(f"完成；累計 NT${Ledger.twd():.0f}")
    return 0


def plain(s: str | None) -> str:
    return re.sub(r"</?u[^>]*>|\s", "", s or "")


def keep_errata(res: dict, q: dict) -> None:
    """模型照原卷重抄時會把先前「原卷有誤，已修正」改好的字抄回原卷的錯字：把那些修正重新套上，並保留註記。"""
    if res.get("_errata_kept") or res.get("verdict") != "fixed":
        return
    res["_errata_kept"] = True
    had_mark = MARK in (q.get("stem") or "")
    res["stem"] = (res.get("stem") or "").replace(MARK, "").rstrip()
    for e in q.get("errata") or []:
        if e.get("extraction") or not e.get("from") or not e.get("to"):
            continue
        old, new = (re.sub(r"^\s*[(（][A-Z][)）]\s*", "", e[k]) for k in ("from", "to"))
        for o in [res, *(res.get("options") or [])]:
            for k in ("stem", "group_stem", "content"):
                v = o.get(k)
                if v and old in v and new not in v:
                    o[k] = v.replace(old, new)
    if had_mark:
        res["stem"] += MARK


def judge(res: dict, q: dict) -> str | None:
    """自動檢查：回傳 None＝可寫回，否則是交人工的原因。"""
    if res.get("_manual"):
        return res["_manual"]
    if res.get("verdict") != "fixed":
        return {"already_ok": "模型判定目前內容正確", "cannot": "模型判定無法只改這一題"}.get(res.get("verdict"), "沒有結果")
    if res.get("_approved"):                         # 逐筆看過、確認改動量大是合理的（補漏擷取的短文、改附圖等）
        return None
    old = plain(q.get("stem")) + "".join(plain(o.get("content")) for o in q.get("options") or [])
    new = plain(res.get("stem")) + "".join(plain(o.get("content")) for o in res.get("options") or [])
    ratio = difflib.SequenceMatcher(None, old, new, autojunk=False).ratio() if old or new else 1.0
    notes = " ".join(r.get("note") or "" for r in res["_reports"])
    if ratio < 0.6 and not STRUCTURAL.search(notes):
        return f"改動過大（相似度 {ratio:.0%}）"
    if q.get("type") in ("single", "multiple") and len(res.get("options") or []) < 2 <= len(q.get("options") or []) \
            and not STRUCTURAL.search(notes):
        return "選項被拿掉"
    if plain(res.get("group_stem")) == "" and plain(q.get("group_stem")) and not STRUCTURAL.search(notes):
        return "題組短文被拿掉"
    return None


def changed(res: dict, q: dict) -> bool:
    if res.get("figures_changed"):
        return True
    if (res.get("stem") or "") != (q.get("stem") or ""):
        return True
    if (res.get("group_stem") or None) != (q.get("group_stem") or None):
        return True
    return [o.get("content") for o in res.get("options") or []] != [o.get("content") for o in q.get("options") or []]


def results() -> list[tuple[str, dict]]:
    return [(f.stem, json.loads(f.read_text(encoding="utf-8"))) for f in sorted(OUT.glob("*.json"))]


def cmd_check(args) -> int:
    ok = manual = same = 0
    reasons: dict[str, int] = {}
    for code, res in results():
        _, d = load_doc(res["_qid"].rsplit("_s", 1)[0])
        q = next((x for x in d["questions"] if x["id"] == res["_qid"]), None)
        if q:
            keep_errata(res, q)
        why = judge(res, q) if q else "找不到題目"
        if why:
            manual += 1
            reasons[why.split("（")[0]] = reasons.get(why.split("（")[0], 0) + 1
        elif not changed(res, q):
            same += 1
        else:
            ok += 1
            if args.verbose:
                print(f"{code}｜{res['note'][:80]}")
    print(f"可寫回 {ok} 題；內容不變 {same} 題；交人工 {manual} 題 {reasons}")
    return 0


def fig_files(doc_id: str, q: dict, res: dict, pdf: Path) -> dict[str, list[tuple[str, dict]]]:
    """依模型給的位置重新裁圖 → {target: [(檔名, crop)]}。"""
    doc = fitz.open(pdf)
    stamp = time.strftime("%m%d%H%M")
    out: dict[str, list[tuple[str, dict]]] = {}
    for j, f in enumerate(res.get("figures") or [], start=1):
        pages = res["_pages"]
        if not (1 <= f.get("page", 0) <= len(pages)) or len(f.get("box") or []) != 4:
            continue
        pno = pages[f["page"] - 1]
        key = f"s{q['section']}_{q['number']}_r{stamp}_{j}"
        rel = f"{doc_id}/{key}.png"
        rect = crop(doc[pno - 1], f["box"], ASSETS / rel)
        notes = " ".join(r.get("note") or "" for r in res["_reports"])
        if rect and (int(f.get("rotate") or 0) or ROTATE_NOTE.search(notes)):
            # 模型給的旋轉角度不可靠（常轉反），改用四個方向的縮圖比較判斷
            im = Image.open(ASSETS / rel).convert("RGB")
            turn, _ = ask_rotation(im)
            if turn:
                im.rotate(-turn, expand=True).save(ASSETS / rel)
        if rect:
            out.setdefault(f["target"], []).append((rel, {"page": pno, "rect": rect}))
    return out


def cmd_apply(args) -> int:
    load_env_file()
    only = set(args.only.split(",")) if args.only else None
    url, H = supa()
    H = {**H, "Prefer": "return=minimal"}
    s = requests.Session()
    con = sqlite3.connect(DB)
    by_doc: dict[str, list[tuple[str, dict]]] = {}
    for code, res in results():
        if only and code not in only:
            continue
        if res.get("_applied"):
            continue
        by_doc.setdefault(res["_qid"].rsplit("_s", 1)[0], []).append((code, res))
    for doc_id, items in by_doc.items():
        yp, d = load_doc(doc_id)
        by_id = {x["id"]: x for x in d.get("questions") or []}
        shared = {a["key"]: a for a in d.get("shared_assets") or []}
        pdf = pdf_of(d["document"], args.pdf_root)
        done: list[tuple[str, dict]] = []
        for code, res in items:
            q = by_id.get(res["_qid"])
            if q is None:
                continue
            keep_errata(res, q)
            why = judge(res, q)
            notes = " ".join(r.get("note") or "" for r in res["_reports"])
            if (why == "模型判定目前內容正確" or (why is None and not changed(res, q))) and STRUCT_NOTE.search(notes):
                continue                              # 題號、大題、題型這類結構問題，文字沒錯不代表回報不成立
            if why == "模型判定目前內容正確" or (why is None and not changed(res, q)):
                res.update(_applied="already_ok", _what="已是正確寫法（重新對照原卷）")
                (OUT / f"{code}.json").write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
                continue
            if why:
                continue
            before = {"stem": q.get("stem"), "group_stem": q.get("group_stem"),
                      "options": [o.get("content") for o in q.get("options") or []],
                      "assets": [a.get("file") for a in q.get("assets") or []]}
            old_gs = q.get("group_stem")
            q["stem"] = res["stem"] if res["stem"].strip() or not res.get("figures_changed") else "（題目見附圖）"
            new_gs = res.get("group_stem") or None
            # 題組短文（或補充了內容的大題說明）改了就套用到整個題組；
            # 只是大題標題、而模型換成另一段標題時（例如這題被掛錯大題說明），只改這一題
            shared_gs = old_gs and (len(plain(old_gs)) >= 60 or plain(old_gs) in plain(new_gs))
            if old_gs and new_gs != old_gs and shared_gs and new_gs:
                for x in by_id.values():
                    if x.get("group_stem") == old_gs:
                        x["group_stem"] = new_gs
            elif new_gs != old_gs and (new_gs or not shared_gs):
                q["group_stem"] = new_gs
            new_opts = res.get("options") or []
            if len({o["label"] for o in new_opts}) < len(new_opts):      # 模型偶爾給重複代號（原卷印錯）：依序重編
                new_opts = [{**o, "label": "ABCDEFGHIJKLMNOP"[i]} for i, o in enumerate(new_opts)]
            if [o["label"] for o in new_opts] == [o.get("label") for o in q.get("options") or []]:
                for o, n in zip(q.get("options") or [], new_opts):
                    o["content"] = n["content"]
                opts_replaced = False
            else:
                keep = {o.get("label"): o.get("asset") for o in q.get("options") or []}
                q["options"] = [{"label": n["label"], "content": n["content"],
                                 **({"asset": keep[n["label"]]} if keep.get(n["label"]) else {})} for n in new_opts]
                opts_replaced = True
            figs = fig_files(doc_id, q, res, pdf) if res.get("figures_changed") and pdf else {}
            if res.get("figures_changed") and pdf:
                q["assets"] = [{"key": Path(rel).stem, "kind": "figure", "file": rel, "crop": c}
                               for rel, c in figs.get("stem", [])]
                if not q["assets"]:
                    q.pop("assets")
                for o in q.get("options") or []:
                    got = figs.get(f"option:{o['label']}")
                    if got:
                        o["asset"] = {"key": f"opt{o['label']}", "file": got[0][0], "crop": got[0][1]}
                if figs.get("group") and q.get("shared_asset") in shared:
                    sa = shared[q["shared_asset"]]
                    sa["file"], sa["crop"] = figs["group"][0]
                res["_uploads"] = [rel for v in figs.values() for rel, _ in v]
            q.setdefault("errata", []).append({"from": (before["stem"] or "")[:200], "to": (q["stem"] or "")[:200],
                                               "note": "；".join(r["note"] or "" for r in res["_reports"])[:200],
                                               "by": "review", "extraction": True})
            # ── 資料庫 ──
            qid = q["id"]
            rn = f"擷取錯誤已修正（審題回報，重新對照原卷）：{res['note']}"[:500]
            con.execute("update question set stem_md=?, review_note=? where id=?", (q["stem"], rn, qid))
            s.patch(f"{url}/rest/v1/question", params={"id": f"eq.{qid}"}, headers=H,
                    json={"stem_md": q["stem"], "review_note": rn}, timeout=60).raise_for_status()
            if (q.get("group_stem") or None) != (old_gs or None):
                if old_gs and shared_gs and new_gs:
                    con.execute("update question set group_stem=? where document_id=? and group_stem=?",
                                (q["group_stem"], doc_id, old_gs))
                    s.patch(f"{url}/rest/v1/question", params={"document_id": f"eq.{doc_id}",
                                                                 "group_stem": f"eq.{old_gs}"},
                            headers=H, json={"group_stem": q["group_stem"]}, timeout=60).raise_for_status()
                else:
                    con.execute("update question set group_stem=? where id=?", (q["group_stem"], qid))
                    s.patch(f"{url}/rest/v1/question", params={"id": f"eq.{qid}"}, headers=H,
                            json={"group_stem": q["group_stem"]}, timeout=60).raise_for_status()
            rows = con.execute("select id, label from option where question_id=? order by ord", (qid,)).fetchall()
            if opts_replaced or len(rows) != len(q.get("options") or []):
                con.execute("delete from option where question_id=?", (qid,))
                s.delete(f"{url}/rest/v1/option", params={"question_id": f"eq.{qid}"}, headers=H,
                         timeout=60).raise_for_status()
                new_rows = [{"question_id": qid, "label": o["label"], "content_md": o["content"],
                             "asset_file": (o.get("asset") or {}).get("file"), "ord": i}
                            for i, o in enumerate(q.get("options") or [])]
                for r in new_rows:
                    cur = con.execute("insert into option (question_id, label, content_md, asset_file, ord) "
                                      "values (?, ?, ?, ?, ?)", tuple(r.values()))
                    r["id"] = cur.lastrowid
                if new_rows:
                    s.post(f"{url}/rest/v1/option", headers=H, json=new_rows, timeout=60).raise_for_status()
            else:
                for (oid, _), o in zip(rows, q.get("options") or []):
                    af = (o.get("asset") or {}).get("file")
                    con.execute("update option set content_md=?, asset_file=? where id=?", (o["content"], af, oid))
                    s.patch(f"{url}/rest/v1/option", params={"id": f"eq.{oid}"}, headers=H,
                            json={"content_md": o["content"], "asset_file": af}, timeout=60).raise_for_status()
            if res.get("figures_changed") and pdf:
                con.execute("delete from asset where question_id=? and scope='question'", (qid,))
                s.delete(f"{url}/rest/v1/asset", params={"question_id": f"eq.{qid}", "scope": "eq.question"},
                         headers=H, timeout=60).raise_for_status()
                rows_a = [{"document_id": doc_id, "question_id": qid, "key": a["key"], "kind": "figure",
                           "scope": "question", "file": a["file"], "pending": 0} for a in q.get("assets") or []]
                for r in rows_a:
                    cur = con.execute("insert into asset (document_id, question_id, key, kind, scope, file, pending) "
                                      "values (?, ?, ?, ?, ?, ?, ?)", tuple(r.values()))
                    r["id"] = cur.lastrowid
                if rows_a:
                    s.post(f"{url}/rest/v1/asset", headers=H, json=rows_a, timeout=60).raise_for_status()
                if figs.get("group") and q.get("shared_asset") in shared:
                    sa = shared[q["shared_asset"]]
                    con.execute("update asset set file=? where document_id=? and key=? and scope='shared'",
                                (sa["file"], doc_id, sa["key"]))
                    s.patch(f"{url}/rest/v1/asset", params={"document_id": f"eq.{doc_id}", "key": f"eq.{sa['key']}",
                                                              "scope": "eq.shared"},
                            headers=H, json={"file": sa["file"]}, timeout=60).raise_for_status()
            res["_log"] = ({"doc": doc_id, "qid": qid, "code": code, "kind": "重新擷取", "note": res["note"],
                        "before": before, "at": time.strftime("%Y-%m-%d %H:%M:%S"),
                        "after": {"stem": q.get("stem"), "group_stem": q.get("group_stem"),
                                  "options": [o.get("content") for o in q.get("options") or []],
                                  "assets": [a.get("file") for a in q.get("assets") or []]}})
            res.update(_applied="fixed", _what="擷取錯誤已修正（重新對照原卷）")
            done.append((code, res))
        if done:                                          # 整份卷寫完才記狀態：中斷時這份卷下次整份重做
            yp.write_text(yaml.safe_dump(d, allow_unicode=True, sort_keys=False), encoding="utf-8")
            con.commit()
            for code, res in done:
                (OUT / f"{code}.json").write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
    con.commit()
    finish(url, H, s)
    return 0


def finish(url: str, H: dict, s: requests.Session) -> None:
    """寫回後的收尾（可重跑）：上傳新裁的附圖、回報標為已修正、記錄改動。"""
    log, uploads, n_rep = [], 0, 0
    auth = {"apikey": H["apikey"], "Authorization": H["Authorization"]}
    now = time.strftime("%Y-%m-%dT%H:%M:%S")
    for code, res in results():
        if not res.get("_applied") or res.get("_done"):
            continue
        for rel in res.get("_uploads") or []:
            s.post(f"{url}/storage/v1/object/{BUCKET}/{storage_key(rel)}", data=(ASSETS / rel).read_bytes(),
                   headers={**auth, "Content-Type": "image/png", "x-upsert": "true"}, timeout=120).raise_for_status()
            uploads += 1
        what = res["_what"]
        for r in res["_reports"]:
            row = s.get(f"{url}/rest/v1/review_doc", headers=H, timeout=60,
                        params={"select": "data", "site": f"eq.{r['site']}", "collection": "eq.reports",
                                "id": f"eq.{r['id']}"}).json()
            if not row:
                continue
            data = {**row[0]["data"], "resolved": {"at": now, "by": "claude", "what": what}}
            s.patch(f"{url}/rest/v1/review_doc", params={"site": f"eq.{r['site']}", "collection": "eq.reports",
                                                           "id": f"eq.{r['id']}"},
                    headers=H, json={"data": data}, timeout=60).raise_for_status()
            n_rep += 1
        if res.get("_log"):
            log.append(res["_log"])
        res["_done"] = True
        (OUT / f"{code}.json").write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
    prev = json.loads(LOG.read_text(encoding="utf-8")) if LOG.is_file() else []
    LOG.write_text(json.dumps(prev + log, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"寫回 {len(log)} 題、新裁附圖 {uploads} 張；{n_rep} 筆回報標為已修正")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["run", "check", "apply"])
    ap.add_argument("--codes-root", type=Path)
    ap.add_argument("--pdf-root", type=Path)
    ap.add_argument("--budget-twd", type=float, default=0)
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--only")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--wide", action="store_true", help="附上題目前後頁（重跑跨頁的題目）")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()
    return {"run": cmd_run, "check": cmd_check, "apply": cmd_apply}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
