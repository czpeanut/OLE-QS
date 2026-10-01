#!/usr/bin/env python3
"""題庫作答：Gemini（批次半價）與 Qwen（即時）各答一次，不一致的再由 Gemini 高思考投第三票。

步驟（各自可重跑，已完成的卷自動跳過）：
    gemini    送出／收回 Gemini 批次作答        → out/answers/gemini/<卷>.json
    qwen      Qwen 即時作答                      → out/answers/qwen/<卷>.json
    tiebreak  兩者不一致的題目送 Gemini 高思考   → out/answers/tiebreak/<卷>.json
    merge     合併寫回題庫 YAML

作答範圍：單選、多選、是非、填充，且通過品管閘門的題目。已有答案（答案卷）的題目也答，
用來抓答案卷讀錯的題：兩個模型一致且與答案卷不同時標成 disputed，保留原答案。

寫回的狀態（沿用 apps/api/models.py 的 AnswerStatus）：
    ai_generated  兩個模型一致，或三票中兩票相同
    disputed      三票都不同，或模型一致推翻答案卷
    answer_source: ai:gemini+qwen、ai:majority；每題另存 ai_answers 供查核

費用記在 out/phase2_spend.json，每次送出或呼叫前檢查 --budget-twd。
"""

from __future__ import annotations

import argparse
import base64
import json
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))
sys.path.insert(0, str(REPO))
import batch_api as B  # noqa: E402
from answer_batch import ANSWERABLE, PROMPT, SCHEMA, ask_qwen, norm, question_block  # noqa: E402
from apps.api.quality import evaluate  # noqa: E402

OUT = Path("out/answers")
GEMINI_MODEL = "gemini-3.7-flash"
THINK = {"數學": "medium", "自然": "medium"}            # 其他科 low
EST_USD_PER_Q = {"low": 0.00035, "medium": 0.0007, "high": 0.0015}   # 批次價，含兩成緩衝
QWEN_EST_USD_PER_Q = 0.00025


def targets(d: dict) -> list[tuple[int, dict]]:
    return [(i, q) for i, q in enumerate(d.get("questions") or [])
            if q.get("type") in ANSWERABLE and evaluate(q, d)[0]]


def load_docs(bank: Path, only: str | None = None):
    for p in sorted(bank.glob("*.yaml")):
        if only and only not in p.name:
            continue
        yield p


def gemini_request(d: dict, qs: list[tuple[int, dict]], assets: Path, level: str) -> dict:
    text, images = question_block(d, qs, assets)
    parts: list[dict] = [{"text": PROMPT.format(subject=d["document"]["subject"], questions=text)}]
    for label, path in images:
        parts.append({"text": f"[{label}]"})
        parts.append({"inline_data": {"mime_type": "image/png",
                                      "data": base64.b64encode(path.read_bytes()).decode()}})
    return {"contents": [{"parts": parts}],
            "generationConfig": {"responseMimeType": "application/json", "responseSchema": SCHEMA,
                                 "temperature": 0, "maxOutputTokens": 32768,
                                 "thinkingConfig": {"thinkingLevel": level}}}


def committed_twd(phases: tuple[str, ...]) -> float:
    pend = sum(j["est_usd"] for ph in phases for j in B.load_jobs(ph) if not j.get("collected"))
    return B.Ledger.twd() + pend * B.TWD_PER_USD


# ─────────────────────────── Gemini 批次（第一票／第三票）───────────────────────────

def submit_gemini(args, phase: str, pick) -> None:
    """pick(d) → [(i, q)]：這份卷要送的題目；回傳空的就略過。"""
    dest = OUT / phase.removeprefix("answer_")
    dest.mkdir(parents=True, exist_ok=True)
    queued = {k for j in B.load_jobs(phase) if not j.get("collected") for k in j["keys"]}
    batch: list[tuple[str, dict]] = []
    est = 0.0
    n_docs = 0

    def flush():
        nonlocal batch, est
        if not batch:
            return True
        if committed_twd(("answer_gemini", "answer_tiebreak")) + est * B.TWD_PER_USD > args.budget_twd:
            print(f"預算不足，停止送出（本批預估 NT${est * B.TWD_PER_USD:.0f}）", flush=True)
            return False
        label = f"{phase}_{time.strftime('%m%d_%H%M%S')}"
        job = B.gemini_submit(phase, label, GEMINI_MODEL, batch, est_usd=est)
        print(f"  送出 {label}：{len(batch)} 份卷、{job['bytes'] / 1e6:.0f} MB、"
              f"預估 NT${est * B.TWD_PER_USD:.0f} → {job['name']}", flush=True)
        batch, est = [], 0.0
        time.sleep(2)
        return True

    for p in load_docs(args.bank, args.only):
        if (dest / f"{p.stem}.json").is_file() or p.stem in queued:
            continue
        d = yaml.safe_load(p.read_text(encoding="utf-8"))
        qs = pick(d)
        if not qs:
            continue
        level = "high" if phase == "answer_tiebreak" else THINK.get(d["document"]["subject"], "low")
        batch.append((p.stem, gemini_request(d, qs, args.assets, level)))
        est += EST_USD_PER_Q[level] * len(qs)
        n_docs += 1
        if len(batch) >= args.chunk and not flush():
            return
        if args.limit and n_docs >= args.limit:
            break
    flush()


def collect_gemini(args, phase: str) -> int:
    dest = OUT / phase.removeprefix("answer_")
    dest.mkdir(parents=True, exist_ok=True)
    left = 0
    for job in B.load_jobs(phase):
        if job.get("collected"):
            continue
        try:
            s = B.state(job)
            if s not in B.DONE["gemini"] and s not in B.FAILED["gemini"]:
                left += 1
                print(f"  {job['label']} {s}", flush=True)
                continue
            res = B.results(job)
        except Exception as exc:  # noqa: BLE001  網路暫時中斷，下一輪再收
            print(f"  {job['label']} 查詢失敗，下一輪再試：{str(exc)[:100]}", flush=True)
            left += 1
            continue
        usd, ok, bad = 0.0, 0, 0
        for doc_id in job["keys"]:
            r = res.get(doc_id) or {}
            usd += B.gemini_cost(job["model"], r.get("usageMetadata") or {})
            try:
                cand = r["candidates"][0]
                text = "".join(x.get("text", "") for x in cand["content"]["parts"])
                out = json.loads(text)
                (dest / f"{doc_id}.json").write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
                ok += 1
            except Exception:  # noqa: BLE001
                bad += 1
        twd = B.Ledger.add(phase, usd)
        job.update({"collected": True, "state": s, "usd": round(usd, 4)})
        job.pop("_raw", None)
        B.save_job(phase, job)
        print(f"  收回 {job['label']}：成功 {ok}、失敗 {bad}，US${usd:.2f}｜階段累計 NT${twd:.0f}", flush=True)
    return left


# ─────────────────────────── Qwen 即時 ───────────────────────────

def qwen_model_for(subject: str) -> tuple[str, bool]:
    # 社會科用 qwen3-vl-plus：3.5-flash 系列會被內容審查整份拒絕（台灣史地、公民題材）
    return ("qwen3-vl-plus", False) if subject == "社會" else ("qwen3.5-flash", True)


def run_qwen(args) -> None:
    dest = OUT / "qwen"
    dest.mkdir(parents=True, exist_ok=True)
    todo = [p for p in load_docs(args.bank, args.only) if not (dest / f"{p.stem}.json").is_file()]
    if args.limit:
        todo = todo[:args.limit]
    print(f"Qwen 待作答 {len(todo)} 份", flush=True)
    stop = threading.Event()
    lock = threading.Lock()

    def one(p: Path):
        if stop.is_set():
            return p, "stop", 0.0
        d = yaml.safe_load(p.read_text(encoding="utf-8"))
        qs = targets(d)
        if not qs:
            return p, "none", 0.0
        if committed_twd(("answer_gemini", "answer_tiebreak")) >= args.budget_twd:
            stop.set()
            return p, "budget", 0.0
        text, images = question_block(d, qs, args.assets)
        prompt = PROMPT.format(subject=d["document"]["subject"], questions=text)
        model, think = qwen_model_for(d["document"]["subject"])
        tries = [(model, think), ("qwen3-vl-plus", False)] if model != "qwen3-vl-plus" else [(model, think)]
        last = None
        for m, t in tries:
            try:
                out, _ = ask_qwen(prompt, images, m, t)
                if not isinstance(out, dict) or not out.get("answers"):
                    raise ValueError("輸出沒有 answers")
                out["model"] = m + ("+think" if t else "")
                (dest / f"{p.stem}.json").write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
                return p, out["model"], 0.0
            except Exception as exc:  # noqa: BLE001
                last = str(exc)[:160]
        return p, f"失敗：{last}", 0.0

    from answer_batch import QwenUsage
    base = QwenUsage.usd
    done = 0
    with ThreadPoolExecutor(args.workers) as pool:
        futs = [pool.submit(one, p) for p in todo]
        for f in as_completed(futs):
            p, status, _ = f.result()
            done += 1
            with lock:
                spent = QwenUsage.usd - base
                if spent > 0:
                    B.Ledger.add("answer_qwen", spent)
                    base = QwenUsage.usd
            if status not in ("none", "stop") and (done % 25 == 0 or status.startswith("失敗")):
                print(f"[{done}/{len(todo)}] {p.stem} {status}｜階段累計 NT${B.Ledger.twd():.0f}", flush=True)
    print(f"Qwen 完成；階段累計 NT${B.Ledger.twd():.1f}", flush=True)


def run_qwen_split(args) -> None:
    """整份被 Qwen 內容審查擋下的卷：每 5 題一段送出，段落再被擋就逐題送，
    只有真正觸發審查的題目沒有 Qwen 答案（交給 Gemini 高思考當第二票）。"""
    dest = OUT / "qwen"
    todo = []
    for p in load_docs(args.bank, args.only):
        if (dest / f"{p.stem}.json").is_file():
            continue
        d = yaml.safe_load(p.read_text(encoding="utf-8"))
        if targets(d):
            todo.append((p, d))
    print(f"Qwen 分段重試 {len(todo)} 份", flush=True)
    from answer_batch import QwenUsage

    def ask_part(d, part):
        text, images = question_block(d, part, args.assets)
        prompt = PROMPT.format(subject=d["document"]["subject"], questions=text)
        out, _ = ask_qwen(prompt, images, "qwen3-vl-plus", False)
        return {a.get("i"): a for a in out.get("answers") or [] if isinstance(a, dict)}

    def one(item):
        p, d = item
        qs = targets(d)
        got, blocked = {}, []
        for k in range(0, len(qs), 5):
            part = qs[k:k + 5]
            try:
                got.update(ask_part(d, part))
            except Exception:  # noqa: BLE001
                for single in part:
                    try:
                        got.update(ask_part(d, [single]))
                    except Exception:  # noqa: BLE001
                        blocked.append(single[1]["id"])
        out = {"answers": list(got.values()), "model": "qwen3-vl-plus(split)", "blocked": blocked}
        (dest / f"{p.stem}.json").write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
        return p, len(got), len(blocked)

    base = QwenUsage.usd
    with ThreadPoolExecutor(args.workers) as pool:
        for p, n, nb in pool.map(one, todo):
            spent = QwenUsage.usd - base
            base = QwenUsage.usd
            twd = B.Ledger.add("answer_qwen", spent) if spent else B.Ledger.twd()
            print(f"  {p.stem}：答 {n} 題、擋 {nb} 題｜階段累計 NT${twd:.0f}", flush=True)


# ─────────────────────────── 比對與寫回 ───────────────────────────

def answers_of(path: Path, qs: list[tuple[int, dict]]) -> dict[str, str]:
    """{題目 ID: 答案}（依送出時的題目序號 i 對回去）"""
    if not path.is_file():
        return {}
    out = json.loads(path.read_text(encoding="utf-8"))
    by_i = {a.get("i"): a.get("answer") for a in out.get("answers") or [] if isinstance(a, dict)}
    return {q["id"]: by_i[i] for i, q in qs if by_i.get(i) not in (None, "")}


def disagreements(d: dict) -> list[tuple[int, dict]]:
    qs = targets(d)
    g = answers_of(OUT / "gemini" / f"{d['document']['id']}.json", qs)
    w = answers_of(OUT / "qwen" / f"{d['document']['id']}.json", qs)
    if not (OUT / "qwen" / f"{d['document']['id']}.json").is_file():
        return []                                  # Qwen 還沒答完，先不送第三票
    return [(i, q) for i, q in qs if q["id"] in g and (
        q["id"] not in w or norm(g[q["id"]], q["type"]) != norm(w[q["id"]], q["type"]))]


def to_list(ans: str, q: dict) -> list[str]:
    if q["type"] in ("single", "multiple"):
        return list(norm(ans, q["type"]))
    if q["type"] == "tf":
        return [norm(ans, "tf")]
    return [a.strip() for a in str(ans).split("；") if a.strip()]


def merge(args) -> None:
    stats = {"consensus": 0, "majority": 0, "gemini_only": 0, "disputed": 0, "key_ok": 0, "key_conflict": 0, "pending": 0}
    for p in load_docs(args.bank, args.only):
        d = yaml.safe_load(p.read_text(encoding="utf-8"))
        qs = targets(d)
        did = d["document"]["id"]
        g = answers_of(OUT / "gemini" / f"{did}.json", qs)
        w = answers_of(OUT / "qwen" / f"{did}.json", qs)
        t = answers_of(OUT / "tiebreak" / f"{did}.json", disagreements(d))
        dirty = False
        for _, q in qs:
            qid, typ = q["id"], q["type"]
            if qid not in g or (qid not in w and qid not in t):
                stats["pending"] += 1
                continue
            votes = {"gemini": g[qid]}
            if qid in w:
                votes["qwen"] = w[qid]
            if qid in t:
                votes["gemini_high"] = t[qid]
            q["ai_answers"] = votes
            ng = norm(g[qid], typ)
            nw = norm(w[qid], typ) if qid in w else None
            if ng == nw:
                final, how = g[qid], "consensus"
            elif qid in t and norm(t[qid], typ) in (ng, nw):
                # Qwen 擋下的題只有 Gemini 兩種思考等級互證，同家族，另外標註
                final, how = t[qid], ("majority" if nw is not None else "gemini_only")
            else:
                final, how = None, "disputed"
            if q.get("answer") and not str(q.get("answer_source", "")).startswith("ai:"):
                # 已有答案卷的答案：只在兩模型一致推翻時標記，保留原答案
                if how == "consensus" and norm(q["answer"], typ) != ng:
                    q["answer_status"] = "disputed"
                    q["review_note"] = f"答案卷 {q['answer']}，Gemini 與 Qwen 一致判為 {final}"
                    stats["key_conflict"] += 1
                else:
                    stats["key_ok"] += 1
                dirty = True
                continue
            if final is not None:
                q["answer"] = to_list(final, q)
                q["answer_status"] = "ai_generated"
                q["answer_source"] = {"consensus": "ai:gemini+qwen", "majority": "ai:majority",
                                      "gemini_only": "ai:gemini-only"}[how]
                stats[how] += 1
            else:
                q.pop("answer", None)
                q["answer_status"] = "disputed"
                q["answer_source"] = "ai:none"
                stats["disputed"] += 1
            dirty = True
        if dirty:
            p.write_text(yaml.safe_dump(d, allow_unicode=True, sort_keys=False), encoding="utf-8")
    print("寫回：", stats, flush=True)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("step", choices=["gemini", "qwen", "qwen-split", "tiebreak", "merge"])
    ap.add_argument("--bank", type=Path, default=Path("data/bank"))
    ap.add_argument("--assets", type=Path, default=Path("data/assets"))
    ap.add_argument("--budget-twd", type=float, default=7000)
    ap.add_argument("--chunk", type=int, default=400)
    ap.add_argument("--limit", type=int)
    ap.add_argument("--only")
    ap.add_argument("--workers", type=int, default=16)
    ap.add_argument("--collect-only", action="store_true")
    args = ap.parse_args()
    B._env("QWEN_API_KEY")          # 從 .env.local 載入兩家的金鑰
    B._env("GEMINI_API_KEY")

    if args.step == "gemini":
        if not args.collect_only:
            submit_gemini(args, "answer_gemini", targets)
        collect_gemini(args, "answer_gemini")
    elif args.step == "tiebreak":
        if not args.collect_only:
            submit_gemini(args, "answer_tiebreak", disagreements)
        collect_gemini(args, "answer_tiebreak")
    elif args.step == "qwen":
        run_qwen(args)
    elif args.step == "qwen-split":
        run_qwen_split(args)
    elif args.step == "merge":
        merge(args)
    print(f"階段累計 NT${B.Ledger.twd():.1f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
