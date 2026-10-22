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
  // 化學、箭頭與其他常見符號
  "\\rightleftharpoons": "⇌", "\\longrightarrow": "⟶", "\\Longrightarrow": "⟹", "\\to": "→", "\\uparrow": "↑",
  "\\downarrow": "↓", "\\rightharpoonup": "⇀", "\\leftrightharpoons": "⇋", "\\Leftrightarrow": "⇔",
  "\\fallingdotseq": "≒", "\\doteq": "≐", "\\equiv": "≡", "\\ll": "≪", "\\gg": "≫", "\\simeq": "≃",
  "\\bigcirc": "◯", "\\bigstar": "★", "\\star": "⋆", "\\ominus": "⊖", "\\oplus": "⊕", "\\bigoplus": "⨁",
  "\\otimes": "⊗", "\\odot": "⊙", "\\Box": "□", "\\blacksquare": "■", "\\blacktriangle": "▲", "\\vdots": "⋮",
  "\\ddots": "⋱", "\\ell": "ℓ", "\\surd": "√", "\\mid": "∣", "\\vert": "|", "\\Vert": "‖", "\\frown": "⌒",
  "\\gamma": "γ", "\\delta": "δ", "\\sigma": "σ", "\\phi": "φ", "\\varphi": "φ", "\\omega": "ω",
  "\\epsilon": "ε", "\\varepsilon": "ε", "\\eta": "η", "\\Sigma": "Σ", "\\Pi": "Π", "\\Phi": "Φ",
  "\\in": "∈", "\\notin": "∉", "\\subset": "⊂", "\\supset": "⊃", "\\cup": "∪", "\\cap": "∩",
  "\\emptyset": "∅", "\\neg": "¬", "\\prime": "′", "\\quad": "\u2003", "\\qquad": "\u2003\u2003",
  "\\implies": "⟹", "\\bullet": "•", "\\wedge": "∧", "\\vee": "∨", "\\langle": "⟨", "\\rangle": "⟩",
  "\\blacktriangleright": "▶", "\\dashrightarrow": "⇢", "\\nearrow": "↗", "\\swarrow": "↙", "\\searrow": "↘",
  "\\nwarrow": "↖", "\\newline": " ", "\\hline": "",
  "\\;": " ", "\\:": " ", "\\{": "{", "\\}": "}", "\\#": "#", "\\&": "&", "\\_": "_",
};
// 排成正體的函數名
const FUNCS = new Set(["\\sin", "\\cos", "\\tan", "\\cot", "\\sec", "\\csc", "\\log", "\\ln", "\\lim", "\\max", "\\min", "\\exp"]);
// 只影響字級或樣式、內容照常輸出的命令
const IGNORE = new Set(["\\Large", "\\large", "\\small", "\\normalsize", "\\displaystyle", "\\textstyle", "\\limits", "\\nolimits"]);
// 弧：考卷 OCR 常寫成 \overparen、\overgroup、\widehat、\wideparen
const ARCS = new Set(["\\overparen", "\\overgroup", "\\wideparen", "\\widehat", "\\overarc"]);
const over = (mark: string, body: string) => `<span class="ovarr"><span class="arr">${mark}</span><span>${body}</span></span>`;

// \begin{cases}…\end{cases} 這類環境：列以 \\ 分隔、欄以 & 分隔
function env(name: string, body: string): string {
  const rows = body.split(/\\\\(?:\[[^\]]*\])?/).map((r) => r.trim()).filter(Boolean)
    .map((r) => r.split(/(?<!\\)&/).map((c) => latexToHtml(c.trim())));
  if (name === "cases") return `<span class="cases">${rows.map((r) => `<span>${r.join(" ")}</span>`).join("")}</span>`;
  if (name === "aligned" || name === "align" || name === "gathered")
    return `<span class="rows">${rows.map((r) => `<span>${r.join("")}</span>`).join("")}</span>`;
  const cls = name === "vmatrix" ? "mtx vbar" : name === "pmatrix" ? "mtx paren" : "mtx";
  return `<span class="${cls}">${rows.map((r) => `<span class="mrow">${r.map((c) => `<span>${c}</span>`).join("")}</span>`).join("")}</span>`;
}

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
      if (cmd === "\\begin") {
        const [name, j] = group(tex, after);
        const close = tex.indexOf(`\\end{${name}}`, j);
        let k = j;
        if (name === "array" || name === "tabular") k = group(tex, j)[1];   // 欄位格式 {cc}
        const body = close < 0 ? tex.slice(k) : tex.slice(k, close);
        out.push(env(name, body));
        i = close < 0 ? tex.length : close + `\\end{${name}}`.length; continue;
      }
      if (ARCS.has(cmd)) { const [b, j] = group(tex, after); out.push(over("⌒", latexToHtml(b))); i = j; continue; }
      if (cmd === "\\vec") { const [b, j] = group(tex, after); out.push(over("→", latexToHtml(b))); i = j; continue; }
      if (cmd === "\\overset" || cmd === "\\stackrel") {
        const [a, j] = group(tex, after);
        const [b, k] = group(tex, j);
        out.push(over(/\\frown/.test(a) ? "⌒" : latexToHtml(a), latexToHtml(b))); i = k; continue;
      }
      if (cmd === "\\underset") {
        const [a, j] = group(tex, after);
        const [b, k] = group(tex, j);
        out.push(`<span class="unarr"><span>${latexToHtml(b)}</span><span class="arr">${latexToHtml(a)}</span></span>`); i = k; continue;
      }
      if (cmd === "\\xrightarrow" || cmd === "\\xleftarrow" || cmd === "\\xrightleftharpoons") {
        const [a, j] = group(tex, after);
        out.push(over(latexToHtml(a), cmd === "\\xrightarrow" ? "⟶" : cmd === "\\xleftarrow" ? "⟵" : "⇌")); i = j; continue;
      }
      if (cmd === "\\underline") { const [b, j] = group(tex, after); out.push(`<span class="unl">${latexToHtml(b) || "\u2003"}</span>`); i = j; continue; }
      if (cmd === "\\hspace" || cmd === "\\vspace" || cmd === "\\phantom") { const [, j] = group(tex, after); out.push(cmd === "\\vspace" ? "" : "\u2003"); i = j; continue; }
      if (["\\mathbin", "\\mathrel", "\\operatorname", "\\rm"].includes(cmd)) {
        const [b, j] = group(tex, after); out.push(`<span class="up">${latexToHtml(b)}</span>`); i = j; continue;
      }
      if (FUNCS.has(cmd)) { out.push(`<span class="up">${cmd.slice(1)}</span>`); i = after; continue; }
      if (IGNORE.has(cmd)) { i = after; continue; }
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
      if (cmd === "\\left" || cmd === "\\right") { i = tex[after] === "." ? after + 1 : after; continue; }
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
.unarr{display:inline-flex;flex-direction:column;align-items:center;line-height:1.1;vertical-align:top}
.unarr .arr{font-size:.7em;font-style:normal}
.unl{display:inline-block;min-width:1.5em;border-bottom:1px solid currentColor;text-align:center}
.cases{display:inline-flex;flex-direction:column;vertical-align:middle;text-align:left;border-left:1.5px solid currentColor;border-radius:.7em 0 0 .7em;padding:0 .2em 0 .45em;margin:0 .15em}
.rows{display:inline-flex;flex-direction:column;vertical-align:middle;text-align:left}
.mtx{display:inline-table;vertical-align:middle;margin:0 .2em}
.mtx .mrow{display:table-row}.mtx .mrow>span{display:table-cell;padding:0 .4em;text-align:center}
.mtx.vbar{border-left:1px solid currentColor;border-right:1px solid currentColor}
.mtx.paren{border-left:1px solid currentColor;border-right:1px solid currentColor;border-radius:.6em}
`;

// 原卷題幹開頭的作答括號與題號（「( )16.」）：組卷後題號重編，留著會兩個題號並列
export function cleanStem(stem: string | null | undefined): string {
  return (stem || "").replace(/^\s*(?:[(（]\s*[)）]\s*)?(?:\d{1,3}\s*[.．、](?!\d)\s*)?/, "");
}
