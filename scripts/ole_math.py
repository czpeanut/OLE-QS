#!/usr/bin/env python3
"""MathType／方程式編輯器 3.0 物件（Word 內嵌 OLE，oleObject*.bin）→ LaTeX。

流程（全部用公開工具，不花 API 費用）：
  oleObject.bin ──Ruby gem mathtype_to_mathml_plus──▶ MathML ──pandoc（texmath）──▶ LaTeX ──正規化──▶ $…$

安裝（一次）：
    gem install mathtype_to_mathml_plus pry nokogiri --no-document
    apt-get install -y pandoc            # 或 https://github.com/jgm/pandoc/releases
    python3 -m pip install olefile

轉換結果再檢查一次：只准用題庫網站渲染得了的指令（docs/HIGH_SCHOOL_HANDOFF.md 第 6 節），
大括號要成對、不能是空的。不合格的回傳 None，呼叫端把那題列入 dropped，不要用圖片或占位字頂替。

    python scripts/ole_math.py oleObject1.bin oleObject2.bin ...      # 測試用：逐個印出 LaTeX
"""
from __future__ import annotations

import html
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

RUBY = r'''
require 'mathtype_to_mathml_plus'
$stderr.reopen(File::NULL)
ARGV.each do |f|
  begin
    puts "@@#{File.basename(f)}\t" + MathTypeToMathMLPlus::Converter.new(f).convert.gsub(/\s*\n\s*/, " ")
  rescue Exception => e
    puts "@@#{File.basename(f)}\tERROR #{e.class}"
  end
end
'''

# 題庫網站公式渲染器（web/lib/mathfmt.ts）認得的指令；不在這裡的一律當轉換失敗
ALLOWED = set("""
Box Delta Gamma Lambda Leftrightarrow Longrightarrow Omega Phi Pi Psi Rightarrow Sigma Theta Vert alpha angle
approx because begin beta bigcirc bigoplus bigstar binom blacksquare blacktriangle blacktriangleright boldsymbol
bot bullet cap cdot cdots circ cong cos cot csc cup dashrightarrow dbinom ddots degree delta dfrac div doteq dots
downarrow ell emptyset end epsilon equiv eta exists exp fallingdotseq forall frac frown gamma ge geq gg gt iint
implies in infty int kappa lambda langle ldots le left leftarrow leftrightarrow leftrightharpoons leq lim ll ln
log longrightarrow lt mathbb mathbf mathit mathrm max mid min mp mu nabla ne nearrow neg neq notin nwarrow odot
oint omega ominus oplus otimes overleftrightarrow overline overparen overrightarrow overset parallel partial perp
phi pi pm prime prod propto psi qquad quad rangle rho right rightarrow rightharpoonup rightleftharpoons searrow
sec setminus sigma sim simeq sin sqrt square stackrel star subset subseteq sum supset supseteq surd swarrow tan
tau text therefore theta times to triangle underline underset uparrow varepsilon varphi vdots vec vee vert wedge
widehat xi xleftarrow xrightarrow xrightleftharpoons
""".split())

FULL = str.maketrans({"－": "-", "＋": "+", "＝": "=", "（": "(", "）": ")", "，": ",", "＜": "<", "＞": ">",
                      "／": "/", "：": ":", "；": ";", "｜": "|", "［": "[", "］": "]", "．": r"\cdot ", "‧": r"\cdot ", "　": " ",
                      "×": r"\times ", "÷": r"\div ", "−": "-", "∙": r"\cdot ", "·": r"\cdot ", "°": r"^{\circ}"})


def available() -> str | None:
    """缺什麼工具就回傳說明；都在就回傳 None。"""
    if not shutil.which("ruby"):
        return "找不到 ruby"
    if not shutil.which("pandoc"):
        return "找不到 pandoc"
    r = subprocess.run(["ruby", "-e", "require 'mathtype_to_mathml_plus'"], capture_output=True)
    if r.returncode:
        return "Ruby 缺 mathtype_to_mathml_plus（gem install mathtype_to_mathml_plus pry nokogiri --no-document）"
    return None


def is_equation(data: bytes) -> bool:
    """OLE 物件裡（或它包著的 Word 文件裡）剛好有一個「Equation Native」資料流，才是 MathType／方程式編輯器的公式。"""
    if "Equation Native".encode("utf-16-le") not in data:
        return False
    try:
        import mtef3
        return bool(mtef3.mtef_bytes(data))
    except Exception:                                   # noqa: BLE001
        return False


def to_mathml(files: list[Path]) -> dict[str, str]:
    out: dict[str, str] = {}
    for i in range(0, len(files), 200):                  # 一次啟動 Ruby 轉 200 個
        with tempfile.NamedTemporaryFile("w", suffix=".rb", delete=False) as f:
            f.write(RUBY)
        r = subprocess.run(["ruby", f.name, *map(str, files[i:i + 200])], capture_output=True, text=True, timeout=900)
        Path(f.name).unlink()
        for line in r.stdout.splitlines():
            if line.startswith("@@") and "\t" in line:
                k, v = line[2:].split("\t", 1)
                if not v.startswith("ERROR"):
                    out[k] = re.sub(r"<\?xml[^>]*\?>", "", v).strip()
    return out


def mathml_to_latex(mml: dict[str, str]) -> dict[str, str]:
    """一次呼叫 pandoc 轉完一批：每個公式一段，前面加 @@key 標記。"""
    if not mml:
        return {}
    parts = ["<p>@@" + html.escape(k) + "@@ " + v.replace(' display="block"', "") + "</p>" for k, v in mml.items()]
    r = subprocess.run(["pandoc", "-f", "html", "-t", "latex", "--wrap=none"], input="\n".join(parts),
                       capture_output=True, text=True, timeout=900)
    out = {}
    for block in r.stdout.split("\n\n"):
        m = re.match(r"\s*@@(.+?)@@\s*\\\((.*)\\\)\s*$", block, re.S)
        if m:
            out[html.unescape(m.group(1))] = m.group(2)
    return out


def normalize(tex: str) -> str | None:
    t = tex.translate(FULL)
    t = re.sub(r"\\mspace\{[^}]*\}|\\[,;:!]|\\ |\\quad|\\qquad", " ", t)
    t = t.replace(r"\lbrack", "[").replace(r"\rbrack", "]").replace(r"\lbrace", r"\{").replace(r"\rbrace", r"\}")
    t = re.sub(r"\{\}(?=[\^_])", "", t)                                 # (b-a){}^{2} → (b-a)^{2}
    t = re.sub(r"\\overset\{\\overset\{\}\{(\\leftrightarrow|\\rightarrow)\}\}\{", lambda m: "\\over" + m.group(1)[1:].replace("leftrightarrow", "leftrightarrow").replace("rightarrow", "rightarrow") + "{", t)
    t = re.sub(r"\\overset\{[⃑⃗]\}\{", r"\\vec{", t)
    if r"\middle" in t:                                                   # \left. … \middle| … \right. 只是直線
        t = t.replace(r"\middle|", "|").replace(r"\left.", "").replace(r"\right.", "")
    t = re.sub(r"(?<![\\A-Za-z])(sin|cos|tan|cot|sec|csc|log|ln|exp)(?![A-Za-z])", r"\\\1", t)
    t = re.sub(r"\\text\{([A-Za-z0-9.]+)\}", r"\1", t)                 # \text{B} 這類單純字母
    for over, cmd in ((r"\leftarrow\rightarrow", "overleftrightarrow"), (r"\leftrightarrow", "overleftrightarrow"),
                      (r"\rightarrow", "overrightarrow"), (r"\frown", "overparen"), ("⌢", "overparen"),
                      ("︵", "overparen"), (r"\overline{}", "overline"), ("¯", "overline")):
        t = t.replace(r"\overset{" + over + "}{", "\\" + cmd + "{")
    t = re.sub(r"\\mathit\{([^{}]*)\}", r"\1", t)
    t = re.sub(r"\\operatorname\{(sin|cos|tan|cot|sec|csc|log|ln|lim|max|min|exp)\}", r"\\\1", t)
    t = re.sub(r"\^\{\\prime\}", "'", t)
    t = re.sub(r"(\^|_)([A-Za-z0-9])", r"\1{\2}", t)                    # x^2 → x^{2}
    t = re.sub(r"\\left\\\{\s*\\begin\{matrix\}(.*?)\\end\{matrix\}\s*\\right\.", r"\\begin{cases}\1\\end{cases}", t, flags=re.S)
    t = re.sub(r"\s+", " ", t).strip()
    if not t or "{}" in re.sub(r"\\(begin|end)\{[a-z]+\}", "", t):
        return None                                                     # 有空的分子、分母、根號：轉換掉字
    depth = 0
    for ch in re.sub(r"\\[{}]", "", t):
        depth += {"{": 1, "}": -1}.get(ch, 0)
        if depth < 0:
            return None
    if depth:
        return None
    for cmd in re.findall(r"\\([A-Za-z]+)", t):
        if cmd not in ALLOWED:
            return None
    return t


def mtef_version(data: bytes) -> int | None:
    try:
        import mtef3
        return mtef3.mtef_bytes(data)[0]
    except Exception:                                   # noqa: BLE001
        return None


def convert(bins: dict[str, bytes]) -> dict[str, str | None]:
    """{名稱: oleObject.bin 內容} → {名稱: LaTeX（不含 $）或 None}。
    MTEF 第 3 版（Office 方程式編輯器 3.0）用 mtef3.py；第 4、5 版（MathType）用 Ruby gem＋pandoc。"""
    import mtef3
    res: dict[str, str | None] = {}                      # 只放公式；不是公式的物件（Excel、圖檔）不在結果裡
    with tempfile.TemporaryDirectory() as d:
        files, names = [], {}
        for i, (k, data) in enumerate(bins.items()):
            if not is_equation(data):
                continue
            res[k] = None
            if mtef_version(data) == 3:
                try:
                    res[k] = normalize(mtef3.to_latex(data))
                except Exception:                       # noqa: BLE001
                    res[k] = None
                continue
            f = Path(d) / f"eq{i:05d}.bin"
            f.write_bytes(data)
            files.append(f)
            names[f.name] = k
        if files:
            for fname, tex in mathml_to_latex(to_mathml(files)).items():
                res[names[fname]] = normalize(tex)
    return res


def main() -> int:
    miss = available()
    if miss:
        print(miss)
        return 2
    bins = {Path(a).name: Path(a).read_bytes() for a in sys.argv[1:]}
    for k, v in convert(bins).items():
        print(k, v if v is not None else "✗ 轉不出來")
    return 0


if __name__ == "__main__":
    sys.exit(main())
