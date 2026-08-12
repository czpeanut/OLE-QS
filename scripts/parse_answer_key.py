#!/usr/bin/env python3
"""從考卷 PDF 裡找出答案頁，把「題號 → 答案」解析出來。

作法：**用 PDF 的表格結構，不要對展平後的文字寫正則。**
答案卷在 PDF 裡本來就是表格，PyMuPDF 的 find_tables() 能直接還原格線與儲存格；
一旦展平成純文字，「1 C 2 B」和「1 2 3 / C B A」會變成同一串數字與字母，
再怎麼寫正則都在猜。實測五份真實考卷，走表格結構全部一次解析成功。

實測到的四種版型（同一所學校、同一次段考，五個科目就用了四種）:

    交錯 interleaved   [1, C, 2, B, 3, A, ...]        社會、自然
    分列 stacked       [1, 2, 3, ...] / [C, B, A, ...] 數學、英語、國文
    標籤 labeled       ['1.', '「ㄉㄧㄢˋ」高座椅：墊', ...]  國文字音字形
    自由 freeform      整格是計算題的詳解文字            數學計算題

必須排除的雜訊表:
    配分表（答對題數 → 得分）長得和「分列」版型一模一樣，但它不是答案。
    判別方式：答案欄全部是整數且遞增。

用法:
    python scripts/parse_answer_key.py 考卷.pdf
    python scripts/parse_answer_key.py 目錄/ -o out/answer_keys
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import unicodedata
from dataclasses import asdict, dataclass, field
from pathlib import Path

try:
    import pymupdf as fitz
except ImportError:
    try:
        import fitz
    except ImportError:
        sys.exit("需要 PyMuPDF，請先執行：pip install pymupdf")

# 答案頁的標題關鍵字。「答案卷」可能是空白的（給學生寫），也可能已填答案，
# 兩者都會被撈進來，之後靠「有沒有解析出答案」自然分辨。
ANSWER_PAGE_HINTS = ("解答", "答案卷", "解析卷", "參考答案", "答案表")

# 配分表的表頭關鍵字 —— 這種表和答案表結構相同但語意完全不同
SCORE_TABLE_HINTS = ("答對", "得分", "配分", "題數")

NUM_RE = re.compile(r"^\(?\s*(\d{1,3})\s*\)?\s*[.、．]?$")
# 題號格常帶註記：「(6) (全對才給分)」。只允許「數字 + 一組括號註記」，
# 刻意不放寬成「開頭是數字就算」—— 那會把計算題詳解「38/15=2.533…」誤判成第 38 題。
LABEL_RE = re.compile(r"^\(?\s*(\d{1,3})\s*\)?\s*[.、．]?\s*(?:[（(][^）)]*[）)])?$")
CHOICE_RE = re.compile(r"^[A-EＡ-Ｅ]$")

# 空白答案卷（給學生作答用）會把題幹或作答格印出來，解析時會擠出假答案。
# 這些是「這一格不是答案」的訊號。
NOT_AN_ANSWER = ("全對才給分", "分段給分", "請寫出", "請用黑筆", "否則不予計分",
                 "座號", "姓名", "____")
# 空白答案卷會預印作答提示，例如「x =」「y =」各一行，等號後面是空的。
# 這種格看起來有內容，其實是留白給學生填。
PLACEHOLDER_RE = re.compile(r"^[A-Za-z]\s*[=＝]\s*$")


def norm(s: str | None) -> str:
    if not s:
        return ""
    return unicodedata.normalize("NFKC", s).strip()


def as_number(cell: str | None) -> int | None:
    m = NUM_RE.match(norm(cell))
    return int(m.group(1)) if m else None


def as_label(cell: str | None) -> int | None:
    """題號格專用：容忍尾隨的括號註記。"""
    m = LABEL_RE.match(norm(cell))
    return int(m.group(1)) if m else None


def is_choice(cell: str | None) -> bool:
    return bool(CHOICE_RE.match(norm(cell)))


def clean_answer(cell: str | None, allow_numeric: bool = True) -> str:
    """把一格的內容整理成純答案，判定不是答案時回傳空字串。

    答案卷常把題幹和答案寫在同一格（「『ㄉㄧㄢˋ』高座椅：墊」），
    取冒號之後才是答案 —— 但只在「冒號後比冒號前短」時才切，
    避免把本身含冒號的完整答案切壞。

    allow_numeric：純數字是不是合法答案。
        數學填充題的答案本來就常是數字（290、14、40000），預設允許。
        只有「交錯」版型要關掉 —— 那種版型裡題號與答案相鄰，
        [1, 2, 3, 4] 這種題號列會被讀成「1 的答案是 2」。
        「分列」版型的題號列是獨立辨識的，沒有這個歧義。
    """
    a = norm(cell)
    if not a or any(bad in a for bad in NOT_AN_ANSWER):
        return ""
    if not allow_numeric and as_number(a) is not None:
        return ""
    lines = [ln.strip() for ln in a.splitlines() if ln.strip()]
    if lines and all(PLACEHOLDER_RE.match(ln) for ln in lines):
        return ""                       # 只有「x =」「y =」這種預印提示

    for sep in ("：", ":"):
        if sep in a:
            before, _, after = a.partition(sep)
            after = after.strip()
            if after and len(after) < len(before):
                a = after
                break
    return a.strip()


@dataclass
class KeyEntry:
    number: int
    answer: str
    layout: str
    table_index: int


@dataclass
class PageKeys:
    page_no: int
    title: str
    entries: list[KeyEntry] = field(default_factory=list)
    skipped_tables: list[str] = field(default_factory=list)
    likely_blank_sheet: bool = False   # 疑似「給學生作答的空白答案卷」


# ─────────────────────── 三種版型的解析 ───────────────────────

def parse_interleaved(rows: list[list[str]]) -> list[tuple[int, str]]:
    """[1, C, 2, B, 3, A, ...] —— 題號與答案在同一列交錯。"""
    out: list[tuple[int, str]] = []
    for row in rows:
        cells = [norm(c) for c in row]
        # 必須成對出現：偶數位是題號、奇數位是答案
        for i in range(0, len(cells) - 1, 2):
            n, a = as_label(cells[i]), clean_answer(cells[i + 1], allow_numeric=False)
            if n is not None and a:
                out.append((n, a))
    return out


def parse_stacked(rows: list[list[str]]) -> list[tuple[int, str]]:
    """[1, 2, 3, ...] 一列題號，下一列是對應答案。可以有多組（題號列/答案列）交替。"""
    out: list[tuple[int, str]] = []
    for i in range(len(rows) - 1):
        head = [norm(c) for c in rows[i]]
        body = [norm(c) for c in rows[i + 1]]
        nums = [as_label(c) for c in head]
        # 題號列的判準：**所有非空格都是題號**，且遞增。
        # 不設「至少幾格」的下限 —— 最後一格常單獨成列（例：填充題只剩 (13) 一格），
        # 設下限會把它整列丟掉。改用「非空格全部都是題號」這個更嚴格的條件來防誤判。
        head_ne = [c for c in head if c]
        got = [n for n in nums if n is not None]
        if not got or len(got) < len(head_ne) or got != sorted(got):
            continue
        for n, cell in zip(nums, body):
            a = clean_answer(cell)
            if n is not None and a:
                out.append((n, a))
    return out


def parse_labeled(rows: list[list[str]]) -> list[tuple[int, str]]:
    """['1.', '「ㄉㄧㄢˋ」高座椅：墊', '2.', ...] —— 題號後緊跟內容。

    內容格裡答案通常在冒號之後；沒有冒號就整格當答案。

    和「交錯」版型一樣有相鄰歧義：空白答案卷上的題號列 [1,2,3,4,5]
    會被讀成「1 的答案是 2」。因此同樣不接受純數字答案 ——
    這個版型的答案是國字或注音，本來就不會是裸數字。
    """
    out: list[tuple[int, str]] = []
    for row in rows:
        cells = [norm(c) for c in row]
        for i in range(0, len(cells) - 1, 2):
            n, ans = as_label(cells[i]), clean_answer(cells[i + 1], allow_numeric=False)
            if n is not None and ans:
                out.append((n, ans))
    return out


# ─────────────────────── 雜訊表排除 ───────────────────────

def looks_like_score_table(rows: list[list[str]], pairs: list[tuple[int, str]]) -> bool:
    """配分表（答對題數 → 得分）與答案表結構相同，必須排除。

    兩個訊號，任一命中即排除：
      1. 表頭出現「答對／得分／配分」
      2. 解析出的「答案」全部是整數，而且隨題號遞增 —— 那是分數，不是答案
    """
    flat = " ".join(norm(c) for row in rows[:2] for c in row)
    if any(h in flat for h in SCORE_TABLE_HINTS):
        return True

    if len(pairs) < 4:
        return False
    values = [as_number(a) for _, a in pairs]
    if any(v is None for v in values):
        return False
    return values == sorted(values) and len(set(values)) > 2


def parse_table(rows: list[list[str]], idx: int) -> tuple[list[KeyEntry], str | None]:
    """挑選最適合的版型。回傳 (答案, 略過原因)。"""
    if not rows:
        return [], "空表"

    candidates = [
        ("interleaved", parse_interleaved(rows)),
        ("stacked", parse_stacked(rows)),
        ("labeled", parse_labeled(rows)),
    ]
    # 選抓到最多筆的版型；平手時偏好選項字母佔比高的（比較像選擇題答案表）
    def score(item):
        name, pairs = item
        letters = sum(1 for _, a in pairs if is_choice(a))
        return (len(pairs), letters)

    flat_head = " ".join(norm(c) for row in rows[:2] for c in row)
    if any(h in flat_head for h in SCORE_TABLE_HINTS):
        return [], "疑似配分表，已排除"

    layout, pairs = max(candidates, key=score)
    if not pairs:
        return [], "無法辨識版型"

    if looks_like_score_table(rows, pairs):
        return [], "疑似配分表，已排除"

    # 去重只在同一個表格內做。
    # 全卷去重是錯的 —— 不同大題的題號會重複（數學卷選擇題 1~10 與填充題 (1)~(13)
    # 都從 1 開始），全域壓平會把後面的大題整段吃掉。
    seen: dict[int, str] = {}
    for n, a in pairs:
        seen.setdefault(n, a)

    return [KeyEntry(n, a, layout, idx) for n, a in sorted(seen.items())], None


# ─────────────────────── 主流程 ───────────────────────

def parse_pdf(path: Path) -> dict:
    doc = fitz.open(path)
    pages: list[PageKeys] = []

    for pno, page in enumerate(doc, start=1):
        text = page.get_text("text")
        if not any(h in text for h in ANSWER_PAGE_HINTS):
            continue

        title = next((ln.strip() for ln in text.splitlines() if ln.strip()), "")
        pk = PageKeys(page_no=pno, title=title[:80])

        for idx, tab in enumerate(page.find_tables().tables, start=1):
            try:
                rows = tab.extract()
            except Exception:
                continue
            entries, skip = parse_table(rows, idx)
            if skip:
                pk.skipped_tables.append(f"表{idx}：{skip}")
            pk.entries.extend(entries)

        # 空白答案卷（發給學生填的那份）與已填答案的解答卷長得幾乎一樣，
        # 差別只在格子有沒有內容。表格很多但幾乎解析不出答案 → 幾乎確定是空白卷。
        pk.likely_blank_sheet = (len(pk.entries) < 5
                                 and len(pk.skipped_tables) + bool(pk.entries) >= 3)
        if pk.entries or pk.skipped_tables:
            pages.append(pk)

    doc.close()

    # 全卷彙整。同一份卷裡不同大題的題號可能重複（例：選擇 1-10、填充 1-13），
    # 因此保留「頁 + 表」的來源資訊，不直接壓成單一 dict。
    total = sum(len(p.entries) for p in pages)
    return {"source": str(path), "answer_pages": [asdict(p) for p in pages],
            "total_entries": total}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("target", type=Path, help="PDF 檔或含 PDF 的目錄")
    ap.add_argument("-o", "--out", type=Path, help="輸出目錄（省略則只印摘要）")
    args = ap.parse_args()

    pdfs = (sorted(args.target.rglob("*.pdf")) if args.target.is_dir()
            else [args.target])
    if not pdfs:
        return sys.exit("找不到 PDF")

    if args.out:
        args.out.mkdir(parents=True, exist_ok=True)

    grand = 0
    for pdf in pdfs:
        res = parse_pdf(pdf)
        grand += res["total_entries"]
        print(f"\n{'=' * 60}\n{pdf.name}  —  解析出 {res['total_entries']} 個答案")
        for p in res["answer_pages"]:
            tag = "  ⚠ 疑似空白答案卷，解析結果不可信" if p["likely_blank_sheet"] else ""
            print(f"  p{p['page_no']}  {p['title']}{tag}")
            by_layout: dict[str, list] = {}
            for e in p["entries"]:
                by_layout.setdefault(e["layout"], []).append(e)
            for layout, es in by_layout.items():
                nums = [e["number"] for e in es]
                preview = "  ".join(f"{e['number']}={e['answer']}" for e in es[:6])
                print(f"     [{layout}] {len(es)} 筆  題號 {min(nums)}~{max(nums)}")
                print(f"        {preview}{'  …' if len(es) > 6 else ''}")
            for s in p["skipped_tables"]:
                print(f"     ⊘ {s}")

        if args.out:
            dest = args.out / f"{pdf.stem}.answers.json"
            dest.write_text(json.dumps(res, ensure_ascii=False, indent=2),
                            encoding="utf-8")

    print(f"\n{'=' * 60}\n{len(pdfs)} 份檔案，共解析出 {grand} 個答案")
    if args.out:
        print(f"寫入 {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
