#!/usr/bin/env python3
"""AI 初審：把原卷頁面影像和擷取出來的題目一起送 Gemini，逐題比對，列出有問題的題目。

結果寫成審題台每站的 data/ai.json（初審完成的卷、每題的初審意見），由審題台讀取顯示；
不寫進題庫，也不寫進審題者的回報。

誤報的三道關卡：
1. 提示詞要求只比對「擷取有沒有照原卷」，原卷印錯但擷取照抄不算錯
2. 程式比對原卷文字層（verify）：擷取的寫法在原卷文字層裡就剔除
3. 報錯字的題目再請模型照抄原卷整題（recheck，不給它看擷取結果），抄出來和擷取一樣就剔除

試跑 52 份（2026-10）：每份約 US$0.017（即時，批次半價），留下的意見人工核對約九成屬實。

    # --pdf-root：原卷根目錄；--codes-root：審題台站點輸出根目錄（底下有 s7-1/codes.json 等）
    # 試跑幾份（即時呼叫），結果存到 out/ai_review/results/
    python scripts/ai_review.py pilot --pdf-root P --codes-root C --docs doc_a,doc_b --budget-twd 7000 --thinking medium
    # 整批：送批次、收回，再複核錯字
    python scripts/ai_review.py submit --pdf-root P --codes-root C --budget-twd 9000 --thinking medium
    python scripts/ai_review.py collect --pdf-root P --codes-root C
    python scripts/ai_review.py recheck --pdf-root P --codes-root C --batch   # 收回同樣用 collect
    # 產生並上傳每站的 ai.json
    python scripts/ai_review.py publish --codes-root C --upload
"""

from __future__ import annotations

import argparse
import base64
import json
import re
import sys
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import fitz
import requests
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from batch_api import (Ledger, _env, gemini_cost, gemini_submit, load_jobs, results,  # noqa: E402
                       save_job, state, DONE, FAILED)

REPO = Path(__file__).resolve().parent.parent
BANK = REPO / "data" / "bank"
OUT = REPO / "out" / "ai_review"
PHASE = "ai_review"
RESULTS = OUT / "results"
MODEL = "gemini-3.7-flash"
IMG_DPI = 150
THINKING = "medium"                 # low 會拿自己的知識「修正」原卷，誤報多
SITES = ["7-1", "7-2", "8-1", "8-2", "9-1", "9-2"]
# 與審題台的回報類別相同；答案對錯另有雙模型驗證，初審不檢查
CATS = ["公式錯誤", "錯字或漏字", "圖片錯誤或缺圖", "選項錯誤", "題組或短文錯誤",
        "題型或大題錯誤", "其他"]
TYPE_NAME = {"single": "單選", "multiple": "多選", "tf": "是非", "fill": "填充",
             "calc": "計算", "essay": "問答", "matching": "配合"}

PROMPT = """你是國中段考題庫的審題員。下面先附上一份考卷的原卷頁面（影像，有文字層的頁面另附文字層），
接著是從這份考卷擷取出來的題目。請逐題對照原卷，找出擷取結果「與原卷不一致」的地方。

最重要的原則：你只比對「擷取結果有沒有照原卷」。原卷本身印錯字、用詞不當，擷取照抄，就不是錯誤，
不要用你的知識去「修正」原卷（例如原卷印「增小」，擷取也是「增小」，就不要報）。
文字層的字形最準確（但順序可能亂、可能缺符號），影像用來看版面、公式與符號。

要檢查的錯誤（cats 從這些類別選）：
- 公式錯誤：數學式、化學式、單位與原卷不同（漏了絕對值、根號、次方、分數線、負號、括號，上下標錯位等）
- 錯字或漏字：文字與原卷不同，或漏了詞句造成意思改變
- 圖片錯誤或缺圖：題目需要圖（「如圖」「附圖」「下表」等）卻沒有附圖；或原卷的文字表格沒有擷取出來
- 選項錯誤：漏選項、選項內容錯置或合併、選項標號錯
- 題組或短文錯誤：題組說明或閱讀短文缺漏、截斷，或掛錯題目
- 題型或大題錯誤：題型標錯（例如填充題標成單選），或一題被拆成兩題、兩題被併成一題
- 其他：漏擷取整題、題號錯置等

擷取結果的標記方式（這些都是正確的，不是錯誤）：
- 「附圖 N 張」＝這題已經附上 N 張圖；「（選項為圖片）」＝這個選項已經附上圖片；
  「【題組共用附圖 N 張】」＝圖片已附在題組上，題組內各題共用。你看不到圖檔本身，不必檢查圖的內容或裁切。
- 聽力測驗、作文、需要聽音檔的題目刻意不收錄，不要報漏題。
- 題號、大題標題、配分文字不在題幹裡是正常的。
- 「承上題」的題目會把前一題題幹以「（第 N 題）…」附成題組說明，方便單獨出題時看得懂；
  大題標題（例如「一、寫出國字注音」）出現在題組說明也是刻意的。這兩種都不要報。
- 原卷畫底線、雙底線、粗體、框線的文字，擷取結果不一定保留格式；只有題目明確要你看「畫線處」卻無從辨認時才報。

不算錯誤、不要報：全形半形、空格、換行、標點的細微差別、填空底線的長短或寫法、用 LaTeX 或純文字寫同一個式子、
選項排版方式、答案對錯（另有雙模型驗證）。原卷本身的命題瑕疵只有在題目因此無法作答時才報（類別用「其他」）。

只列出確實有錯的題目，不要列出「擷取無誤」的題目。每個錯誤給：code（題目方括號裡的編號）、cats、
orig（原卷上那段文字，逐字照抄，10～30 字）、got（擷取結果裡對應的那段，逐字照抄）、
note（繁體中文，30 字內，說明哪裡不同），sure（true＝對照原卷確定有錯；false＝可能有錯、值得人工看）。
漏附圖、漏題、題型錯這類沒有對應文字的錯誤，orig、got 可以留空。
原卷有、擷取結果卻整題沒有的題目，寫進 missing（例如「第二大題第 5 題」）。整份都沒問題就回傳空的 issues。"""

SCHEMA = {
    "type": "object",
    "properties": {
        "issues": {"type": "array", "items": {
            "type": "object",
            "properties": {"code": {"type": "string"},
                           "cats": {"type": "array", "items": {"type": "string", "enum": CATS}},
                           "orig": {"type": "string"},
                           "got": {"type": "string"},
                           "note": {"type": "string"},
                           "sure": {"type": "boolean"}},
            "required": ["code", "cats", "orig", "got", "note", "sure"]}},
        "missing": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["issues", "missing"],
}


# ─────────────────────────── 題目代碼 ───────────────────────────

def load_codes(codes_root: Path) -> tuple[dict[str, str], dict[str, str]]:
    """回傳 (題目 ID → 審題編號, 題目 ID → 站)。"""
    q2c, q2s = {}, {}
    for site in SITES:
        f = codes_root / f"s{site}" / "codes.json"
        for c, qid in json.loads(f.read_text(encoding="utf-8")).items():
            q2c[qid], q2s[qid] = c, site
    return q2c, q2s


# ─────────────────────────── 組請求 ───────────────────────────

def listing(d: dict, q2c: dict[str, str]) -> tuple[str, dict[str, str]]:
    """擷取結果的文字版；回傳 (文字, 送出的代碼 → 題目 ID)。"""
    sec = {s.get("ord"): s.get("name") or "" for s in d["document"].get("sections") or []}
    shared: dict[str, int] = {}                 # 題目 ID → 它所屬題組共用的附圖張數
    for a in d.get("shared_assets") or []:
        for qid in a.get("used_by") or []:
            shared[qid] = shared.get(qid, 0) + 1
    lines, sent, last_sec, last_group = [], {}, None, None
    for q in d.get("questions") or []:
        c = q2c.get(q["id"])
        if not c:
            continue
        sent[c] = q["id"]
        if q.get("section") != last_sec:
            last_sec = q.get("section")
            lines.append(f"\n＝＝ 第 {last_sec} 大題：{sec.get(last_sec, '')} ＝＝")
        gs = (q.get("group_stem") or "").strip()
        if gs and gs != last_group:
            lines.append(f"【題組說明／短文】\n{gs}")
        if gs and gs != last_group and shared.get(q["id"]):
            lines.append(f"【題組共用附圖 {shared[q['id']]} 張】")
        last_group = gs or None
        head = f"[{c}] 第 {q.get('number')} 題（{TYPE_NAME.get(q.get('type'), q.get('type'))}，第 {q.get('page')} 頁）"
        figs = len(q.get("assets") or [])
        if shared.get(q["id"]) and not gs:
            figs += shared[q["id"]]
        if figs:
            head += f"　附圖 {figs} 張"
        lines.append(head)
        lines.append(q.get("stem") or "（無題幹）")
        for o in q.get("options") or []:
            body = o.get("content") or ""
            if o.get("asset"):
                body = (body + "（選項為圖片）").strip()
            lines.append(f"({o.get('label')}) {body}")
    return "\n".join(lines), sent


def build(path: Path, pdf_root: Path, q2c: dict[str, str]) -> tuple[dict | None, dict]:
    d = yaml.safe_load(path.read_text(encoding="utf-8"))
    src = (d.get("document") or {}).get("source_file")
    info = {"doc": d["document"]["id"], "pages": 0}
    if not src or not (pdf_root / src).is_file():
        info["skip"] = "找不到原卷"
        return None, info
    text, sent = listing(d, q2c)
    if not sent:
        info["skip"] = "沒有題目"
        return None, info
    parts: list[dict] = [{"text": PROMPT}]
    pdf = fitz.open(pdf_root / src)
    for i, page in enumerate(pdf, start=1):
        img = page.get_pixmap(dpi=IMG_DPI).tobytes("jpeg", jpg_quality=80)
        parts.append({"text": f"\n【原卷第 {i} 頁】"})
        parts.append({"inline_data": {"mime_type": "image/jpeg", "data": base64.b64encode(img).decode()}})
        layer = page.get_text("text").strip()
        if len(layer) > 50:
            parts.append({"text": f"【原卷第 {i} 頁文字層】\n{layer}"})
    info["pages"] = len(pdf)
    pdf.close()
    parts.append({"text": "\n【擷取結果】" + text})
    info["n"] = len(sent)
    body = {"contents": [{"parts": parts}],
            "generationConfig": {"responseMimeType": "application/json", "responseSchema": SCHEMA,
                                 "temperature": 0, "maxOutputTokens": 32768,
                                 "thinkingConfig": {"thinkingLevel": THINKING}}}
    return body, info


# ─────────────────────────── 程式複核 ───────────────────────────
#
# 模型偶爾會用自己的知識「修正」原卷（原卷印「增小」，它說應為「增加」），或引用擷取結果裡沒有的字。
# 有文字層的考卷可以直接比對：擷取的寫法在原卷文字層裡找得到、模型說的原卷寫法卻找不到，就是誤報。

def norm(s: str) -> str:
    s = unicodedata.normalize("NFKC", s or "")
    return re.sub(r"[\s\u3000_＿$]+", "", s)


def q_text(q: dict) -> str:
    return " ".join([q.get("group_stem") or "", q.get("stem") or "",
                     *[o.get("content") or "" for o in q.get("options") or []]])


TEXTUAL = {"錯字或漏字", "選項錯誤"}


def overlap(a: str, b: str, n: int = 4) -> float:
    """a 的 n 字片段有多少比例出現在 b 裡（模型引用時常加標記、省略號，不能要求整段相同）。"""
    a = re.sub(r"【[^】]*】|\(第\d+題\)|\([A-E]\)|…+|\.{3,}", "", a)
    grams = [a[i:i + n] for i in range(max(1, len(a) - n + 1))]
    return sum(g in b for g in grams) / len(grams)


def verify(doc: str, out: dict, pdf_root: Path) -> None:
    """在每個 issue 加上 drop（剔除理由）或 checked（文字層證實）。"""
    path = BANK / f"{doc}.yaml"
    d = yaml.safe_load(path.read_text(encoding="utf-8"))
    by_code = {}
    q2c = CODES[0]
    for q in d.get("questions") or []:
        if q["id"] in q2c:
            by_code[q2c[q["id"]]] = q
    layer = ""
    src = (d.get("document") or {}).get("source_file")
    if src and (pdf_root / src).is_file():
        with fitz.open(pdf_root / src) as pdf:
            layer = norm("".join(p.get_text("text") for p in pdf))
    for it in out.get("issues") or []:
        c = (it.get("code") or "").strip().strip("[]")
        q = by_code.get(c)
        orig, got = norm(it.get("orig", "")), norm(it.get("got", ""))
        if q is None:
            it["drop"] = "編號不在這份卷"
        elif (orig and orig == got and set(it.get("cats") or []) <= TEXTUAL
              and not re.search(r"底線|畫線|雙線|圖|框", it.get("note", ""))):
            it["drop"] = "原卷與擷取引文相同"
        elif len(got) >= 6 and overlap(got, norm(q_text(q))) < 0.3:
            it["drop"] = "擷取引文對不上"
        elif (layer and set(it.get("cats") or []) <= TEXTUAL and len(got) >= 2
              and not re.search(r"[$\\]", it.get("got", "")) and got in layer
              and (not orig or orig not in layer)):
            it["drop"] = "擷取與原卷文字層相同"
        elif layer and orig and orig in layer and got and got not in layer:
            it["checked"] = True


CODES: list[dict[str, str]] = [{}]


def parse(resp: dict) -> dict | None:
    try:
        text = "".join(p.get("text", "") for p in resp["candidates"][0]["content"]["parts"])
        return json.loads(text)
    except (KeyError, IndexError, json.JSONDecodeError):
        return None


def save(doc: str, out: dict, model: str, usage: dict) -> None:
    (RESULTS).mkdir(parents=True, exist_ok=True)
    out = {"doc": doc, "model": model, "at": time.strftime("%Y-%m-%dT%H:%M:%S"),
           "usage": usage, **out}
    (RESULTS / f"{doc}.json").write_text(json.dumps(out, ensure_ascii=False, indent=1),
                                                  encoding="utf-8")


# ─────────────────────────── 複核：請模型照抄原卷那一段 ───────────────────────────
#
# 掃描卷沒有文字層可比對。報「錯字」的題目再送一次，只附那一頁，請模型逐字照抄整題
# （不給它看擷取結果，也不告訴它哪裡可能有錯，免得被帶著抄），抄出來含擷取的寫法、不含它說的原卷寫法，就是誤報；
# 反過來則算證實。

RECHECK_PROMPT = """這是考卷的原卷頁面影像。請找到「{where}」這一題，逐字照抄原卷上印的內容{extra}。
原卷有錯字、怪字、注音、符號都照樣抄，不要修正，也不要補上原卷沒有的字；選項照原卷的標號抄。
數學式用 LaTeX 寫在 $…$ 裡。找不到這一題就 found 填 false。"""
RECHECK_SCHEMA = {"type": "object", "properties": {"found": {"type": "boolean"}, "text": {"type": "string"}},
                  "required": ["found", "text"]}
RECHECK_CATS = TEXTUAL | {"題組或短文錯誤"}


def recheck_requests(pdf_root: Path, docs: list[str] | None = None) -> list[tuple[str, dict]]:
    """每題一筆（同一題有好幾條意見也只抄一次）；key＝"doc|題目編號"。"""
    reqs = []
    q2c = CODES[0]
    for f in sorted(RESULTS.glob("*.json")):
        r = json.loads(f.read_text(encoding="utf-8"))
        if docs and r["doc"] not in docs:
            continue
        codes = {it["code"].strip().strip("[]") for it in r.get("issues") or []
                 if not it.get("drop") and not it.get("checked") and "recheck" not in it
                 and set(it.get("cats") or []) <= RECHECK_CATS
                 and not re.search(r"底線|畫線|雙線|圖|框", it.get("note", ""))
                 and len(norm(it.get("got", ""))) >= 2}
        if not codes:
            continue
        d = yaml.safe_load((BANK / f"{r['doc']}.yaml").read_text(encoding="utf-8"))
        sec = {s_.get("ord"): s_.get("name") or "" for s_ in d["document"].get("sections") or []}
        src = d["document"].get("source_file")
        if not src or not (pdf_root / src).is_file():
            continue
        with fitz.open(pdf_root / src) as pdf:
            for q in d.get("questions") or []:
                c = q2c.get(q["id"])
                if c not in codes:
                    continue
                pg = q.get("page") or 1
                pages = [p for p in ([pg - 1, pg] if q.get("group_stem") else [pg]) if 1 <= p <= len(pdf)]
                parts = []
                for p in pages:
                    img = pdf[p - 1].get_pixmap(dpi=200).tobytes("jpeg", jpg_quality=85)
                    parts.append({"inline_data": {"mime_type": "image/jpeg", "data": base64.b64encode(img).decode()}})
                where = f"{sec.get(q.get('section'), '第 %s 大題' % q.get('section'))}　第 {q.get('number')} 題"
                extra = "：題組說明或閱讀短文（這題所屬的）、題幹、選項" if q.get("group_stem") else "：題幹、選項"
                parts.append({"text": RECHECK_PROMPT.format(where=where.strip(), extra=extra)})
                reqs.append((f"{r['doc']}|{c}", {
                    "contents": [{"parts": parts}],
                    "generationConfig": {"responseMimeType": "application/json", "responseSchema": RECHECK_SCHEMA,
                                         "temperature": 0, "maxOutputTokens": 16384,
                                         "thinkingConfig": {"thinkingLevel": "low"}}}))
    return reqs


def in_text(quote: str, text: str) -> bool:
    """引文（可能含省略號、選項標號）的每一段都出現在 text 裡。"""
    segs = [norm(x) for x in re.split(r"…+|\.{3,}|\([A-E]\)|【[^】]*】", quote)]
    segs = [x for x in segs if len(x) >= 2]
    return bool(segs) and all(x in text for x in segs)


def apply_recheck(answers: dict[str, dict]) -> int:
    """answers: "doc|題目編號" → 模型照抄的結果。回傳剔除的數量。"""
    by_doc: dict[str, dict[str, dict]] = {}
    for k, v in answers.items():
        doc, c = k.rsplit("|", 1)
        by_doc.setdefault(doc, {})[c] = v
    dropped = 0
    for doc, items in by_doc.items():
        f = RESULTS / f"{doc}.json"
        r = json.loads(f.read_text(encoding="utf-8"))
        for it in r.get("issues") or []:
            v = items.get(it["code"].strip().strip("[]"))
            if v is None or it.get("drop"):
                continue
            if not v.get("found"):
                it["recheck"] = None
                continue
            text = norm(v.get("text", ""))
            it["recheck"] = v.get("text", "")[:2000]
            if in_text(it.get("got", ""), text) and not in_text(it.get("orig", ""), text):
                it["drop"] = "複核：原卷與擷取相同"
                dropped += 1
            elif not in_text(it.get("got", ""), text):
                it["checked"] = True             # 照抄的原卷裡沒有擷取的寫法
        f.write_text(json.dumps(r, ensure_ascii=False, indent=1), encoding="utf-8")
    return dropped


def cmd_recheck(args) -> int:
    CODES[0], _ = load_codes(args.codes_root)
    docs = args.docs.split(",") if args.docs else None
    reqs = recheck_requests(args.pdf_root, docs)
    print(f"要複核 {len(reqs)} 條")
    if not reqs:
        return 0
    if args.batch:
        job = gemini_submit(PHASE, f"ai_recheck_{time.strftime('%m%d_%H%M%S')}", args.model, reqs,
                            est_usd=len(reqs) * 0.002)
        print(f"  送出 {job['label']}")
        return 0
    key = _env("GEMINI_API_KEY")

    def one(kr):
        k, body = kr
        for attempt in range(5):
            r = requests.post(f"https://generativelanguage.googleapis.com/v1beta/models/{args.model}:generateContent",
                              params={"key": key}, json=body, timeout=300)
            if r.status_code in (429, 500, 502, 503, 504):
                time.sleep(5 * 2 ** attempt)
                continue
            r.raise_for_status()
            data = r.json()
            return k, parse(data), gemini_cost(args.model, data.get("usageMetadata", {})) * 2
        return k, None, 0.0

    answers, usd = {}, 0.0
    with ThreadPoolExecutor(args.workers) as pool:
        for k, out, c in pool.map(one, reqs):
            usd += c
            if out is not None:
                answers[k] = out
    Ledger.add(PHASE, usd)
    print(f"剔除 {apply_recheck(answers)} 條；US${usd:.3f}，累計 NT${Ledger.twd():.0f}")
    return 0


# ─────────────────────────── 指令 ───────────────────────────

def todo(args, q2c) -> list[Path]:
    done = {p.stem for p in (RESULTS).glob("*.json")} if (RESULTS).is_dir() else set()
    paths = sorted(BANK.glob("*.yaml"))
    if args.docs:
        want = set(args.docs.split(","))
        paths = [p for p in paths if p.stem in want]
    return [p for p in paths if p.stem not in done][: args.limit or None]


def cmd_pilot(args) -> int:
    q2c, _ = load_codes(args.codes_root)
    CODES[0] = q2c
    key = _env("GEMINI_API_KEY")
    paths = todo(args, q2c)

    def one(p: Path):
        body, info = build(p, args.pdf_root, q2c)
        if body is None:
            return info, 0.0
        if Ledger.twd() > args.budget_twd:
            info["skip"] = "超過預算"
            return info, 0.0
        for attempt in range(5):
            r = requests.post(f"https://generativelanguage.googleapis.com/v1beta/models/{args.model}:generateContent",
                              params={"key": key}, json=body, timeout=600)
            if r.status_code in (429, 500, 502, 503, 504):
                time.sleep(5 * 2 ** attempt)
                continue
            r.raise_for_status()
            break
        data = r.json()
        usage = data.get("usageMetadata", {})
        usd = gemini_cost(args.model, usage) * 2          # 即時呼叫沒有批次折扣
        Ledger.add(PHASE, usd)
        out = parse(data)
        if out is None:
            info["skip"] = "輸出無法解析"
            return info, usd
        verify(info["doc"], out, args.pdf_root)
        save(info["doc"], out, args.model, usage)
        info["issues"] = sum(1 for it in out.get("issues") or [] if not it.get("drop"))
        return info, usd

    total = 0.0
    with ThreadPoolExecutor(args.workers) as pool:
        for info, usd in pool.map(one, paths):
            total += usd
            print(f"  {info['doc']}  {info.get('pages')} 頁  {info.get('n', 0)} 題  "
                  f"{info.get('skip') or str(info.get('issues')) + ' 個問題'}  US${usd:.3f}")
    print(f"共 {len(paths)} 份，US${total:.2f}（NT${total * 32.5:.0f}）；累計 NT${Ledger.twd():.0f}")
    return 0


def cmd_submit(args) -> int:
    q2c, _ = load_codes(args.codes_root)
    paths = todo(args, q2c)
    pending = {k for j in load_jobs(PHASE) if not j.get("collected") for k in j["keys"]}
    paths = [p for p in paths if p.stem not in pending]
    est_per_doc = args.est_usd_per_doc
    if Ledger.twd() + len(paths) * est_per_doc * 32.5 > args.budget_twd:
        print(f"預估超過預算：{len(paths)} 份 × US${est_per_doc} → 累計會到 "
              f"NT${Ledger.twd() + len(paths) * est_per_doc * 32.5:.0f}")
        return 1
    chunk, size, n = [], 0, 0
    for p in paths:
        body, info = build(p, args.pdf_root, q2c)
        if body is None:
            print("  略過", info["doc"], info.get("skip"))
            continue
        b = len(json.dumps(body))
        if chunk and size + b > args.chunk_mb * 2**20:
            job = gemini_submit(PHASE, f"ai_review_{time.strftime('%m%d_%H%M%S')}_{n}", args.model, chunk,
                                est_usd=len(chunk) * est_per_doc)
            print(f"  送出 {job['label']}：{len(chunk)} 份")
            chunk, size, n = [], 0, n + 1
        chunk.append((info["doc"], body))
        size += b
    if chunk:
        job = gemini_submit(PHASE, f"ai_review_{time.strftime('%m%d_%H%M%S')}_{n}", args.model, chunk,
                            est_usd=len(chunk) * est_per_doc)
        print(f"  送出 {job['label']}：{len(chunk)} 份")
    return 0


def cmd_collect(args) -> int:
    CODES[0], _ = load_codes(args.codes_root)
    waiting = 0
    for job in load_jobs(PHASE):
        if job.get("collected"):
            continue
        s = state(job)
        if s in FAILED[job["provider"]]:
            print(f"  {job['label']} 失敗：{s}")
            job["collected"] = True
            save_job(PHASE, {k: v for k, v in job.items() if k != "_raw"})
            continue
        if s not in DONE[job["provider"]]:
            print(f"  {job['label']} {s}")
            waiting += 1
            continue
        usd, ok = 0.0, 0
        if job["label"].startswith("ai_recheck"):
            res = results(job)
            usd = sum(gemini_cost(job["model"], (v or {}).get("usageMetadata", {})) for v in res.values())
            n = apply_recheck({k: parse(v) for k, v in res.items() if isinstance(v, dict) and parse(v)})
            Ledger.add(PHASE, usd)
            job["collected"], job["usd"] = True, usd
            save_job(PHASE, {k: v for k, v in job.items() if k != "_raw"})
            print(f"  {job['label']} 複核 {len(res)} 條，剔除 {n}；US${usd:.2f}")
            continue
        for doc, resp in results(job).items():
            usage = resp.get("usageMetadata", {}) if isinstance(resp, dict) else {}
            usd += gemini_cost(job["model"], usage)
            out = parse(resp) if isinstance(resp, dict) else None
            if out is not None:
                verify(doc, out, args.pdf_root)
                save(doc, out, job["model"], usage)
                ok += 1
        Ledger.add(PHASE, usd)
        job["collected"] = True
        job["usd"] = usd
        save_job(PHASE, {k: v for k, v in job.items() if k != "_raw"})
        print(f"  {job['label']} 收回 {ok}/{len(job['keys'])} 份，US${usd:.2f}")
    print(f"未完成 {waiting} 批；累計 NT${Ledger.twd():.0f}")
    return 0


def cmd_publish(args) -> int:
    """每站一個 ai.json：{papers: {doc: {at, n}}, q: {code: [{cats, note, sure}]}, missing: {doc: [...]}}"""
    q2c, q2s = load_codes(args.codes_root)
    c2q = {c: q for q, c in q2c.items()}
    sites = {s: {"model": None, "papers": {}, "q": {}, "missing": {}} for s in SITES}
    for f in sorted((RESULTS).glob("*.json")):
        r = json.loads(f.read_text(encoding="utf-8"))
        doc = r["doc"]
        qids = [q for q in q2c if q.startswith(doc + "_")]
        if not qids:
            continue
        site = sites[q2s[qids[0]]]
        site["model"] = r["model"]
        n = 0
        for it in r.get("issues") or []:
            if it.get("drop"):
                continue
            c = (it.get("code") or "").strip().strip("[]")
            if c2q.get(c, "").startswith(doc + "_") and it.get("cats"):
                site["q"].setdefault(c, []).append({"cats": it["cats"], "note": it.get("note", "")[:120],
                                                    "sure": bool(it.get("sure"))})
                n += 1
        if r.get("missing"):
            site["missing"][doc] = r["missing"][:20]
        site["papers"][doc] = {"at": r["at"], "n": n}
    dest = OUT / "sites"
    for s, data in sites.items():
        p = dest / s / "ai.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        print(f"  {s}：{len(data['papers'])} 份初審完成，{len(data['q'])} 題有意見")
    if args.upload:
        url, key = _env("SUPABASE_URL").rstrip("/"), _env("SUPABASE_SERVICE_ROLE_KEY")
        h = {"Authorization": f"Bearer {key}", "apikey": key, "x-upsert": "true",
             "Content-Type": "application/json"}
        for s in SITES:
            r = requests.post(f"{url}/storage/v1/object/review/{s}/data/ai.json", headers=h,
                              data=(dest / s / "ai.json").read_bytes(), timeout=120)
            r.raise_for_status()
        print("已上傳到 Supabase Storage（review/<站>/data/ai.json）")
    return 0


def main() -> int:
    global THINKING, RESULTS
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("pilot", "submit"):
        p = sub.add_parser(name)
        p.add_argument("--pdf-root", type=Path, required=True)
        p.add_argument("--codes-root", type=Path, required=True)
        p.add_argument("--budget-twd", type=float, required=True, help="累計花費（phase2 帳本）的上限")
        p.add_argument("--model", default=MODEL)
        p.add_argument("--docs", default="")
        p.add_argument("--limit", type=int, default=0)
        p.add_argument("--workers", type=int, default=6)
        p.add_argument("--chunk-mb", type=int, default=1500)
        p.add_argument("--est-usd-per-doc", type=float, default=0.009)
        p.add_argument("--thinking", default=THINKING, choices=["minimal", "low", "medium", "high"])
        p.add_argument("--results", type=Path, default=RESULTS, help="試跑比較設定時另存一個資料夾")
    p = sub.add_parser("recheck")
    p.add_argument("--pdf-root", type=Path, required=True)
    p.add_argument("--codes-root", type=Path, required=True)
    p.add_argument("--model", default=MODEL)
    p.add_argument("--docs", default="")
    p.add_argument("--workers", type=int, default=8)
    p.add_argument("--batch", action="store_true")
    p.add_argument("--results", type=Path, default=RESULTS)
    p = sub.add_parser("collect")
    p.add_argument("--pdf-root", type=Path, required=True)
    p.add_argument("--codes-root", type=Path, required=True)
    p = sub.add_parser("publish")
    p.add_argument("--codes-root", type=Path, required=True)
    p.add_argument("--upload", action="store_true")
    args = ap.parse_args()
    THINKING = getattr(args, "thinking", THINKING)
    RESULTS = getattr(args, "results", RESULTS)
    return {"pilot": cmd_pilot, "submit": cmd_submit, "recheck": cmd_recheck, "collect": cmd_collect, "publish": cmd_publish}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
