// 把選好的題目排成考卷 HTML（預覽與列印成 PDF 共用）。與 apps/api/export.py 同規則。
//   exam 試題卷（可選卷末附答案）、answer 答案卷（作答格）、key 教師解答卷
// 來源標註依授權條件固定輸出，不提供關閉選項。
import { esc, MATH_CSS, render as md, cleanStem } from "./mathfmt";
import type { Asset, Q } from "./questions";

const TYPE_ORDER = ["single", "multiple", "tf", "fill", "matching", "calc", "essay", "group"];
const TYPE_NAME: Record<string, string> = { single: "單選題", multiple: "多選題", tf: "是非題", fill: "填充題", matching: "配合題", calc: "計算題", essay: "非選擇題", group: "題組" };
const GROUP_NAME: Record<string, string> = { 英語: "閱讀測驗", 國文: "閱讀測驗" };
const CHOICE = new Set(["single", "multiple", "tf"]);
const NUM = "一二三四五六七八九十";
const CROP_DPI = 220;      // 擷取管線裁圖的解析度；據此把圖印回原卷上的實際大小

export interface Settings {
  subtitle: string; header_fields: boolean; auto_sections: boolean; columns: number; font_size: number;
  answer_lines: number; show_score: boolean; answer_appendix: boolean; choice_paren: boolean; uniform_option_images: boolean;
}
export const DEFAULTS: Settings = { subtitle: "", header_fields: true, auto_sections: true, columns: 1, font_size: 11.5,
  answer_lines: 4, show_score: true, answer_appendix: false, choice_paren: true, uniform_option_images: true };
export interface Item { q: Q; score: number | null }
export type Widths = Map<string, { w: number; h: number }>;   // 附圖路徑 → 像素寬高
const mm = (px: number) => px / CROP_DPI * 25.4;
// 選項圖：每張都至少這麼高（像素）才算「圖片選項」，統一成同一高度；
// 更矮的多半是「甲→丙→乙」這類文字式圖，放大會很突兀，維持原卷比例
const PICTURE_PX = 100;
// 題目在比大小（顯微鏡倍率、比例尺、面積…）時，選項圖的大小本身就是答案，不能統一
const KEEP_SCALE = /倍|放大|縮小|比例|大小|面積|長度|尺寸/;

function img(file: string, widths: Widths, alt = "", style?: string): string {
  const d = widths.get(file);
  style ??= d ? `width:${mm(d.w).toFixed(1)}mm` : "";
  return `<img src="/api/assets/${encodeURI(file)}" alt="${esc(alt)}"${style ? ` style="${style}"` : ""}>`;
}

// 同一題的選項圖排成一致的大小（原卷各選項的圖常常裁得大小不一）
function optionImages(q: Q, widths: Widths, narrow: boolean, uniform: boolean): { cols: number; style: Map<string, string> } | null {
  const files = q.options.map((o) => o.asset_file).filter((f): f is string => !!f);
  const dims = files.map((f) => widths.get(f));
  if (!files.length || dims.some((d) => !d)) return null;
  const ds = dims as { w: number; h: number }[];
  const style = new Map<string, string>();
  let shown: number[];                                  // 每張圖排出來的寬（mm）
  if (uniform && !KEEP_SCALE.test(q.stem) && Math.min(...ds.map((d) => d.h)) >= PICTURE_PX) {
    // 圖片選項：同一高度（取中位數，限制在 18–38mm，雙欄時 15–28mm），太寬的等比縮進格子
    const hs = ds.map((d) => d.h).sort((a, b) => a - b);
    const [lo, hi] = narrow ? [15, 28] : [18, 38];
    const H = Math.min(hi, Math.max(lo, mm(hs[Math.floor(hs.length / 2)])));
    files.forEach((f) => style.set(f, `height:${H.toFixed(1)}mm;width:auto;max-width:100%;object-fit:contain`));
    shown = ds.map((d) => H * d.w / d.h);
  } else {
    // 文字式小圖、比大小的題目或關閉統一時：維持原卷比例；格子放不下時整題一起等比縮小，而不是只縮最寬的那張
    const maxW = Math.max(...ds.map((d) => d.w));
    files.forEach((f, i) => style.set(f, `width:calc(min(100%, ${mm(maxW).toFixed(1)}mm) * ${(ds[i].w / maxW).toFixed(3)})`));
    shown = ds.map((d) => mm(d.w));
  }
  // 圖都夠窄就一列排四個，否則兩個
  const cols = files.length === q.options.length && Math.max(...shown) <= (narrow ? 17 : 36) ? 4 : 2;
  return { cols, style };
}

function options(q: Q, widths: Widths, narrow: boolean, uniform: boolean): string {
  if (!q.options.length) return "";
  const hasImg = q.options.some((o) => o.asset_file);
  const longest = Math.max(...q.options.map((o) => (o.content || "").length));
  const [four, two] = narrow ? [4, 10] : [7, 20];      // 雙欄每欄只有一半寬，門檻跟著減半
  const pics = hasImg ? optionImages(q, widths, narrow, uniform) : null;
  const cols = hasImg ? pics?.cols ?? 2 : longest <= four ? 4 : longest <= two ? 2 : 1;
  return `<div class="opts c${cols}">` + q.options.map((o) =>
    `<div class="opt"><span class="ol">(${esc(o.label)})</span><span class="oc">${o.content_html}${o.asset_file
      ? img(o.asset_file, widths, "選項" + o.label, pics?.style.get(o.asset_file)) : ""}</span></div>`).join("") + "</div>";
}

function assets(list: Asset[], widths: Widths): string {
  return list.map((a) => {
    if (a.kind === "table" && a.markdown) return md(a.markdown);
    if (a.file) return `<figure>${img(a.file, widths, a.alt ?? "")}${a.label ? `<figcaption>${esc(a.label)}</figcaption>` : ""}</figure>`;
    return "";
  }).join("");
}

function sharedHtml(a: Asset, widths: Widths): string {
  if (a.text) return `<div class="passage">${a.text.split("\n\n").filter((p) => p.trim()).map((p) => `<p>${md(p)}</p>`).join("")}</div>`;
  if (a.file) return `<figure>${img(a.file, widths, a.alt ?? "")}${a.label ? `<figcaption>${esc(a.label)}</figcaption>` : ""}</figure>`;
  return "";
}

function sections(items: Item[], auto: boolean, groupName: string): [string | null, Item[]][] {
  if (!auto) return [[null, items]];
  const groups = new Map<string, Item[]>(TYPE_ORDER.map((t) => [t, []]));
  for (const it of items) {
    // 閱讀、圖表題組自成一個大題，放在一般題型之後
    const key = it.q.group_stem || it.q.shared_asset_key ? "group" : it.q.type;
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key)!.push(it);
  }
  const named = [...groups].filter(([, its]) => its.length).map(([t, its]) => [t === "group" ? groupName : TYPE_NAME[t] ?? t, its] as [string, Item[]]);
  return named.map(([n, its], i) => [i < NUM.length ? `${NUM[i]}、${n}` : n, its]);
}

function sectionNote(its: Item[]): string {
  const s = its.map((i) => i.score);
  if (!s.every((x) => x)) return "";
  const total = s.reduce((a, b) => a! + b!, 0)!;
  return new Set(s).size === 1 ? `（每題 ${s[0]} 分，共 ${total} 分）` : `（共 ${total} 分）`;
}

export function renderPaper(title: string, items: Item[], mode: "exam" | "answer" | "key", settings: Partial<Settings>, widths: Widths): string {
  const st = { ...DEFAULTS, ...settings };
  const cols = Number(st.columns) === 2 && mode !== "answer" ? 2 : 1;       // 答案卷的作答格不分欄
  const suffix = { exam: "", answer: "　答案卷", key: "　教師解答卷" }[mode];
  const total = items.reduce((a, i) => a + (i.score || 0), 0);
  const head = [`<header class="head"><h1>${esc(title)}${suffix}</h1>`];
  if (st.subtitle) head.push(`<div class="sub">${esc(st.subtitle)}</div>`);
  head.push(`<div class="meta">共 ${items.length} 題${st.show_score && total ? `　滿分 ${total} 分` : ""}</div></header>`);
  if (st.header_fields && mode !== "key")
    head.push('<div class="fields"><span>班級：＿＿＿＿</span><span>座號：＿＿＿</span><span>姓名：＿＿＿＿＿＿</span><span>得分：＿＿＿＿</span></div>');

  const subjects = new Set(items.map((i) => i.q.subject));
  const secs = sections(items, st.auto_sections, subjects.size === 1 ? GROUP_NAME[[...subjects][0]] ?? "題組" : "題組");
  const body: string[] = [];
  let n = 0;
  if (mode === "answer") {
    for (const [name, its] of secs) {
      if (name) body.push(`<div class="sec">${esc(name)}</div>`);
      const grid: string[] = [];
      for (let s = 0; s < its.length; s += 10) {
        const nums = Array.from({ length: Math.min(10, its.length - s) }, (_, k) => n + s + k + 1);
        grid.push(nums.map((x) => `<div class="n">${x}</div>`).join(""), nums.map(() => '<div class="b"></div>').join(""));
      }
      n += its.length;
      body.push(`<div class="grid">${grid.join("")}</div>`);
    }
  } else {
    const seenShared = new Set<string>();
    let lastGroup: string | null = null;
    for (const [name, its] of secs) {
      if (name) body.push(`<div class="sec">${esc(name)}<span class="sn">${st.show_score ? sectionNote(its) : ""}</span></div>`);
      for (const it of its) {
        const q = it.q;
        n++;
        // 共用素材與題組說明只在該組第一題前印一次
        if (q.shared_asset && !seenShared.has(`${q.document_id}|${q.shared_asset_key}`)) {
          seenShared.add(`${q.document_id}|${q.shared_asset_key}`);
          body.push(sharedHtml(q.shared_asset, widths));
        }
        if (q.group_stem && q.group_stem !== lastGroup) body.push(`<div class="group-stem">${q.group_stem_html}</div>`);
        lastGroup = q.group_stem;
        const paren = st.choice_paren && mode === "exam" && CHOICE.has(q.type) ? '<span class="paren">(　　)</span>' : "";
        const parts = [paren + md(cleanStem(q.stem)), assets(q.assets, widths), options(q, widths, cols === 2, st.uniform_option_images !== false)];
        if (mode === "exam" && (q.type === "calc" || q.type === "essay") && st.answer_lines)
          parts.push('<div class="lines">' + '<div class="ln"></div>'.repeat(Number(st.answer_lines)) + "</div>");
        if (mode === "key") {
          const note = q.answer_status === "ai_generated" ? (q.answer_source === "ai:gemini+qwen" ? "（AI 雙模型一致，未經人工確認）" : "（AI 多數決，未經人工確認）")
            : q.answer_status === "disputed" ? "（模型答案不一致，待判定）" : "";
          parts.push(`<div class="ans">答：${esc((q.answer ?? []).join("、") || "（無答案）")}<span class="warn">${esc(note)}</span></div>`);
          if (q.explanation) parts.push(`<div class="expl">${md(q.explanation)}</div>`);
        }
        parts.push(`<div class="cite">${esc(q.citation)}</div>`);
        const score = st.show_score && it.score ? `<span class="sc">（${it.score} 分）</span>` : "";
        body.push(`<div class="q"><span class="qn">${n}.</span><div class="qb">${parts.join("")}</div>${score}</div>`);
      }
    }
    if (mode === "exam" && st.answer_appendix) {
      let k = 0;
      const cells = secs.flatMap(([, its]) => its).map((it) => `<div><b>${++k}.</b> ${esc((it.q.answer ?? []).join("、") || "—")}</div>`);
      body.push(`<div class="appendix"><div class="sec">參考答案</div><div class="anslist">${cells.join("")}</div></div>`);
    }
  }
  const sources = [...new Set(items.flatMap((i) => i.q.citation.split(" ").filter(Boolean)))];
  const foot = `<footer class="src">本卷題目取自下列公開試題，著作權屬原命題單位所有：${sources.map(esc).join("、")}</footer>`;
  return `<!doctype html><html lang="zh-Hant"><head><meta charset="utf-8"><title>${esc(title)}${suffix}</title>`
    + `<style>${PRINT_CSS}:root{--fs:${Number(st.font_size)}pt}</style></head>`
    + `<body><div class="paper">${head.join("")}<main class="cols${cols}">${body.join("")}</main>${foot}</div></body></html>`;
}

const PRINT_CSS = `
@import url("https://fonts.googleapis.com/css2?family=Noto+Serif+TC:wght@400;700&display=swap");
@page { size: A4; margin: 15mm 13mm 16mm 13mm; }
* { box-sizing: border-box; }
html { background: #fff; }
/* 英數字先用西文字型：CJK 字型裡的 ’ “ 是全形寬，英文閱讀題會出現怪空格 */
body { font-family: "Times New Roman", "Liberation Serif", "Noto Serif TC", "Noto Serif CJK TC", "Songti TC", "PMingLiU", serif;
       font-size: var(--fs); line-height: 1.75; color: #000; margin: 0; }
@media screen { body { background: #e9ebee; padding: 16px 0; }
  .paper { background: #fff; width: 210mm; min-height: 297mm; margin: 0 auto; padding: 15mm 13mm; box-shadow: 0 1px 6px rgba(0,0,0,.18); } }
.head { text-align: center; border-bottom: 2px solid #000; padding-bottom: 5px; margin-bottom: 8px; }
.head h1 { font-size: 1.45em; margin: 0; letter-spacing: .08em; }
.head .sub { font-size: 1.02em; margin-top: 2px; }
.head .meta { font-size: .82em; color: #333; }
.fields { display: flex; gap: 1.6em; justify-content: flex-end; font-size: .9em; margin: 4px 0 10px; }
main.cols2 { column-count: 2; column-gap: 9mm; column-rule: 1px solid #999; }
.sec { font-weight: 700; font-size: 1.05em; margin: 12px 0 6px; break-after: avoid; }
.sec .sn { font-weight: 400; font-size: .85em; margin-left: .4em; }
.q { display: flex; gap: .35em; margin: 0 0 .85em; break-inside: avoid; }
.qn { font-weight: 700; min-width: 1.9em; text-align: right; }
.qb { flex: 1; min-width: 0; }
.sc { font-size: .8em; color: #555; white-space: nowrap; }
.opts { display: grid; gap: 1px 1.2em; margin-top: 2px; }
.opts.c4 { grid-template-columns: repeat(4, 1fr); } .opts.c2 { grid-template-columns: repeat(2, 1fr); } .opts.c1 { grid-template-columns: 1fr; }
.opt { display: flex; gap: .25em; } .ol { white-space: nowrap; } .paren { margin-right: .3em; }
.group-stem { padding: 4px 8px; margin: 6px 0; border-left: 3px solid #555; break-inside: avoid; }
.passage { padding: 6px 10px; margin: 6px 0; border: 1px solid #888; break-inside: avoid; }
.passage p { margin: 0 0 .4em; text-indent: 2em; }
figure { margin: 4px 0; text-align: center; break-inside: avoid; }
figure img { max-width: 100%; height: auto; }
.opt .oc { flex: 1; min-width: 0; } .opt img { display: block; max-width: 100%; margin: 1px 0 3px; }
figcaption { font-size: .8em; }
.lines { margin-top: 4px; } .lines .ln { border-bottom: 1px solid #999; height: 2em; }
.cite { font-size: .68em; color: #777; text-align: right; line-height: 1.3; }
.ans { color: #b00; font-weight: 700; } .ans .warn { font-weight: 400; font-size: .8em; margin-left: .4em; }
.expl { font-size: .9em; border-left: 3px solid #ccc; padding: 2px 8px; margin-top: 2px; }
.grid { display: grid; grid-template-columns: repeat(10, 1fr); border: 1px solid #000; margin-bottom: 8px; }
.grid div { border: 1px solid #999; text-align: center; min-height: 2.3em; padding-top: .3em; }
.grid .n { background: #eee; font-weight: 700; min-height: 0; padding: 0; }
.appendix { break-before: page; column-span: all; }
.anslist { display: grid; grid-template-columns: repeat(5, 1fr); gap: 2px 10px; }
footer.src { margin-top: 14px; padding-top: 5px; border-top: 1px solid #999; font-size: .68em; color: #555; }
` + MATH_CSS;
