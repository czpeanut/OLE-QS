// 題幹裡的 Markdown（粗體、管線式表格）+ 行內 $…$ 公式 → HTML。
// 與 apps/api/mathfmt.py 同一套規則：不引入 KaTeX，國中考卷用到的符號是很小的子集。

const COMMANDS: Record<string, string> = {
  "\\times": "×", "\\div": "÷", "\\pm": "±", "\\mp": "∓", "\\cdot": "·", "\\leq": "≤", "\\geq": "≥",
  "\\neq": "≠", "\\approx": "≈", "\\infty": "∞", "\\degree": "°", "\\circ": "∘", "\\alpha": "α",
  "\\beta": "β", "\\theta": "θ", "\\pi": "π", "\\angle": "∠", "\\triangle": "△", "\\parallel": "∥",
  "\\perp": "⊥", "\\rightarrow": "→", "\\Rightarrow": "⇒", "\\ldots": "…", "\\cdots": "⋯", "\\dots": "…",
  "\\leftrightarrow": "↔", "\\leftarrow": "←", "\\le": "≤", "\\ge": "≥", "\\ne": "≠", "\\lt": "<",
  "\\gt": ">", "\\sim": "∼", "\\cong": "≅", "\\propto": "∝", "\\square": "□", "\\therefore": "∴",
  "\\because": "∵", "\\bot": "⊥", "\\Delta": "Δ", "\\mu": "μ", "\\Omega": "Ω", "\\lambda": "λ",
  "\\rho": "ρ", "\\%": "%", "\\,": " ", "\\ ": " ", "\\!": "",
};

export function esc(s: unknown): string {
  return String(s ?? "").replace(/[&<>]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" })[c]!);
}

function group(s: string, i: number): [string, number] {
  if (i >= s.length) return ["", i];
  if (s[i] !== "{") return [s[i], i + 1];
  let depth = 1, j = i + 1;
  while (j < s.length && depth) {
    if (s[j] === "{") depth++;
    else if (s[j] === "}") depth--;
    j++;
  }
  return [s.slice(i + 1, j - 1), j];
}

export function latexToHtml(tex: string): string {
  const out: string[] = [];
  let i = 0;
  while (i < tex.length) {
    const c = tex[i];
    if (c === "\\") {
      const m = tex.slice(i).match(/^\\[a-zA-Z]+|^\\./);
      const cmd = m ? m[0] : "\\";
      const after = i + cmd.length;
      if (cmd === "\\frac" || cmd === "\\dfrac" || cmd === "\\tfrac") {
        const [num, j] = group(tex, after);
        const [den, k] = group(tex, j);
        out.push(`<span class="frac"><span class="num">${latexToHtml(num)}</span><span class="den">${latexToHtml(den)}</span></span>`);
        i = k; continue;
      }
      if (cmd === "\\sqrt") { const [b, j] = group(tex, after); out.push(`√<span class="sqrt">${latexToHtml(b)}</span>`); i = j; continue; }
      if (cmd === "\\overline") { const [b, j] = group(tex, after); out.push(`<span class="ovl">${latexToHtml(b)}</span>`); i = j; continue; }
      if (cmd === "\\overleftrightarrow" || cmd === "\\overrightarrow") {
        const [b, j] = group(tex, after);
        const arrow = cmd === "\\overleftrightarrow" ? "↔" : "→";
        out.push(`<span class="ovarr"><span class="arr">${arrow}</span><span>${latexToHtml(b)}</span></span>`);
        i = j; continue;
      }
      if (["\\mathrm", "\\mathbf", "\\mathit", "\\boldsymbol", "\\textrm"].includes(cmd)) {
        const [b, j] = group(tex, after); out.push(`<span class="up">${latexToHtml(b)}</span>`); i = j; continue;
      }
      if (cmd === "\\text") { const [b, j] = group(tex, after); out.push(esc(b)); i = j; continue; }
      if (cmd === "\\left" || cmd === "\\right") { i = after; continue; }
      out.push(cmd in COMMANDS ? COMMANDS[cmd] : esc(cmd));       // 未知命令保留原樣
      i = after; continue;
    }
    if (c === "^" || c === "_") {
      const [b, j] = group(tex, i + 1);
      out.push(c === "^" ? `<sup>${latexToHtml(b)}</sup>` : `<sub>${latexToHtml(b)}</sub>`);
      i = j; continue;
    }
    if (c === "{" || c === "}") { i++; continue; }
    out.push(esc(c));
    i++;
  }
  return out.join("");
}

function inline(md: string): string {
  return md.split(/\$([^$]*)\$/).map((chunk, i) =>
    i % 2 ? `<span class="math">${latexToHtml(chunk)}</span>`
      : esc(chunk).replace(/\*\*(.+?)\*\*/g, "<b>$1</b>").replace(/\n/g, "<br>"),
  ).join("");
}

// 只有出現表格分隔列（| --- |）才當表格；絕對值 |a－c| 也可能在行首
const SEP = /^\s*\|?[\s:|-]*-{3,}[\s:|-]*\|?\s*$/;

function table(rows: string[]): string {
  if (!rows.some((r) => SEP.test(r))) return inline(rows.join("\n"));
  const cells = rows.filter((r) => !SEP.test(r)).map((r) => r.trim().replace(/^\||\|$/g, "").split("|").map((c) => c.trim()));
  if (!cells.length) return "";
  const head = cells[0].map((c) => `<th>${inline(c)}</th>`).join("");
  const body = cells.slice(1).map((r) => "<tr>" + r.map((c) => `<td>${inline(c)}</td>`).join("") + "</tr>").join("");
  return `<table class="md"><tr>${head}</tr>${body}</table>`;
}

export function render(md: string | null | undefined): string {
  if (!md) return "";
  if (!md.split("\n").some((l) => SEP.test(l))) return inline(md);
  const out: string[] = [];
  let buf: string[] = [], rows: string[] = [];
  const flushText = () => { if (buf.length) { out.push(inline(buf.join("\n"))); buf = []; } };
  const flushTable = () => { if (rows.length) { out.push(table(rows)); rows = []; } };
  for (const line of md.split("\n")) {
    if (line.trim().startsWith("|")) { flushText(); rows.push(line); } else { flushTable(); buf.push(line); }
  }
  flushText(); flushTable();
  return out.join("");
}

export const MATH_CSS = `
.math{font-family:"Cambria Math","Latin Modern Math",Georgia,serif;font-style:italic}
.math sup,.math sub{font-style:normal;font-size:.72em}
.frac{display:inline-flex;flex-direction:column;vertical-align:middle;text-align:center;font-size:.92em;margin:0 .15em}
.frac .num{border-bottom:1px solid currentColor;padding:0 .3em}
.frac .den{padding:0 .3em}
.ovl{border-top:1px solid currentColor;padding-top:1px}
.sqrt{border-top:1px solid currentColor;padding:0 .15em}
.up{font-style:normal}
table.md{border-collapse:collapse;margin:4px 0;font-size:.92em}
table.md td,table.md th{border:1px solid currentColor;padding:1px 8px;text-align:center}
.ovarr{display:inline-flex;flex-direction:column;align-items:center;line-height:1;vertical-align:-0.1em}
.ovarr .arr{font-size:.7em;line-height:.8;font-style:normal}
`;

// 原卷題幹開頭的作答括號與題號（「( )16.」）：組卷後題號重編，留著會兩個題號並列
export function cleanStem(stem: string | null | undefined): string {
  return (stem || "").replace(/^\s*(?:[(（]\s*[)）]\s*)?(?:\d{1,3}\s*[.．、](?!\d)\s*)?/, "");
}
