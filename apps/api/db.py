"""資料庫連線與全文檢索索引。

搜尋用 SQLite FTS5 的 trigram 分詞器 —— 中文沒有空白分詞，一般分詞器無法處理，
trigram 把字串切成三字滑動窗，能支援「浮力」「歐姆定律」這種子字串搜尋，
不需要外掛中文斷詞或另外架 Elasticsearch。

之後遷移到 PostgreSQL 時，這一層換成 pg_bigm 或 pgroonga，其餘程式不動。
"""

from __future__ import annotations

import os
from pathlib import Path

from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import Session, sessionmaker

from .models import Base

DB_PATH = Path(os.environ.get("OLEQS_DB", "data/oleqs.db"))
DB_PATH.parent.mkdir(parents=True, exist_ok=True)

engine = create_engine(f"sqlite:///{DB_PATH}", future=True)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False, future=True)


@event.listens_for(engine, "connect")
def _sqlite_pragmas(conn, _):
    cur = conn.cursor()
    cur.execute("PRAGMA foreign_keys=ON")     # SQLite 預設不強制外鍵，必須手動開
    cur.execute("PRAGMA journal_mode=WAL")
    cur.close()


# 題幹與選項合併成一份可搜尋文字。用 external content 會讓刪除同步變複雜，
# 題庫規模在十萬題以內，直接存一份副本更單純。
FTS_DDL = """
CREATE VIRTUAL TABLE IF NOT EXISTS question_fts USING fts5(
    question_id UNINDEXED,
    body,
    tokenize='trigram'
);
"""


def init_db() -> None:
    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        conn.execute(text(FTS_DDL))


def reindex_question(session: Session, question_id: str, body: str) -> None:
    session.execute(text("DELETE FROM question_fts WHERE question_id = :qid"),
                    {"qid": question_id})
    session.execute(
        text("INSERT INTO question_fts (question_id, body) VALUES (:qid, :body)"),
        {"qid": question_id, "body": body})


def search_ids(session: Session, query: str, limit: int = 500) -> list[str]:
    """回傳符合關鍵字的題目 id。

    trigram 需要至少三個字元；不足時退回 LIKE，避免使用者輸入兩個字就搜不到。
    """
    q = (query or "").strip()
    if not q:
        return []
    if len(q) >= 3:
        rows = session.execute(
            text("SELECT question_id FROM question_fts "
                 "WHERE question_fts MATCH :q LIMIT :lim"),
            {"q": f'"{q}"', "lim": limit}).all()
    else:
        rows = session.execute(
            text("SELECT question_id FROM question_fts "
                 "WHERE body LIKE :q LIMIT :lim"),
            {"q": f"%{q}%", "lim": limit}).all()
    return [r[0] for r in rows]


def get_session() -> Session:
    return SessionLocal()
