#!/usr/bin/env python3
"""段落流（docx_stream.py 的輸出）→ 一份卷的結構：大題、題組、題目、選項、答案、詳解、附圖位置。

規則式，不用模型（參考國中 extract.py 的原則：寧可漏抓，不可錯抓）：
- 題號一定要連號（上一題＋1，新大題可以從 1 重新開始），不連號的段落當成上一題的內容；
- 認不出結構的題目由 word_pipeline.py 的品管閘門剔除、列入 dropped，不猜。

    python scripts/word_paper.py 卷.docx          # 測試用：印出解析結果（JSON）
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

ZW = "​"
IMG = re.compile(r"\[圖(\d+)\]")
LEADIMG = r"(?:\[圖\d+\]|⟦IMG:\d+⟧)"                 # 段落開頭的附圖錨點（docx_stream 的 [圖n] 或 word_pipeline 的 ⟦IMG:n⟧）
CN = "零一二三四五六七八九十"
SECTION = re.compile(r"^(第\s*[壹貳參肆伍一二三四五]\s*部分|[壹貳參肆伍陸柒捌玖拾]+\s*[、.．]|[一二三四五六七八九十]{1,3}\s*[、.．])")
SECTION_WORDS = re.compile(r"題|選擇|填充|計算|問答|作答|混合|非選|作圖|閱讀|題組")
GROUP = [
    re.compile(r"[（(]\s*(\d{1,3})\s*[）)]\s*[、,，～~〜\-－–—至到]\s*[（(]\s*(\d{1,3})\s*[）)]\s*題"),   # (40)～(42)題為題組
    re.compile(r"第?\s*(\d{1,3})\s*[.．]?\s*(?:[、,，～~〜\-－–—至到]|\s)\s*(?:第\s*)?(\d{1,3})\s*[.．]?\s*(?:題\s*(?:為|是|屬)?|為|是|屬)\s*題組"),
    re.compile(r"(?:閱讀|根據|依據|參考)[^。，,]{0,30}?(?:回答|作答)\s*第?\s*(\d{1,3})\s*[.．]?\s*[、,，～~〜\-－–至到]\s*(\d{1,3})\s*[.．]?\s*題"),
    re.compile(r"[（(【〔]\s*第?\s*(\d{1,3})\s*[.．]?\s*[、,，～~〜\-－–至到]\s*(\d{1,3})\s*[.．]?\s*題\s*[）)】〕]"),
]
BLANK = r"[（(][\s　]*[）)]"
QSTART = [
    re.compile(rf"^{BLANK}\s*(\d{{1,3}})\s*[.．、]"),                              # 翰林：（　）12.
    re.compile(rf"^(\d{{1,3}})\s*[.．、]\s*{BLANK}"),                               # 南一：12. (　)
    re.compile(r"^(\d{1,3})\s*[.．、](?![\d.．])"),                                 # 12. 題目
    re.compile(r"^(\d{1,3})[ \u3000]+(?=[\u4e00-\u9fff「（(])"),                     # 12　題目（原卷漏打句點）
    re.compile(rf"^{BLANK}\s*[（(]\s*(\d{{1,3}})\s*[）)]"),                          # (　)(41) 題目
    re.compile(r"^[（(]\s*(\d{1,3})\s*[）)](?![\d])"),                              # (12) 題目
]
ANSWER_HEAD = re.compile(r"《\s*(?:參考)?答\s*案\s*》|【\s*(?:參考)?答\s*案\s*】|^[\s－\-—=＝*＊]*(?:參考答案|試題答案|答案卷|標準答案|解答)[\s－\-—=＝*＊:：]*$|"
                         r"^[\s－\-—=＝]*(?:答案|解答)[\s－\-—=＝]*$|參考答案|試題答案")
EXPLAIN_HEAD = re.compile(r"《\s*(?:試題)?(?:解析|詳解)\s*》|【\s*(?:試題)?(?:解析|詳解)\s*】|^[\s－\-—=＝]*(?:試題解析|詳解|解析)[\s－\-—=＝:：]*$")
OPT = re.compile(r"[（(]\s*([A-E])\s*[）)]")
ANS_BLANK_LINE = re.compile(r"^\s*(答|解|Ans)\s*[:：]?\s*$")


def plain(s: str) -> str:
    return re.sub(r"</?u>", "", s).replace(ZW, "")


def is_section(text: str) -> bool:
    t = re.sub(rf"^(\s*{LEADIMG}\s*)+", "", plain(text)).strip()      # 標題段落開頭錨著的圖不算
    return bool(SECTION.match(t)) and bool(SECTION_WORDS.search(t[:40])) and len(t) < 120


def group_range(text: str) -> tuple[int, int, int, int] | None:
    t = plain(text)
    for g in GROUP:
        m = g.search(t[:80])
        if m:
            a, b = int(m.group(1)), int(m.group(2))
            if a < b <= a + 15:
                return a, b, m.start(), m.end()
    return None


def question_number(text: str) -> tuple[int, int] | None:
    """段落開頭的題號 → (題號, 題號後的位置)；開頭的 [圖n] 不算。"""
    t = plain(text)
    lead = re.match(rf"^(\s*{LEADIMG}\s*)*\s*", t).end()
    for rx in QSTART:
        m = rx.match(t[lead:])
        if m:
            return int(m.group(1)), lead + m.end()
    return None


def section_type(name: str) -> str | None:
    n = plain(name)
    if re.search(r"多選|多重選擇|複選", n):
        return "multiple"
    if re.search(r"單選|單一選擇|選擇題", n) and not re.search(r"非選", n):
        return "single"
    if re.search(r"填充|選填", n):
        return "fill"
    if re.search(r"非選|計算|問答|簡答|作圖|申論|手寫", n):
        return "calc"
    return None


def find_answer_zone(rows: list[dict], first_q: int) -> tuple[int, int]:
    """(答案區起點, 詳解區起點)；沒有就是 len(rows)。"""
    n = len(rows)
    ans = exp = n
    for k in range(first_q + 1, n):
        t = plain(rows[k]["text"]).strip()
        if len(t) < 60 and ANSWER_HEAD.search(t):
            ans = k
            break
    for k in range(ans, n):
        t = plain(rows[k]["text"]).strip()
        if len(t) < 60 and EXPLAIN_HEAD.search(t):
            exp = k
            break
    return ans, exp


def parse(rows: list[dict]) -> dict:
    """主流程。回傳 {prelude, sections, questions, groups, answers, explanations}。"""
    # 1. 找題目區：第一個題號 1 的段落
    first = next((k for k, r in enumerate(rows) if (q := question_number(r["text"])) and q[0] == 1), None)
    if first is None:
        raise ValueError("找不到第 1 題")
    ans_at, exp_at = find_answer_zone(rows, first)
    head = rows[:first]
    body = rows[first:ans_at]
    # 卷頭最後一個大題標題也算（例如「一、單選題」在第 1 題之前）
    sections: list[dict] = []
    for r in head:
        if is_section(r["text"]):
            sections.append({"name": plain(r["text"]).strip(), "rows": []})
    if not sections:
        sections.append({"name": "", "rows": []})
    sections = sections[-1:]                           # 卷頭只留最靠近第 1 題的大題標題

    questions: list[dict] = []
    groups: list[dict] = []
    cur_q: dict | None = None
    cur_group: dict | None = None
    expect = 1
    pending_imgs = ""
    # 第 1 題之前、最後一個大題標題之後的文章（例如「一、閱讀測驗」後直接接文章）
    last_sec = max((k for k, r in enumerate(head) if is_section(r["text"])), default=-1)
    pre = [r for r in head[last_sec + 1:] if not is_instruction(r["text"])] if last_sec >= 0 else []
    if pre and passage_like(pre):
        cur_group = {"from": None, "to": None, "rows": pre, "section": 1, "implicit": True}
        groups.append(cur_group)
    for r in body:
        text = r["text"]
        qn = question_number(text)
        if qn and qn[0] == 1 and expect > 1 and sections[-1]["rows"]:
            qn = None if not any(is_section(x["text"]) for x in sections[-1]["rows"][-1:]) else qn
        if is_section(text) and not qn:
            lead = re.findall(LEADIMG, plain(text)[:20])
            sections.append({"name": re.sub(rf"^(\s*{LEADIMG}\s*)+", "", plain(text)).strip(), "rows": []})
            pending_imgs = "".join(lead)                    # 標題段落上錨的圖，交給下一題（位置再由 PDF 決定）
            cur_q = None
            cur_group = None
            continue
        g = group_range(text)
        if g and qn and g[2] <= 4:
            qn = None                                       # 段落開頭就是「21.～23.為題組」：是題組標題，不是第 21 題
        if g and not qn:
            a, b, s0, s1 = g
            rest = plain(text)[s1:].strip(" 　:：")
            lead = "".join(re.findall(LEADIMG, plain(text)[:s1]))   # 錨定在題組標題段落的圖
            cur_group = {"from": a, "to": b, "rows": [], "section": len(sections)}
            if rest or lead:
                cur_group["rows"].append({**r, "text": lead + rest})
            groups.append(cur_group)
            cur_q = None
            continue
        jump = qn and expect < qn[0] <= expect + 2 and strong_start(text)    # 原卷漏了一個題號：往後對上，不讓後面整段被併進上一題
        if qn and (qn[0] == expect or jump or (qn[0] == 1 and sections[-1]["rows"] == [] and len(sections) > 1)):
            num, pos = qn
            lead_imgs = "".join(re.findall(LEADIMG, plain(text)[:pos]))
            cur_q = {"number": num, "section": len(sections), "rows": [{**r, "text": pending_imgs + lead_imgs + strip_lead(text)}]}
            pending_imgs = ""
            questions.append(cur_q)
            sections[-1]["rows"].append(r)
            expect = num + 1
            if cur_group and cur_group.get("implicit"):
                cur_group["from"] = cur_group["from"] or num
                cur_group["to"] = num
            elif cur_group and not (cur_group["from"] <= num <= cur_group["to"]):
                cur_group = None
            if cur_group:
                cur_q["group"] = cur_group
            continue
        # 一般段落：在題組材料區（題組標題之後、第一題之前）就是材料，否則併入目前這題
        if cur_group and (not cur_q or cur_q.get("group") is not cur_group):
            cur_group["rows"].append(r)
        elif not cur_q:
            # 大題標題後面直接接文章（沒有「第 X～Y 題為題組」）：隱含題組
            if not is_instruction(text) and passage_like([r]):
                cur_group = {"from": None, "to": None, "rows": [r], "section": len(sections), "implicit": True}
                groups.append(cur_group)
        elif r["kind"] == "p" and options_complete(cur_q) and passage_like([r], need_text=True) \
                and not OPT.match(plain(text).strip()) and not refers_ahead(cur_q):
            # 上一題選項已經完整，後面又出現長文：下一組題目的文章
            cur_group = {"from": None, "to": None, "rows": [r], "section": len(sections), "implicit": True}
            groups.append(cur_group)
            cur_q = None
        else:
            cur_q["rows"].append(r)
        sections[-1]["rows"].append(r)
    blocks = answer_blocks(rows[ans_at:]) if ans_at < len(rows) else []
    ans_b = [b["ans"] for b in blocks if b["ans"]]
    exp_b = [b["exp"] for b in blocks if b["exp"]]
    answers = [dict(e, block=k) for k, b in enumerate(ans_b) for e in parse_entries(b)]
    explanations = [dict(e, block=k) for k, b in enumerate(exp_b) for e in parse_entries(b)]
    # 大題底下只有一段沒編號的內容（例如非選題只有 (1)(2) 小題）：當成這一大題的第 1 題
    for g in list(groups):
        if g.get("implicit") and g["from"] is None and g["rows"]:
            sec = g["section"]
            if not any(q["section"] == sec for q in questions) and any(plain(r["text"]).strip() for r in g["rows"]):
                questions.append({"number": 1, "section": sec, "rows": g["rows"], "unnumbered": True})
                groups.remove(g)
    questions.sort(key=lambda q: (q["section"], q["number"]))
    return {"head": head, "sections": sections, "questions": questions, "groups": groups,
            "answers": answers, "explanations": explanations, "has_answer_zone": ans_at < len(rows)}


def strong_start(text: str) -> bool:
    """明確的題目開頭（有作答括號，或題號後直接接題目文字），跳號時才敢接受。"""
    t = re.sub(rf"^(\s*{LEADIMG}\s*)*", "", plain(text))
    return bool(re.match(rf"^({BLANK}\s*\d{{1,3}}\s*[.．、]|\d{{1,3}}\s*[.．、]\s*{BLANK}|\d{{1,3}}\s*[.．、]\s*[\u4e00-\u9fff「（(])", t))


def is_instruction(text: str) -> bool:
    """作答說明（「說明：第1題至第20題…」「每題2分」），不是文章。"""
    t = plain(text).strip()
    return bool(re.match(r"^[※＊*]?\s*(說明|作答|注意|每題|答錯|本大題|請將|請依|請在|以下|下列各題)", t))


def passage_like(rows: list[dict], need_text: bool = False) -> bool:
    """像文章（共用材料）：中文字夠多（30 字以上）；need_text=False 時只有附圖也算。"""
    cjk = sum(len(re.findall(r"[\u4e00-\u9fff]", plain(r["text"]))) for r in rows)
    return cjk >= 30 or (not need_text and any(re.search(LEADIMG, r["text"]) for r in rows))


def refers_ahead(q: dict) -> bool:
    """這題提到「下圖」「下表」「下文」，後面的材料是它自己的。"""
    return bool(re.search(r"下(圖|表|文|列(圖|表|資料))|如下|附(圖|表)", plain(q["rows"][0]["text"])[:200]))


def options_complete(q: dict) -> bool:
    """這題的選項是不是已經到 (D) 或 (E)。"""
    txt = "\n".join(r["text"] for r in q["rows"])
    _, opts = split_options(txt)
    return len(opts) >= 4


def strip_lead(text: str) -> str:
    """去掉段落開頭的 [圖n]、作答括號與題號，留下題幹。"""
    t = text.replace(ZW, "")
    lead = re.match(rf"^((?:<u>)?\s*{LEADIMG}\s*(?:</u>)?)*\s*", t).end()
    t = t[lead:]
    for rx in QSTART:
        m = rx.match(plain(t))
        if m:
            # 對回含 <u> 的原字串：題號區段不會有底線，直接用長度
            cut = len(plain(t)[:m.end()])
            k, seen = 0, 0
            while seen < cut and k < len(t):
                if t.startswith("<u>", k):
                    k += 3
                    continue
                if t.startswith("</u>", k):
                    k += 4
                    continue
                k += 1
                seen += 1
            t = t[k:]
            break
    t = re.sub(rf"^\s*{BLANK}", "", t)                    # 南一「12. (　)」題號後的作答括號
    return t


GLOBAL_EXPLAIN = re.compile(r"《\s*(?:試題)?(?:解析|詳解)\s*》|^[\s－\-—=＝]*(?:試題解析|詳解)[\s－\-—=＝]*$")


def answer_blocks(zone: list[dict]) -> list[dict]:
    """答案區依大題分段，每段再分答案與解析：
    - 「一、單選題 → 1.(C) 2.(E) → 【解析】→ 1.… → 二、多選題 → …」（每大題自己的解析）；
    - 「《答案》各大題答案 … 《試題解析》各大題解析」（整卷一次）：《試題解析》之後全部算解析。
    回傳 [{"ans": 答案段落, "exp": 解析段落}]，答案與解析各自依大題順序。"""
    ans_blocks, exp_blocks = [[]], [[]]
    mode, global_exp = "ans", False
    for r in zone[1:]:
        t = plain(r["text"]).strip()
        if len(t) < 60 and GLOBAL_EXPLAIN.search(t):
            global_exp, mode = True, "exp"
            if exp_blocks[-1]:
                exp_blocks.append([])
            continue
        if len(t) < 40 and (is_section(t) or re.match(r"^[一二三四五六七八九十壹貳參肆伍]+\s*[、.．]", t)):
            if global_exp:
                if exp_blocks[-1]:
                    exp_blocks.append([])
            else:
                if ans_blocks[-1] or exp_blocks[-1]:
                    ans_blocks.append([])
                    exp_blocks.append([])
                mode = "ans"
            continue
        if len(t) < 60 and EXPLAIN_HEAD.search(t):
            mode = "exp"
            continue
        (exp_blocks if mode == "exp" else ans_blocks)[-1].append(r)
    blocks = []
    for k in range(max(len(ans_blocks), len(exp_blocks))):
        a = ans_blocks[k] if k < len(ans_blocks) else []
        e = exp_blocks[k] if k < len(exp_blocks) else []
        blocks.append({"ans": a, "exp": e})
    return blocks


def parse_entries(rows: list[dict]) -> list[dict]:
    """答案區、詳解區：依序找「題號. 內容」，表格式答案（題號列＋答案列）先攤平成同樣格式。"""
    parts: list[str] = []
    for r in rows:
        t = r["text"]
        if r["kind"] == "table":
            t = flatten_answer_table(t)
        if r.get("images"):
            t = IMG.sub("⟦圖⟧", t)
        parts.append(t)
    text = "\n".join(parts)
    out = []
    marks = list(re.finditer(r"(?:(?<=\s)|^|(?<=[。）)\]]))(\d{1,3})(?:\s*[.．、:：](?![\d])|[ \u3000]+(?=[（(]?[A-E][）)]?))", text))
    expect = None
    kept = []
    for m in marks:
        n = int(m.group(1))
        if expect is None or n == expect or n == 1 or (kept and n == kept[-1][0] + 1):
            kept.append((n, m))
            expect = n + 1
    for k, (n, m) in enumerate(kept):
        end = kept[k + 1][1].start() if k + 1 < len(kept) else len(text)
        out.append({"number": n, "text": text[m.end():end].strip(" 　\n")})
    return out


def flatten_answer_table(md: str) -> str:
    """「| 題號 | 1 | 2 |」「| 答案 | B | C |」這種表格 → 「1.B 2.C」。看不出結構就原樣回傳。"""
    rows = [[c.strip() for c in line.strip().strip("|").split("|")] for line in md.split("\n")
            if line.strip().startswith("|") and not re.match(r"^\|\s*-", line.strip())]
    out = []
    k = 0
    while k + 1 < len(rows):
        nums, vals = rows[k], rows[k + 1]
        cells = [c for c in nums if c]
        if cells and sum(bool(re.fullmatch(r"\d{1,3}\.?", c)) for c in cells) >= max(2, len(cells) - 1):
            for a, b in zip(nums, vals):
                if re.fullmatch(r"\d{1,3}\.?", a):
                    out.append(f"{a.rstrip('.')}.{b}")
            k += 2
        else:
            k += 1
    return " ".join(out) if out else md


def split_options(text: str) -> tuple[str, list[dict]]:
    """題目文字 → (題幹, 選項)。選項代號要依序 A、B、C…，表格裡的代號不算。"""
    masked = mask(text)
    pos = [(m.start(), m.end(), m.group(1)) for m in OPT.finditer(masked)]
    best = None
    for i, (s, e, lab) in enumerate(pos):
        if lab != "A":
            continue
        chain = [(s, e, lab)]
        for s2, e2, lab2 in pos[i + 1:]:
            if ord(lab2) == ord(chain[-1][2]) + 1:
                chain.append((s2, e2, lab2))
        if len(chain) >= 2 and (best is None or len(chain) >= len(best)):
            best = chain
            if len(chain) >= 4:
                break
    if not best:
        return text, []
    stem = text[:best[0][0]].rstrip()
    opts = []
    for k, (s, e, lab) in enumerate(best):
        end = best[k + 1][0] if k + 1 < len(best) else len(text)
        opts.append({"label": lab, "content": text[e:end].strip(" 　\n")})
    return stem, opts


def mask(text: str) -> str:
    """把表格列與 $…$ 換成等長的空白，找選項代號時不看它們。"""
    out = re.sub(r"\$[^$]*\$", lambda m: " " * len(m.group(0)), text)
    out = re.sub(r"(?m)^\|.*$", lambda m: " " * len(m.group(0)), out)
    return out


def main() -> int:
    import docx_stream
    rows = list(docx_stream.stream(Path(sys.argv[1])))
    res = parse(rows)
    for q in res["questions"]:
        txt = "\n".join(r["text"] for r in q["rows"])
        stem, opts = split_options(txt)
        print(q["section"], q["number"], ("G" if q.get("group") else " "), len(opts), stem[:60].replace("\n", "⏎"))
    print("groups", [(g["from"], g["to"]) for g in res["groups"]])
    print("sections", [s["name"][:20] for s in res["sections"]])
    print("answers", [(a["number"], a["text"][:8]) for a in res["answers"]][:60])
    return 0


if __name__ == "__main__":
    sys.exit(main())
