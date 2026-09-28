#!/usr/bin/env python3
"""考卷 PDF → 結構化題目 YAML（視覺模型擷取，文字層驗證）。

為什麼不用規則式（extract.py）：
    規則式是為數學卷調出來的。擴展到八個科目後實測 33 份樣本卷：英文卷
    一份只抓到 5 題（實際 40~50 題），7 份卷一題都抓不到，而其中 5 份
    其實有完整的文字層 —— 失敗的是版面解析，不是檔案。另外規則式天生
    做不好三件事，而這三件恰好是題庫最不能錯的：
      - 公式：分數被攤平成「1 2」，次方「a³」變成「a 3」
      - 閱讀題組：短文夾在兩題之間，被黏到上一題的最後一個選項
      - 掃描件：沒有文字層就一個字都取不到

做法：
    每份卷的頁面影像**連同該頁的文字層**一起送給模型。版面結構（哪些字是
    分數、上標，哪段短文屬於哪幾題，哪張圖是哪題的）由模型看影像判斷；
    字則要求它照抄文字層，而不是自己辨識 —— 模型負責讀懂版面，
    不負責重新打字。

    模型的輸出一律要過文字層驗證：每一題的內容都必須能在原卷文字層裡找到。
    找不到的題目（模型自己補寫、改寫、或誤讀）標為辨識不清，由品管閘門剔除。
    出處（年級、科目、學年度、學期、段考次別、縣市、學校）一律取自路徑，
    不交給模型。

用法:
    python scripts/vlm_extract.py 考卷.pdf -o data/bank --assets data/assets
    python scripts/vlm_extract.py --list 清單.txt --root 考卷根目錄 -o data/bank --assets data/assets
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
import unicodedata
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import requests
import yaml

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts"))

from extract import (attach_answers, attach_carry_context, build_school,  # noqa: E402
                     decode_mojibake, fitz, parse_path, split_answer, split_options)
from generate_answers import load_env_file, loads_lenient, redact  # noqa: E402

MODEL = "gemini-3.7-flash"
IMG_DPI = 150          # 送模型的解析度：夠看清上下標，token 又不會太多
CROP_DPI = 220         # 切圖用的解析度：老師印卷時要清楚

TYPES = ["single", "multiple", "tf", "fill", "calc", "essay", "matching"]

FIG = {"type": "OBJECT", "properties": {
    "page": {"type": "INTEGER"},
    "box": {"type": "ARRAY", "items": {"type": "INTEGER"}}},
    "required": ["page", "box"]}

SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "header": {"type": "OBJECT", "properties": {
            "school": {"type": "STRING", "nullable": True},
            "exam_title": {"type": "STRING", "nullable": True}}},
        "scope": {"type": "OBJECT", "nullable": True, "properties": {
            "raw": {"type": "STRING"},
            "publisher": {"type": "STRING", "nullable": True},
            "volume": {"type": "STRING", "nullable": True},
            "lessons": {"type": "ARRAY", "items": {"type": "OBJECT", "properties": {
                "number": {"type": "STRING"},
                "title": {"type": "STRING", "nullable": True}}}}}},
        "sections": {"type": "ARRAY", "items": {"type": "OBJECT", "properties": {
            "ord": {"type": "INTEGER"},
            "name": {"type": "STRING"}},
            "required": ["ord", "name"]}},
        "groups": {"type": "ARRAY", "items": {"type": "OBJECT", "properties": {
            "key": {"type": "STRING"},
            "section": {"type": "INTEGER"},
            "first": {"type": "INTEGER"},
            "last": {"type": "INTEGER"},
            "passage": {"type": "STRING"},
            "figures": {"type": "ARRAY", "items": FIG}},
            "required": ["key", "section", "first", "last", "passage"]}},
        "questions": {"type": "ARRAY", "items": {"type": "OBJECT", "properties": {
            "section": {"type": "INTEGER"},
            "number": {"type": "INTEGER"},
            "type": {"type": "STRING", "enum": TYPES},
            "page": {"type": "INTEGER"},
            "stem": {"type": "STRING"},
            "options": {"type": "ARRAY", "items": {"type": "OBJECT", "properties": {
                "label": {"type": "STRING"},
                "content": {"type": "STRING"},
                "figure": {**FIG, "nullable": True}},
                "required": ["label", "content"]}},
            "group": {"type": "STRING", "nullable": True},
            "figures": {"type": "ARRAY", "items": FIG},
            "listening": {"type": "BOOLEAN"},
            "lesson": {"type": "OBJECT", "nullable": True, "properties": {
                "number": {"type": "STRING", "nullable": True},
                "title": {"type": "STRING", "nullable": True},
                "basis": {"type": "STRING", "nullable": True}}}},
            "required": ["section", "number", "type", "page", "stem"]}},
        "answer_key": {"type": "ARRAY", "items": {"type": "OBJECT", "properties": {
            "section": {"type": "INTEGER"},
            "number": {"type": "INTEGER"},
            "answer": {"type": "ARRAY", "items": {"type": "STRING"}}},
            "required": ["section", "number", "answer"]}},
    },
    "required": ["sections", "groups", "questions"],
}

PROMPT = """你是題庫建置人員，要把一份台灣國中段考考卷逐題轉成結構化資料。
附上的是每一頁的影像，以及同一頁 PDF 的文字層（【第N頁文字層】）。

# 最重要的規則：照抄，不要改寫
- 題目、選項、短文的**文字一律照抄文字層**，不得改寫、潤飾、摘要、補字、翻譯。
- 文字層的字序被版面打亂（例如分數的分子分母、上標被拆開）時，依影像還原正確的結構，
  但字本身仍以文字層為準。
- 只有文字層缺字或是亂碼（或該頁沒有文字層）時，才依影像辨識。
- 看不清楚的字寫成「□」，不要猜。

# 公式（數學、自然）：用行內 LaTeX，前後加 $
- 分數 $\\frac{2}{3}$；帶分數 $-5\\frac{1}{2}$；次方 $x^{2}$、$10^{-3}$、$(-2)^{3}$
- 下標與化學式 $H_{2}O$、$CO_{2}$；離子 $Na^{+}$、$SO_{4}^{2-}$；單位 cm$^{3}$
- 根號 $\\sqrt{5}$；線段 $\\overline{AB}$；角 $\\angle A$；三角形 $\\triangle ABC$
- 乘除 $\\times$ $\\div$；不等號 $\\leq$ $\\geq$ $\\neq$；約等於 $\\approx$；π 用 $\\pi$
- **只能用上面列出的指令**。其他符號直接寫 Unicode 字元（∵ ∴ ≒ ° ⊥ ∥），
  不要用 \\mathrm、\\text、\\cdot 以外的指令。
- 普通的數字與文字不要包進 $ 裡。
- 輸出是 JSON：字串裡的反斜線一律寫成兩個（例如 "$\\\\frac{1}{2}$"），否則 \\t、\\f、\\n 會被當成控制字元。

# 題組（閱讀測驗、克漏字、題組）
- 凡是「閱讀下文回答第X～Y題」「克漏字」「根據下圖回答…」這類多題共用的素材，
  一律放進 groups：passage 照抄**完整**短文（含標題、註釋、表格），
  first／last 是它涵蓋的題號，figures 是共用的圖。
- 題組內每一題的 group 填該題組的 key；stem 只放這一題自己的題幹。
- 克漏字的題目通常沒有題幹，stem 寫「（克漏字第N格）」。
- 短文**不可以**併到任何一題的題幹或選項裡。

# 圖
- 每題專屬的圖放在該題 figures；選項本身是圖時放在該選項的 figure；
  題組共用的圖放在 group.figures。
- box 是該頁影像上的位置 [ymin, xmin, ymax, xmax]，範圍 0～1000。
  框要把圖內的標號（A、B、O、x、y、甲乙丙）一起框進去。
- 純文字的表格請轉成 Markdown 表格寫進題幹或短文；看不出表格結構的才當成圖。
- 校徽、浮水印、裝飾線不是圖。

# 其他
- sections：大題依卷面順序從 1 開始編號（例如「一、選擇題」ord=1）。沒有大題標題時整份卷算一個大題。
- 題號照卷面。type：single 單選、multiple 多選、tf 是非、fill 填充、calc 計算／非選、
  essay 問答／作文／翻譯、matching 配合。
- 英文聽力題 listening=true（內容照抄即可）。
- 答案頁、答案卷、空白作答卷**不要**當成題目。若卷上印有正確答案，填入 answer_key。
- scope：卷頭若寫了命題範圍（例如「康軒版第三冊第1課～第4課」「Book 1 Starter～Lesson 3」），
  raw 照抄，並拆出版本、冊別與課次清單（有寫課名就一併填）。
- lesson（僅國文、英文）：這一題出自哪一課。number 填課次（「第3課」「Lesson 2」），
  title 填課名（「夏夜」「What Time Is It?」），basis 用一句話寫出判斷依據
  （題目寫了課名、作者、引用了該課課文原句或注釋）。
  只有能確定時才填；像字音字形、成語這類看不出出自哪一課的題目填 null，不要猜。
- header.school：卷面印的校名；exam_title：卷面印的考試名稱。
"""


# ─────────────────────────── 模型呼叫 ───────────────────────────

# 官方價格（美元／每百萬 tokens，2026 年底前）。思考用的 tokens 以輸出價計。
# 2027 年起 3.6～3.8 flash 漲為兩倍，屆時要更新這張表。
PRICES = {
    "gemini-3.7-flash": (0.75, 3.75),
    "gemini-3.6-flash": (0.75, 3.75),
    "gemini-3.8-flash": (0.75, 3.75),
    "gemini-3-flash-preview": (0.50, 3.00),
}
# 主力模型塞車（503）是常態而非例外 —— 實測深夜也會連續六次 503。
# 依序改用同價位的其他 flash，不要整份卷因為一個模型忙線就失敗。
FALLBACK = ["gemini-3.7-flash", "gemini-3.6-flash", "gemini-3.8-flash", "gemini-3-flash-preview"]
TWD_PER_USD = 32.5      # 保守換算，高估台幣費用比低估安全


class Usage:
    """累計用量與費用。費用寫進檔案，分好幾輪執行時接續累計，預算上限才有意義。"""
    lock = threading.Lock()
    prompt = out = thought = calls = 0
    usd = 0.0              # 含先前各輪的累計
    run_usd = 0.0          # 只算這一輪，用來估每份卷的平均花費
    ledger: Path | None = None

    @classmethod
    def load(cls, ledger: Path) -> None:
        cls.ledger = ledger
        if ledger.is_file():
            d = json.loads(ledger.read_text(encoding="utf-8"))
            cls.usd = d.get("usd", 0.0)

    @classmethod
    def add(cls, model: str, meta: dict) -> float:
        p_in, p_out = PRICES.get(model, max(PRICES.values()))
        pt = meta.get("promptTokenCount", 0)
        ot = meta.get("candidatesTokenCount", 0)
        th = meta.get("thoughtsTokenCount", 0)
        cost = (pt * p_in + (ot + th) * p_out) / 1e6
        with cls.lock:
            cls.calls += 1
            cls.prompt += pt
            cls.out += ot
            cls.thought += th
            cls.usd += cost
            cls.run_usd += cost
            if cls.ledger:
                cls.ledger.write_text(json.dumps(
                    {"usd": round(cls.usd, 4), "twd": round(cls.usd * TWD_PER_USD, 1),
                     "updated": time.strftime("%Y-%m-%d %H:%M:%S")}), encoding="utf-8")
        return cost

    @classmethod
    def twd(cls) -> float:
        return cls.usd * TWD_PER_USD


def call_model(parts: list[dict], model: str) -> tuple[dict, str, float]:
    """回傳 (輸出, 實際使用的模型, 這次花費美元)。"""
    key = os.environ["GEMINI_API_KEY"]
    body = {"contents": [{"parts": parts}],
            "generationConfig": {"responseMimeType": "application/json",
                                 "responseSchema": SCHEMA,
                                 "temperature": 0,
                                 "maxOutputTokens": 65536,
                                 # 擷取是照抄加判斷版面，不需要深度推理；
                                 # 思考 tokens 以輸出價計費，放著不管會是最大的一筆
                                 "thinkingConfig": {"thinkingLevel": "low"}}}
    order = [model] + [m for m in FALLBACK if m != model]
    last = None
    attempt_skip: set[str] = set()
    for attempt in range(8):
        m = next((x for x in order[min(attempt // 2, len(order) - 1):] if x not in attempt_skip),
                 order[-1])
        try:
            r = requests.post(
                f"https://generativelanguage.googleapis.com/v1beta/models/{m}:generateContent",
                params={"key": key}, json=body, timeout=600)
            if r.status_code in (429, 500, 502, 503, 504):
                last = f"{m} HTTP {r.status_code}"
                time.sleep(min(30, 3 * 2 ** (attempt % 3)))
                continue
            r.raise_for_status()
            data = r.json()
            cost = Usage.add(m, data.get("usageMetadata", {}))
            cand = data["candidates"][0]
            reason = cand.get("finishReason")
            if reason == "RECITATION":
                # 輸出與受版權保護的已知文本（英文閱讀短文常見）重複度過高而被擋。
                # temperature 0 下同一模型重試必然再擋，換模型並稍微放寬取樣
                last = f"{m} RECITATION"
                body["generationConfig"]["temperature"] = 0.3
                attempt_skip.add(m)
                continue
            if reason not in (None, "STOP"):
                raise RuntimeError(f"模型輸出中斷：{reason}")
            text = "".join(p.get("text", "") for p in cand["content"]["parts"])
            return repair_tree(loads_lenient(text)), m, cost
        except requests.HTTPError as exc:
            last = redact(f"{m} {exc.response.status_code}: {exc.response.text[:200]}")
            break                                  # 400 這類請求本身有錯，換模型也沒用
        except (requests.RequestException, KeyError, json.JSONDecodeError) as exc:
            last = redact(f"{m} {type(exc).__name__}: {exc}")
            time.sleep(5)
    raise RuntimeError(f"呼叫失敗：{last}")


# ─────────────────────────── 公式逸出修復 ───────────────────────────
#
# 模型寫 JSON 時，LaTeX 的反斜線有時逸出（\\times），有時沒有（\times）。
# 沒逸出的那些，恰好有一部分是**合法的** JSON 逸出序列，解析時不會報錯，
# 而是被靜默改成控制字元：
#     \times → Tab + "imes"        \frac → 換頁 + "rac"
#     \neq   → 換行 + "eq"         \beta → 退格 + "eta"
#     \rightarrow → 歸位 + "ightarrow"
# 實測大灣卷第 21 題的「×」就這樣變成了「<Tab>imes」。解析後逐字串還原：
# 控制字元後面接的字母若能湊成已知的 LaTeX 指令，就改回反斜線加指令。

LATEX_NAMES = sorted("""
times frac dfrac tfrac theta tau beta neq ne nu rho rightarrow leftarrow leftrightarrow
Rightarrow right left ldots leq le geq ge pm mp div cdot cdots circ angle triangle sqrt
overline overleftrightarrow overrightarrow mathrm mathbf mathit text textrm boldsymbol pi alpha
perp parallel approx infty degree sim square dots because therefore bot Delta mu Omega
lambda propto cong lt gt underline
""".split(), key=len, reverse=True)
_CTRL = {"\t": "t", "\f": "f", "\b": "b", "\r": "r"}
_CTRL_RE = re.compile("([\t\f\b\r])(" + "|".join(
    n[1:] for n in LATEX_NAMES if n[0] in "tfbr") + ")(?![a-zA-Z])")
_NL_RE = re.compile("\n(" + "|".join(n[1:] for n in LATEX_NAMES if n[0] == "n") + ")(?![a-zA-Z])")


def repair_latex(s: str) -> str:
    s = _CTRL_RE.sub(lambda m: "\\" + _CTRL[m.group(1)] + m.group(2), s)
    # 換行在一般文字裡是合法的，只在 $…$ 公式裡還原
    return re.sub(r"\$[^$]*\$", lambda m: _NL_RE.sub(lambda k: "\\n" + k.group(1), m.group(0)), s)


def repair_tree(x):
    if isinstance(x, str):
        return repair_latex(x)
    if isinstance(x, list):
        return [repair_tree(v) for v in x]
    if isinstance(x, dict):
        return {k: repair_tree(v) for k, v in x.items()}
    return x


# ─────────────────────────── 文字層驗證 ───────────────────────────

LATEX_CMD = re.compile(r"\\[a-zA-Z]+")


def sig_chars(s: str) -> Counter:
    """用來比對的「實質字元」：中日韓文字、英文字母、數字。

    LaTeX 指令名稱與標點都不算 —— $\\frac{1}{2}$ 與文字層的「1 2」
    實質字元相同，只是結構不同，而結構正是我們要模型還原的東西。
    """
    s = unicodedata.normalize("NFKC", LATEX_CMD.sub(" ", s or "")).lower()
    return Counter(c for c in s if c.isalnum())


def coverage(part: Counter, whole: Counter) -> float:
    n = sum(part.values())
    if not n:
        return 1.0
    return sum(min(v, whole.get(k, 0)) for k, v in part.items()) / n


def lift_inline_options(item: dict) -> bool:
    """模型有時把選項寫進題幹（「…何者正確？(A)甲 (B)乙 (C)丙 (D)丁」）而不是選項欄位。
    內容都在，只是位置錯了 —— 這種題目會被品管閘門判成「單選題沒有選項」剔除。
    只處理選擇題、且選項代號從 A 起依序連續的情況；回傳是否有修改。"""
    if item.get("options") or item.get("type") not in ("single", "multiple"):
        return False

    def sequential(opts: list[dict]) -> bool:
        labels = [o["label"] for o in opts]
        return len(opts) >= 2 and labels == [chr(ord("A") + i) for i in range(len(opts))]

    stem, opts = split_options(item.get("stem") or "")
    if not sequential(opts):
        # 多題共用一組選項的配合題：選項印在題組說明裡（「學生的權利有(A)學習權
        # (B)受教權…請依題意判斷」），每一題只有一句陳述。把那組選項複製到題目上，
        # 題組說明保持原樣。原卷選項代號本身就有錯（兩個 B）的不處理，留給閘門擋下。
        _, shared = split_options(item.get("group_stem") or "")
        if not sequential(shared):
            return False
        # 最後一個選項後面常接著作答指示（「(D)財產權，請依題意判斷…」）；
        # 只有逗號後面真的是指示才切，選項本身含逗號時不動
        last = shared[-1]["content"]
        m = re.search(r"[，。,；;]\s*(?=.*(?:請|判斷|選出|回答|填入|作答))", last)
        if m:
            shared[-1]["content"] = last[:m.start()].strip()
        item["options"] = shared
        return True
    # 最後一個選項後面若接著空行與表格／說明，那是題幹的一部分（題目附表），移回題幹
    tail = ""
    last = opts[-1]["content"]
    if "\n\n" in last:
        last, tail = last.split("\n\n", 1)
        opts[-1]["content"] = last.strip()
    item["stem"] = (stem + ("\n\n" + tail.strip() if tail.strip() else "")).strip()
    item["options"] = opts
    return True


# ─────────────────────────── 主流程 ───────────────────────────

def crop(page, box: list[int], dest: Path) -> bool:
    """依 0~1000 正規化座標從頁面切圖。"""
    if not box or len(box) != 4:
        return False
    y0, x0, y1, x1 = box
    r = page.rect
    clip = fitz.Rect(r.x0 + x0 / 1000 * r.width, r.y0 + y0 / 1000 * r.height,
                     r.x0 + x1 / 1000 * r.width, r.y0 + y1 / 1000 * r.height)
    clip = fitz.Rect(clip.x0 - 4, clip.y0 - 4, clip.x1 + 4, clip.y1 + 4) & r
    if clip.is_empty or clip.width < 8 or clip.height < 8:
        return False
    dest.parent.mkdir(parents=True, exist_ok=True)
    page.get_pixmap(dpi=CROP_DPI, clip=clip).save(dest)
    return True


def lesson_tag(scope: dict | None, lesson: str | None) -> str | None:
    if not lesson:
        return None
    bits = [(scope or {}).get("publisher"), (scope or {}).get("volume"), lesson.strip()]
    return " ".join(b.strip() for b in bits if b and b.strip())


def extract(path: Path, fig_dir: Path | None, model: str) -> tuple[dict | None, dict]:
    """回傳 (擷取結果, 統計)。擷取失敗時結果為 None。"""
    stats: dict = {"path": str(path)}
    meta = parse_path(path)
    for field in ("grade", "subject", "academic_year_roc", "semester", "exam_seq",
                  "city", "school_short"):
        if not meta.get(field):
            stats["skip"] = f"路徑缺 {field}"
            return None, stats

    doc = fitz.open(path)
    parts: list[dict] = [{"text": PROMPT}]
    page_sig: list[Counter] = []
    all_sig = Counter()
    for i, page in enumerate(doc, start=1):
        text = page.get_text("text")
        sig = sig_chars(text)
        page_sig.append(sig)
        all_sig += sig
        img = page.get_pixmap(dpi=IMG_DPI).tobytes("jpeg", jpg_quality=80)
        parts.append({"text": f"\n【第{i}頁影像】"})
        parts.append({"inline_data": {"mime_type": "image/jpeg",
                                      "data": base64.b64encode(img).decode()}})
        parts.append({"text": f"【第{i}頁文字層】\n{text.strip() or '（此頁沒有文字層）'}"})
    stats["pages"] = len(doc)
    stats["text_chars"] = sum(all_sig.values())
    has_text = stats["text_chars"] > 200

    t0 = time.time()
    try:
        out, used_model, cost = call_model(parts, model)
    except Exception as exc:
        stats["error"] = str(exc)
        doc.close()
        return None, stats
    stats["seconds"] = round(time.time() - t0, 1)
    stats["model"] = used_model
    stats["usd"] = round(cost, 4)

    # ── 出處：一律取自路徑 ──────────────────────────────────
    short = meta["school_short"]
    header = out.get("header") or {}
    printed = (header.get("school") or "").strip()
    meta["school"] = printed if printed and short in printed else build_school(meta["city"], short)
    scope = out.get("scope") or None
    if scope and scope.get("raw"):
        meta["scope_note"] = scope["raw"][:300]

    subj = meta["subject"] + (f"_{meta['sub_subject']}" if meta.get("sub_subject") else "")
    stem_id = re.sub(r"[^\w]+", "_", decode_mojibake(path.stem)).strip("_").lower()
    stem_id = re.sub(r"[^\w]+", "_",
                     f"{meta['city']}_{stem_id}_{subj}_g{meta['grade']}"
                     f"s{meta['semester']}e{meta['exam_seq']}").strip("_")
    doc_id = f"doc_{meta['academic_year_roc']}_{stem_id}"

    sections = [{"ord": s["ord"], "name": s["name"]} for s in out.get("sections") or []]
    if not sections:
        sections = [{"ord": 1, "name": "試題"}]
    known_sections = {s["ord"] for s in sections}

    def save_fig(fig: dict | None, key: str) -> str | None:
        if not fig or not fig_dir:
            return None
        pno = fig.get("page", 0)
        if not (1 <= pno <= len(doc)):
            return None
        rel = f"{doc_id}/{key}.png"
        return rel if crop(doc[pno - 1], fig.get("box"), fig_dir / rel) else None

    # ── 題組：短文與共用圖 ────────────────────────────────────
    groups: dict[str, dict] = {}
    shared_assets: list[dict] = []
    for g in out.get("groups") or []:
        key = re.sub(r"[^\w]+", "_", f"g{g.get('section', 1)}_{g.get('first')}_{g.get('last')}")
        files = [f for j, fig in enumerate(g.get("figures") or [], start=1)
                 if (f := save_fig(fig, f"{key}_f{j}"))]
        g["_key"] = key
        groups[g["key"]] = g
        if files:
            # 題組共用的圖只能掛一張共用資產；多張時把第一張當代表，其餘改掛到題組的第一題
            shared_assets.append({"key": key, "label": "題組附圖", "kind": "figure",
                                  "file": files[0], "used_by": []})
            g["_extra_files"] = files[1:]

    # ── 題目 ────────────────────────────────────────────────
    questions: list[dict] = []
    seen: set[tuple[int, int]] = set()
    listening = 0
    for q in out.get("questions") or []:
        if q.get("listening"):
            listening += 1
            continue                      # 沒有音檔的聽力題無法使用
        sec = q.get("section") if q.get("section") in known_sections else sections[0]["ord"]
        num = q.get("number")
        if num is None or (sec, num) in seen:
            continue
        seen.add((sec, num))
        qid = f"{doc_id}_s{sec}_{num}"
        item: dict = {"id": qid, "section": sec, "number": num,
                      "type": q.get("type") if q.get("type") in TYPES else "fill",
                      "page": q.get("page"), "stem": (q.get("stem") or "").strip()}

        opts = []
        for o in q.get("options") or []:
            label = unicodedata.normalize("NFKC", (o.get("label") or "")).strip("()（） .")
            opt = {"label": label.upper(), "content": (o.get("content") or "").strip()}
            if f := save_fig(o.get("figure"), f"{qid.split('_s')[-1]}_opt{label}"):
                opt["asset"] = {"key": f"opt{label}", "file": f}
            opts.append(opt)
        if opts:
            item["options"] = opts
        else:
            lift_inline_options(item)
            opts = item.get("options") or []

        assets = []
        for j, fig in enumerate(q.get("figures") or [], start=1):
            if f := save_fig(fig, f"s{sec}_{num}_f{j}"):
                assets.append({"key": f"s{sec}_{num}_f{j}", "kind": "figure", "file": f})

        g = groups.get(q.get("group") or "")
        if g:
            item["group_stem"] = (g.get("passage") or "").strip()
            sa = next((a for a in shared_assets if a["key"] == g["_key"]), None)
            if sa:
                item["shared_asset"] = sa["key"]
                sa["used_by"].append(qid)
            if g.get("_extra_files") and num == g.get("first"):
                assets += [{"key": f"{g['_key']}_x{j}", "kind": "figure", "file": f}
                           for j, f in enumerate(g["_extra_files"], start=2)]
                g["_extra_files"] = []
        if assets:
            item["assets"] = assets

        les = q.get("lesson") or {}
        if les.get("number") or les.get("title"):
            # 課次標籤先存原始判讀，章節名稱由 build_chapters.py 跨卷彙整後統一
            item["lesson_raw"] = {k: les.get(k) for k in ("number", "title", "basis") if les.get(k)}

        # ── 文字層驗證 ───────────────────────────────────────
        # 題目的每個實質字元都必須能在原卷文字層裡找到。對不上代表模型
        # 補寫、改寫或誤讀了內容 —— 這種題目看起來完全正常，最危險。
        if has_text:
            body = item["stem"] + " " + " ".join(o["content"] for o in opts)
            cov = coverage(sig_chars(body), all_sig)
            if cov < 0.9:
                item["uncertain_spans"] = [f"內容與原卷文字層不符（吻合 {cov:.0%}）"]
        else:
            item["review_note"] = "掃描件：無文字層可驗證，內容為影像辨識"
            item["source_ocr"] = True
        questions.append(item)

    # 題組短文也要驗證 —— 閱讀測驗整題的依據都在短文裡
    if has_text:
        for g in groups.values():
            cov = coverage(sig_chars(g.get("passage") or ""), all_sig)
            if cov < 0.9:
                for item in questions:
                    if item.get("group_stem") == (g.get("passage") or "").strip():
                        item.setdefault("uncertain_spans", []).append(
                            f"題組短文與原卷文字層不符（吻合 {cov:.0%}）")

    # 字詞題的作答指示寫在大題標題上（「一、國字注音」），題目本身只有「朦朧」。
    # 老師單獨取用一題時看不到大題標題，把指示帶到題目上才看得懂要做什麼。
    sec_names = {s["ord"]: s["name"] for s in sections}
    for item in questions:
        if len(item["stem"]) < 6 and not item.get("group_stem") and sec_names.get(item["section"]):
            item["group_stem"] = sec_names[item["section"]]

    questions.sort(key=lambda x: (x["section"], x["number"]))

    # ── 答案：答案卷解析（規則式）優先，模型讀到的印刷答案補空缺 ─────
    attach_answers(path, sections, questions)
    key_by = {(k.get("section"), k.get("number")): k.get("answer")
              for k in out.get("answer_key") or []}
    for item in questions:
        printed_ans = key_by.get((item["section"], item["number"]))
        if not printed_ans:
            continue
        printed_ans = split_answer("、".join(printed_ans), item) if item.get("options") \
            else [str(a).strip() for a in printed_ans if str(a).strip()]
        if not item.get("answer"):
            item["answer"] = printed_ans
            item["answer_source"] = "answer_key_vlm"
        elif [str(a) for a in item["answer"]] != [str(a) for a in printed_ans]:
            item["answer_status"] = "disputed"
            item["review_note"] = f"答案卷解析與影像判讀不一致：{item['answer']} vs {printed_ans}"
    attach_carry_context(questions)

    for s in sections:
        s["count"] = sum(1 for q in questions if q["section"] == s["ord"])

    # ── 反向覆蓋率：原卷有多少內容沒被擷取到 ─────────────────────
    if has_text:
        out_sig = Counter()
        for item in questions:
            out_sig += sig_chars(item["stem"] + " " + " ".join(
                o["content"] for o in item.get("options") or []))
        for g in groups.values():
            out_sig += sig_chars(g.get("passage") or "")
        stats["recall"] = round(coverage(all_sig, out_sig), 3)

    meta.update({
        "id": doc_id,
        "title": f"{meta['school']} {meta['academic_year_roc']}學年度第{meta['semester']}學期"
                 f"第{meta['exam_seq']}次定期評量 {meta['grade']}年級{meta['subject']}"
                 + (f"（{meta['sub_subject']}）" if meta.get("sub_subject") else "") + "科試題",
        "exam_name": f"{meta['academic_year_roc']}學年度第{meta['semester']}學期"
                     f"第{meta['exam_seq']}次定期評量",
        "sections": sections,
        "extractor": f"vlm:{used_model}",
        "source_file": "/".join(path.parts[-6:]),
    })
    if scope:
        meta["scope"] = scope
    stats.update({"questions": len(questions), "listening_skipped": listening,
                  "groups": len(groups),
                  "uncertain": sum(1 for q in questions if q.get("uncertain_spans")),
                  "ocr": not has_text})
    doc.close()
    res = {"document": meta, "questions": questions}
    if shared_assets:
        res["shared_assets"] = shared_assets
    return res, stats


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("target", nargs="?", type=Path)
    ap.add_argument("--list", type=Path, help="一行一個 PDF 路徑（相對於 --root）")
    ap.add_argument("--root", type=Path, default=Path("."))
    ap.add_argument("-o", "--out", type=Path, required=True)
    ap.add_argument("--assets", type=Path)
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--log", type=Path, help="每份卷的統計寫成 JSON Lines")
    ap.add_argument("--deadline", help="超過這個時間（ISO 格式）就不再送出新的卷")
    ap.add_argument("--budget-twd", type=float,
                    help="累計費用（台幣，含先前各輪）達到這個數字就不再送出新的卷")
    ap.add_argument("--ledger", type=Path, default=Path("out/vlm_spend.json"),
                    help="累計費用的記錄檔，跨輪次接續")
    args = ap.parse_args()
    load_env_file()
    model = os.environ.get("GEMINI_EXTRACT_MODEL") or MODEL

    if args.list:
        paths = [args.root / p.strip() for p in args.list.read_text(encoding="utf-8").splitlines()
                 if p.strip()]
    elif args.target and args.target.is_dir():
        paths = sorted(args.target.rglob("*.pdf"))
    else:
        paths = [args.target]
    args.out.mkdir(parents=True, exist_ok=True)
    deadline = None
    if args.deadline:
        from datetime import datetime
        deadline = datetime.fromisoformat(args.deadline).timestamp()

    args.ledger.parent.mkdir(parents=True, exist_ok=True)
    Usage.load(args.ledger)
    print(f"模型 {model}；先前累計費用 NT${Usage.twd():.0f}"
          + (f"，上限 NT${args.budget_twd:.0f}" if args.budget_twd else ""), flush=True)
    # 續跑只跳過「視覺模型已經擷取過」的卷。輸出目錄裡若有規則式（extract.py）
    # 的舊結果，ID 相同但品質較差，要被新結果取代，不能當成已完成。
    done_ids = set()
    for y in args.out.glob("*.yaml"):
        head = y.read_text(encoding="utf-8")[:4000]
        if "extractor: vlm:" in head:
            done_ids.add(y.stem)
    log_lock = threading.Lock()

    def work(p: Path) -> dict:
        # 續跑：已經有輸出的卷不再花錢。ID 取決於路徑，先算一次比對
        meta = parse_path(p)
        if meta.get("school_short"):
            subj = (meta.get("subject") or "") + (f"_{meta['sub_subject']}"
                                                  if meta.get("sub_subject") else "")
            sid = re.sub(r"[^\w]+", "_", decode_mojibake(p.stem)).strip("_").lower()
            sid = re.sub(r"[^\w]+", "_", f"{meta.get('city')}_{sid}_{subj}_g{meta.get('grade')}"
                                         f"s{meta.get('semester')}e{meta.get('exam_seq')}").strip("_")
            if f"doc_{meta.get('academic_year_roc')}_{sid}" in done_ids:
                return {"path": str(p), "skip": "已完成"}
        if deadline and time.time() > deadline:
            return {"path": str(p), "skip": "已過截止時間"}
        # 預算以「預估」計：同時在跑的卷還沒結帳，所以預留每份卷的平均花費當緩衝，
        # 寧可早一點停，也不要超過上限才發現
        if args.budget_twd:
            per = (Usage.run_usd * TWD_PER_USD / Usage.calls) if Usage.calls else 5.0
            if Usage.twd() + per * args.workers >= args.budget_twd:
                return {"path": str(p), "skip": "已達預算上限"}
        res, stats = extract(p, args.assets, model)
        if res:
            dest = args.out / f"{res['document']['id']}.yaml"
            dest.write_text(yaml.safe_dump(res, allow_unicode=True, sort_keys=False),
                            encoding="utf-8")
        if args.log:
            with log_lock, open(args.log, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(stats, ensure_ascii=False) + "\n")
        return stats

    t0 = time.time()
    n_ok = n_q = 0
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = [ex.submit(work, p) for p in paths]
        for i, fut in enumerate(as_completed(futs), start=1):
            s = fut.result()
            if s.get("questions") is not None:
                n_ok += 1
                n_q += s["questions"]
            tag = (f"{s.get('questions')} 題 題組{s.get('groups')} 存疑{s.get('uncertain')}"
                   f" 覆蓋{s.get('recall', '—')} {s.get('seconds')}s {s.get('model','')}"
                   f" ${s.get('usd', 0):.3f}｜累計 NT${Usage.twd():.0f}"
                   if s.get("questions") is not None
                   else s.get("skip") or s.get("error", "")[:80])
            print(f"[{i}/{len(paths)}] {'/'.join(Path(s['path']).parts[-6:])}  {tag}", flush=True)

    el = time.time() - t0
    print(f"\n完成 {n_ok} 份、{n_q} 題，{el/60:.1f} 分鐘；呼叫 {Usage.calls} 次，"
          f"輸入 {Usage.prompt/1e6:.2f}M、輸出 {Usage.out/1e6:.2f}M、思考 {Usage.thought/1e6:.2f}M tokens；"
          f"累計費用 US${Usage.usd:.2f}（NT${Usage.twd():.0f}）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
