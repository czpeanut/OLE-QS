#!/usr/bin/env python3
"""由 apps/api/models.py 產生 Supabase（PostgreSQL）的建表 SQL。

    python scripts/supabase_schema.py > deploy/supabase/schema.sql

在 Supabase 的 SQL Editor 貼上執行一次。內容：
- 各資料表（與 SQLite 版相同的欄位與約束）
- 中文子字串搜尋：question_search 表 + pg_trgm GIN 索引（取代 SQLite 的 FTS5 trigram）
- 全部資料表開啟 RLS 且不設任何 policy：anon／authenticated 讀不到，
  只有網站後端（service role 或直連資料庫）能存取
- oleqs_reset_sequences()：用 REST 匯入帶 id 的資料後，把自增序號對齊最大 id
"""

from __future__ import annotations

import sys
from pathlib import Path

from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateIndex, CreateTable

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from apps.api.models import Base  # noqa: E402

pg = postgresql.dialect()
out = ["-- 由 scripts/supabase_schema.py 產生，勿手改", "create extension if not exists pg_trgm;", ""]
tables = list(Base.metadata.sorted_tables)
for t in tables:
    out.append(str(CreateTable(t, if_not_exists=True).compile(dialect=pg)).strip() + ";")
    for ix in t.indexes:
        out.append(str(CreateIndex(ix, if_not_exists=True).compile(dialect=pg)).strip() + ";")
    out.append("")

out += [
    "create table if not exists question_search (",
    "    question_id varchar(64) primary key references question(id) on delete cascade,",
    "    body text not null",
    ");",
    "create index if not exists ix_question_search_trgm on question_search using gin (body gin_trgm_ops);",
    "",
]
for name in [t.name for t in tables] + ["question_search"]:
    out.append(f"alter table {name} enable row level security;")
out += ["", "create or replace function oleqs_reset_sequences() returns void language plpgsql as $$",
        "declare r record;", "begin",
        "  for r in select c.relname as tbl, a.attname as col, pg_get_serial_sequence(c.relname, a.attname) as seq",
        "           from pg_class c join pg_attribute a on a.attrelid = c.oid",
        "           where c.relnamespace = 'public'::regnamespace and c.relkind = 'r'",
        "             and pg_get_serial_sequence(c.relname, a.attname) is not null loop",
        "    execute format('select setval(%L, coalesce((select max(%I) from %I), 0) + 1, false)', r.seq, r.col, r.tbl);",
        "  end loop;", "end $$;",
        "revoke execute on function oleqs_reset_sequences() from public, anon, authenticated;", ""]
print("\n".join(out))
