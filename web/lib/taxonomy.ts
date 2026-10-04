// 選題篩選用的分類樹：科目 → 子科 → 冊別 → 版本 → 單元（與 apps/api/taxonomy.py 同規則）。
import data from "./data/curriculum.json";
import counts from "./data/counts.json";
import patternData from "./data/patterns.json";

export const SUBJECTS = ["國文", "英語", "數學", "自然", "社會"];
export const SUBS: Record<string, string[]> = { 自然: ["生物", "理化", "地科"], 社會: ["歷史", "地理", "公民"] };
export const PUBLISHERS = ["翰林", "康軒", "南一"];
export const BOOKS: [number, number, string][] = [[7, 1, "七上"], [7, 2, "七下"], [8, 1, "八上"], [8, 2, "八下"], [9, 1, "九上"], [9, 2, "九下"]];
// 數學、自然各版本節次對齊，篩選只比代號；國文、社會要連同版本比對；英語是跨版本主題
export const MERGED = new Set(["數學", "自然"]);

export function bookName(grade: number, semester: number | null): string {
  return BOOKS.find(([g, s]) => g === grade && s === semester)?.[2] ?? `${grade}年級`;
}

export interface Pattern { key: string; code: string; name: string; desc: string; n: number }
export interface Unit { code: string; title: string; chapter: string | null; subject: string; lessons?: Record<string, string>; key: string; count: number; patterns?: Pattern[] }

// 數學題型目錄（scripts/math_patterns.py）：單元鍵「年級-學期_代號」→ 題型；題目的題型標籤是「單元鍵:題型代號」
type PatternUnit = { title: string; types: { code: string; name: string; desc: string; n: number }[] };
const PATTERNS = patternData as Record<string, PatternUnit>;
export function unitPatterns(grade: number, semester: number, code: string): Pattern[] {
  const k = `${grade}-${semester}_${code}`;
  return (PATTERNS[k]?.types ?? []).map((t) => ({ key: `${k}:${t.code}`, ...t }));
}
export function patternName(key: string): string | null {
  const [k, code] = key.split(":");
  return PATTERNS[k]?.types.find((t) => t.code === code)?.name ?? null;
}

type Raw = Record<string, unknown>;
const junior = (data as { junior: Raw }).junior as Record<string, Record<string, Record<string, Record<string, Record<string, Raw[]>>>>>;
const english = (data as { english: Raw[] }).english;
const unitCounts = (counts as unknown as { units: [string, number, number, string, string, string, number][] }).units;

export function units(subject: string, sub: string | null, grade: number, semester: number, publisher: string | null): Unit[] {
  const pubFilter = publisher && !MERGED.has(subject) && subject !== "英語" ? publisher : null;
  const n = (s: string, code: string) => unitCounts
    .filter(([sj, g, se, us, uc, up]) => sj === subject && g === grade && se === semester && us === s && uc === code && (!pubFilter || up === pubFilter))
    .reduce((a, r) => a + r[6], 0);
  if (subject === "英語") {
    return english
      // 列到這一冊為止教過的主題：後面的冊別常考前面的文法
      .filter((t) => t.group === "綜合" || (t.grade as number) < grade ||
                     ((t.grade as number) === grade && (t.semester as number) <= semester))
      .map((t) => ({ code: t.id as string, title: t.title as string, chapter: (t.group as string) ?? null, subject: "英語",
                     lessons: (t.lessons as Record<string, string>) ?? {}, key: `英語|${t.id}`, count: n("英語", t.id as string) }));
  }
  const book = junior[String(grade)]?.[String(semester)]?.[subject] ?? {};
  const subs = sub ? [sub] : SUBS[subject] ?? [""];
  const pub = publisher || "翰林";
  const out: Unit[] = [];
  for (const s of subs) {
    for (const u of book[s]?.[pub] ?? []) {
      const code = (u.lesson as string) || (u.section as string);
      const subj = s || subject;
      out.push({ code, title: u.title as string,
                 chapter: u.chapter ? `第${u.chapter}章 ${u.chapter_title}` : ((u.kind as string) ?? null),
                 subject: subj, key: `${subj}|${code}`, count: n(subj, code),
                 ...(subject === "數學" ? { patterns: unitPatterns(grade, semester, code) } : {}) });
    }
  }
  return out;
}

export function tree() {
  const c = counts as { subjects: Record<string, number>; years: number[] };
  return { subjects: SUBJECTS.map((s) => ({ name: s, subs: SUBS[s] ?? [], count: c.subjects[s] ?? 0 })),
           books: BOOKS.map(([g, s, n]) => ({ grade: g, semester: s, name: n })),
           publishers: PUBLISHERS, merged: [...MERGED], years: c.years,
           total: Object.values(c.subjects).reduce((a, b) => a + b, 0) };
}
