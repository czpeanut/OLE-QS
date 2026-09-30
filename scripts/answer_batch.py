#!/usr/bin/env python3
"""一份卷一次呼叫，讓模型作答全卷的選擇、是非、填充題（附圖一起送）。

和 generate_answers.py（一題一次呼叫）相比，題組短文、指示語只送一次，
費用約為十分之一；多個模型各跑一次後比對，一致的才採用。

--eval：只挑已有答案（答案卷解析而來）的題目作答，和答案卷比對算正確率，
用來在正式跑之前比較模型。結果不寫回題庫。

用法:
    python scripts/answer_batch.py data/bank --eval --docs docs.txt \\
        --models gemini-3.7-flash,qwen3.5-flash -o out/answer_eval
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from generate_answers import load_env_file, loads_lenient  # noqa: E402
from vlm_extract import TWD_PER_USD, Usage, call_model  # noqa: E402

QWEN_URL = "https://dashscope-intl.aliyuncs.com/compatible-mode/v1/chat/completions"
# 每百萬 tokens 美元（輸入, 輸出）。qwen 取國際版公開報價，較新型號取 OpenRouter 報價。
QWEN_PRICES = {
    "qwen3.5-flash": (0.10, 0.40),
    "qwen3.6-flash": (0.1875, 1.125),
    "qwen3.7-flash": (0.10, 0.40),        # 查無可靠報價，先比照 3.5-flash
    "qwen3.8-flash": (0.15, 0.47),
    "qwen3-vl-flash": (0.10, 0.40),       # 同上
    "qwen3-vl-plus": (0.20, 1.60),
    "qwen3.5-plus": (0.40, 2.40),
}
ANSWERABLE = {"single", "multiple", "tf", "fill"}

SCHEMA = {
    "type": "OBJECT",
    "properties": {"answers": {"type": "ARRAY", "items": {
        "type": "OBJECT",
        "properties": {
            "i": {"type": "INTEGER"},
            "answer": {"type": "STRING"},
            "missing_context": {"type": "BOOLEAN"},
        },
        "required": ["i", "answer"]}}},
    "required": ["answers"],
}

PROMPT = """你是國中{subject}老師，請作答下面這份段考卷的題目。

規則：
- 單選題 answer 只填一個選項字母（例如 B）；多選題填所有正確字母（例如 AC）。
- 是非題填 O 或 X。
- 填充題填最簡答案（數學寫最簡分數或數值，可用 LaTeX，例如 $\\frac{{3}}{{4}}$；多格用「；」分隔）。
- 題目需要的圖、表或短文缺漏而無法作答時，missing_context 填 true，answer 填你最可能的猜測。
- 每一題都要回答，i 是題目序號。

只輸出 JSON：{{"answers":[{{"i":0,"answer":"B","missing_context":false}}, ...]}}

{questions}
"""


def question_block(d: dict, qs: list[tuple[int, dict]], assets: Path) -> tuple[str, list[tuple[str, Path]]]:
    lines: list[str] = []
    images: list[tuple[str, Path]] = []
    shared = {a["key"]: a for a in d.get("shared_assets") or []}
    seen_g: set[str] = set()
    for i, q in qs:
        g = (q.get("group_stem") or "").strip()
        if g and g not in seen_g:
            seen_g.add(g)
            lines.append(f"\n【題組】{g}")
            sa = shared.get(q.get("shared_asset") or "")
            if sa and sa.get("file") and (assets / sa["file"]).is_file():
                label = f"題組圖{len(images) + 1}"
                images.append((label, assets / sa["file"]))
                lines.append(f"（見{label}）")
        lines.append(f"\n[{i}]（{q.get('type')}）{q.get('stem') or ''}")
        for a in q.get("assets") or []:
            if a.get("file") and (assets / a["file"]).is_file():
                label = f"第{i}題圖{len(images) + 1}"
                images.append((label, assets / a["file"]))
                lines.append(f"（見{label}）")
        for o in q.get("options") or []:
            body = (o.get("content") or "").strip()
            f = (o.get("asset") or {}).get("file")
            if f and (assets / f).is_file():
                label = f"第{i}題選項{o.get('label')}圖"
                images.append((label, assets / f))
                body = (body + f"（見{label}）").strip()
            lines.append(f"({o.get('label')}) {body}")
    return "\n".join(lines), images


class QwenUsage:
    lock = threading.Lock()
    usd = 0.0


def ask_qwen(prompt: str, images: list[tuple[str, Path]], model: str, thinking: bool) -> tuple[dict, float]:
    key = os.environ["QWEN_API_KEY"]
    content: list[dict] = [{"type": "text", "text": prompt}]
    for label, path in images:
        content.append({"type": "text", "text": f"[{label}]"})
        content.append({"type": "image_url", "image_url": {
            "url": "data:image/png;base64," + base64.b64encode(path.read_bytes()).decode()}})
    body = {"model": model, "messages": [{"role": "user", "content": content}],
            "temperature": 0, "enable_thinking": thinking,
            "stream": True, "stream_options": {"include_usage": True}}
    if not thinking:
        body["response_format"] = {"type": "json_object"}
    last = None
    for attempt in range(4):
        try:
            text, usage = [], {}
            with requests.post(QWEN_URL, headers={"Authorization": f"Bearer {key}"},
                               json=body, timeout=600, stream=True) as r:
                if r.status_code in (429, 500, 502, 503, 504):
                    last = f"HTTP {r.status_code}"
                    time.sleep(5 * 2 ** attempt)
                    continue
                if r.status_code != 200:
                    raise RuntimeError(f"HTTP {r.status_code}: {r.text[:300]}")
                for line in r.iter_lines(decode_unicode=True):
                    if not line or not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    ev = json.loads(data)
                    if ev.get("usage"):
                        usage = ev["usage"]
                    for ch in ev.get("choices") or []:
                        text.append((ch.get("delta") or {}).get("content") or "")
            p_in, p_out = QWEN_PRICES.get(model, (0.5, 3.0))
            cost = (usage.get("prompt_tokens", 0) * p_in + usage.get("completion_tokens", 0) * p_out) / 1e6
            with QwenUsage.lock:
                QwenUsage.usd += cost
            raw = "".join(text)
            m = re.search(r"\{.*\}", raw, re.S)
            return loads_lenient(m.group(0) if m else raw), cost
        except (requests.RequestException, json.JSONDecodeError) as exc:
            last = f"{type(exc).__name__}: {exc}"
            time.sleep(5)
    raise RuntimeError(f"呼叫失敗：{last}")


def ask(model_spec: str, prompt: str, images: list[tuple[str, Path]]) -> tuple[dict, float]:
    """model_spec：gemini-… 或 qwen…，qwen 加 +think 開啟思考。"""
    if model_spec.startswith("qwen"):
        name, _, flag = model_spec.partition("+")
        return ask_qwen(prompt, images, name, thinking=(flag == "think"))
    parts: list[dict] = [{"text": prompt}]
    for label, path in images:
        parts.append({"text": f"[{label}]"})
        parts.append({"inline_data": {"mime_type": "image/png",
                                      "data": base64.b64encode(path.read_bytes()).decode()}})
    out, _, cost = call_model(parts, model_spec, schema=SCHEMA, fallback=[model_spec])
    return out, cost


def norm(ans: str | None, qtype: str) -> str:
    s = str(ans or "").strip().upper()
    if qtype in ("single", "multiple"):
        return "".join(sorted(set(re.findall(r"[A-H]", s))))
    if qtype == "tf":
        return {"○": "O", "〇": "O", "T": "O", "TRUE": "O", "✓": "O", "×": "X", "F": "X",
                "FALSE": "X", "✗": "X"}.get(s, s)
    return re.sub(r"[\s$\\{}]|dfrac|frac", "", s)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("bank", type=Path)
    ap.add_argument("--assets", type=Path, default=Path("data/assets"))
    ap.add_argument("--docs", type=Path, required=True, help="一行一個卷 ID")
    ap.add_argument("--models", required=True)
    ap.add_argument("--eval", action="store_true")
    ap.add_argument("-o", "--out", type=Path, default=Path("out/answer_eval"))
    ap.add_argument("--workers", type=int, default=6)
    args = ap.parse_args()

    load_env_file()
    Usage.load(Path("out/answer_spend.json"))
    models = args.models.split(",")
    args.out.mkdir(parents=True, exist_ok=True)
    doc_ids = [l.strip() for l in args.docs.read_text().splitlines() if l.strip()]

    jobs = []
    for did in doc_ids:
        d = yaml.safe_load((args.bank / f"{did}.yaml").read_text(encoding="utf-8"))
        qs = [(i, q) for i, q in enumerate(d["questions"]) if q.get("type") in ANSWERABLE
              and (q.get("answer") or not args.eval)
              and (not args.eval or q.get("answer_status") != "disputed")]
        if qs:
            for m in models:
                jobs.append((did, d, qs, m))

    results: dict = {}

    def run(job):
        did, d, qs, m = job
        text, images = question_block(d, qs, args.assets)
        prompt = PROMPT.format(subject=d["document"]["subject"], questions=text)
        t = time.time()
        try:
            out, cost = ask(m, prompt, images)
            err = None
        except Exception as exc:  # noqa: BLE001
            out, cost, err = {}, 0.0, str(exc)[:200]
        return did, m, out, cost, round(time.time() - t, 1), err, len(images)

    with ThreadPoolExecutor(args.workers) as pool:
        for did, m, out, cost, sec, err, nimg in pool.map(run, jobs):
            results.setdefault(did, {})[m] = {"out": out, "usd": cost, "sec": sec, "err": err}
            print(f"{did} {m} {sec}s ${cost:.4f} 圖{nimg}" + (f" 錯誤：{err}" if err else ""), flush=True)

    # 比對
    summary = {m: {"n": 0, "right": 0, "usd": 0.0, "by_subject": {}, "by_type": {}} for m in models}
    detail = []
    for did in doc_ids:
        d = yaml.safe_load((args.bank / f"{did}.yaml").read_text(encoding="utf-8"))
        subj = d["document"]["subject"]
        for m in models:
            r = results.get(did, {}).get(m)
            if not r:
                continue
            summary[m]["usd"] += r["usd"]
            got = {a.get("i"): a for a in (r["out"].get("answers") or [])}
            for i, q in enumerate(d["questions"]):
                if q.get("type") not in ANSWERABLE or not q.get("answer") or q.get("answer_status") == "disputed":
                    continue
                a = got.get(i) or {}
                ok = norm(a.get("answer"), q["type"]) == norm(q["answer"], q["type"])
                s = summary[m]
                s["n"] += 1
                s["right"] += ok
                for key, val in (("by_subject", subj), ("by_type", q["type"])):
                    b = s[key].setdefault(val, [0, 0])
                    b[0] += 1
                    b[1] += ok
                detail.append({"doc": did, "i": i, "type": q["type"], "model": m,
                               "key": q["answer"], "got": a.get("answer"), "ok": ok,
                               "stem": (q.get("stem") or "")[:60]})
    (args.out / "detail.json").write_text(json.dumps(detail, ensure_ascii=False, indent=1), encoding="utf-8")
    (args.out / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8")
    for m, s in summary.items():
        acc = s["right"] / s["n"] if s["n"] else 0
        subj = "、".join(f"{k} {v[1]}/{v[0]}" for k, v in s["by_subject"].items())
        typ = "、".join(f"{k} {v[1]}/{v[0]}" for k, v in s["by_type"].items())
        print(f"{m:22s} 正確 {s['right']}/{s['n']}（{acc:.1%}） US${s['usd']:.4f}｜{subj}｜{typ}")
    print(f"Gemini 累計 NT${Usage.twd():.1f}；Qwen US${QwenUsage.usd:.4f}（NT${QwenUsage.usd * TWD_PER_USD:.2f}）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
