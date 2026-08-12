"""OLE-QS API 與網頁介面。

啟動：
    python -m apps.api.importer data/samples/expected/     # 先載入題目
    uvicorn apps.api.main:app --reload
    開啟 http://127.0.0.1:8000
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from .db import get_session, init_db, search_ids
from .export import render_paper
from .mathfmt import render as md
from .models import (Asset, Document, Option, Paper, PaperItem, Question,
                     Section, Tag)

STATIC = Path(__file__).parent / "static"
# 圖檔目錄。擷取管線輸出在這裡，匯出與預覽都從此讀取。
ASSET_ROOT = Path(os.environ.get("OLEQS_ASSETS", "data/assets")).resolve()

app = FastAPI(title="OLE-QS 題庫系統", version="0.1.0")


@app.on_event("startup")
def _startup() -> None:
    init_db()
    ASSET_ROOT.mkdir(parents=True, exist_ok=True)


def db() -> Session:
    session = get_session()
    try:
        yield session
    finally:
        session.close()


# ─────────────────────────── 序列化 ───────────────────────────

def question_json(q: Question, *, with_answer: bool = True) -> dict:
    shared = None
    if q.shared_asset_key:
        a = next((x for x in q.document.assets if x.key == q.shared_asset_key), None)
        if a:
            shared = {"key": a.key, "label": a.label, "kind": a.kind.value,
                      "file": a.file, "text": a.text, "alt": a.alt}
    return {
        "id": q.id,
        "document_id": q.document_id,
        "section_ord": q.section_ord,
        "number": q.number,
        "type": q.type.value,
        "stem": q.stem_md,
        # 伺服器端渲染 Markdown+LaTeX，前端不重複實作一套
        "stem_html": md(q.stem_md),
        "group_stem": q.group_stem,
        "group_stem_html": md(q.group_stem),
        "options": [{"label": o.label, "content": o.content_md,
                     "content_html": md(o.content_md),
                     "asset_file": o.asset_file} for o in q.options],
        "assets": [{"key": a.key, "label": a.label, "kind": a.kind.value,
                    "file": a.file, "markdown": a.markdown, "alt": a.alt,
                    "pending": a.pending} for a in q.assets],
        "shared_asset": shared,
        "answer": q.answer if with_answer else None,
        "answer_status": q.answer_status.value,
        "explanation": q.explanation_md if with_answer else None,
        "difficulty": q.difficulty,
        "score": q.score,
        "status": q.status.value,
        "review_note": q.review_note,
        "tags": {axis: [t.value for t in q.tags if t.axis == axis]
                 for axis in ("textbook", "curriculum", "concept")},
        # 出處來自題目自身的快照，不是它所屬的文件 —— 一題可有多個出處
        "citation": q.citation,
        "sources": [{
            "citation": s.citation, "relation": s.relation,
            "school": s.school, "exam_name": s.exam_name,
            "year": s.academic_year_roc, "grade": s.grade, "subject": s.subject,
            "number_in_paper": s.number_in_paper, "note": s.note,
        } for s in q.sources],
    }


# ─────────────────────────── 檢索 ───────────────────────────

@app.get("/api/facets")
def facets(session: Session = Depends(db)) -> dict:
    """篩選面板的可選值，直接由現有資料統計得出。"""
    def counts(col, *joins):
        stmt = select(col, func.count()).join(*joins) if joins else \
            select(col, func.count())
        return [{"value": v, "count": c}
                for v, c in session.execute(stmt.group_by(col).order_by(col)).all()
                if v is not None]

    subjects = session.execute(
        select(Document.subject, func.count(Question.id))
        .join(Question, Question.document_id == Document.id)
        .group_by(Document.subject)).all()
    grades = session.execute(
        select(Document.grade, func.count(Question.id))
        .join(Question, Question.document_id == Document.id)
        .group_by(Document.grade)).all()
    types = session.execute(
        select(Question.type, func.count()).group_by(Question.type)).all()
    diffs = session.execute(
        select(Question.difficulty, func.count())
        .where(Question.difficulty.is_not(None))
        .group_by(Question.difficulty).order_by(Question.difficulty)).all()
    chapters = session.execute(
        select(Tag.value, func.count())
        .where(Tag.axis == "textbook")
        .group_by(Tag.value).order_by(func.count().desc())).all()
    docs = session.execute(select(Document).order_by(Document.created_at)).scalars().all()

    return {
        "subjects": [{"value": v, "count": c} for v, c in subjects],
        "grades": [{"value": v, "count": c} for v, c in grades],
        "types": [{"value": t.value, "count": c} for t, c in types],
        "difficulties": [{"value": v, "count": c} for v, c in diffs],
        "chapters": [{"value": v, "count": c} for v, c in chapters],
        "documents": [{"id": d.id, "title": d.title, "subject": d.subject,
                       "grade": d.grade, "citation": d.citation,
                       "scope_note": d.scope_note} for d in docs],
    }


@app.get("/api/questions")
def list_questions(
    q: str | None = Query(None, description="關鍵字（中文子字串）"),
    subject: str | None = None,
    grade: int | None = None,
    type: str | None = None,
    chapter: str | None = None,
    document_id: str | None = None,
    difficulty_min: int | None = None,
    difficulty_max: int | None = None,
    has_answer: bool | None = None,
    has_figure: bool | None = None,
    include_rejected: bool = Query(False, description="含被品管剔除的題目"),
    limit: int = Query(50, le=200),
    offset: int = 0,
    session: Session = Depends(db),
) -> dict:
    stmt = (select(Question)
            .join(Document, Question.document_id == Document.id)
            .options(selectinload(Question.options),
                     selectinload(Question.assets),
                     selectinload(Question.tags),
                     selectinload(Question.sources),
                     selectinload(Question.document).selectinload(Document.assets)))

    # 被品管閘門剔除的題目預設不出現在檢索與組卷
    if not include_rejected:
        stmt = stmt.where(Question.status != "rejected")
    if subject:
        stmt = stmt.where(Document.subject == subject)
    if grade:
        stmt = stmt.where(Document.grade == grade)
    if document_id:
        stmt = stmt.where(Question.document_id == document_id)
    if type:
        stmt = stmt.where(Question.type == type)
    if difficulty_min:
        stmt = stmt.where(Question.difficulty >= difficulty_min)
    if difficulty_max:
        stmt = stmt.where(Question.difficulty <= difficulty_max)
    if has_answer is not None:
        stmt = (stmt.where(Question.answer.is_not(None)) if has_answer
                else stmt.where(Question.answer.is_(None)))
    if chapter:
        stmt = stmt.where(Question.id.in_(
            select(Tag.question_id).where(Tag.axis == "textbook",
                                          Tag.value.like(f"%{chapter}%"))))
    if has_figure is not None:
        sub = select(Asset.question_id).where(Asset.question_id.is_not(None))
        stmt = (stmt.where(Question.id.in_(sub)) if has_figure
                else stmt.where(Question.id.not_in(sub)))
    if q:
        ids = search_ids(session, q)
        if not ids:
            return {"total": 0, "items": []}
        stmt = stmt.where(Question.id.in_(ids))

    total = session.execute(
        select(func.count()).select_from(stmt.subquery())).scalar_one()
    rows = session.execute(
        stmt.order_by(Question.document_id, Question.section_ord, Question.number)
        .limit(limit).offset(offset)).scalars().all()

    return {"total": total, "items": [question_json(r) for r in rows]}


@app.get("/api/questions/{qid}")
def get_question(qid: str, session: Session = Depends(db)) -> dict:
    q = session.get(Question, qid)
    if not q:
        raise HTTPException(404, "找不到題目")
    return question_json(q)


# ─────────────────────────── 組卷 ───────────────────────────

class PaperIn(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    subject: str | None = None
    notes: str | None = None
    question_ids: list[str] = []


class PaperItemIn(BaseModel):
    question_id: str
    score: float | None = None
    section_name: str | None = None


class PaperUpdate(BaseModel):
    title: str | None = None
    notes: str | None = None
    items: list[PaperItemIn] | None = None


def paper_json(p: Paper) -> dict:
    return {
        "id": p.id, "title": p.title, "subject": p.subject, "notes": p.notes,
        "total_score": p.total_score,
        "items": [{"ord": i.ord, "score": i.score, "section_name": i.section_name,
                   "question": question_json(i.question)} for i in p.items],
    }


@app.get("/api/papers")
def list_papers(session: Session = Depends(db)) -> list[dict]:
    papers = session.execute(
        select(Paper).order_by(Paper.updated_at.desc())).scalars().all()
    return [{"id": p.id, "title": p.title, "subject": p.subject,
             "count": len(p.items), "total_score": p.total_score,
             "updated_at": p.updated_at.isoformat()} for p in papers]


@app.post("/api/papers", status_code=201)
def create_paper(body: PaperIn, session: Session = Depends(db)) -> dict:
    p = Paper(id=f"paper_{uuid.uuid4().hex[:12]}", title=body.title,
              subject=body.subject, notes=body.notes)
    session.add(p)
    session.flush()
    _replace_items(session, p, [PaperItemIn(question_id=qid)
                                for qid in body.question_ids])
    session.commit()
    session.refresh(p)
    return paper_json(p)


def _replace_items(session: Session, paper: Paper, items: list[PaperItemIn]) -> None:
    """整批取代考卷內容。

    透過 paper.items 這個 relationship 操作，而不是各自 session.add ——
    直接 add 會讓 ORM 的集合停留在舊狀態，接著序列化就會回傳更新前的內容
    （順序、配分、分節全部是舊的），而且不會報錯。
    """
    seen: set[str] = set()
    rows: list[PaperItem] = []
    for it in items:
        if it.question_id in seen:
            raise HTTPException(400, f"題目重複：{it.question_id}")
        seen.add(it.question_id)
        q = session.get(Question, it.question_id)
        if not q:
            raise HTTPException(400, f"題目不存在：{it.question_id}")
        rows.append(PaperItem(
            paper_id=paper.id, question_id=q.id, ord=len(rows),
            score=it.score if it.score is not None else q.score,
            section_name=it.section_name))

    paper.items.clear()          # cascade delete-orphan 會刪掉舊的
    session.flush()
    paper.items.extend(rows)
    session.flush()


@app.get("/api/papers/{pid}")
def get_paper(pid: str, session: Session = Depends(db)) -> dict:
    p = session.get(Paper, pid)
    if not p:
        raise HTTPException(404, "找不到考卷")
    return paper_json(p)


@app.patch("/api/papers/{pid}")
def update_paper(pid: str, body: PaperUpdate, session: Session = Depends(db)) -> dict:
    p = session.get(Paper, pid)
    if not p:
        raise HTTPException(404, "找不到考卷")
    if body.title is not None:
        p.title = body.title
    if body.notes is not None:
        p.notes = body.notes
    if body.items is not None:
        _replace_items(session, p, body.items)
    session.commit()
    session.refresh(p)
    return paper_json(p)


@app.delete("/api/papers/{pid}", status_code=204)
def delete_paper(pid: str, session: Session = Depends(db)) -> None:
    p = session.get(Paper, pid)
    if p:
        session.delete(p)
        session.commit()


@app.get("/api/papers/{pid}/export", response_class=HTMLResponse)
def export_paper(pid: str, mode: str = Query("exam", pattern="^(exam|answer|key)$"),
                 session: Session = Depends(db)) -> HTMLResponse:
    p = session.get(Paper, pid)
    if not p:
        raise HTTPException(404, "找不到考卷")
    return HTMLResponse(render_paper(p, mode))


# ─────────────────────────── 靜態資源 ───────────────────────────

@app.get("/assets/{path:path}")
def asset(path: str) -> FileResponse:
    # 防目錄穿越：解析後必須仍在資產根目錄底下
    target = (ASSET_ROOT / path).resolve()
    if not target.is_file() or ASSET_ROOT not in target.parents:
        raise HTTPException(404, "找不到檔案")
    return FileResponse(target)


@app.get("/", response_class=HTMLResponse)
def index() -> HTMLResponse:
    return HTMLResponse((STATIC / "index.html").read_text(encoding="utf-8"))
