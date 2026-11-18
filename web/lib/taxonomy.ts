// 選題篩選用的分類樹：科目 → 子科 → 冊別 → 版本 → 單元（與 apps/api/taxonomy.py 同規則）。
import data from "./data/curriculum.json";
import counts from "./data/counts.json";
import patternData from "./data/patterns.json";
import seniorData from "./data/senior.json";

export const SUBJECTS = ["國文", "英語", "數學", "自然", "社會"];
export const SUBS: Record<string, string[]> = { 自然: ["生物", "理化", "地科"], 社會: ["歷史", "地理", "公民"] };
export const PUBLISHERS = ["翰林", "康軒", "南一"];
export const BOOKS: [number, number, string][] = [[7, 1, "國一上"], [7, 2, "國一下"], [8, 1, "國二上"], [8, 2, "國二下"], [9, 1, "國三上"], [9, 2, "國三下"],
  [10, 1, "高一上"], [10, 2, "高一下"], [11, 1, "高二上"], [11, 2, "高二下"], [12, 1, "高三上"], [12, 2, "高三下"]];
// 高中：分科不同（理化分成物理、化學），單元用跨版本的標準單元（data/curriculum/senior.yaml），版本欄是「標準」
export const SENIOR_SUBS: Record<string, string[]> = { 自然: ["物理", "化學", "生物", "地科"], 社會: ["歷史", "地理", "公民與社會"] };
export const isSenior = (grade: number) => grade >= 10;
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
  const k = isSenior(grade) ? code : `${grade}-${semester}_${code}`;      // 高中題型目錄的鍵就是單元代號
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
  const pubFilter = publisher && !MERGED.has(subject) && subject !== "英語" && !isSenior(grade) ? publisher : null;
  const n = (s: string, code: string) => unitCounts
    .filter(([sj, g, se, us, uc, up]) => sj === subject && g === grade && se === semester && us === s && uc === code && (!pubFilter || up === pubFilter))
    .reduce((a, r) => a + r[6], 0);
  if (isSenior(grade)) return seniorUnits(subject, sub, grade, semester, n);
  if (subject === "英語") {
    return english
      // 列到這一冊為止教過的文法主題（後面的冊別常考前面的文法），再列這一冊的第幾次段考（字彙、閱讀等依段考次分）；
      // 綜合類只給模型歸類用，題目已改歸到段考範圍
      .filter((t) => t.group !== "綜合")
      .filter((t) => t.exam || (t.grade as number) < grade ||
                     ((t.grade as number) === grade && (t.semester as number) <= semester))
      .map((t) => ({ code: t.id as string, title: t.title as string, chapter: (t.group as string) ?? null, subject: "英語",
                     lessons: (t.lessons as Record<string, string>) ?? {}, key: `英語|${t.id}`, count: n("英語", t.id as string) }))
      .filter((u) => !u.code.startsWith("E") || u.count > 0);     // 九下多半只有兩次段考
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

type SeniorUnit = { code: string; title: string; book: string; chapter: string | null };
const SENIOR = seniorData as Record<string, Record<string, SeniorUnit[]>>;
// 高中各冊（選修、數A／數B…）在不同學校排在不同學期，列出這學期有題目的單元，依冊、章分組
function seniorUnits(subject: string, sub: string | null, grade: number, semester: number,
                     n: (s: string, code: string) => number): Unit[] {
  const subs = sub ? [sub] : SENIOR_SUBS[subject] ?? [subject];
  const out: Unit[] = [];
  for (const s of subs) {
    for (const u of SENIOR[subject]?.[s] ?? []) {
      out.push({ code: u.code, title: u.title, chapter: u.chapter ? `${u.book}｜${u.chapter}` : u.book, subject: s,
                 key: `${s}|${u.code}`, count: n(s, u.code),
                 ...(PATTERNS[u.code]?.types.length ? { patterns: unitPatterns(grade, semester, u.code) } : {}) });
    }
  }
  return out;
}

export function tree() {
  const c = counts as { subjects: Record<string, number>; years: number[] };
  return { subjects: SUBJECTS.map((s) => ({ name: s, subs: SUBS[s] ?? [], senior_subs: SENIOR_SUBS[s] ?? [], count: c.subjects[s] ?? 0 })),
           books: BOOKS.map(([g, s, n]) => ({ grade: g, semester: s, name: n })),
           publishers: PUBLISHERS, merged: [...MERGED], years: c.years,
           total: Object.values(c.subjects).reduce((a, b) => a + b, 0) };
}
