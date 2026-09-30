"""Gemini 與 Qwen（阿里雲 Model Studio）的批次 API。

批次 API 半價、24 小時內回結果。流程都是：寫 JSONL → 上傳 → 建立批次 → 輪詢 → 下載結果。
每個批次的狀態記在 out/batches/<phase>.json（工作名稱、送出的 key、預估費用），
中途重開程式也能接著收結果；費用依回傳的實際 token 數記入同一份帳本。

用法（程式內）：
    job = gemini_submit(phase, name, model, [(key, request), ...])
    gemini_wait(job) → {key: response 或 {"error": ...}}
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

import requests

GEMINI = "https://generativelanguage.googleapis.com"
QWEN = "https://dashscope-intl.aliyuncs.com/compatible-mode/v1"
TWD_PER_USD = 32.5

# 每百萬 tokens 美元（輸入, 輸出），一般價；批次再打五折
GEMINI_PRICES = {
    "gemini-3.7-flash": (0.75, 3.75),
    "gemini-3.1-flash-lite": (0.25, 1.50),
}
QWEN_PRICES = {
    "qwen3.5-flash": (0.10, 0.40),
    "qwen3-vl-plus": (0.20, 1.60),
}
BATCH_DISCOUNT = 0.5
STATE_DIR = Path("out/batches")
LEDGER = Path("out/phase2_spend.json")


def _env(name: str) -> str:
    if name not in os.environ:
        for line in Path(".env.local").read_text(encoding="utf-8").splitlines():
            k, _, v = line.strip().partition("=")
            if k and k not in os.environ:
                os.environ[k] = v
    return os.environ[name]


# ─────────────────────────── 帳本 ───────────────────────────

class Ledger:
    lock = threading.Lock()

    @classmethod
    def read(cls) -> dict:
        if LEDGER.is_file():
            return json.loads(LEDGER.read_text(encoding="utf-8"))
        return {"usd": 0.0, "by_phase": {}}

    @classmethod
    def add(cls, phase: str, usd: float) -> float:
        with cls.lock:
            d = cls.read()
            d["usd"] = round(d["usd"] + usd, 5)
            d["by_phase"][phase] = round(d["by_phase"].get(phase, 0.0) + usd, 5)
            d["twd"] = round(d["usd"] * TWD_PER_USD, 1)
            d["updated"] = time.strftime("%Y-%m-%d %H:%M:%S")
            LEDGER.parent.mkdir(parents=True, exist_ok=True)
            tmp = LEDGER.with_suffix(".tmp")
            tmp.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
            tmp.replace(LEDGER)
            return d["twd"]

    @classmethod
    def twd(cls) -> float:
        return cls.read()["usd"] * TWD_PER_USD


def gemini_cost(model: str, usage: dict) -> float:
    p_in, p_out = GEMINI_PRICES.get(model, max(GEMINI_PRICES.values()))
    out = usage.get("candidatesTokenCount", 0) + usage.get("thoughtsTokenCount", 0)
    return (usage.get("promptTokenCount", 0) * p_in + out * p_out) / 1e6 * BATCH_DISCOUNT


def qwen_cost(model: str, usage: dict, batch: bool = True) -> float:
    p_in, p_out = QWEN_PRICES.get(model, (0.5, 3.0))
    c = (usage.get("prompt_tokens", 0) * p_in + usage.get("completion_tokens", 0) * p_out) / 1e6
    return c * (BATCH_DISCOUNT if batch else 1.0)


# ─────────────────────────── 狀態檔 ───────────────────────────

def _state_path(phase: str) -> Path:
    return STATE_DIR / f"{phase}.json"


def load_jobs(phase: str) -> list[dict]:
    p = _state_path(phase)
    return json.loads(p.read_text(encoding="utf-8")) if p.is_file() else []


def save_job(phase: str, job: dict) -> None:
    jobs = [j for j in load_jobs(phase) if j["name"] != job["name"]] + [job]
    p = _state_path(phase)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(jobs, ensure_ascii=False, indent=1), encoding="utf-8")


# ─────────────────────────── Gemini ───────────────────────────

def gemini_submit(phase: str, label: str, model: str, reqs: list[tuple[str, dict]],
                  est_usd: float = 0.0) -> dict:
    key = _env("GEMINI_API_KEY")
    h = {"x-goog-api-key": key}
    data = "\n".join(json.dumps({"key": k, "request": r}, ensure_ascii=False)
                     for k, r in reqs).encode("utf-8")
    r = requests.post(f"{GEMINI}/upload/v1beta/files", headers={
        **h, "X-Goog-Upload-Protocol": "resumable", "X-Goog-Upload-Command": "start",
        "X-Goog-Upload-Header-Content-Length": str(len(data)),
        "X-Goog-Upload-Header-Content-Type": "application/jsonl",
        "Content-Type": "application/json"}, json={"file": {"display_name": label}}, timeout=120)
    r.raise_for_status()
    up = r.headers["x-goog-upload-url"]
    r = requests.post(up, headers={"X-Goog-Upload-Offset": "0",
                                   "X-Goog-Upload-Command": "upload, finalize"},
                      data=data, timeout=3600)
    r.raise_for_status()
    fname = r.json()["file"]["name"]
    r = requests.post(f"{GEMINI}/v1beta/models/{model}:batchGenerateContent", headers=h,
                      json={"batch": {"display_name": label, "input_config": {"file_name": fname}}},
                      timeout=120)
    r.raise_for_status()
    job = {"provider": "gemini", "name": r.json()["name"], "label": label, "model": model,
           "keys": [k for k, _ in reqs], "est_usd": est_usd, "bytes": len(data),
           "submitted": time.strftime("%Y-%m-%d %H:%M:%S"), "collected": False}
    save_job(phase, job)
    return job


def gemini_state(job: dict) -> str:
    r = requests.get(f"{GEMINI}/v1beta/{job['name']}",
                     headers={"x-goog-api-key": _env("GEMINI_API_KEY")}, timeout=60)
    r.raise_for_status()
    d = r.json()
    job["_raw"] = d
    return (d.get("metadata") or {}).get("state") or d.get("state") or "UNKNOWN"


def gemini_results(job: dict) -> dict[str, dict]:
    d = job.get("_raw") or {}
    resp = d.get("response") or (d.get("metadata") or {}).get("output") or {}
    fname = resp.get("responsesFile") or (resp.get("output") or {}).get("responsesFile")
    out: dict[str, dict] = {}
    if fname:
        r = requests.get(f"{GEMINI}/download/v1beta/{fname}:download", params={"alt": "media"},
                         headers={"x-goog-api-key": _env("GEMINI_API_KEY")}, timeout=3600)
        r.raise_for_status()
        for line in r.content.decode("utf-8").splitlines():
            if line.strip():
                x = json.loads(line)
                out[x.get("key")] = x.get("response") or {"error": x.get("error") or x.get("status")}
    for item in (resp.get("inlinedResponses") or {}).get("inlinedResponses") or []:
        out[(item.get("metadata") or {}).get("key")] = item.get("response") or {"error": item.get("error")}
    return out


# ─────────────────────────── Qwen ───────────────────────────

def qwen_submit(phase: str, label: str, reqs: list[tuple[str, dict]], est_usd: float = 0.0) -> dict:
    h = {"Authorization": f"Bearer {_env('QWEN_API_KEY')}"}
    lines = [json.dumps({"custom_id": k, "method": "POST", "url": "/v1/chat/completions",
                         "body": body}, ensure_ascii=False) for k, body in reqs]
    data = "\n".join(lines).encode("utf-8")
    r = requests.post(f"{QWEN}/files", headers=h, files={"file": (f"{label}.jsonl", data)},
                      data={"purpose": "batch"}, timeout=3600)
    r.raise_for_status()
    fid = r.json()["id"]
    r = requests.post(f"{QWEN}/batches", headers=h, json={
        "input_file_id": fid, "endpoint": "/v1/chat/completions", "completion_window": "24h"},
        timeout=120)
    r.raise_for_status()
    job = {"provider": "qwen", "name": r.json()["id"], "label": label,
           "keys": [k for k, _ in reqs], "est_usd": est_usd, "bytes": len(data),
           "submitted": time.strftime("%Y-%m-%d %H:%M:%S"), "collected": False}
    save_job(phase, job)
    return job


def qwen_state(job: dict) -> str:
    r = requests.get(f"{QWEN}/batches/{job['name']}",
                     headers={"Authorization": f"Bearer {_env('QWEN_API_KEY')}"}, timeout=60)
    r.raise_for_status()
    job["_raw"] = r.json()
    return job["_raw"].get("status", "unknown")


def qwen_results(job: dict) -> dict[str, dict]:
    h = {"Authorization": f"Bearer {_env('QWEN_API_KEY')}"}
    out: dict[str, dict] = {}
    for fid_key in ("output_file_id", "error_file_id"):
        fid = (job.get("_raw") or {}).get(fid_key)
        if not fid:
            continue
        r = requests.get(f"{QWEN}/files/{fid}/content", headers=h, timeout=3600)
        r.raise_for_status()
        for line in r.content.decode("utf-8").splitlines():
            if line.strip():
                x = json.loads(line)
                resp = x.get("response") or {}
                out[x.get("custom_id")] = resp.get("body") if resp.get("status_code") == 200 \
                    else {"error": x.get("error") or resp}
    return out


DONE = {"gemini": ("BATCH_STATE_SUCCEEDED", "JOB_STATE_SUCCEEDED"),
        "qwen": ("completed",)}
FAILED = {"gemini": ("BATCH_STATE_FAILED", "BATCH_STATE_CANCELLED", "BATCH_STATE_EXPIRED",
                     "JOB_STATE_FAILED", "JOB_STATE_CANCELLED", "JOB_STATE_EXPIRED"),
          "qwen": ("failed", "expired", "cancelled")}


def state(job: dict) -> str:
    return gemini_state(job) if job["provider"] == "gemini" else qwen_state(job)


def results(job: dict) -> dict[str, dict]:
    return gemini_results(job) if job["provider"] == "gemini" else qwen_results(job)


def wait(job: dict, every: int = 60, log=print) -> tuple[str, dict[str, dict]]:
    """輪詢到結束，回傳 (最後狀態, 結果)。失敗時結果可能只有部分。"""
    while True:
        s = state(job)
        if s in DONE[job["provider"]] or s in FAILED[job["provider"]]:
            return s, results(job)
        log(f"  {job['label']} {s}")
        time.sleep(every)
