"""把擷取結果（YAML/JSON）載入資料庫。

刻意設計成可重複執行：同一份文件重載時先整份刪除再寫入，
不做增量合併 —— 校對階段會頻繁重跑，增量合併的邊界情況遠比重建昂貴。

載入時強制檢查來源標註。依授權條件，公開試題可用但須保留出處，
所以缺來源的資料在這裡就會被擋下，而不是等到匯出才發現。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import yaml
from sqlalchemy import delete
from sqlalchemy.orm import Session

from .db import get_session, init_db, reindex_question
from .models import (Asset, AssetKind, AnswerStatus, Document, Option,
                     Question, QuestionSource, QuestionType, ReviewStatus,
                     Section, Tag, split_school)

REQUIRED_SOURCE_FIELDS = ("title", "school", "exam_name", "academic_year_roc",
                          "grade", "subject")


def load_file(path: Path) -> dict:
    text = path.read_text(encoding="utf-8")
    return yaml.safe_load(text) if path.suffix in {".yaml", ".yml"} else json.loads(text)


def searchable_body(q: dict) -> str:
    """組出這一題的可搜尋文字：題幹 + 題組說明 + 選項 + 標籤。"""
    parts = [q.get("stem", ""), q.get("group_stem") or ""]
    parts += [(o.get("content") or "") for o in (q.get("options") or [])]
    tags = q.get("tags") or {}
    parts.append(tags.get("textbook") or "")
    parts += tags.get("concept") or []
    return " ".join(p for p in parts if p)


def import_document(session: Session, doc: dict, source_file: str | None = None) -> str:
    meta = doc.get("document") or {}

    missing = [f for f in REQUIRED_SOURCE_FIELDS if not meta.get(f)]
    if missing:
        raise ValueError(
            f"來源標註不完整，缺少 {missing}。"
            "依授權條件，題目必須可追溯出處，因此這些欄位不可為空。")

    doc_id = meta["id"]

    # 重載：整份清掉再寫。
    # 題目已不隨文件級聯刪除（見 models.Document.questions），
    # 因此這裡必須明確刪除本文件匯入的題目，否則重跑會殘留舊題。
    session.execute(delete(Question).where(Question.document_id == doc_id))
    session.execute(delete(Document).where(Document.id == doc_id))
    session.execute(
        __import__("sqlalchemy").text(
            "DELETE FROM question_fts WHERE question_id IN "
            "(SELECT id FROM question WHERE document_id = :d)"), {"d": doc_id})
    session.flush()

    d = Document(
        id=doc_id,
        title=meta["title"], school=meta["school"], exam_name=meta["exam_name"],
        academic_year_roc=int(meta["academic_year_roc"]),
        semester=meta.get("semester"), exam_seq=meta.get("exam_seq"),
        grade=int(meta["grade"]), subject=meta["subject"],
        sub_subject=meta.get("sub_subject"), scope_note=meta.get("scope_note"),
        publisher=(meta.get("textbook") or {}).get("publisher"),
        source_file=source_file, page_count=meta.get("page_count"),
        total_score=meta.get("total_score"), numbering=meta.get("numbering"),
    )
    session.add(d)

    for s in meta.get("sections") or []:
        session.add(Section(
            document_id=doc_id, ord=s["ord"], name=s["name"], type=s.get("type"),
            score_rule=s.get("score_rule"), per_item_score=s.get("per_item_score"),
            declared_count=s.get("count")))

    # 共用資產（多題共用的圖）與閱讀短文
    for a in doc.get("shared_assets") or []:
        session.add(Asset(
            document_id=doc_id, key=a["key"], label=a.get("label"),
            kind=AssetKind(a.get("kind", "figure")), scope="shared",
            file=a.get("file"), markdown=a.get("markdown"), alt=a.get("alt"),
            bbox=a.get("bbox"), used_by=a.get("used_by")))

    for p in doc.get("passages") or []:
        session.add(Asset(
            document_id=doc_id, key=p["key"], label=p.get("label", "閱讀短文"),
            kind=AssetKind.table, scope="shared", text=p.get("text"),
            used_by=p.get("used_by")))

    session.flush()

    n = 0
    for q in doc.get("questions") or []:
        qid = q["id"]
        answer = q.get("answer")
        question = Question(
            id=qid, document_id=doc_id,
            section_ord=q.get("section", 1), number=q["number"],
            type=QuestionType(q["type"]),
            stem_md=q.get("stem", ""), group_stem=q.get("group_stem"),
            answer=answer,
            answer_status=(AnswerStatus.verified if answer else AnswerStatus.missing),
            answer_source=("source_answer_key" if answer else None),
            explanation_md=q.get("explanation"),
            difficulty=q.get("difficulty"), score=q.get("score"), page=q.get("page"),
            answer_count=q.get("answer_count"),
            shared_asset_key=q.get("shared_asset"),
            status=ReviewStatus.reviewed,      # 黃金樣本已人工確認
            review_note=q.get("review_note"),
            uncertain_spans=q.get("uncertain_spans"),
        )
        session.add(question)
        session.flush()

        # 出處快照到題目上。這裡刻意複製欄位而非只存 document_id ——
        # 題目日後可能被合併、改編，或原文件被修改，出處都必須留在題目自己身上。
        city, short = split_school(d.school)
        session.add(QuestionSource(
            question_id=qid, ord=0, relation="original",
            school=d.school, city=meta.get("city") or city,
            school_short=meta.get("school_short") or short,
            exam_name=d.exam_name,
            academic_year_roc=d.academic_year_roc,
            semester=d.semester, exam_seq=d.exam_seq,
            grade=d.grade, subject=d.subject,
            page=q.get("page"), number_in_paper=q.get("number"),
            document_id=doc_id))

        for i, o in enumerate(q.get("options") or []):
            asset = o.get("asset") or {}
            session.add(Option(
                question_id=qid, label=o["label"],
                content_md=o.get("content") or "",
                asset_file=asset.get("file"), ord=i))

        for a in q.get("assets") or []:
            file = a.get("file") or a.get("source_file")
            session.add(Asset(
                document_id=doc_id, question_id=qid, key=a["key"],
                label=a.get("label"), kind=AssetKind(a.get("kind", "figure")),
                scope="question", file=file,
                markdown=a.get("markdown"), alt=a.get("alt"), bbox=a.get("bbox"),
                pending=bool(a.get("must_crop")) and not file))

        tags = q.get("tags") or {}
        seen: set[tuple[str, str]] = set()
        for axis in ("textbook", "curriculum"):
            raw = tags.get(axis)
            if not raw:
                continue
            # 一格可能寫多個值，用「；」或「, 」分隔
            for v in str(raw).replace("；", ";").replace(",", ";").split(";"):
                v = v.strip()
                if v and (axis, v) not in seen:
                    seen.add((axis, v))
                    session.add(Tag(question_id=qid, axis=axis, value=v,
                                    is_primary=True, labeled_by="human"))
        for v in tags.get("concept") or []:
            if ("concept", v) not in seen:
                seen.add(("concept", v))
                session.add(Tag(question_id=qid, axis="concept", value=v,
                                labeled_by="human"))

        reindex_question(session, qid, searchable_body(q))
        n += 1

    return f"{doc_id}：{n} 題"


def main(argv: list[str]) -> int:
    paths: list[Path] = []
    for arg in argv[1:]:
        p = Path(arg)
        paths.extend(sorted(p.glob("*.y*ml")) if p.is_dir() else [p])

    if not paths:
        print("用法：python -m apps.api.importer <yaml 檔或目錄> ...")
        return 2

    init_db()
    with get_session() as session:
        for path in paths:
            try:
                msg = import_document(session, load_file(path), str(path))
                print(f"  ✅ {path.name}  →  {msg}")
            except Exception as exc:
                session.rollback()
                print(f"  ❌ {path.name}  →  {type(exc).__name__}: {exc}")
                continue
            session.commit()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
