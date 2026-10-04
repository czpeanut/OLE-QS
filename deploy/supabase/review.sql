-- 審題台的共用紀錄（回報、審完、邊緣調整、審題者名字），取代 claude.ai 頁面內建的資料庫
create table if not exists review_doc (site varchar(10) not null, collection varchar(20) not null, id varchar(200) not null, data jsonb not null, updated_at timestamptz not null default now(), primary key (site, collection, id));
alter table review_doc enable row level security;
