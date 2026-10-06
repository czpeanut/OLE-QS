"""OLE-QS API 與網頁介面。

啟動：
    python -m apps.api.importer data/samples/expected/     # 先載入題目
    uvicorn apps.api.main:app --reload
    開啟 http://127.0.0.1:8000
"""

from __future__ import annotations

import os
import uuid
from urllib.parse import quote
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, Response
from pydantic import BaseModel, Field
from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session, selectinload

from .db import get_session, init_db, search_ids
from . import taxonomy
from .storage import asset_path
from .export import Item, clean_stem, render, to_pdf
from .mathfmt import render as md
from .models import (Asset, Document, Option, Paper, PaperItem, Question,
                     Section, Tag)

STATIC = Path(__file__).parent / "static"
# 圖檔目錄。擷取管線輸出在這裡，匯出與預覽都從此讀取。
ASSET_ROOT = Path(os.environ.get("OLEQS_ASSETS", "data/assets")).resolve()

app = FastAPI(title="OLE-QS 題庫系統", version="0.2.0")


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
        "stem_html": md(clean_stem(q.stem_md)),
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
        "answer_source": q.answer_source,
        "book": taxonomy.book_name(q.document.grade, q.document.semester) if q.document else None,
        "unit": ({"publisher": q.unit_publisher, "subject": q.unit_subject, "code": q.unit_code,
                  "title": q.unit_title, "chapter": q.unit_chapter} if q.unit_code else None),
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
    sub: str | None = Query(None, description="子科：生物、理化、地科、歷史、地理、公民"),
    semester: int | None = None,
    publisher: str | None = Query(None, description="版本；國文、社會比對單元時要一起看"),
    units: str | None = Query(None, description="單元，逗號分隔的「子科|代號」（/api/units 的 key）"),
    answer: str | None = Query(None, description="verified、ai、disputed、missing，逗號分隔"),
    year: int | None = None,
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
    if semester:
        stmt = stmt.where(Document.semester == semester)
    if year:
        stmt = stmt.where(Document.academic_year_roc == year)
    if sub:
        stmt = stmt.where(Question.unit_subject == sub)
    if units:
        # 歷史、地理、公民（生物、理化、地科）的節次代號會重複，要連同子科比對
        pairs = [k.split("|", 1) for k in units.split(",") if "|" in k]
        stmt = stmt.where(or_(*[and_(Question.unit_subject == sb, Question.unit_code == c)
                                for sb, c in pairs]))
        # 國文、社會各版本課序不同，同一個代號在不同版本是不同的課
        if publisher and subject not in taxonomy.MERGED and subject != "英語":
            stmt = stmt.where(Question.unit_publisher == publisher)
    if answer:
        want = set(answer.split(","))
        conds = []
        if "verified" in want:
            conds.append(Question.answer_status == "verified")
        if "ai" in want:
            conds.append(Question.answer_status == "ai_generated")
        if "disputed" in want:
            conds.append(Question.answer_status == "disputed")
        if "missing" in want:
            conds.append(Question.answer_status == "missing")
        if conds:
            stmt = stmt.where(or_(*conds))
    if document_id:
        stmt = stmt.where(Question.document_id == document_id)
    if type:
        stmt = stmt.where(Question.type.in_(type.split(",")))
    if difficulty_min:
        stmt = stmt.where(Question.difficulty >= difficulty_min)
    if difficulty_max:
        stmt = stmt.where(Question.difficulty <= difficulty_max)
    if has_answer is not None:
        # 用 answer_status 而非 answer 欄位：SQLAlchemy 的 JSON 欄位把 Python None
        # 存成 JSON null 而不是 SQL NULL，is_(None) 因此永遠不成立。
        stmt = (stmt.where(Question.answer_status != "missing") if has_answer
                else stmt.where(Question.answer_status == "missing"))
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
        stmt.order_by(Question.sort_key, Question.document_id, Question.section_ord, Question.number)
        .limit(limit).offset(offset)).scalars().all()

    return {"total": total, "items": [question_json(r) for r in rows]}


@app.get("/api/taxonomy")
def get_taxonomy() -> dict:
    return taxonomy.tree()


@app.get("/api/units")
def get_units(subject: str, grade: int, semester: int, sub: str | None = None,
              publisher: str | None = None, session: Session = Depends(db)) -> list[dict]:
    """某冊的單元列表與各單元題數（只算收錄的題目）。"""
    us = taxonomy.units(subject, sub, grade, semester, publisher)
    stmt = (select(Question.unit_subject, Question.unit_code, func.count())
            .join(Document, Question.document_id == Document.id)
            .where(Document.subject == subject, Document.grade == grade,
                   Document.semester == semester, Question.status != "rejected"))
    if sub:
        stmt = stmt.where(Question.unit_subject == sub)
    if publisher and subject not in taxonomy.MERGED and subject != "英語":
        stmt = stmt.where(Question.unit_publisher == publisher)
    counts = {(sb, c): n for sb, c, n in
              session.execute(stmt.group_by(Question.unit_subject, Question.unit_code)).all()}
    return [{**u, "key": f"{u['subject']}|{u['code']}", "count": counts.get((u["subject"], u["code"]), 0)}
            for u in us]


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
    settings: dict | None = None
    question_ids: list[str] = []
    items: list["PaperItemIn"] | None = None


class PaperItemIn(BaseModel):
    question_id: str
    score: float | None = None
    section_name: str | None = None


class PaperUpdate(BaseModel):
    title: str | None = None
    notes: str | None = None
    settings: dict | None = None
    items: list[PaperItemIn] | None = None


PaperIn.model_rebuild()


class RenderIn(BaseModel):
    """不存檔直接排版：預覽與下載都走這裡，題籃只存在瀏覽器。"""
    title: str = Field("試卷", max_length=200)
    mode: str = Field("exam", pattern="^(exam|answer|key)$")
    settings: dict | None = None
    items: list[PaperItemIn]


def paper_json(p: Paper) -> dict:
    return {
        "id": p.id, "title": p.title, "subject": p.subject, "notes": p.notes,
        "settings": p.settings or {}, "total_score": p.total_score,
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
              subject=body.subject, notes=body.notes, settings=body.settings)
    session.add(p)
    session.flush()
    _replace_items(session, p, body.items if body.items is not None else
                   [PaperItemIn(question_id=qid) for qid in body.question_ids])
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
    if body.settings is not None:
        p.settings = body.settings
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


def _items(session: Session, items: list[PaperItemIn]) -> list[Item]:
    qs = {q.id: q for q in session.execute(
        select(Question).where(Question.id.in_([i.question_id for i in items]))
        .options(selectinload(Question.options), selectinload(Question.assets),
                 selectinload(Question.sources),
                 selectinload(Question.document).selectinload(Document.assets))).scalars()}
    missing = [i.question_id for i in items if i.question_id not in qs]
    if missing:
        raise HTTPException(400, f"題目不存在：{missing[:5]}")
    return [Item(qs[i.question_id], i.score if i.score is not None else qs[i.question_id].score,
                 i.section_name) for i in items]


def _output(html_text: str, fmt: str, title: str, request: Request) -> Response:
    if fmt == "pdf":
        # 交給 headless Chromium 前補上 <base>，圖檔的相對路徑才解析得到
        base = str(request.base_url)
        pdf = to_pdf(html_text.replace("<head>", f'<head><base href="{base}">', 1))
        return Response(pdf, media_type="application/pdf", headers={
            "Content-Disposition": f"attachment; filename*=UTF-8''{quote(title)}.pdf"})
    return HTMLResponse(html_text)


@app.post("/api/render")
def render_adhoc(body: RenderIn, request: Request, format: str = Query("html", pattern="^(html|pdf)$"),
                 session: Session = Depends(db)) -> Response:
    if not body.items:
        raise HTTPException(400, "沒有選任何題目")
    html_text = render(body.title, _items(session, body.items), body.mode, body.settings, ASSET_ROOT)
    return _output(html_text, format, body.title, request)


@app.get("/api/papers/{pid}/export")
def export_paper(pid: str, request: Request,
                 mode: str = Query("exam", pattern="^(exam|answer|key)$"),
                 format: str = Query("html", pattern="^(html|pdf)$"),
                 session: Session = Depends(db)) -> Response:
    p = session.get(Paper, pid)
    if not p:
        raise HTTPException(404, "找不到考卷")
    items = [Item(i.question, i.score, i.section_name) for i in p.items]
    return _output(render(p.title, items, mode, p.settings, ASSET_ROOT), format, p.title, request)


# ─────────────────────────── 靜態資源 ───────────────────────────

@app.get("/assets/{path:path}")
def asset(path: str) -> FileResponse:
    # asset_path 會擋目錄穿越；本機沒有時從 Supabase Storage 取回並快取
    target = asset_path(path)
    if not target:
        raise HTTPException(404, "找不到檔案")
    return FileResponse(target, media_type="image/png", headers={"Cache-Control": "public, max-age=604800"})


@app.get("/", response_class=HTMLResponse)
def index() -> HTMLResponse:
    return HTMLResponse((STATIC / "index.html").read_text(encoding="utf-8"))
