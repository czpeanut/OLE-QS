// 隨機選題：在目前的篩選條件下抽出指定題數。
// - 題組整組一起抽（parent_id 指向題組第一題，由 scripts/build_sort_keys.py 寫入）
// - 可平均分配到各題型（數學）或各單元，也可指定易／中／難的比例
// - 抽出的題目依選題頁的順序排列（sort_key）
import { byIds, filtered, type Filters, type Q } from "./questions";

type F = Omit<Filters, "limit" | "offset">;
export type Spread = "pattern" | "unit" | "none";
export type Mix = "none" | "even" | "easy" | "hard";
export interface DrawOpts { n: number; spread: Spread; mix: Mix; exclude: string[] }

// 易：中：難（易＝1、中＝2、難＝3 以上，與選題頁相同）
const MIX: Record<Exclude<Mix, "none">, [number, number, number]> = { even: [3, 4, 3], easy: [5, 4, 1], hard: [2, 4, 4] };
const LEVEL_NAME = ["易", "中", "難"];
const levelOf = (d: number | null) => (d == null ? -1 : d <= 1 ? 0 : d === 2 ? 1 : 2);

const BASE = "id,parent_id,difficulty,unit_subject,unit_code,sort_key,document!document_id!inner(subject,grade,semester,academic_year_roc),ptag:tag!question_id(axis,value)";
const MAX_POOL = 30000;

// eslint-disable-next-line
type Row = Record<string, any>;
interface Block { ids: string[]; level: number; key: string }

function shuffle<T>(a: T[]): T[] {
  for (let i = a.length - 1; i > 0; i--) { const j = Math.floor(Math.random() * (i + 1)); [a[i], a[j]] = [a[j], a[i]]; }
  return a;
}

// 先查總數，再分頁同時抓（Supabase 每次最多回 1000 列）
async function candidates(f: F): Promise<Row[]> {
  const head = await filtered(f, BASE, true).order("id").range(0, 999);
  if (head.error) throw new Error(head.error.message);
  const total = Math.min(head.count ?? 0, MAX_POOL);
  const starts: number[] = [];
  for (let from = 1000; from < total; from += 1000) starts.push(from);
  const out: Row[] = [...(head.data ?? [])];
  for (let i = 0; i < starts.length; i += 8) {
    const pages = await Promise.all(starts.slice(i, i + 8).map((from) => filtered(f, BASE).order("id").range(from, from + 999)));
    for (const p of pages) {
      if (p.error) throw new Error(p.error.message);
      out.push(...(p.data ?? []));
    }
  }
  return out;
}

// 依分配方式輪流從各組抽：每輪每組抽一個（題組算一個），直到湊滿 n 題
function pick(blocks: Block[], n: number): Block[] {
  const groups = new Map<string, Block[]>();
  for (const b of shuffle([...blocks])) { if (!groups.has(b.key)) groups.set(b.key, []); groups.get(b.key)!.push(b); }
  const keys = shuffle([...groups.keys()]);
  const out: Block[] = [];
  let count = 0;
  for (let progress = true; count < n && progress;) {
    progress = false;
    for (const k of keys) {
      const list = groups.get(k)!;
      // 題組放不下就換同組下一個
      const i = list.findIndex((b) => count + b.ids.length <= n);
      if (i < 0) continue;
      const [b] = list.splice(i, 1);
      out.push(b);
      count += b.ids.length;
      progress = true;
      if (count >= n) break;
    }
  }
  return out;
}

export async function draw(f: F, o: DrawOpts): Promise<{ pool: number; items: Q[]; note: string | null }> {
  const rows = await candidates(f);
  const exclude = new Set(o.exclude);
  const byParent = new Map<string, Row[]>();
  for (const r of rows) {
    const k = r.parent_id ?? r.id;
    if (!byParent.has(k)) byParent.set(k, []);
    byParent.get(k)!.push(r);
  }
  const blocks: Block[] = [];
  for (const members of byParent.values()) {
    if (members.some((r) => exclude.has(r.id))) continue;          // 已在題籃（含同題組）就不再抽
    const first = members[0];
    const pattern = (first.ptag ?? []).find((t: Row) => t.axis === "pattern")?.value;
    const unit = `${first.unit_subject ?? ""}|${first.unit_code ?? ""}`;
    const key = o.spread === "pattern" ? pattern ?? unit : o.spread === "unit" ? unit : "";
    blocks.push({ ids: members.map((r) => r.id), level: levelOf(first.difficulty), key });
  }
  const pool = blocks.reduce((a, b) => a + b.ids.length, 0);
  const n = Math.max(1, Math.min(o.n, 100));
  let chosen: Block[] = [];
  const notes: string[] = [];
  if (o.mix !== "none") {
    // 依比例分配各難度的題數，某個難度不夠時用其他難度補
    const ratio = MIX[o.mix], sum = ratio.reduce((a, b) => a + b, 0);
    const target = ratio.map((r) => Math.floor((n * r) / sum));
    for (let i = 0; target.reduce((a, b) => a + b, 0) < n; i++) target[[1, 0, 2][i % 3]]++;
    target.forEach((t, lv) => {
      const got = pick(blocks.filter((b) => b.level === lv), t);
      const c = got.reduce((a, b) => a + b.ids.length, 0);
      if (c < t) notes.push(`${LEVEL_NAME[lv]}題只有 ${c} 題`);
      chosen.push(...got);
    });
    const have = chosen.reduce((a, b) => a + b.ids.length, 0);
    if (have < n) {
      const used = new Set(chosen);
      chosen.push(...pick(blocks.filter((b) => !used.has(b)), n - have));
      if (notes.length) notes.push("其餘用其他難度的題目補足");
    }
  } else {
    chosen = pick(blocks, n);
  }
  const ids = chosen.flatMap((b) => b.ids);
  if (ids.length < n) notes.unshift(`符合條件的題目不夠，只抽到 ${ids.length} 題`);
  const qs = await byIds(ids);
  const items = ids.map((i) => qs.get(i)).filter((q): q is Q => !!q)
    .sort((a, b) => (a.sort_key ?? 9e15) - (b.sort_key ?? 9e15));
  return { pool, items, note: notes.length ? notes.join("；") : null };
}
