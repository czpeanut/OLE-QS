#!/usr/bin/env python3
"""第三輪監工自驗工具：拿原始 Word 檔對照 YAML，找出解析器漏掉的東西（docs/HIGH_SCHOOL_ROUND3.md 規則 A、B）。

    python scripts/round3_qa.py <原始 .doc 或 .docx> <卷.yaml>
    python scripts/round3_qa.py --batch <原始檔資料夾> <bank 資料夾>     # 依 manifest 的 origin 檔名或卷 id 配對

檢查項目（只看題目區，也就是卷末「《答案》」「參考答案」「試題答案」之前）：
- 規則 A：原卷加括號的代號（EQ 欄位 \\o(　,\\l((　)),甲)，代號不是 A～E）數量，與 YAML 裡 (甲)、(一)… 的數量；
  列出原卷有括號、YAML 卻是單獨代號的題目。
- 規則 B：原卷有底線且不是空白的文字段數（題組標題不算），與 YAML 裡 <u>…</u> 的段數；
  需要同名 .docx，沒有的話自動用 LibreOffice（soffice）轉一份。
原始檔請給 .doc（規則 A 只能讀 .doc）；原始檔本來就是 .docx 的，規則 A 會略過。
結束碼：有短少時為 1。只用標準函式庫與 pyyaml。
"""

from __future__ import annotations

import argparse
import collections
import json
import re
import sys
import zipfile
from pathlib import Path

import yaml

FLD = re.compile(r"\x13\s*EQ \\o\([\u3000 ]*,\\l\(\([\u3000 ]*\)\),[\u3000 ]?([^)\x13\x15]{1,2})\)\x15")
ANS = re.compile(r"《\s*答案\s*》|試題答案|參考答案")
LABEL = re.compile(r"[（(]([\u4e00-\u9fffㄅ-ㄩ])[)）]")


def doc_fields(path: Path) -> str:
    """原始 .doc 的全文（含 Word 欄位標記 \\x13…\\x14…\\x15）。規則 A 只能讀 .doc：
    LibreOffice 轉成 .docx 時會把 EQ 括號欄位換掉。"""
    return path.read_bytes().decode("utf-16-le", errors="ignore")       # Word 97 文字流是 UTF-16


def docx_underlines(path: Path) -> tuple[str, list[str]]:
    """.docx 的純文字與底線文字段（相鄰的底線 run 合併成一段；題組標題、空白作答格不算）。"""
    x = zipfile.ZipFile(path).read("word/document.xml").decode("utf-8")
    text, segs, n = [], [], 0
    POS.clear()
    for para in re.findall(r"<w:p[ >].*?</w:p>", x, re.S):
        cur, start = "", 0
        for r in re.findall(r"<w:r[ >].*?</w:r>", para, re.S):
            ts = "".join(re.findall(r"<w:t[^>]*>([^<]*)</w:t>", r))
            text.append(ts)
            if re.search(r'<w:u w:val="(?!none)', r):
                if not cur:
                    start = n
                cur += ts
            elif cur:
                segs.append(cur); POS.append(start); cur = ""
            n += len(ts)
        if cur:
            segs.append(cur); POS.append(start)
        text.append("\n")
        n += 1
    # 有字的底線段才算（中文、英文人名如 Bao、數字）；只有空白或底線符號的作答格不算
    keep = [k for k, s in enumerate(segs) if re.search(r"[\u4e00-\u9fffA-Za-z0-9]", s) and "題組" not in s]
    POS[:] = [POS[k] for k in keep]
    return "".join(text), [segs[k] for k in keep]


POS: list[int] = []        # docx_underlines 最近一次各段底線在全文的位置


def to_docx(path: Path) -> Path | None:
    """找同名 .docx；沒有就用 LibreOffice 轉一份到暫存資料夾。"""
    if path.suffix.lower() == ".docx":
        return path
    sib = path.with_suffix(".docx")
    if sib.is_file():
        return sib
    import shutil, subprocess, tempfile
    exe = shutil.which("soffice") or shutil.which("libreoffice")
    if not exe:
        return None
    out = Path(tempfile.mkdtemp())
    profile = Path(tempfile.mkdtemp(prefix="lo_qa_"))        # 自己的設定檔：別的 LibreOffice 在跑也轉得出來
    subprocess.run([exe, f"-env:UserInstallation={profile.as_uri()}", "--headless", "--convert-to", "docx",
                    "--outdir", str(out), str(path)], capture_output=True, timeout=300)
    shutil.rmtree(profile, ignore_errors=True)
    f = out / (path.stem + ".docx")
    return f if f.is_file() else None


def question_area(text: str) -> str:
    hits = [m.start() for m in ANS.finditer(text)]
    # 也認管線（word_paper）的答案區標題，例如「…期末考試題答案」單獨一行（裁示 #015）
    pos = 0
    for line in text.split("\n"):
        t = line.strip()
        if t and len(t) < 60:
            try:
                sys.path.insert(0, str(Path(__file__).resolve().parent))
                import word_paper
                if word_paper.ANSWER_HEAD.search(word_paper.plain(t)):
                    hits.append(pos)
            except Exception:                                # noqa: BLE001
                pass
        pos += len(line) + 1
    hits = sorted(h for h in hits if h > min(2000, len(text) * 0.3))   # 卷頭的作答說明不算（短卷用全文 30% 當門檻）
    return text[:hits[0]] if hits else text


def clean(s: str) -> str:
    return re.sub(r"\x13[^\x14\x15]*\x15|\x13[^\x15]*\x14|[\x00-\x1f\s\u3000]", "", s)


def check(orig: Path, ypath: Path, dropped_text: str = "") -> list[str]:
    body = question_area(doc_fields(orig)) if orig.suffix.lower() == ".doc" else ""
    d = yaml.safe_load(ypath.read_text(encoding="utf-8"))
    segs, seen = [], set()
    # 詳解、答案欄（非選題的評分說明常有底線）也算 YAML 的內容
    exps = [q.get("explanation") or "" for q in d.get("questions") or []]
    exps += [str(a) for q in d.get("questions") or [] for a in (q.get("answer") or [])]
    for q in d.get("questions") or []:
        g = q.get("group_stem") or ""
        parts = [("題組", g)] if g and g not in seen else []
        seen.add(g)
        parts += [("題幹", q.get("stem") or "")]
        parts += [(o.get("label"), o.get("content") or "") for o in q.get("options") or []]
        for k, v in parts:
            # 比對用：去掉空白、<u>、LaTeX 記號（「(2)$\\log_{3}$」→「(2)log3」）
            segs.append((q.get("number"), k, re.sub(r"[\s\u3000$\\{}_^]|</?u>", "", v), v))
    problems = []
    want = [m for m in FLD.finditer(body) if not re.fullmatch(r"[A-EＡ-Ｅ]", m.group(1).strip("\u3000 "))]   # 選項代號不算
    have = sum(len(LABEL.findall(v)) for *_, v in segs)
    missing = collections.defaultdict(set)
    for m in want:
        x = m.group(1).strip("\u3000 ")
        ctx = clean(body[m.end():m.end() + 24]).lstrip(")）")[:3]   # 巢狀欄位外層的右括號不算
        if not ctx:
            continue
        if any(f"({x}){ctx}" in v or f"（{x}）{ctx}" in v for _, _, v, _ in segs):
            continue
        found = [(n, k) for n, k, v, _ in segs if x + ctx in v]
        if found:
            missing[found[0][0]].add(f"{found[0][1]}「{x}{ctx}…」")
    if orig.suffix.lower() == ".doc":
        print(f"規則 A：原卷括號代號 {len(want)} 個，YAML 括號代號 {have} 個（個數僅供參考，下面列出的題才是要修的）")
    else:
        print("規則 A：要給原始 .doc 才能查（.docx 已失去 EQ 欄位）")
    for n in sorted(missing, key=lambda v: (v is None, v)):
        line = f"  第 {n} 題缺括號：" + "、".join(sorted(missing[n]))
        print(line)
        problems.append(line)
    dx = to_docx(orig)
    if dx:
        dtext, under = docx_underlines(dx)
        dbody = question_area(dtext)
        head = min(500, len(dbody) // 10)                # 卷頭（校名、科目、張數）的底線不是題目文字
        # 依底線實際位置判斷在不在題目區（同一個詞在題目裡沒畫線、在答案區畫線，不算）
        under = [s for s, at in zip(under, POS) if head <= at < len(dbody)]
        u = sum(len(re.findall(r"<u>", v)) for *_, v in segs)
        print(f"規則 B：原卷題目區底線文字 {len(under)} 段，YAML <u> {u} 段（段數僅供參考，下面列出的才是缺的）")
        # 依內容比對：原卷每段底線文字要在 YAML 的 <u> 裡找得到（題幹、選項、題組、解析都算）；
        # 大題說明（sections 名稱）裡的底線不轉成 <u>，出現在說明裡就不算缺
        ws = lambda v: re.sub(r"[\s\u3000]", "", v)
        have_u = ws("".join(re.findall(r"<u>(.*?)</u>", "".join(v for *_, v in segs) + "".join(exps))))
        secs = ws("".join(str(x.get("name") or "") for x in (d.get("document") or {}).get("sections") or []))
        # 只有選項代號「(A)」畫底線的不算；在 manifest 申報剔除的題目裡的也不算（另列出來）
        under = [s for s in under if not re.fullmatch(r"[\s\u3000]*[（(][A-EＡ-Ｅ][)）][\s\u3000]*", s)]
        # 題組範圍標號（「第30-31」題為題組）、只有數字（「本試卷計 2 張」）、卷名裡的底線不是題目文字（裁示 #015）
        doc_meta = ws(" ".join(str((d.get("document") or {}).get(k) or "") for k in ("title", "exam_name", "school")))
        under = [s for s in under if not re.fullmatch(r"[\s\u3000]*第?\s*\d{1,3}\s*[-~～－至到、]\s*\d{1,3}\s*題?[\s\u3000]*", s)
                 and not re.fullmatch(r"[\s\u3000\d]*", s) and ws(s) not in doc_meta]
        lack = [s for s in under if ws(s) not in have_u and ws(s) not in secs]
        in_drop = [s for s in lack if ws(s) in ws(dropped_text)]
        if in_drop:
            print(f"  在剔除題裡（不算短少）{len(in_drop)} 段：" + "、".join(f"「{s[:12]}」" for s in in_drop[:8]))
            lack = [s for s in lack if s not in in_drop]
        if lack:
            line = f"  底線少了 {len(lack)} 段：" + "、".join(f"「{s[:12]}」" for s in lack[:8])
            print(line)
            problems.append(line)
    else:
        print("規則 B：找不到 .docx，也沒有 LibreOffice 可轉，略過")
    return problems


DROPPED: dict[str, str] = {}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("orig", type=Path)
    ap.add_argument("yaml", type=Path)
    ap.add_argument("--batch", action="store_true", help="orig 與 yaml 都是資料夾")
    a = ap.parse_args()
    pairs = [(a.orig, a.yaml)]
    if a.batch:
        manifest = a.yaml.parent / "manifest.json"
        origin = {}
        if manifest.is_file():
            for p in json.loads(manifest.read_text(encoding="utf-8")).get("papers") or []:
                origin[p.get("id")] = str(p.get("origin") or p.get("source_origin") or "")
                DROPPED[p.get("id")] = "\n".join(str(x.get("text") or "") for x in p.get("dropped") or [])
        files = {f.name: f for f in a.orig.rglob("*") if f.suffix.lower() in (".doc", ".docx")}
        pairs = []
        for y in sorted(a.yaml.glob("*.yaml")):
            o = origin.get(y.stem, "")
            hit = [f for n, f in files.items() if n in o or Path(n).stem == y.stem]
            if hit:
                pairs.append((sorted(hit, key=lambda f: f.suffix != ".docx")[0], y))
            else:
                print(f"== {y.name}：找不到原始檔，跳過")
    bad = 0
    for o, y in pairs:
        print(f"== {y.name}  ←  {o.name}")
        bad += bool(check(o, y, DROPPED.get(y.stem, "")))
    print(f"\n{len(pairs)} 份卷，{bad} 份有短少")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
