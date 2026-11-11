#!/usr/bin/env python3
"""檢查外部轉出的題庫 YAML 是否符合 docs/QUESTION_FORMAT.md，交件前自我檢查用。

    python scripts/check_bank_yaml.py <bank 資料夾或單一 YAML> [--assets <附圖根目錄>] [--existing]

國中、高中卷都適用（高中卷依 document.level: senior 判斷，見 docs/HIGH_SCHOOL_HANDOFF.md）。
會列出：缺必填欄位、卷 id／題目 id 不合規則、題型或大題不對、圖檔不存在、會被品管閘門剔除的題目
（規則同 apps/api/quality.py）。--existing 再比對 data/export/papers.jsonl.gz，列出題庫已有的卷。

另外檢查「格式正確但內容不完整」的題目（◇，見 CONTENT 說明），最後列出各類題數：
缺圖、缺表、選項混入、題組材料遺失、上下標遺失、題幹混入解答、圖檔沒被引用、占位文字、Word 欄位碼、
表格可疑、附圖沒有 crop、附圖頁碼不符；加 --assets 再查同圖多掛、附圖空白或過小；
加 --sources <原卷 PDF 根目錄> 再照 crop 回原卷比對附圖、比對原卷文字有沒有漏抄（需 pymupdf、pillow）。
--strict 時這些也算錯誤；--issues-csv 檔名 會把每一題的問題寫成 CSV（題目 id、類別、說明）。
manifest.json 各卷 dropped 申報剔除的題目（含 text 原文）算覆蓋率時不當成遺漏（3.2 版）。
檢查程式的誤判，在 manifest.json 的 waivers 登記 [{"id": 題目 id, "category": 類別, "reason": 理由}]，
登記的項目不計入 --strict，但會逐條列出給督察確認（manifest 預設讀 bank 資料夾上一層，或用 --manifest 指定）。
基本檢查只需要 pyyaml；有錯誤時結束碼為 1。
"""

from __future__ import annotations

import argparse
import gzip
import json
import re
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from apps.api.quality import evaluate  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
REQUIRED = ("id", "grade", "subject", "academic_year_roc", "semester", "exam_seq", "city", "school_short",
            "school", "title", "exam_name", "sections")
SUBJECTS = {"國文", "英語", "數學", "自然", "社會"}
SUBS = {"自然": {"理化", "生物", "地科"}, "社會": {"歷史", "地理", "公民"}}
# 高中（docs/HIGH_SCHOOL_HANDOFF.md）：自然分物理、化學、生物、地科，社會的公民是「公民與社會」
SUBS_SENIOR = {"自然": {"物理", "化學", "生物", "地科"}, "社會": {"歷史", "地理", "公民與社會"}}
TYPES = {"single", "multiple", "tf", "fill", "calc", "essay", "matching"}
STATUS = {"verified", "ai_generated", "disputed", "missing"}

# ── 內容完整性（格式對、但題目看不懂或答不出來）─────────────────────────
CONTENT = {
    "缺圖": "題目提到圖（下圖、如圖、附圖、圖(一)、照片…），但這題沒有 assets／shared_asset／選項圖",
    "缺表": "題目提到表（下表、附表、表(一)…），但題幹沒有 Markdown 表格，也沒有附圖",
    "選項混入": "選擇題選項裡有換行或超過 150 字，通常是表格或下一題的材料被併進最後一個選項",
    "題組材料遺失": "題幹一開頭就寫「由上述／根據上文…」，卻沒有 group_stem（材料不見了）",
    "上下標遺失": "數學、自然卷在 $…$ 以外出現 x2、10³ 寫成 103、H2O 這類上下標被攤平的寫法",
    "題幹混入解答": "題幹或題組文字裡出現「參考答案」「試題解析」「《答案》」",
    "圖檔沒被引用": "assets/{卷 id}/ 裡有 PNG，但 YAML 沒有任何一題引用它",
    # ── 以下為第三輪加入：擋下「為了通過檢查而改內容」的做法 ──
    "占位文字": "出現「詳見原卷」「圖略」「表略」這類占位字，內容其實沒補",
    "Word欄位碼": "出現 EMBED Equation、EQ \\o、MERGEFORMAT 等 Word 欄位碼，公式或格式沒有轉出來",
    "表格可疑": "Markdown 表格表頭是「項目｜內容」、只有表頭、各列欄數不一致、或八成以上是空格（沒照原卷轉）",
    "附圖沒有crop": "附圖沒寫 crop，或 crop 格式不對（page 整數、rect 四個數字且 x1>x0、y1>y0）",
    "附圖頁碼不符": "附圖的 crop.page 和題目的 page 差超過 1 頁，通常是圖掛錯題",
    "同圖多掛": "同一個圖檔（內容相同）掛在兩題以上；題組共用圖要放 shared_assets",
    "附圖空白或過小": "附圖寬不到 120 像素、高不到 40 像素，或幾乎整張空白",
    "crop與圖檔不符": "（--sources）照 crop 從原卷 PDF 裁出來的區域和附圖 PNG 對不上",
    "原卷文字遺漏": "（--sources）原卷 PDF 題目區的文字，在 YAML 裡找不到的比例太高（題目文字被刪掉或漏抄）",
    "轉檔殘留": "出現 pandoc 等轉檔工具留下的標記：行首「> 」、\\( \\)、{.underline}、[]{…}、<!--、跳脫的 \\| 等",
}
LEFTOVER_RE = re.compile(r"(?m)^\s*>\s|\\\(|\\\)|\{\.(underline|mark|smallcaps)\}|\[\]\{|<!--|\\\||\\\*|\\_(?![{])")
PLACEHOLDER_RE = re.compile(r"詳見原卷|請見原卷|參見原卷|見原卷|[（(]\s*[圖表]\s*略\s*[)）]|[圖表]略|\[[圖表][^\]]{0,6}\]|此處有[圖表]")
FIELD_RE = re.compile(r"[\x01\x13\x14\x15]|EMBED\s+\S|\bEQ\s*\\|\beq\s*\\|\\o\\al|\\o\s*\(|MERGEFORMAT|INCLUDEPICTURE|SHAPE\s+\\\*|HYPERLINK\s+\"")


def table_issue(txt: str) -> str | None:
    """Markdown 表格看起來沒照原卷轉時回傳原因。"""
    blocks, cur = [], []
    for line in txt.split("\n"):
        if line.strip().startswith("|"):
            s = line.strip()
            cur.append(s[:s.rfind("|") + 1])           # 表格列最後一個 | 之後的附註不算
        elif cur:
            blocks.append(cur); cur = []
    if cur:
        blocks.append(cur)
    for b in blocks:
        rows = [r for r in b if not re.fullmatch(r"[|:\-\s]+", r)]
        cells = [[c.strip() for c in r.strip("|").split("|")] for r in rows]
        if not cells:
            continue
        if re.fullmatch(r"\|\s*項目\s*\|\s*內容\s*\|", rows[0]):
            return "表頭是「項目｜內容」"
        if len(rows) < 2:
            return "只有表頭沒有資料列"
        if len({len(c) for c in cells}) > 1:
            return f"各列欄數不一致 {sorted({len(c) for c in cells})}"
        flat = [c for r in cells for c in r]
        if sum(1 for c in flat if not c) > len(flat) * 0.8:
            return "八成以上是空格"
    return None


def asset_list(q: dict) -> list[dict]:
    out = [a for a in q.get("assets") or [] if isinstance(a, dict)]
    out += [o["asset"] for o in q.get("options") or [] if isinstance(o, dict) and isinstance(o.get("asset"), dict)]
    return out


def crop_ok(a: dict) -> bool:
    c = a.get("crop")
    if not isinstance(c, dict) or not isinstance(c.get("page"), int):
        return False
    r = c.get("rect")
    return (isinstance(r, list) and len(r) == 4 and all(isinstance(v, (int, float)) for v in r)
            and r[2] > r[0] and r[3] > r[1])
FIG_RE = re.compile(r"下圖|如圖|附圖|右圖|左圖|上圖|圖[一二三四五六七八九十]|圖[(（]\s*[一二三四五六七八九十0-9]|圖\s*[0-9]|地圖所示"
                    r"|(下列|以下|如|附|右|左|上|下|此|這|該)[兩二三四一]?[張幅]?照片|照片(中|所示|裡)")   # 「數位照片的檔案大小」不算
TAB_RE = re.compile(r"下表|如表|附表|右表|上表"
                    r"|(?<![代發外報列年課時])表[一二三四五六七八九十\(（]")   # 「代表一個」不算
GROUP_RE = re.compile(r"(由|根據|依據|依|從|閱讀)(上述|上文|上列|上段|上方|此段|這段|該段)")
ANS_RE = re.compile(r"參考答案|試題解析|【解析】|《答案》")
# 數學式中被攤平的上下標：x2、y3、)2、×103、H2O；排除 $…$ 裡的 LaTeX 與年份等長數字
SCRIPT_RE = re.compile(r"(?<![A-Za-z0-9])[A-Za-z]\d(?![\d.．])"            # x2、a1、H2
                       r"|(?<![A-Za-z0-9甲乙丙丁戊己庚辛壬癸一二三四五六七八九十])[)）]\d(?![\d.．])"  # (x－1)3
                       r"|\d[.．]\d+\s*×\s*10[－\-]?\d"                     # 2.0×103（科學記號）
                       r"|[A-Z][a-z]?\d[A-Z(]")                              # Cr2O3、FeCl3(
SCRIPT_OK = re.compile(r"[pq]\d|[AB]\d(?![A-Za-z])|Q\d|H\dN\d")   # 頁碼、習作題號、A4 紙、四分位數、H1N1


def content_issues(q: dict, m: dict) -> list[tuple[str, str]]:
    """回傳 [(類別, 說明)]。只看內容是否完整，不重複 evaluate() 的格式規則。"""
    out = []
    stem, grp = q.get("stem") or "", q.get("group_stem") or ""
    txt = grp + "\n" + stem
    opts = [o for o in q.get("options") or [] if isinstance(o, dict)]
    has_fig = bool(q.get("assets") or q.get("shared_asset") or any(o.get("asset") for o in opts))
    if (hit := FIG_RE.search(txt)) and not has_fig:
        out.append(("缺圖", f"提到「{hit.group()}」"))
    if (hit := TAB_RE.search(txt)) and "|" not in txt and not has_fig:
        out.append(("缺表", f"提到「{hit.group()}」"))
    if q.get("type") in ("single", "multiple"):
        bad = [o.get("label") for o in opts if "\n" in (o.get("content") or "") or len(o.get("content") or "") > 150]
        if bad:
            out.append(("選項混入", f"選項 {','.join(map(str, bad))}"))
    if (hit := GROUP_RE.search(stem)) and hit.start() < 12 and not grp:   # 材料在題幹前段就不算
        out.append(("題組材料遺失", f"題幹寫「{hit.group()}」"))
    if m.get("subject") in ("數學", "自然") and m.get("sub_subject") not in ("生物", "地科"):
        plain = re.sub(r"\$[^$]*\$", " ", txt + "\n" + "\n".join(o.get("content") or "" for o in opts))
        for hit in SCRIPT_RE.finditer(plain):
            if SCRIPT_OK.match(plain, hit.start()):
                continue
            out.append(("上下標遺失", "「" + " ".join(plain[max(0, hit.start() - 6):hit.end() + 6].split()) + "」"))
            break
    if hit := ANS_RE.search(txt):
        out.append(("題幹混入解答", f"出現「{hit.group()}」"))
    every = txt + "\n" + "\n".join(o.get("content") or "" for o in opts) + "\n" + (q.get("explanation") or "")
    if hit := PLACEHOLDER_RE.search(every):
        out.append(("占位文字", f"出現「{hit.group()}」"))
    if hit := FIELD_RE.search(every):
        out.append(("Word欄位碼", f"出現「{hit.group()}」"))
    plain_every = re.sub(r"\$[^$]*\$", " ", every)          # $…$ 裡的 LaTeX 不算
    if hit := LEFTOVER_RE.search(plain_every):
        out.append(("轉檔殘留", "「" + " ".join(plain_every[max(0, hit.start() - 8):hit.end() + 8].split()) + "」"))
    if why := table_issue(txt):
        out.append(("表格可疑", why))
    for a in asset_list(q):
        if not crop_ok(a):
            out.append(("附圖沒有crop", str(a.get("file"))))
        elif isinstance(q.get("page"), int) and abs(a["crop"]["page"] - q["page"]) > 1:
            out.append(("附圖頁碼不符", f"{a.get('file')}：crop 第 {a['crop']['page']} 頁，題目在第 {q['page']} 頁"))
    return out


# ── 需要讀圖檔／原卷 PDF 的檢查（--assets、--sources）─────────────────────────
def image_issue(p: Path) -> str | None:
    try:
        from PIL import Image, ImageStat
    except ImportError:
        return None
    try:
        im = Image.open(p)
        w, h = im.size
        if w < 120 or h < 40:
            return f"{w}×{h} 像素"
        g = im.convert("L")
        g.thumbnail((200, 200))
        if ImageStat.Stat(g).stddev[0] < 4:
            return "幾乎整張同一個顏色"
    except Exception as exc:  # noqa: BLE001
        return f"圖檔打不開：{exc}"
    return None


def _fitz():
    try:
        import pymupdf as fitz  # PyMuPDF 1.24 以後的名稱
    except ImportError:
        import fitz
    return fitz


def find_pdf(sources: Path, m: dict) -> Path | None:
    for cand in (sources / (m.get("source_file") or "_"), sources / f"{m.get('id')}.pdf"):
        if cand.is_file():
            return cand
    return None


def _small_gray(im):
    from PIL import ImageOps
    g = ImageOps.autocontrast(im.convert("L"))
    return g.resize((48, 48))


def crop_matches(pdf, a: dict, png: Path) -> float | None:
    """照 crop 從 PDF 裁圖，和 PNG 比相似度（-1～1）。"""
    try:
        fitz = _fitz()
        from PIL import Image
    except ImportError:
        return None
    c = a["crop"]
    if not 1 <= c["page"] <= len(pdf):
        return -1.0
    page = pdf[c["page"] - 1]
    r = fitz.Rect(*c["rect"]) & page.rect
    if r.is_empty or r.width < 2 or r.height < 2:
        return -1.0
    pix = page.get_pixmap(dpi=72, clip=r, alpha=False)
    a1 = _small_gray(Image.frombytes("RGB", (pix.width, pix.height), pix.samples))
    a2 = _small_gray(Image.open(png).convert("RGB"))
    x, y = list(a1.getdata()), list(a2.getdata())
    mx, my = sum(x) / len(x), sum(y) / len(y)
    sxy = sum((u - mx) * (v - my) for u, v in zip(x, y))
    sx = sum((u - mx) ** 2 for u in x) ** 0.5
    sy = sum((v - my) ** 2 for v in y) ** 0.5
    return sxy / (sx * sy) if sx and sy else (1.0 if sx == sy else 0.0)


def norm_text(s: str) -> str:
    """只留中文字比對：題號、選項代號、括號、空白、公式寫法不同都不影響。"""
    return re.sub(r"[^一-鿿]", "", s or "")


# 卷末答案、解析區的開頭（之後的文字不算題目）
ANSWER_HEAD = re.compile(r"《\s*答案\s*》|《\s*試題解析\s*》|(參考答案|試題解析|答案卷|解答卷)\s*$|^\s*【答案】", re.M)


# 卷頭、作答說明、配分這類不必轉進 YAML 的行
HEADER_LINE = re.compile(r"學年|學期|段考|期末考|期中考|月考|試題卷|試卷|班級|座號|姓名|命題|範圍|每題|共\s*\d+\s*分"
                         r"|答錯|倒扣|作答|劃記|畫記|答案卡|答案卷|配分|得分|不予計分|選擇題|非選擇題|填充題|計算題|題組")


def coverage(pdf, d: dict, n: int = 4, dropped_text: str = "") -> float | None:
    """原卷 PDF 題目區（卷末答案區以前、扣掉卷頭與作答說明）的中文字，有多少比例出現在 YAML 裡。
    以行為單位：一行的 n 字片段有六成以上找得到就算有抄到，依字數加權。
    dropped_text：manifest 該卷 dropped 申報剔除的題目原文（第 3.2 版起），申報過的不算遺漏。"""
    text = "\n".join(p.get_text() for p in pdf)
    for m in ANSWER_HEAD.finditer(text):                 # 卷首說明也可能提到「答案卷」，只認後段出現的
        if m.start() > len(text) * 0.3:
            text = text[:m.start()]
            break
    lines = [norm_text(x) for x in text.split("\n") if not HEADER_LINE.search(x)]
    lines = [x for x in lines if len(x) >= n]
    total = sum(len(x) for x in lines)
    if total < 150:
        return None                                   # 掃描檔沒有文字層，或幾乎都是公式，無法比對
    parts = []
    for q in d.get("questions") or []:
        parts += [q.get("group_stem") or "", q.get("stem") or "", q.get("explanation") or ""]
        parts += [o.get("content") or "" for o in q.get("options") or [] if isinstance(o, dict)]
    have = norm_text("\n".join(parts) + "\n" + dropped_text)
    got = 0
    for x in lines:
        grams = [x[i:i + n] for i in range(len(x) - n + 1)]
        if sum(1 for g in grams if g in have) >= 0.6 * len(grams):
            got += len(x)
    return got / total


def doc_id(m: dict) -> str:
    subj = m["subject"] + (f"_{m['sub_subject']}" if m.get("sub_subject") else "")
    if m.get("course"):                              # 高中：數A／數B、選修課程
        subj += f"_{m['course']}"
    return (f"doc_{m['academic_year_roc']}_{m['city']}_{m['school_short']}_{subj}"
            f"_g{m['grade']}s{m['semester']}e{m['exam_seq']}")


CROP_MIN = 0.45          # crop 與 PNG 相似度低於此值視為對不上
COVER_MIN = 0.90         # 原卷文字覆蓋率低於此值視為遺漏


def check(path: Path, assets: Path | None, sources: Path | None = None,
          hashes: dict | None = None, dropped_text: str = "") -> tuple[list[str], list[str], int, int, list[tuple[str, str, str]]]:
    """回傳 (錯誤, 提醒, 題數, 會被剔除的題數, 內容問題 [(題目 id, 類別, 說明)])。
    hashes：跨卷累積 {圖檔內容 md5: [(題目 id, 檔名)]}，用來找同圖多掛。"""
    err, warn, issues = [], [], []
    try:
        d = yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        return [f"YAML 讀不進來：{exc}"], [], 0, 0, []
    if not isinstance(d, dict) or not isinstance(d.get("document"), dict):
        return ["最上層要有 document（對照 docs/QUESTION_FORMAT.md 第 3 節）"], [], 0, 0, []
    bad = [a for a in d.get("shared_assets") or [] if not isinstance(a, dict)]
    if bad:
        err.append(f"shared_assets 每一項要寫成 {{key, file, used_by, crop}}，有 {len(bad)} 項只寫了字串")
        d["shared_assets"] = [a for a in d["shared_assets"] if isinstance(a, dict)]
    bad = [q for q in d.get("questions") or [] if not isinstance(q, dict)]
    if bad:
        err.append(f"questions 每一題要是一個物件，有 {len(bad)} 項不是")
        d["questions"] = [q for q in d["questions"] if isinstance(q, dict)]
    m = d["document"]
    miss = [k for k in REQUIRED if m.get(k) in (None, "", [])]
    if miss:
        err.append(f"document 缺少 {miss}")
    if m.get("subject") and m["subject"] not in SUBJECTS:
        err.append(f"subject 只能是 {sorted(SUBJECTS)}，寫的是「{m['subject']}」")
    for k in ("grade", "academic_year_roc", "semester", "exam_seq"):
        if m.get(k) is not None and not isinstance(m[k], int):
            err.append(f"{k} 要寫整數，寫的是「{m[k]}」")
            m[k] = None
    senior = m.get("level") == "senior" or (m.get("grade") or 0) >= 10
    subs = SUBS_SENIOR if senior else SUBS
    if m.get("sub_subject") and m.get("sub_subject") not in subs.get(m.get("subject"), set()):
        err.append(f"sub_subject「{m['sub_subject']}」不屬於 {m.get('subject')}")
    if m.get("grade") not in ((10, 11, 12) if senior else (7, 8, 9)):
        err.append(f"grade 應為 {'10、11、12' if senior else '7、8、9'}，寫的是 {m.get('grade')}")
    if senior and m.get("level") != "senior":
        err.append("高中卷 document 要加 level: senior")
    if m.get("semester") not in (1, 2) or m.get("exam_seq") not in (1, 2, 3):
        err.append(f"semester 應為 1、2，exam_seq 應為 1～3（{m.get('semester')}, {m.get('exam_seq')}）")
    if "臺" in str(m.get("city", "")):
        err.append("city 請用「台」不用「臺」")
    if not miss:
        want = doc_id(m)
        if m["id"] != want:
            err.append(f"卷 id 應為 {want}，寫的是 {m['id']}")
        if path.stem != m["id"]:
            err.append(f"檔名應為 {m['id']}.yaml")
    secs = {s.get("ord") for s in m.get("sections") or []}
    shared = {a.get("key"): a for a in d.get("shared_assets") or []}
    qs = d.get("questions") or []
    if not qs:
        err.append("沒有任何題目")
    seen, rejected, used = set(), 0, set()
    for q in qs:
        qid = q.get("id", "?")
        if q.get("id") in seen:
            err.append(f"{qid}：題目 id 重複")
        seen.add(q.get("id"))
        if m.get("id") and q.get("id") != f"{m['id']}_s{q.get('section')}_{q.get('number')}":
            err.append(f"{qid}：題目 id 應為 {m['id']}_s{q.get('section')}_{q.get('number')}")
        if q.get("section") not in secs:
            err.append(f"{qid}：section {q.get('section')} 不在 sections 裡")
        if q.get("type") not in TYPES:
            err.append(f"{qid}：type「{q.get('type')}」不是 {sorted(TYPES)} 之一")
        if not isinstance(q.get("page"), int):
            warn.append(f"{qid}：缺 page（原卷第幾頁）")
        if q.get("answer") is not None and not isinstance(q["answer"], list):
            err.append(f"{qid}：answer 要寫成清單，例如 [A]")
        if q.get("answer") and q.get("answer_status") not in STATUS:
            warn.append(f"{qid}：有答案時請填 answer_status（verified／ai_generated／disputed）")
        if q.get("shared_asset") and q["shared_asset"] in shared and \
                q["id"] not in (shared[q["shared_asset"]].get("used_by") or []):
            warn.append(f"{qid}：共用圖 {q['shared_asset']} 的 used_by 沒列這題")
        if re.search(r"<(?!/?u[ >])[a-zA-Z/]", (q.get("stem") or "") + (q.get("group_stem") or "")):
            warn.append(f"{qid}：文字裡有 <u> 以外的角括號標籤，網站會當成文字顯示")
        files = [a.get("file") for a in q.get("assets") or [] if isinstance(a, dict)]
        files += [(o.get("asset") or {}).get("file") for o in q.get("options") or [] if isinstance(o, dict)]
        if q.get("shared_asset") in shared:
            files.append(shared[q["shared_asset"]].get("file"))
        used.update(f for f in files if f)
        if assets:
            for f in files:
                if f and not (assets / f).is_file():
                    err.append(f"{qid}：附圖檔不存在 {f}")
        issues += [(qid, k, v) for k, v in content_issues(q, m)]
        try:
            ok, why = evaluate(q, d)
        except Exception as exc:  # noqa: BLE001  欄位型別不對時品管規則跑不動
            ok, why = False, [f"品管檢查無法執行：{exc}"]
        if not ok:
            rejected += 1
            warn.append(f"{qid}：會被品管剔除（{'；'.join(why)}）")
    if assets and m.get("id") and (assets / m["id"]).is_dir():
        for p in sorted((assets / m["id"]).rglob("*.png")):
            rel = p.relative_to(assets).as_posix()
            if rel not in used:
                issues.append((m["id"], "圖檔沒被引用", rel))
    # 圖檔本身、同圖多掛、crop 對原卷
    pdf = None
    if sources:
        if (pp := find_pdf(sources, m)) is None:
            err.append(f"sources/ 找不到原卷 PDF（{m.get('source_file')} 或 {m.get('id')}.pdf）")
        else:
            pdf = _fitz().open(pp)
            cov = coverage(pdf, d, dropped_text=dropped_text)
            if cov is not None and cov < COVER_MIN:
                issues.append((m.get("id", "?"), "原卷文字遺漏", f"原卷文字只有 {cov:.0%} 出現在 YAML（至少要 {COVER_MIN:.0%}）"))
    if assets:
        import hashlib
        pairs = [(q.get("id", "?"), a) for q in qs for a in asset_list(q)]
        pairs += [(f"{m.get('id')}:{a.get('key')}", a) for a in shared.values()]
        for owner, a in pairs:
            f = a.get("file")
            if not f or not (assets / f).is_file():
                continue
            if why := image_issue(assets / f):
                issues.append((owner, "附圖空白或過小", f"{f}：{why}"))
            if hashes is not None:
                hashes.setdefault(hashlib.md5((assets / f).read_bytes()).hexdigest(), []).append((owner, f))
            if pdf is not None and crop_ok(a):
                sim = crop_matches(pdf, a, assets / f)
                if sim is not None and sim < CROP_MIN:
                    issues.append((owner, "crop與圖檔不符", f"{f}：相似度 {sim:.2f}"))
    return err, warn, len(qs), rejected, issues


def load_dropped(path: Path) -> dict[str, str]:
    """manifest.json 各卷 dropped 申報的題目原文 {卷 id: 文字}，算覆蓋率時不當成遺漏。"""
    if not path or not path.is_file():
        return {}
    try:
        m = json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}
    return {p.get("id"): "\n".join(str(x.get("text") or "") for x in p.get("dropped") or [] if isinstance(x, dict))
            for p in m.get("papers") or [] if isinstance(p, dict)}


def load_waivers(path: Path) -> list[dict]:
    """manifest.json 的 waivers：[{id, category, reason}]，登記檢查程式的誤判。"""
    if not path or not path.is_file():
        return []
    try:
        m = json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return []
    return [w for w in m.get("waivers") or [] if isinstance(w, dict)]


def apply_waivers(issues: list, waivers: list[dict]):
    """回傳 (已豁免 [(id, 類別, 說明, 理由)], 剩下的問題, 無效登記說明)。
    每筆登記要對應到一個實際被標出的問題（同 id、同類別），理由至少 10 個字。"""
    bad, keep, waived, used = [], [], [], set()
    index = {}
    for i, w in enumerate(waivers):
        why = str(w.get("reason") or "")
        if not w.get("id") or w.get("category") not in CONTENT:
            bad.append(f"第 {i + 1} 筆缺 id 或 category 不是檢查類別：{w}")
        elif len(why) < 10:
            bad.append(f"{w['id']}：{w['category']} 的理由太短（至少 10 個字寫清楚為什麼是誤判）")
        else:
            index[(w["id"], w["category"])] = (i, why)
    for qid, k, v in issues:
        if (qid, k) in index:
            i, why = index[(qid, k)]
            waived.append((qid, k, v, why)); used.add(i)
        else:
            keep.append((qid, k, v))
    for (qid, k), (i, _) in index.items():
        if i not in used:
            bad.append(f"{qid}：{k} 沒有對應的檢查結果（已經不是問題就把這筆登記刪掉）")
    return waived, keep, bad


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("target", type=Path)
    ap.add_argument("--assets", type=Path, help="附圖根目錄（YAML 裡的 file 相對於這裡）")
    ap.add_argument("--existing", action="store_true", help="比對題庫已有的卷（data/export/papers.jsonl.gz）")
    ap.add_argument("--strict", action="store_true", help="內容問題（◇）也算錯誤")
    ap.add_argument("--issues-csv", type=Path, help="把每一題的內容問題寫成 CSV")
    ap.add_argument("--quiet", action="store_true", help="只印總結")
    ap.add_argument("--sources", type=Path,
                    help="原卷 PDF 根目錄：比對 crop 是否對得上附圖、原卷文字是否完整（需 pymupdf、pillow）")
    ap.add_argument("--manifest", type=Path,
                    help="manifest.json（讀 waivers 誤判登記）；不給時自動找 bank 資料夾上一層的 manifest.json")
    args = ap.parse_args()
    mpath = args.manifest or (args.target if args.target.is_dir() else args.target.parent).parent / "manifest.json"
    waivers = load_waivers(mpath)
    dropped = load_dropped(mpath)
    hashes: dict = {}
    paths = sorted(args.target.glob("*.yaml")) if args.target.is_dir() else [args.target]
    have = set()
    if args.existing:
        with gzip.open(REPO / "data/export/papers.jsonl.gz", "rt", encoding="utf-8") as f:
            have = {json.loads(line)["id"] for line in f}
    n_err = n_q = n_rej = 0
    all_issues: list[tuple[str, str, str]] = []
    for p in paths:
        err, warn, nq, rej, issues = check(p, args.assets, args.sources, hashes, dropped.get(p.stem, ""))
        n_q += nq
        n_rej += rej
        all_issues += issues
        if args.existing and p.stem in have:
            err.append("題庫已有這份卷（同一場考試），不要重複交")
        if (err or warn or issues) and not args.quiet:
            print(f"== {p.name}")
            for e in err:
                print(f"  ✗ {e}")
            for w in warn[:30]:
                print(f"  △ {w}")
            if len(warn) > 30:
                print(f"  △ …另有 {len(warn) - 30} 則提醒")
            for qid, k, v in issues[:30]:
                print(f"  ◇ {qid}：{k}（{v}）")
            if len(issues) > 30:
                print(f"  ◇ …另有 {len(issues) - 30} 則內容問題")
        n_err += len(err)
    for lst in hashes.values():                      # 同一張圖（內容相同）掛在兩個以上的地方
        if len({o for o, _ in lst}) > 1:
            for o, f in lst:
                others = sorted({x for x, _ in lst if x != o})
                all_issues.append((o, "同圖多掛", f"{f} 與 {others[0]}{' 等' if len(others) > 1 else ''} 相同"))
    if hashes and not args.quiet:
        dup = [x for x in all_issues if x[1] == "同圖多掛"]
        for qid, k, v in dup[:30]:
            print(f"  ◇ {qid}：{k}（{v}）")
    waived, all_issues, bad_w = apply_waivers(all_issues, waivers)
    if args.issues_csv:
        import csv
        with args.issues_csv.open("w", encoding="utf-8-sig", newline="") as f:
            w = csv.writer(f)
            w.writerow(["題目或卷 id", "類別", "說明", "已登記誤判理由"])
            w.writerows([(*x, "") for x in all_issues] + [(q, k, v, why) for q, k, v, why in waived])
    print(f"\n{len(paths)} 份卷、{n_q} 題：錯誤 {n_err} 項；會被品管剔除 {n_rej} 題")
    for w in bad_w:
        print(f"  ✗ 誤判登記無效：{w}")
    n_err += len(bad_w)
    if waived:
        print(f"已登記誤判（不計入 --strict，督察會逐條確認）：{len(waived)} 項")
        if not args.quiet:
            for qid, k, v, why in waived:
                print(f"  ☐ {qid}：{k}（{v}）— 登記理由：{why}")
    by = {}
    for qid, k, v in all_issues:
        by.setdefault(k, set()).add(v if k == "圖檔沒被引用" else qid)   # 沒被引用的圖以張數計
    if by:
        print("內容問題（◇）：" + "、".join(f"{k} {len(by[k])}" for k in CONTENT if k in by)
              + ("" if args.strict else "（--strict 時算錯誤）"))
    n_content = sum(len(v) for v in by.values())
    return 1 if n_err or (args.strict and n_content) else 0


if __name__ == "__main__":
    sys.exit(main())
