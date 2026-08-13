#!/usr/bin/env python3
"""用多個模型各自作答，靠「彼此是否同意」把人工複核的範圍縮到最小。

為什麼不是單跑一個模型就好：
    錯的答案比沒有答案更糟 —— 老師用錯的答案卷改分，信任壞掉就回不來。
    單一模型的問題不在正確率是 95%，而在**你不知道是哪 5%**。
    兩個不同家族的模型交叉比對後，一致的題目可以放心採用，
    不一致的才需要人看。實測型態的題目通常有 85~90% 會一致，
    人工量因此降到原本的十分之一左右。

    刻意用**不同家族**的模型：同一個模型跑兩次，錯誤是相關的，
    會一起錯而且一起很有信心。

為什麼必須把圖一起送進去：
    本專案的自然科樣本裡，第 4 題問「A 顯微鏡的癸鏡頭」——
    癸是圖(一)上的標號，沒有圖就無解，而模型不會說不知道，它會掰。
    第 23 題的四個選項本身就是圖，純文字送過去等於四個空選項。
    所以作答一律跑在「組裝後的完整題目」上，並要求模型在覺得
    缺少看不到的資訊時回報 missing_context —— 這能反過來抓出組裝漏圖的 bug。

用法:
    export GEMINI_API_KEY=...
    export ANTHROPIC_API_KEY=...

    # 單份試水溫
    python scripts/generate_answers.py data/bank/doc_111_嘉義_北興_數學_g7s1e1.yaml \\
        --figures data/assets --limit 5

    # 整個題庫，並把答案寫回擷取結果
    python scripts/generate_answers.py data/bank --figures data/assets --merge

輸出:
    out/answers/<卷id>.answers.yaml   每題的各家答案、是否一致、建議狀態
    終端摘要                           一致 / 爭議 / 缺素材 的題數統計
    --merge 時                        答案連同 answer_status 寫回來源 YAML

已經有答案的題目一律跳過 —— 答案卷解析出來的答案是確認過的，
不該被模型的作答覆蓋，跑過一次也不必再花錢跑第二次。
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import sys
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import yaml

try:
    import requests
except ImportError:
    sys.exit("需要 requests，請先執行：pip install requests")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from apps.api.quality import evaluate  # noqa: E402

SYSTEM = """你是一位國中教師，正在為考卷編寫答案卷。

規則：
1. 只依據提供的題目內容作答，不要臆測看不到的資訊。
2. 如果作答需要你沒有拿到的圖、表或前文，把 missing_context 設為 true 並說明缺什麼，
   answer 留空。不要猜。
3. 選擇題的 answer 只填代號（例：["B"]）；多個答案就列多個。
4. 填充題的 answer 填最簡答案本身（例：["-53"]、["複式顯微鏡"]、["甲","丙"]）。
5. reasoning 用一到三句話說明關鍵推理，不要長篇解題。

只輸出 JSON，格式：
{"answer": [...], "reasoning": "...", "confidence": 0.0-1.0, "missing_context": false, "missing_what": ""}"""


# ─────────────────────────── 題目組裝 ───────────────────────────

def collect_images(q: dict, doc: dict, fig_dir: Path) -> list[tuple[str, Path]]:
    """蒐集這一題作答所需的全部圖片，含共用圖與選項圖。"""
    out: list[tuple[str, Path]] = []
    shared = {a["key"]: a for a in doc.get("shared_assets", [])}

    ref = q.get("shared_asset")
    if ref and ref in shared and shared[ref].get("file"):
        out.append((f"共用{shared[ref].get('label', ref)}", fig_dir / shared[ref]["file"]))

    for a in q.get("assets", []):
        if a.get("kind") == "table":
            continue                      # 表格已結構化成 Markdown，不需送圖
        if a.get("file"):
            out.append((a.get("label") or a["key"], fig_dir / a["file"]))

    for o in q.get("options") or []:
        asset = o.get("asset") or {}
        if asset.get("file"):
            out.append((f"選項({o['label']})的圖", fig_dir / asset["file"]))

    return [(label, p) for label, p in out if p.is_file()]


def build_prompt(q: dict, doc: dict) -> str:
    meta = doc.get("document", {})
    parts = [f"科目：{meta.get('subject', '')} {meta.get('sub_subject', '') or ''}".strip(),
             f"年級：國中{meta.get('grade', '')}年級",
             ""]

    shared = {a["key"]: a for a in doc.get("shared_assets", [])}
    passages = {p["key"]: p for p in doc.get("passages", [])}

    ref = q.get("shared_asset")
    if ref in passages:
        parts += ["【共用短文】", passages[ref]["text"], ""]
    if ref in shared:
        parts += [f"【共用圖：{shared[ref].get('label', ref)}】（見附圖）", ""]

    # 題組說明通常只寫在該組第一題上，但同組每一題作答時都需要它。
    # 例：「圖(一)是顯微鏡A、B與相關構造，請根據圖(一)回答第1~5題」寫在第1題，
    # 第4題若拿不到這句，就少了「A、B 是兩台顯微鏡」這個關鍵前提。
    group_stem = q.get("group_stem")
    if not group_stem and ref:
        group_stem = next((o.get("group_stem") for o in doc.get("questions", [])
                           if o.get("shared_asset") == ref and o.get("group_stem")), None)
    if group_stem:
        parts += [f"【題組說明】{group_stem}", ""]

    parts.append(f"【題目】（{q.get('type')}）")
    parts.append(q.get("stem", ""))

    for a in q.get("assets", []):
        if a.get("kind") == "table" and a.get("markdown"):
            parts += ["", f"{a.get('label', '表')}：", a["markdown"]]

    if q.get("options"):
        parts.append("")
        for o in q["options"]:
            body = (o.get("content") or "").strip() or "（此選項內容為圖片，見附圖）"
            parts.append(f"({o['label']}) {body}")

    if q.get("answer_count"):
        parts += ["", f"（本題應有 {q['answer_count']} 個答案）"]

    return "\n".join(parts)


# ─────────────────────────── 模型呼叫 ───────────────────────────

def _b64(path: Path) -> str:
    return base64.standard_b64encode(path.read_bytes()).decode()


def ask_gemini(prompt: str, images: list[tuple[str, Path]], model: str) -> dict:
    key = os.environ["GEMINI_API_KEY"]
    parts: list[dict] = [{"text": SYSTEM + "\n\n" + prompt}]
    for label, path in images:
        parts.append({"text": f"\n[{label}]"})
        parts.append({"inline_data": {"mime_type": "image/png", "data": _b64(path)}})

    r = requests.post(
        f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
        params={"key": key},
        json={"contents": [{"parts": parts}],
              "generationConfig": {"responseMimeType": "application/json", "temperature": 0}},
        timeout=120)
    r.raise_for_status()
    return json.loads(r.json()["candidates"][0]["content"]["parts"][0]["text"])


def ask_anthropic(prompt: str, images: list[tuple[str, Path]], model: str) -> dict:
    key = os.environ["ANTHROPIC_API_KEY"]
    content: list[dict] = [{"type": "text", "text": prompt}]
    for label, path in images:
        content.append({"type": "text", "text": f"\n[{label}]"})
        content.append({"type": "image", "source": {
            "type": "base64", "media_type": "image/png", "data": _b64(path)}})

    r = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={"x-api-key": key, "anthropic-version": "2023-06-01",
                 "content-type": "application/json"},
        json={"model": model, "max_tokens": 1024, "temperature": 0,
              "system": SYSTEM, "messages": [{"role": "user", "content": content}]},
        timeout=120)
    r.raise_for_status()
    text = r.json()["content"][0]["text"]
    m = re.search(r"\{.*\}", text, re.S)      # 容忍模型在 JSON 前後多寫幾個字
    return json.loads(m.group(0) if m else text)


PROVIDERS = {
    "gemini": (ask_gemini, "GEMINI_API_KEY", "gemini-2.5-pro"),
    "claude": (ask_anthropic, "ANTHROPIC_API_KEY", "claude-sonnet-5"),
}


# ─────────────────────────── 答案比對 ───────────────────────────

def normalize(ans) -> tuple:
    """把答案正規化成可比較的形式。

    要能吃下的差異：大小寫、全半形、標點、數值寫法（-53 / −53 / 負53）、
    以及多重答案的順序（甲丙 == 丙甲）。
    """
    if ans is None:
        return ()
    items = ans if isinstance(ans, list) else [ans]
    out = []
    for x in items:
        s = unicodedata.normalize("NFKC", str(x)).strip()
        s = s.replace("−", "-").replace("－", "-")       # 各種減號
        s = re.sub(r"[\s,，、。．]+", "", s)
        s = s.upper()
        if not s:
            continue
        try:                                             # 數值等價：-53 == -53.0
            out.append(str(float(s)))
            continue
        except ValueError:
            pass
        # 「甲丙」這種黏在一起的多選，拆成單字比較
        if len(s) > 1 and all(c in "甲乙丙丁戊己庚辛壬癸ABCDE" for c in s):
            out.extend(s)
        else:
            out.append(s)
    return tuple(sorted(out))


def judge(results: dict[str, dict]) -> tuple[str, str]:
    """回傳 (狀態, 說明)。"""
    ok = {k: v for k, v in results.items() if v and not v.get("error")}
    if not ok:
        return "failed", "所有模型都呼叫失敗"

    missing = [k for k, v in ok.items() if v.get("missing_context")]
    if missing:
        what = next(ok[k].get("missing_what", "") for k in missing)
        return "missing_context", f"{'、'.join(missing)} 回報缺少素材：{what}"

    keys = {k: normalize(v.get("answer")) for k, v in ok.items()}
    distinct = set(keys.values())
    if len(ok) < 2:
        return "single_source", "只有一個模型成功作答，無法交叉驗證"
    if len(distinct) == 1:
        conf = min(v.get("confidence", 1) or 0 for v in ok.values())
        return ("agreed" if conf >= 0.6 else "agreed_low_confidence",
                "、".join(f"{k}={keys[k]}" for k in keys))
    return "disputed", "；".join(f"{k}={keys[k]}" for k in keys)


# ─────────────────────────── 主流程 ───────────────────────────

def status_for(judged: str, providers: list[str]) -> str | None:
    """把交叉比對的結論翻成題庫的 answer_status。

    只有答案卷與人工能給 verified。單一模型、多模型一致，都還是 ai_generated ——
    「兩個模型都這樣說」提高的是可信度，不是確認。
    """
    return {"agreed": "ai_generated",
            "agreed_low_confidence": "ai_generated",
            "single_source": "ai_generated",
            "disputed": "disputed"}.get(judged)


def merge_back(path: Path, doc: dict, rows: list[dict], providers: list[str]) -> int:
    """把作答結果寫回擷取結果 YAML。回傳寫入的題數。"""
    by_id = {r["id"]: r for r in rows}
    source = "+".join(providers)
    written = 0
    for q in doc.get("questions") or []:
        r = by_id.get(q.get("id"))
        if not r or q.get("answer"):
            continue
        status = status_for(r["status"], providers)
        if not status:
            continue                       # 缺素材／呼叫失敗的不寫，留著下次再跑
        ok = [v for v in r["by_model"].values() if v and not v.get("error")]
        answer = next((v.get("answer") for v in ok if v.get("answer")), None)
        if not answer:
            continue
        q["answer"] = answer if isinstance(answer, list) else [answer]
        q["answer_status"] = status
        q["answer_source"] = source
        written += 1
    if written:
        path.write_text(yaml.safe_dump(doc, allow_unicode=True, sort_keys=False),
                        encoding="utf-8")
    return written


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("source", type=Path, help="擷取結果 YAML/JSON，或整個目錄")
    ap.add_argument("--figures", type=Path, required=True, help="圖檔目錄")
    ap.add_argument("-o", "--out", type=Path, default=Path("out/answers"))
    ap.add_argument("--providers", default="gemini,claude",
                    help="逗號分隔，預設 gemini,claude（刻意用不同家族）")
    ap.add_argument("--limit", type=int, help="每份卷只跑前 N 題，用於試水溫")
    ap.add_argument("--max-docs", type=int, help="只跑前 N 份卷")
    ap.add_argument("--merge", action="store_true", help="把答案寫回來源 YAML")
    ap.add_argument("--workers", type=int, default=4)
    args = ap.parse_args()

    sources = (sorted(args.source.glob("*.y*ml")) if args.source.is_dir()
               else [args.source])[: args.max_docs]

    chosen = []
    for name in args.providers.split(","):
        name = name.strip()
        if name not in PROVIDERS:
            return sys.exit(f"未知的 provider：{name}（可用：{', '.join(PROVIDERS)}）")
        _, env_key, _ = PROVIDERS[name]
        if not os.environ.get(env_key):
            print(f"⚠ 略過 {name}：環境變數 {env_key} 未設定")
            continue
        chosen.append(name)

    if not chosen:
        return sys.exit("沒有可用的 provider，請設定 API key")
    if len(chosen) == 1:
        print(f"⚠ 只有 {chosen[0]} 可用 —— 無法交叉驗證，全部結果都需要人工複核\n")

    def run_one(doc: dict) -> callable:
        def run(q: dict) -> dict:
            prompt = build_prompt(q, doc)
            images = collect_images(q, doc, args.figures)
            results: dict[str, dict] = {}
            for name in chosen:
                fn, _, model = PROVIDERS[name]
                try:
                    results[name] = fn(prompt, images, model)
                except Exception as exc:
                    results[name] = {"error": f"{type(exc).__name__}: {exc}"}
            status, note = judge(results)
            return {"id": q.get("id"), "number": q.get("number"), "type": q.get("type"),
                    "images_sent": len(images), "status": status, "note": note,
                    "by_model": results}
        return run

    from collections import Counter
    args.out.mkdir(parents=True, exist_ok=True)
    total = Counter()
    merged_total = 0

    for src in sources:
        doc = yaml.safe_load(src.read_text(encoding="utf-8"))
        # 兩種題目不送模型，因為錢是實打實地花：
        #   已有答案的 —— 答案卷解析出來的是確認過的，不該被模型的作答覆蓋。
        #   品管閘門會剔除的 —— 那些題目不會出現在檢索與組卷裡，替它們作答沒有用。
        todo = [q for q in doc.get("questions", [])
                if not q.get("answer") and evaluate(q, doc)[0]][: args.limit]
        if not todo:
            continue

        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            rows = list(pool.map(run_one(doc), todo))

        dest = args.out / f"{doc['document']['id']}.answers.yaml"
        dest.write_text(yaml.safe_dump(
            {"source": str(src), "providers": chosen, "answers": rows},
            allow_unicode=True, sort_keys=False), encoding="utf-8")

        tally = Counter(r["status"] for r in rows)
        total.update(tally)
        merged = merge_back(src, doc, rows, chosen) if args.merge else 0
        merged_total += merged
        print(f"  {src.name}  {len(rows)} 題  "
              + "  ".join(f"{k}={v}" for k, v in sorted(tally.items()))
              + (f"  → 寫回 {merged} 題" if args.merge else ""))

    # ── 摘要 ──────────────────────────────────────────────────
    label = {"agreed": "一致（可自動採用）",
             "agreed_low_confidence": "一致但信心偏低（建議抽查）",
             "disputed": "不一致（必須人工判定）",
             "missing_context": "模型回報缺少素材（先查組裝是否漏圖）",
             "single_source": "僅單一來源（無法驗證）",
             "failed": "呼叫失敗"}
    n = sum(total.values())
    print(f"\n{'=' * 56}")
    print(f"{len(sources)} 份卷、{n} 題，使用 {' + '.join(chosen)}")
    for k in label:
        if total.get(k):
            print(f"  {label[k]:<32} {total[k]:>5} 題")

    need = total.get("disputed", 0) + total.get("missing_context", 0)
    if n:
        print(f"\n人工需處理 {need}/{n} 題（{need / n:.0%}）")
    if args.merge:
        print(f"寫回來源 YAML {merged_total} 題（標為 ai_generated，非 verified）")
    print(f"明細寫入 {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
