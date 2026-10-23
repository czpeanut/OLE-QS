#!/usr/bin/env python3
"""把 triage_pages.py 逐頁辨識的結果分成一份份考卷，補齊段考資訊，並和現有題庫比對重複。

    python scripts/group_triage.py <資料夾> out/triage.json -o out/triage_papers.json

分卷規則：
- 同一檔案依頁序走，遇到「卷頭頁」（first_page）就開新的一份；照片頁（JPG、TIFF）以資料夾為一份檔案。
- 只留國中（7～9 年級）的試題頁；解答、答案卷、封面、高中卷不收。
- 學校、學年度、學期、段考、年級、科目：先用這份卷卷頭頁的辨識結果，缺的再用資料夾名稱與檔名補。
  學校以檔名或資料夾裡的校名為準（模型偶爾把校名看錯，例如梧棲看成格樓）。
- 同一份卷有好幾個版本（原卷／清除字跡、重複下載）時，留「清除字跡」、手寫頁少、頁數多的那份。
- 與現有題庫比對：學年度、學期、段考、年級、科目（含子科）、學校都相同就算重複。
"""

from __future__ import annotations

import argparse
import collections
import hashlib
import json
import re
import sqlite3
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
IMAGES = {".jpg", ".jpeg", ".png", ".tif", ".tiff"}
SUBJECT_WORDS = [("理化", "自然", "理化"), ("化學", "自然", "理化"), ("物理", "自然", "理化"), ("生物", "自然", "生物"),
                 ("地科", "自然", "地科"), ("自然", "自然", None), ("國文", "國文", None), ("英文", "英語", None),
                 ("英語", "英語", None), ("數學", "數學", None), ("公民", "社會", "公民"), ("地理", "社會", "地理"),
                 ("歷史", "社會", "歷史"), ("社會", "社會", None)]
ALIASES = {"甲中": "大甲", "日中": "日南", "格樓": "梧棲", "晚明": "曉明", "南": "日南"}
SUBS = {"化學": "理化", "物理": "理化", "地球科學": "地科", "理化": "理化", "生物": "生物", "地科": "地科",
        "歷史": "歷史", "地理": "地理", "公民": "公民"}           # 模型回答的子科 → 題庫用的子科
CN = {"一": 1, "ㄧ": 1, "二": 2, "三": 3, "1": 1, "2": 2, "3": 3}
# 檔名、資料夾與卷頭不一致、已看過卷頭確認的（檔名片段 → 以卷頭為準的欄位）
OVERRIDES = {
    "大甲國中一下二段歷史": {"sub_subject": "地理", "exam_seq": 2},     # 卷頭：第二次段考七年級地理科
    "113衛道國一上一段生物": {"sub_subject": "理化"},                  # 卷頭：一年級理化科
    "龍津國中 國三生物 第一次段考": {"grade": 7},                        # 卷頭：適用年級七年級
    "龍津國中國三生物第一次段考": {"grade": 7},
}


def short_school(name: str | None) -> str | None:
    if not name:
        return None
    s = re.sub(r"^(?:國立|財團法人|私立)", "", name.strip())
    s = re.sub(r"^(?:[臺台]中市|[一-鿿]{2}[縣市])(?:立|私立)?", "", s)       # 只在有縣市字頭時才去掉「立」
    s = s.replace("東海大學附屬", "東大")
    s = re.sub(r"(?:附設)?國中部$", "", s)
    s = re.sub(r"(?:實驗)?(?:國民中學|高級中等學校|高級中學|國中|高中|女中|中學|附中)$", "", s)
    s = s.strip() or None
    return ALIASES.get(s, s) if s else None


def path_hints(rel: str) -> dict:
    h: dict = {}
    if m := re.search(r"(11\d)學年度", rel):
        h["year_roc"] = int(m.group(1))
    if m := re.search(r"(11\d)-([12])_第([一二三])次段考", rel):
        h.update(year_roc=int(m.group(1)), semester=int(m.group(2)), exam_seq=CN[m.group(3)])
    name = rel.rsplit("/", 1)[-1]
    if m := re.search(r"(11\d)-([12])\s", name):
        h.update(year_roc=int(m.group(1)), semester=int(m.group(2)))
    if m := re.search(r"(11\d)-?([12])-?([123])(?!\d)", name):          # 「11322陽明」「113-1-1中港」
        for k, v in (("year_roc", m.group(1)), ("semester", m.group(2)), ("exam_seq", m.group(3))):
            h.setdefault(k, int(v))
    if m := re.search(r"(11\d)(?:年)?[上下]?", name):
        h.setdefault("year_roc", int(m.group(1)))
    if "上" in name or "上學期" in name:
        h.setdefault("semester", 1)
    if "下" in name:
        h.setdefault("semester", 2)
    if m := re.search(r"第([一二三])次|([一二三])段|([123])st|([123])nd|([123])rd", name):
        h["exam_seq"] = CN[next(g for g in m.groups() if g)]          # 檔名比資料夾準（偶有放錯資料夾）
    if m := re.search(r"國([一ㄧ二三])|([一二三])年級|(?<![國高])([一二三])[上下]", name):
        h["grade"] = 6 + CN[next(g for g in m.groups() if g)]
    if m := re.search(r"高([一二三])", name):
        h["grade"] = 9 + CN[m.group(1)]
    named = {subj for w, subj, _ in SUBJECT_WORDS if w in name}
    h["multi"] = len(named) > 1 or "全科" in name
    if len(named) == 1:                                                 # 檔名列了好幾科（多科合併）就改用每份卷的辨識
        w, h["subject"], h["sub_subject"] = next(x for x in SUBJECT_WORDS if x[0] in name)
    parts = rel.split("/")
    folder = next((p for p in reversed(parts[:-1]) if re.search(r"國中|中學|女中|附中|高中", p)
                   and not re.search(r"歷屆試題|段考", p)), None)
    h["school_folder"] = short_school(folder)
    mentioned = [short_school(m) for m in re.findall(r"([一-鿿]{2}(?:國中|女中|高中|附中))", name)]
    mentioned += [ALIASES[a] for a in ("甲中", "日中") if a in name]
    h["school_name"] = next((s for s in mentioned if s), None)
    h["cleaned"] = "清除字跡" in rel
    return h


def load_pages(root: Path, triage: dict) -> dict[str, list[dict]]:
    """檔案（照片以資料夾）→ 依頁序的辨識結果。"""
    by_md5 = {}
    for p in sorted(root.rglob("*")):
        if p.is_file() and (p.suffix.lower() == ".pdf" or p.suffix.lower() in IMAGES):
            by_md5.setdefault(hashlib.md5(p.read_bytes()).hexdigest(), p)
    files: dict[str, list[dict]] = collections.defaultdict(list)
    for key, r in triage.items():
        md5, no = key.split(":")
        p = by_md5.get(md5)
        if p is None or "error" in r:
            continue
        rel = str(p.relative_to(root))
        unit = str(Path(rel).parent) + "/[照片]" if p.suffix.lower() in IMAGES else rel
        pno = re.search(r"P(\d+)\.\w+$", p.name)                     # 照片頁以檔名的 P1、P2 排序
        r = {**r, "sub_subject": SUBS.get(r.get("sub_subject"), r.get("sub_subject"))
             if r.get("sub_subject") in SUBS or r.get("sub_subject") in SUBS.values() else None}
        files[unit].append({**r, "file": rel, "page": int(no),
                            "sort": (int(pno.group(1)) if pno else 0, p.name, int(no))})
    for v in files.values():
        v.sort(key=lambda r: r["sort"])
    return files


def city_of(name: str | None) -> str | None:
    m = re.match(r"(?:國立)?([臺台]中|彰化|南投|苗栗|雲林|嘉義|高雄|臺北|台北|新北|桃園|新竹|臺南|台南|屏東|基隆|宜蘭|花蓮|臺東|台東)",
                 (name or "").strip())
    return {"臺中": "台中", "臺北": "台北", "臺南": "台南", "臺東": "台東"}.get(m.group(1), m.group(1)) if m else None


def first(seg: list[dict], k: str):
    return next((p.get(k) for p in seg if p.get(k)), None)


def same_paper(a: list[dict], b: list[dict]) -> bool:
    """相鄰兩段是不是同一份卷（中間某頁被誤判成卷頭）：科目、年級相同（或後段看不出來），學校沒有衝突。"""
    for k in ("subject", "grade", "sub_subject"):
        if first(b, k) and first(a, k) and first(b, k) != first(a, k):
            return False
    sa, sb = short_school(first(a, "school")), short_school(first(b, "school"))
    return not (sa and sb and sa != sb)


def segments(pages: list[dict]) -> list[list[dict]]:
    out, cur = [], []
    for pg in pages:
        if pg.get("kind") != "exam":
            if cur:
                out.append(cur)
                cur = []
            continue
        if pg.get("first_page") and cur:
            out.append(cur)
            cur = []
        cur.append(pg)
    if cur:
        out.append(cur)
    merged: list[list[dict]] = []
    for seg in out:
        if merged and same_paper(merged[-1], seg):
            merged[-1] += seg
        else:
            merged.append(seg)
    return merged


def multi_subject(pages: list[dict]) -> bool:
    """一個檔案裡有好幾科（卷頭頁的科目或子科不只一種）。"""
    heads = {(p.get("subject"), p.get("sub_subject")) for p in pages
             if p.get("kind") == "exam" and p.get("first_page") and p.get("school") and p.get("subject")}
    return len(heads) > 1


def meta_of(seg: list[dict], unit: str, multi: bool) -> dict:
    head = seg[0]
    # 照片頁以資料夾為一份檔案，資料夾名稱就是卷名（同資料夾的照片檔名偶有複製錯）
    hints = path_hints(unit.removesuffix("/[照片]") if unit.endswith("/[照片]") else seg[0]["file"])
    multi = multi or hints["multi"]
    if multi:                                                       # 多科合併：科目以每份卷自己的卷頭為準
        hints.pop("subject", None)
        hints.pop("sub_subject", None)
    hints.update(next((v for k, v in OVERRIDES.items() if k in seg[0]["file"]), {}))
    vote = lambda k: collections.Counter(p.get(k) for p in seg if p.get(k)).most_common(1)  # noqa: E731
    # 資料夾與檔名是上傳者整理的，比模型讀卷頭可靠：有寫就用，沒寫才用模型的辨識
    m = {}
    for k in ("year_roc", "semester", "exam_seq", "grade", "subject"):
        llm = head.get(k) or (vote(k)[0][0] if vote(k) else None)
        m[k] = hints.get(k) or llm
    if m.get("exam_seq") not in (1, 2, 3):
        m["exam_seq"] = 3 if (m.get("exam_seq") or 0) > 3 else None
    if "sub_subject" in hints:
        m["sub_subject"] = hints["sub_subject"]                       # 單科檔：子科只採檔名寫明的
    elif multi:
        m["sub_subject"] = vote("sub_subject")[0][0] if vote("sub_subject") else None
    else:
        m["sub_subject"] = None
    if m.get("subject") not in ("自然", "社會"):
        m["sub_subject"] = None
    llm_school = short_school(head.get("school") or (vote("school")[0][0] if vote("school") else None))
    m["school"] = hints.get("school_name") or hints.get("school_folder") or llm_school
    m["school_llm"] = llm_school
    m["city"] = city_of(first(seg, "school")) or "台中"
    levels = collections.Counter(p.get("level") for p in seg)
    if m.get("grade") in (7, 8, 9):                  # 完全中學國中部卷頭寫「高級中學」，模型常判成高中
        m["level"] = "junior"
    else:
        m["level"] = "senior" if (m.get("grade") or 0) >= 10 or levels["senior"] > levels["junior"] else "junior"
    m["handwriting"] = sum(1 for p in seg if p.get("handwriting"))
    m["cleaned"] = hints["cleaned"]
    m["answer_file"] = bool(re.search(r"(?<!無)ANS|解答|詳解|答案卷", seg[0]["file"].rsplit("/", 1)[-1], re.I))
    m["pages"] = [[p["file"], p["page"]] for p in seg]
    m["unit"] = unit
    return m


def existing_keys() -> dict[tuple, str]:
    con = sqlite3.connect(REPO / "data" / "oleqs.db")
    out = {}
    for did, sub in con.execute("select id, sub_subject from document"):
        p = did.split("_")
        # doc_<年>_<縣市>_<校>_<科>[_<子科>]_g<年級>s<學期>e<段考>
        m = re.match(r"g(\d)s(\d)e(\d)", p[-1])
        if not m:
            continue
        out[(int(p[1]), int(m.group(2)), int(m.group(3)), int(m.group(1)), p[4], p[5] if len(p) == 7 else None, p[3], p[2])] = did
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("root", type=Path)
    ap.add_argument("triage", type=Path)
    ap.add_argument("-o", "--out", type=Path, required=True)
    args = ap.parse_args()
    files = load_pages(args.root, json.loads(args.triage.read_text(encoding="utf-8")))
    papers = [meta_of(s, u, multi_subject(pages)) for u, pages in files.items() for s in segments(pages)]
    have = existing_keys()
    best: dict[tuple, dict] = {}
    same_unit: dict[tuple, dict] = {}
    for p in papers:
        if p["answer_file"]:
            p["status"] = "解答檔"
        elif p["level"] != "junior" or p.get("grade") not in (7, 8, 9):
            p["status"] = "高中卷"
        elif not all(p.get(k) for k in ("year_roc", "semester", "exam_seq", "subject", "school")):
            p["status"] = "資訊不全"
        else:
            key = (p["year_roc"], p["semester"], p["exam_seq"], p["grade"], p["subject"], p["sub_subject"], p["school"], p["city"])
            p["key"] = list(key)
            if (p["unit"], key) in same_unit:                         # 同一檔案裡被切開的同一份卷：併回去
                q = same_unit[(p["unit"], key)]
                q["pages"] += p["pages"]
                q["handwriting"] += p["handwriting"]
                p["status"] = "併入前段"
                continue
            same_unit[(p["unit"], key)] = p
            loose = [d for k, d in have.items() if k[:5] == key[:5] and k[6:] == key[6:]
                     and (k[5] in (None, key[5]) or key[5] is None)]
            if loose:
                p["status"], p["existing"] = "題庫已有", loose[0]
            else:
                rank = (p["cleaned"], -p["handwriting"], len(p["pages"]))
                if key not in best or rank > best[key][0]:
                    if key in best:
                        best[key][1]["status"] = "重複版本"
                    best[key] = (rank, p)
                    p["status"] = "新卷"
                else:
                    p["status"] = "重複版本"
    papers = [p for p in papers if p["status"] != "併入前段"]
    stats = collections.Counter(p["status"] for p in papers)
    args.out.write_text(json.dumps(papers, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"分出 {len(papers)} 份：" + "、".join(f"{k} {v}" for k, v in stats.most_common()))
    new = [p for p in papers if p["status"] == "新卷"]
    by = collections.Counter((p["subject"], p["grade"]) for p in new)
    print("新卷：" + "、".join(f"{s}{g}年級 {n}" for (s, g), n in sorted(by.items())))
    print(f"新卷頁數 {sum(len(p['pages']) for p in new)}；學校 {len({p['school'] for p in new})} 所")
    return 0


if __name__ == "__main__":
    sys.exit(main())
