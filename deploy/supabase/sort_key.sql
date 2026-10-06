-- 選題頁的排序位置（scripts/build_sort_keys.py 算好寫入）
alter table question add column if not exists sort_key integer;
create index if not exists ix_question_sort_key on question (sort_key);
