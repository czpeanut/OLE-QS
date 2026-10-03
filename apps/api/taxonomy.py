"""選題篩選用的分類樹：科目 → 子科 → 冊別 → 版本 → 單元。

單元列表來自 data/curriculum/junior.yaml（國文、數學、自然、社會，三個版本）與
data/curriculum/english_topics.yaml（英語，跨版本的文法主題）。單元代號的算法與
scripts/classify_chapters.py 寫進題目標籤的 code 一致，篩選時直接比對 Question.unit_code。
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

import yaml

CURRICULUM = Path(os.environ.get("OLEQS_CURRICULUM", "data/curriculum"))

SUBJECTS = ["國文", "英語", "數學", "自然", "社會"]
SUBS = {"自然": ["生物", "理化", "地科"], "社會": ["歷史", "地理", "公民"]}
PUBLISHERS = ["翰林", "康軒", "南一"]
BOOKS = [(7, 1, "七上"), (7, 2, "七下"), (8, 1, "八上"), (8, 2, "八下"), (9, 1, "九上"), (9, 2, "九下")]
# 數學、自然各版本節次對齊（1-1 在三個版本是同一個主題），篩選只比代號；
# 國文、社會各版本課序不同，要連同版本比對。
MERGED = {"數學", "自然"}


def book_name(grade: int, semester: int | None) -> str:
    return next((n for g, s, n in BOOKS if g == grade and s == semester), f"{grade}年級")


@lru_cache(maxsize=1)
def _junior() -> dict:
    return yaml.safe_load((CURRICULUM / "junior.yaml").read_text(encoding="utf-8"))["grades"]


@lru_cache(maxsize=1)
def english_topics() -> list[dict]:
    p = CURRICULUM / "english_topics.yaml"
    return yaml.safe_load(p.read_text(encoding="utf-8"))["topics"] if p.is_file() else []


def units(subject: str, sub: str | None, grade: int, semester: int, publisher: str | None) -> list[dict]:
    """某冊某版本的單元列表：[{code, title, chapter, subject}]。"""
    if subject == "英語":
        return [{"code": t["id"], "title": t["title"], "chapter": t.get("group"), "subject": "英語",
                 "lessons": t.get("lessons") or {}}
                for t in english_topics()
                # 列到這一冊為止教過的主題：後面的冊別常考前面的文法
                if t.get("group") == "綜合" or (t["grade"], t["semester"]) <= (grade, semester)]
    book = ((_junior().get(grade) or {}).get(semester) or {}).get(subject) or {}
    subs = [sub] if sub else (SUBS.get(subject) or [""])
    pub = publisher or "翰林"
    out = []
    for s in subs:
        for u in (book.get(s) or {}).get(pub) or []:
            out.append({"code": u.get("lesson") or u["section"], "title": u["title"],
                        "chapter": (f"第{u['chapter']}章 {u['chapter_title']}" if u.get("chapter")
                                    else (u.get("kind") or None)),
                        "subject": s or subject})
    return out


def tree() -> dict:
    return {"subjects": [{"name": s, "subs": SUBS.get(s, [])} for s in SUBJECTS],
            "books": [{"grade": g, "semester": s, "name": n} for g, s, n in BOOKS],
            "publishers": PUBLISHERS, "merged": sorted(MERGED)}
