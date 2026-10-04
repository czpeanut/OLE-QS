// 從 Supabase 查題目，整理成網頁與排版共用的形狀（欄位與 apps/api/main.py 的 question_json 相同）。
import { db } from "./supabase";
import { bookName, MERGED, patternName } from "./taxonomy";
import { cleanStem, render as md } from "./mathfmt";

const SELECT = [
  "id,document_id,section_ord,number,type,stem_md,group_stem,answer,answer_status,answer_source,explanation_md,score,status,shared_asset_key,difficulty",
  "ptag:tag!question_id(axis,value)",
  "unit_publisher,unit_subject,unit_code,unit_title,unit_chapter",
  "document!document_id!inner(id,subject,grade,semester,academic_year_roc)",
  "option(label,content_md,asset_file,ord)",
  "asset!question_id(key,label,kind,file,markdown,alt)",
  "question_source(city,school_short,school,academic_year_roc,relation,ord)",
].join(",");

export interface Asset { key: string; label: string | null; kind: string; file: string | null; markdown: string | null; alt: string | null; text?: string | null }
export interface Q {
  id: string; document_id: string; type: string; number: number; stem: string; stem_html: string;
  group_stem: string | null; group_stem_html: string; answer: string[] | null; answer_status: string; answer_source: string | null;
  explanation: string | null; score: number | null; shared_asset_key: string | null; shared_asset: Asset | null;
  options: { label: string; content: string; content_html: string; asset_file: string | null }[];
  assets: Asset[]; citation: string; book: string; subject: string;
  unit: { publisher: string | null; subject: string | null; code: string; title: string | null; chapter: string | null } | null;
  difficulty: number | null; pattern: { key: string; name: string } | null;
}

// eslint-disable-next-line
type Row = Record<string, any>;

function citation(sources: Row[]): string {
  const mark: Record<string, string> = { adapted: "改編 ", duplicate: "另見 " };
  return [...sources].sort((a, b) => a.ord - b.ord)
    .map((s) => `[${mark[s.relation] ?? ""}${[s.city, s.school_short || s.school, s.academic_year_roc].filter(Boolean).join(" ")}]`)
    .join(" ") || "（來源未標註）";
}

async function shape(rows: Row[]): Promise<Q[]> {
  // 共用素材（閱讀短文、共用圖）掛在文件上，另外查
  const wants = rows.filter((r) => r.shared_asset_key);
  const shared = new Map<string, Asset>();
  if (wants.length) {
    const { data } = await db().from("asset").select("document_id,key,label,kind,file,markdown,alt,text")
      .in("document_id", [...new Set(wants.map((r) => r.document_id))])
      .in("key", [...new Set(wants.map((r) => r.shared_asset_key))]).is("question_id", null);
    for (const a of data ?? []) shared.set(`${a.document_id}|${a.key}`, a as Asset);
  }
  return rows.map((r) => {
    const d = r.document;
    return {
      id: r.id, document_id: r.document_id, type: r.type, number: r.number,
      stem: r.stem_md, stem_html: md(cleanStem(r.stem_md)),
      group_stem: r.group_stem, group_stem_html: md(r.group_stem),
      answer: r.answer, answer_status: r.answer_status, answer_source: r.answer_source,
      explanation: r.explanation_md, score: r.score, shared_asset_key: r.shared_asset_key,
      shared_asset: r.shared_asset_key ? shared.get(`${r.document_id}|${r.shared_asset_key}`) ?? null : null,
      options: [...(r.option ?? [])].sort((a: Row, b: Row) => a.ord - b.ord)
        .map((o: Row) => ({ label: o.label, content: o.content_md, content_html: md(o.content_md), asset_file: o.asset_file })),
      assets: [...(r.asset ?? [])].sort((a: Row, b: Row) => (a.key < b.key ? -1 : 1)),
      citation: citation(r.question_source ?? []), book: bookName(d.grade, d.semester), subject: d.subject,
      unit: r.unit_code ? { publisher: r.unit_publisher, subject: r.unit_subject, code: r.unit_code, title: r.unit_title, chapter: r.unit_chapter } : null,
      difficulty: r.difficulty ?? null, pattern: pattern(r.ptag),
    };
  });
}

function pattern(tags: Row[] | null): { key: string; name: string } | null {
  const t = (tags ?? []).find((x) => x.axis === "pattern");
  const name = t ? patternName(t.value) : null;
  return t && name ? { key: t.value, name } : null;
}

const quote = (v: string) => `"${v.replace(/"/g, '\\"')}"`;

export interface Filters {
  subject?: string; sub?: string; grade?: number; semester?: number; year?: number; publisher?: string;
  units?: string[]; types?: string[]; answer?: string[]; hasFigure?: boolean | null; q?: string;
  patterns?: string[]; difficulty?: number[];
  limit: number; offset: number;
}

export async function search(f: Filters): Promise<{ total: number; items: Q[] }> {
  let select = SELECT;
  if (f.q) select += ",question_search!inner(body)";
  if (f.hasFigure === true) select += ",fig:asset!question_id!inner(id)";
  if (f.hasFigure === false) select += ",nofig:asset!question_id(id)";
  if (f.patterns?.length) select += ",pf:tag!question_id!inner(value)";
  let qb = db().from("question").select(select, { count: "exact" }).neq("status", "rejected");
  if (f.subject) qb = qb.eq("document.subject", f.subject);
  if (f.grade) qb = qb.eq("document.grade", f.grade);
  if (f.semester) qb = qb.eq("document.semester", f.semester);
  if (f.year) qb = qb.eq("document.academic_year_roc", f.year);
  if (f.sub) qb = qb.eq("unit_subject", f.sub);
  if (f.units?.length) {
    // 歷史、地理、公民的節次代號會重複，要連同子科比對
    qb = qb.or(f.units.map((k) => { const [s, c] = k.split("|"); return `and(unit_subject.eq.${quote(s)},unit_code.eq.${quote(c)})`; }).join(","));
    if (f.publisher && f.subject && !MERGED.has(f.subject) && f.subject !== "英語") qb = qb.eq("unit_publisher", f.publisher);
  }
  if (f.types?.length) qb = qb.in("type", f.types);
  if (f.patterns?.length) qb = qb.in("pf.value", f.patterns);
  if (f.difficulty?.length) qb = qb.in("difficulty", f.difficulty);
  if (f.answer?.length) qb = qb.in("answer_status", f.answer.map((a) => (a === "ai" ? "ai_generated" : a)));
  if (f.hasFigure === false) qb = qb.is("nofig", null);
  if (f.q) qb = qb.ilike("question_search.body", `%${f.q.replace(/[%_\\]/g, (c) => "\\" + c)}%`);
  const { data, count, error } = await qb.order("document_id").order("section_ord").order("number")
    .range(f.offset, f.offset + f.limit - 1);
  if (error) throw new Error(error.message);
  return { total: count ?? 0, items: await shape((data ?? []) as Row[]) };
}

export async function byIds(ids: string[]): Promise<Map<string, Q>> {
  const out = new Map<string, Q>();
  for (let i = 0; i < ids.length; i += 100) {
    const { data, error } = await db().from("question").select(SELECT).in("id", ids.slice(i, i + 100));
    if (error) throw new Error(error.message);
    for (const q of await shape((data ?? []) as Row[])) out.set(q.id, q);
  }
  return out;
}
