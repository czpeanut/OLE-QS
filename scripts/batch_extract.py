#!/usr/bin/env python3
"""用 Gemini 批次 API（半價）擷取考卷；流程與 vlm_extract.py 相同，只是改成整批送出。

    submit   把還沒擷取的卷分批送出（每批 --chunk 份），送出前檢查預算
    collect  收回已完成的批次，組成題庫 YAML、裁圖、記帳
    run      送出後每隔幾分鐘收一次，直到全部收完

批次失敗的卷（RECITATION、MAX_TOKENS、格式錯誤）記在 out/batches/extract_failed.txt，
之後可用 vlm_extract.py 即時重跑。

用法:
    python scripts/batch_extract.py run --list rounds_paths.txt --root <考卷根目錄> \\
        -o data/bank --assets data/assets --budget-twd 7000
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
import batch_api as B  # noqa: E402
from extract import decode_mojibake, parse_path  # noqa: E402
from generate_answers import loads_lenient  # noqa: E402
from vlm_extract import MODEL, finish, prepare, repair_tree, request_body  # noqa: E402

PHASE = "extract"
EST_USD_PER_PAPER = 0.0448 * B.BATCH_DISCOUNT     # 實測一般價每份 US$0.0448


def doc_id_of(p: Path) -> str | None:
    meta = parse_path(p)
    if not meta.get("school_short"):
        return None
    subj = (meta.get("subject") or "") + (f"_{meta['sub_subject']}" if meta.get("sub_subject") else "")
    sid = re.sub(r"[^\w]+", "_", decode_mojibake(p.stem)).strip("_").lower()
    sid = re.sub(r"[^\w]+", "_", f"{meta.get('city')}_{sid}_{subj}_g{meta.get('grade')}"
                                 f"s{meta.get('semester')}e{meta.get('exam_seq')}").strip("_")
    return f"doc_{meta.get('academic_year_roc')}_{sid}"


def pending(paths: list[Path], out: Path) -> list[Path]:
    done = {y.stem for y in out.glob("*.yaml")
            if "extractor: vlm:" in y.read_text(encoding="utf-8")[:4000]}
    queued = {p for j in B.load_jobs(PHASE) if not j.get("collected") for p in j["paths"]}
    failed = set(Path("out/batches/extract_failed.txt").read_text(encoding="utf-8").split("\n")) \
        if Path("out/batches/extract_failed.txt").is_file() else set()
    return [p for p in paths if doc_id_of(p) not in done and str(p) not in queued
            and str(p) not in failed]


def outstanding_usd() -> float:
    return sum(j["est_usd"] for j in B.load_jobs(PHASE) if not j.get("collected"))


def submit(args, paths: list[Path]) -> None:
    todo = pending(paths, args.out)
    print(f"待擷取 {len(todo)} 份；階段累計 NT${B.Ledger.twd():.0f}，"
          f"處理中預估 NT${outstanding_usd() * B.TWD_PER_USD:.0f}", flush=True)
    for start in range(0, len(todo), args.chunk):
        chunk = todo[start:start + args.chunk]
        est = EST_USD_PER_PAPER * len(chunk) * 1.2          # 預留兩成
        committed = B.Ledger.twd() + (outstanding_usd() + est) * B.TWD_PER_USD
        if committed > args.budget_twd:
            print(f"預算不足：送出這批後預估 NT${committed:.0f} > 上限 NT${args.budget_twd:.0f}，停止送出")
            return
        reqs, keep = [], []
        for p in chunk:
            parts, stats = prepare(p)
            if parts is None:
                print(f"  略過 {p}：{stats.get('skip')}")
                continue
            reqs.append((f"k{len(reqs)}", request_body(parts)))
            keep.append(str(p))
        label = f"extract_{time.strftime('%m%d_%H%M%S')}_{start}"
        job = B.gemini_submit(PHASE, label, MODEL, reqs, est_usd=est)
        job["paths"] = keep
        B.save_job(PHASE, job)
        print(f"  送出 {label}：{len(keep)} 份、{job['bytes'] / 1e6:.0f} MB → {job['name']}", flush=True)


def collect(args) -> int:
    left = 0
    for job in B.load_jobs(PHASE):
        if job.get("collected"):
            continue
        s = B.state(job)
        if s not in B.DONE["gemini"] and s not in B.FAILED["gemini"]:
            print(f"  {job['label']} {s}", flush=True)
            left += 1
            continue
        res = B.results(job)
        ok = bad = 0
        usd = 0.0
        failed = []
        for k, path in zip(job["keys"], job["paths"]):
            r = res.get(k) or {"error": "沒有回應"}
            usage = r.get("usageMetadata") or {}
            cost = B.gemini_cost(job["model"], usage)
            usd += cost
            try:
                cand = r["candidates"][0]
                reason = cand.get("finishReason")
                if reason not in (None, "STOP"):
                    raise RuntimeError(reason)
                text = "".join(x.get("text", "") for x in cand["content"]["parts"])
                # 原始輸出先存檔：組裝若出錯，可以不重送、直接重新組裝（reassemble）
                raw = Path("out/batches/raw_extract") / f"{doc_id_of(Path(path))}.json"
                raw.parent.mkdir(parents=True, exist_ok=True)
                raw.write_text(json.dumps({"path": path, "text": text, "usd": cost}, ensure_ascii=False),
                               encoding="utf-8")
                out = repair_tree(loads_lenient(text))
                doc, stats = finish(Path(path), args.assets, out, f"{job['model']}(batch)", cost,
                                    {"path": path})
                if not doc:
                    raise RuntimeError("組裝失敗")
                (args.out / f"{doc['document']['id']}.yaml").write_text(
                    yaml.safe_dump(doc, allow_unicode=True, sort_keys=False), encoding="utf-8")
                ok += 1
            except Exception as exc:  # noqa: BLE001
                bad += 1
                failed.append(path)
                print(f"    失敗 {Path(path).name}：{str(exc)[:120] or r.get('error')}", flush=True)
        if failed:
            with open("out/batches/extract_failed.txt", "a", encoding="utf-8") as fh:
                fh.write("\n".join(failed) + "\n")
        twd = B.Ledger.add(PHASE, usd)
        job["collected"] = True
        job["state"] = s
        job["usd"] = round(usd, 4)
        job.pop("_raw", None)
        B.save_job(PHASE, job)
        print(f"  收回 {job['label']}（{s}）：成功 {ok}、失敗 {bad}，US${usd:.2f}｜階段累計 NT${twd:.0f}",
              flush=True)
    return left


def reassemble(args) -> None:
    """用存下的原始輸出重新組裝（不呼叫模型）。"""
    n = 0
    for raw in sorted(Path("out/batches/raw_extract").glob("*.json")):
        x = json.loads(raw.read_text(encoding="utf-8"))
        out = repair_tree(loads_lenient(x["text"]))
        doc, _ = finish(Path(x["path"]), args.assets, out, f"{MODEL}(batch)", x["usd"], {"path": x["path"]})
        if doc:
            (args.out / f"{doc['document']['id']}.yaml").write_text(
                yaml.safe_dump(doc, allow_unicode=True, sort_keys=False), encoding="utf-8")
            n += 1
    print(f"重新組裝 {n} 份")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["submit", "collect", "run", "reassemble"])
    ap.add_argument("--list", type=Path)
    ap.add_argument("--root", type=Path, default=Path("."))
    ap.add_argument("-o", "--out", type=Path, default=Path("data/bank"))
    ap.add_argument("--assets", type=Path, default=Path("data/assets"))
    ap.add_argument("--chunk", type=int, default=300)
    ap.add_argument("--limit", type=int, help="只送出前 N 份（試跑）")
    ap.add_argument("--budget-twd", type=float, default=7000)
    ap.add_argument("--every", type=int, default=180)
    args = ap.parse_args()

    paths = []
    if args.list:
        paths = [args.root / l.strip() for l in args.list.read_text(encoding="utf-8").splitlines()
                 if l.strip()]
        if args.limit:
            paths = pending(paths, args.out)[:args.limit]
    if args.cmd == "reassemble":
        reassemble(args)
        return 0
    if args.cmd in ("submit", "run"):
        submit(args, paths)
    if args.cmd in ("collect", "run"):
        while True:
            left = collect(args)
            if not left or args.cmd == "collect":
                break
            time.sleep(args.every)
    print(f"階段累計 NT${B.Ledger.twd():.1f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
