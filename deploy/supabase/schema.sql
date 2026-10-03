-- 由 scripts/supabase_schema.py 產生，勿手改
create extension if not exists pg_trgm;

CREATE TABLE IF NOT EXISTS document (
    id VARCHAR(64) NOT NULL,
    title VARCHAR(300) NOT NULL,
    school VARCHAR(120) NOT NULL,
    exam_name VARCHAR(200) NOT NULL,
    academic_year_roc INTEGER NOT NULL,
    semester INTEGER,
    exam_seq INTEGER,
    grade INTEGER NOT NULL,
    subject VARCHAR(40) NOT NULL,
    sub_subject VARCHAR(40),
    scope_note VARCHAR(300),
    publisher VARCHAR(40),
    source_file VARCHAR(500),
    page_count INTEGER,
    total_score INTEGER,
    numbering VARCHAR(20),
    created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
    PRIMARY KEY (id),
    CONSTRAINT ck_document_school_required CHECK (length(trim(school)) > 0),
    CONSTRAINT ck_document_exam_required CHECK (length(trim(exam_name)) > 0)
);
CREATE INDEX IF NOT EXISTS ix_document_subject ON document (subject);

CREATE TABLE IF NOT EXISTS paper (
    id VARCHAR(64) NOT NULL,
    title VARCHAR(200) NOT NULL,
    subject VARCHAR(40),
    owner VARCHAR(80),
    notes TEXT,
    settings JSON,
    created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
    updated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
    PRIMARY KEY (id)
);

CREATE TABLE IF NOT EXISTS question (
    id VARCHAR(64) NOT NULL,
    document_id VARCHAR(64),
    section_ord INTEGER NOT NULL,
    number INTEGER NOT NULL,
    type VARCHAR(20) NOT NULL,
    stem_md TEXT NOT NULL,
    stem_raw TEXT,
    group_stem TEXT,
    answer JSON,
    answer_status VARCHAR(20) NOT NULL,
    answer_source VARCHAR(80),
    explanation_md TEXT,
    difficulty INTEGER,
    score FLOAT,
    page INTEGER,
    answer_count INTEGER,
    parent_id VARCHAR(64),
    shared_asset_key VARCHAR(40),
    unit_publisher VARCHAR(10),
    unit_subject VARCHAR(10),
    unit_code VARCHAR(20),
    unit_title VARCHAR(200),
    unit_chapter VARCHAR(200),
    status VARCHAR(20) NOT NULL,
    review_note TEXT,
    uncertain_spans JSON,
    created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
    PRIMARY KEY (id),
    CONSTRAINT uq_question_position UNIQUE (document_id, section_ord, number),
    CONSTRAINT ck_question_difficulty_range CHECK (difficulty IS NULL OR (difficulty BETWEEN 1 AND 5)),
    FOREIGN KEY(document_id) REFERENCES document (id) ON DELETE SET NULL,
    FOREIGN KEY(parent_id) REFERENCES question (id)
);
CREATE INDEX IF NOT EXISTS ix_question_document_id ON question (document_id);
CREATE INDEX IF NOT EXISTS ix_question_answer_status ON question (answer_status);
CREATE INDEX IF NOT EXISTS ix_question_status ON question (status);
CREATE INDEX IF NOT EXISTS ix_question_difficulty ON question (difficulty);
CREATE INDEX IF NOT EXISTS ix_question_unit_code ON question (unit_code);
CREATE INDEX IF NOT EXISTS ix_question_unit_subject ON question (unit_subject);
CREATE INDEX IF NOT EXISTS ix_question_type ON question (type);

CREATE TABLE IF NOT EXISTS section (
    id SERIAL NOT NULL,
    document_id VARCHAR(64) NOT NULL,
    ord INTEGER NOT NULL,
    name VARCHAR(300) NOT NULL,
    type VARCHAR(20),
    score_rule VARCHAR(200),
    per_item_score FLOAT,
    declared_count INTEGER,
    PRIMARY KEY (id),
    CONSTRAINT uq_section_doc_ord UNIQUE (document_id, ord),
    FOREIGN KEY(document_id) REFERENCES document (id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS ix_section_document_id ON section (document_id);

CREATE TABLE IF NOT EXISTS asset (
    id SERIAL NOT NULL,
    document_id VARCHAR(64) NOT NULL,
    question_id VARCHAR(64),
    key VARCHAR(40) NOT NULL,
    label VARCHAR(40),
    kind VARCHAR(20) NOT NULL,
    scope VARCHAR(10) NOT NULL,
    file VARCHAR(300),
    markdown TEXT,
    text TEXT,
    alt TEXT,
    bbox JSON,
    used_by JSON,
    pending BOOLEAN NOT NULL,
    PRIMARY KEY (id),
    CONSTRAINT uq_asset_key UNIQUE (document_id, key),
    CONSTRAINT ck_asset_has_payload CHECK (file IS NOT NULL OR markdown IS NOT NULL OR text IS NOT NULL OR pending),
    FOREIGN KEY(document_id) REFERENCES document (id) ON DELETE CASCADE,
    FOREIGN KEY(question_id) REFERENCES question (id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS ix_asset_document_id ON asset (document_id);
CREATE INDEX IF NOT EXISTS ix_asset_question_id ON asset (question_id);

CREATE TABLE IF NOT EXISTS option (
    id SERIAL NOT NULL,
    question_id VARCHAR(64) NOT NULL,
    label VARCHAR(4) NOT NULL,
    content_md TEXT NOT NULL,
    asset_file VARCHAR(300),
    ord INTEGER NOT NULL,
    PRIMARY KEY (id),
    CONSTRAINT uq_option_label UNIQUE (question_id, label),
    CONSTRAINT ck_option_has_content CHECK (length(trim(content_md)) > 0 OR asset_file IS NOT NULL),
    FOREIGN KEY(question_id) REFERENCES question (id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS ix_option_question_id ON option (question_id);

CREATE TABLE IF NOT EXISTS paper_item (
    id SERIAL NOT NULL,
    paper_id VARCHAR(64) NOT NULL,
    question_id VARCHAR(64) NOT NULL,
    ord INTEGER NOT NULL,
    score FLOAT,
    section_name VARCHAR(80),
    PRIMARY KEY (id),
    CONSTRAINT uq_paper_question UNIQUE (paper_id, question_id),
    FOREIGN KEY(paper_id) REFERENCES paper (id) ON DELETE CASCADE,
    FOREIGN KEY(question_id) REFERENCES question (id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS ix_paper_item_paper_id ON paper_item (paper_id);

CREATE TABLE IF NOT EXISTS question_source (
    id SERIAL NOT NULL,
    question_id VARCHAR(64) NOT NULL,
    ord INTEGER NOT NULL,
    school VARCHAR(120) NOT NULL,
    city VARCHAR(20),
    school_short VARCHAR(40),
    exam_name VARCHAR(200) NOT NULL,
    academic_year_roc INTEGER NOT NULL,
    semester INTEGER,
    exam_seq INTEGER,
    grade INTEGER NOT NULL,
    subject VARCHAR(40) NOT NULL,
    page INTEGER,
    number_in_paper INTEGER,
    relation VARCHAR(20) NOT NULL,
    note VARCHAR(300),
    document_id VARCHAR(64),
    PRIMARY KEY (id),
    CONSTRAINT ck_qsource_school CHECK (length(trim(school)) > 0),
    CONSTRAINT ck_qsource_exam CHECK (length(trim(exam_name)) > 0),
    FOREIGN KEY(question_id) REFERENCES question (id) ON DELETE CASCADE,
    FOREIGN KEY(document_id) REFERENCES document (id) ON DELETE SET NULL
);
CREATE INDEX IF NOT EXISTS ix_question_source_question_id ON question_source (question_id);

CREATE TABLE IF NOT EXISTS tag (
    id SERIAL NOT NULL,
    question_id VARCHAR(64) NOT NULL,
    axis VARCHAR(20) NOT NULL,
    value VARCHAR(200) NOT NULL,
    is_primary BOOLEAN NOT NULL,
    confidence FLOAT,
    labeled_by VARCHAR(20),
    PRIMARY KEY (id),
    CONSTRAINT uq_tag_unique UNIQUE (question_id, axis, value),
    FOREIGN KEY(question_id) REFERENCES question (id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS ix_tag_axis ON tag (axis);
CREATE INDEX IF NOT EXISTS ix_tag_value ON tag (value);
CREATE INDEX IF NOT EXISTS ix_tag_question_id ON tag (question_id);

create table if not exists question_search (
    question_id varchar(64) primary key references question(id) on delete cascade,
    body text not null
);
create index if not exists ix_question_search_trgm on question_search using gin (body gin_trgm_ops);

alter table document enable row level security;
alter table paper enable row level security;
alter table question enable row level security;
alter table section enable row level security;
alter table asset enable row level security;
alter table option enable row level security;
alter table paper_item enable row level security;
alter table question_source enable row level security;
alter table tag enable row level security;
alter table question_search enable row level security;

create or replace function oleqs_reset_sequences() returns void language plpgsql as $$
declare r record;
begin
  for r in select c.relname as tbl, a.attname as col, pg_get_serial_sequence(c.relname, a.attname) as seq
           from pg_class c join pg_attribute a on a.attrelid = c.oid
           where c.relnamespace = 'public'::regnamespace and c.relkind = 'r'
             and pg_get_serial_sequence(c.relname, a.attname) is not null loop
    execute format('select setval(%L, coalesce((select max(%I) from %I), 0) + 1, false)', r.seq, r.col, r.tbl);
  end loop;
end $$;
revoke execute on function oleqs_reset_sequences() from public, anon, authenticated;
