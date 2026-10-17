#!/usr/bin/env python3
"""OLE 題庫取用模組：讀 data/export 的快照，篩選、隨機抽題、取附圖。只用 Python 標準函式庫。

說明文件：docs/USING_THE_BANK.md

    from oleqs_bank import Bank
    bank = Bank()                                     # 自動找 repo 裡的 data/export
    qs = bank.filter(subject="數學", grade=7, semester=1, unit_codes=["1-1"], difficulty=[1, 2])
    pick = bank.random(10, subject="自然", sub_subject="理化", grade=8, seed=1)
    png = bank.image(qs[0]["images"][0]["file"])      # 附圖（需要 OLEQS_SITE_CODE 或 Supabase 金鑰）

指令列：
    python client/oleqs_bank.py stats
    python client/oleqs_bank.py search --subject 數學 --grade 7 --semester 1 --unit 1-1 --limit 5
    python client/oleqs_bank.py random 10 --subject 英語 --grade 8 --seed 1
    python client/oleqs_bank.py show <題目ID>
    python client/oleqs_bank.py image <附圖 file> -o out.png
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import random as _random
import sys
import urllib.parse
import urllib.request
from collections import defaultdict
from pathlib import Path
from typing import Iterable

HERE = Path(__file__).resolve().parent
DEFAULT_EXPORT = HERE.parent / "data" / "export"
SITE = os.environ.get("OLEQS_SITE_URL", "https://ole-qs.vercel.app")
CACHE = Path(os.environ.get("OLEQS_CACHE", Path.home() / ".cache" / "oleqs"))
FORMAT_ORDER = {"choice": 0, "fill": 1, "apply": 2, "other": 3, "drill": 9}
LEVELS = {"易": [1], "中": [2], "難": [3, 4, 5], "挑戰": [4, 5]}


class Bank:
    def __init__(self, export_dir: str | Path | None = None):
        self.dir = Path(export_dir or os.environ.get("OLEQS_EXPORT", DEFAULT_EXPORT))
        if not (self.dir / "questions.jsonl.gz").is_file():
            raise FileNotFoundError(f"找不到題庫快照 {self.dir}/questions.jsonl.gz（見 docs/USING_THE_BANK.md）")
        self._qs: list[dict] | None = None
        self._by_id: dict[str, dict] = {}
        self._groups: dict[str, list[dict]] = {}
        self._papers: dict[str, dict] | None = None

    # ── 載入 ──
    @property
    def questions(self) -> list[dict]:
        """全部題目（含被品管剔除的，status == "rejected"），依題庫排序（sort_key）。"""
        if self._qs is None:
            with gzip.open(self.dir / "questions.jsonl.gz", "rt", encoding="utf-8") as f:
                qs = [json.loads(line) for line in f]
            qs.sort(key=lambda q: (q["sort_key"] if q["sort_key"] is not None else 1 << 40, q["id"]))
            self._qs = qs
            self._by_id = {q["id"]: q for q in qs}
            groups = defaultdict(list)
            for q in qs:
                if q["group_id"]:
                    groups[q["group_id"]].append(q)
            for g in groups.values():
                g.sort(key=lambda q: (q["section"] or 0, q["number"] or 0))
            self._groups = dict(groups)
        return self._qs

    @property
    def papers(self) -> dict[str, dict]:
        """原卷資料（學校、段考、大題），以 paper_id 為鍵。"""
        if self._papers is None:
            with gzip.open(self.dir / "papers.jsonl.gz", "rt", encoding="utf-8") as f:
                self._papers = {p["id"]: p for p in map(json.loads, f)}
        return self._papers

    @property
    def manifest(self) -> dict:
        return json.loads((self.dir / "manifest.json").read_text(encoding="utf-8"))

    def get(self, qid: str) -> dict | None:
        self.questions
        return self._by_id.get(qid)

    def group(self, q: dict | str) -> list[dict]:
        """題組的全部小題（同一篇閱讀短文、同一張圖）；不是題組就只有自己。"""
        q = self.get(q) if isinstance(q, str) else q
        return self._groups.get(q["group_id"], [q]) if q and q["group_id"] else ([q] if q else [])

    # ── 篩選 ──
    def filter(self, *, subject: str | None = None, sub_subject: str | None = None,
               grade: int | None = None, semester: int | None = None, year: int | None = None,
               exam_seq: int | None = None, unit_codes: Iterable[str] | None = None, publisher: str | None = None,
               types: Iterable[str] | None = None, formats: Iterable[str] | None = None,
               difficulty: Iterable[int | str] | None = None, patterns: Iterable[str] | None = None,
               has_image: bool | None = None, with_answer: bool = False, text: str | None = None,
               paper_id: str | None = None, include_rejected: bool = False, limit: int | None = None) -> list[dict]:
        """依條件篩選，回傳依題庫排序（選擇→填充→應用→其他，各自由易到難）的題目清單。

        unit_codes：單元代號，例如數學 "1-1"、英語主題 "B1-03"；國文、社會的代號各版本不同，請同時給 publisher。
        difficulty：1–5 的數字，或 "易"、"中"、"難"、"挑戰"。
        patterns：數學題型鍵，例如 "7-1_1-1:T07"。
        """
        units = set(unit_codes or [])
        typ, fmt, pat = set(types or []), set(formats or []), set(patterns or [])
        diff = {d for x in (difficulty or []) for d in (LEVELS.get(x, [x]) if isinstance(x, str) else [x])}
        needle = text.lower() if text else None
        out = []
        for q in self.questions:
            if not include_rejected and q["status"] == "rejected":
                continue
            if subject and q["subject"] != subject:
                continue
            if sub_subject and q["sub_subject"] != sub_subject:
                continue
            if grade and q["grade"] != grade:
                continue
            if semester and q["semester"] != semester:
                continue
            if year and q["year"] != year:
                continue
            if exam_seq and q["exam_seq"] != exam_seq:
                continue
            if paper_id and q["paper_id"] != paper_id:
                continue
            u = q["unit"] or {}
            if units and u.get("code") not in units:
                continue
            if publisher and units and q["subject"] in ("國文", "社會") and u.get("publisher") != publisher:
                continue
            if typ and q["type"] not in typ:
                continue
            if fmt and q["format"] not in fmt:
                continue
            if diff and q["difficulty"] not in diff:
                continue
            if pat and (q["pattern"] or {}).get("key") not in pat:
                continue
            if has_image is not None and bool(image_files(q)) != has_image:
                continue
            if with_answer and not q["answer"]:
                continue
            if needle and needle not in search_text(q):
                continue
            out.append(q)
            if limit and len(out) >= limit:
                break
        return out

    def random(self, n: int, *, seed: int | None = None, spread: str = "unit", whole_groups: bool = True,
               **filters) -> list[dict]:
        """隨機抽 n 題（同 filter 的條件）。

        spread："unit" 盡量平均分散到各單元、"pattern" 分散到各題型、"none" 純隨機。
        whole_groups：題組整組一起抽（不會只抽到題組的第 2 小題），題數可能略超過 n。
        回傳依題庫排序。
        """
        pool = self.filter(**filters)
        rng = _random.Random(seed)
        blocks: dict[str, list[dict]] = {}
        for q in pool:
            key = q["group_id"] if whole_groups and q["group_id"] else q["id"]
            blocks.setdefault(key, []).append(q)
        bins: dict[str, list[list[dict]]] = defaultdict(list)
        for b in blocks.values():
            q = b[0]
            k = ((q["unit"] or {}).get("code") if spread == "unit" else
                 (q["pattern"] or {}).get("key") if spread == "pattern" else "") or ""
            bins[k].append(b)
        for v in bins.values():
            rng.shuffle(v)
        keys = list(bins)
        rng.shuffle(keys)
        picked, i = [], 0
        while len(picked) < n and any(bins.values()):
            k = keys[i % len(keys)]
            if bins[k]:
                picked += bins[k].pop()
            i += 1
        return sorted(picked, key=lambda q: (q["sort_key"] if q["sort_key"] is not None else 1 << 40, q["id"]))

    # ── 分類表 ──
    def units(self, subject: str, grade: int, semester: int, publisher: str = "翰林",
              sub_subject: str | None = None) -> list[dict]:
        """某冊的單元表：[{code, title, chapter, subject}]。英語回傳跨版本文法主題。"""
        cur = json.loads((self.dir / "taxonomy" / "curriculum.json").read_text(encoding="utf-8"))
        if subject == "英語":
            return [{"code": t["id"], "title": t["title"], "chapter": t.get("group"), "subject": "英語"}
                    for t in cur["english"]
                    if t.get("group") == "綜合" or (t["grade"], t["semester"]) <= (grade, semester)]
        book = cur["junior"].get(str(grade), {}).get(str(semester), {}).get(subject, {})
        out = []
        for s, by_pub in book.items():
            if sub_subject and s != sub_subject:
                continue
            for u in by_pub.get(publisher, []):
                out.append({"code": u.get("lesson") or u.get("section"), "title": u["title"],
                            "chapter": (f"第{u['chapter']}章 {u['chapter_title']}" if u.get("chapter") else u.get("kind")),
                            "subject": s or subject})
        return out

    def patterns(self, grade: int, semester: int, unit_code: str) -> list[dict]:
        """數學題型目錄：[{key, code, name, desc, n}]。"""
        p = json.loads((self.dir / "taxonomy" / "math_patterns.json").read_text(encoding="utf-8"))
        k = f"{grade}-{semester}_{unit_code}"
        return [{"key": f"{k}:{t['code']}", **t} for t in p.get(k, {}).get("types", [])]

    # ── 附圖 ──
    def image(self, file: str) -> bytes:
        return fetch_image(file)


# ─────────────────────────── 工具函式 ───────────────────────────

def image_files(q: dict) -> list[str]:
    """這題用到的全部附圖檔名（題幹圖、題組共用圖、選項圖）。"""
    return ([i["file"] for i in q["shared_images"]] + [i["file"] for i in q["images"]]
            + [o["image"] for o in q["options"] if o["image"]])


def search_text(q: dict) -> str:
    return " ".join([q["stem"], q["group_stem"] or "", *(o["text"] for o in q["options"])]).lower()


def storage_key(file: str) -> str:
    """附圖在 Supabase Storage（bucket: assets）的物件鍵：路徑的 SHA-1（與 apps/api/storage.py 相同）。"""
    h = hashlib.sha1(file.replace("\\", "/").encode("utf-8")).hexdigest()
    return f"{h[:2]}/{h[2:26]}.png"


def fetch_image(file: str) -> bytes:
    """取附圖 PNG。先看本機快取，再依序試：
    1. OLEQS_SITE_CODE（選題網站通行碼）→ 經網站 /api/assets 讀取（唯讀，建議用這個）
    2. SUPABASE_URL ＋ SUPABASE_SERVICE_ROLE_KEY → 直接讀 Storage
    """
    file = file.replace("\\", "/").lstrip("/")
    cached = CACHE / storage_key(file)
    if cached.is_file():
        return cached.read_bytes()
    _load_env()
    code = os.environ.get("OLEQS_SITE_CODE")
    url, key = os.environ.get("SUPABASE_URL"), os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
    if code:
        token = hashlib.sha256(code.encode("utf-8")).hexdigest()
        req = urllib.request.Request(f"{SITE.rstrip('/')}/api/assets/" + urllib.parse.quote(file),
                                     headers={"Cookie": f"oleqs_access={token}"})
    elif url and key:
        req = urllib.request.Request(f"{url.rstrip('/')}/storage/v1/object/authenticated/assets/{storage_key(file)}",
                                     headers={"Authorization": f"Bearer {key}", "apikey": key})
    else:
        raise RuntimeError("取附圖需要環境變數 OLEQS_SITE_CODE（網站通行碼），或 SUPABASE_URL＋SUPABASE_SERVICE_ROLE_KEY；"
                           "請使用者上傳 .env.local（見 docs/USING_THE_BANK.md）")
    with urllib.request.urlopen(req, timeout=60) as r:
        data = r.read()
    cached.parent.mkdir(parents=True, exist_ok=True)
    cached.write_bytes(data)
    return data


def _load_env() -> None:
    """讀 repo 根目錄或目前目錄的 .env.local（不覆蓋已設定的環境變數）。"""
    for p in (Path.cwd() / ".env.local", HERE.parent / ".env.local"):
        if p.is_file():
            for line in p.read_text(encoding="utf-8").splitlines():
                if "=" in line and not line.lstrip().startswith("#"):
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def to_text(q: dict, answer: bool = True) -> str:
    """純文字（Markdown）呈現一題；公式維持 $…$ LaTeX，附圖以 [圖:檔名] 表示。"""
    lines = []
    if q["group_stem"]:
        lines.append(q["group_stem"])
    lines += [f"[圖:{i['file']}]" for i in q["shared_images"]]
    lines.append(f"{q['number']}. {q['stem']}")
    lines += [f"[圖:{i['file']}]" for i in q["images"]]
    for o in q["options"]:
        lines.append(f"({o['label']}) {o['text']}" + (f" [圖:{o['image']}]" if o["image"] else ""))
    if answer and q["answer"]:
        lines.append("答案：" + "、".join(map(str, q["answer"])))
    return "\n".join(lines)


# ─────────────────────────── 指令列 ───────────────────────────

def _filters(a) -> dict:
    return {k: v for k, v in dict(subject=a.subject, sub_subject=a.sub, grade=a.grade, semester=a.semester,
                                  year=a.year, unit_codes=a.unit, publisher=a.publisher, types=a.type,
                                  formats=a.format, difficulty=a.difficulty, text=a.text).items() if v}


def main() -> int:
    ap = argparse.ArgumentParser(description="OLE 題庫快照取用工具（說明：docs/USING_THE_BANK.md）")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("stats")
    for name in ("search", "random"):
        p = sub.add_parser(name)
        if name == "random":
            p.add_argument("n", type=int)
            p.add_argument("--seed", type=int)
            p.add_argument("--spread", default="unit", choices=["unit", "pattern", "none"])
        p.add_argument("--subject")
        p.add_argument("--sub", help="子科：理化、生物、地科、歷史、地理、公民")
        p.add_argument("--grade", type=int)
        p.add_argument("--semester", type=int)
        p.add_argument("--year", type=int)
        p.add_argument("--unit", action="append", help="單元代號，可重複")
        p.add_argument("--publisher")
        p.add_argument("--type", action="append", help="single multiple fill calc essay tf matching")
        p.add_argument("--format", action="append", help="choice fill apply other drill")
        p.add_argument("--difficulty", action="append", type=lambda s: int(s) if s.isdigit() else s)
        p.add_argument("--text")
        p.add_argument("--limit", type=int, default=20)
        p.add_argument("--json", action="store_true", help="輸出 JSON 而不是文字")
    p = sub.add_parser("show")
    p.add_argument("id")
    p = sub.add_parser("image")
    p.add_argument("file")
    p.add_argument("-o", "--out", required=True)
    a = ap.parse_args()

    bank = Bank()
    if a.cmd == "stats":
        m = bank.manifest
        qs = [q for q in bank.questions if q["status"] != "rejected"]
        by = defaultdict(int)
        for q in qs:
            by[(q["subject"], q["grade"])] += 1
        print(f"快照 {m['exported_at']}：{m['questions']:,} 題（可用 {len(qs):,}）、{m['papers']:,} 份卷")
        for (s, g), n in sorted(by.items()):
            print(f"  {s} {g} 年級：{n:,}")
        return 0
    if a.cmd == "show":
        q = bank.get(a.id)
        if not q:
            print("找不到這題")
            return 1
        print(json.dumps(q, ensure_ascii=False, indent=1))
        return 0
    if a.cmd == "image":
        Path(a.out).write_bytes(bank.image(a.file))
        print(f"→ {a.out}")
        return 0
    qs = (bank.random(a.n, seed=a.seed, spread=a.spread, **_filters(a)) if a.cmd == "random"
          else bank.filter(limit=a.limit, **_filters(a)))
    if a.json:
        print(json.dumps(qs, ensure_ascii=False, indent=1))
    else:
        for q in qs:
            u = q["unit"] or {}
            print(f"── {q['id']}｜{q['subject']}{q['sub_subject'] or ''} {u.get('code', '')} {u.get('title', '')}"
                  f"｜難度 {q['difficulty'] or '-'}")
            print(to_text(q))
        print(f"（{len(qs)} 題）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
