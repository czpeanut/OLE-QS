#!/usr/bin/env python3
"""檢查外部轉出的題庫 YAML 是否符合 docs/QUESTION_FORMAT.md，交件前自我檢查用。

    python scripts/check_bank_yaml.py <bank 資料夾或單一 YAML> [--assets <附圖根目錄>] [--existing]

國中、高中卷都適用（高中卷依 document.level: senior 判斷，見 docs/HIGH_SCHOOL_HANDOFF.md）。
會列出：缺必填欄位、卷 id／題目 id 不合規則、題型或大題不對、圖檔不存在、會被品管閘門剔除的題目
（規則同 apps/api/quality.py）。--existing 再比對 data/export/papers.jsonl.gz，列出題庫已有的卷。

另外檢查「格式正確但內容不完整」的題目（◇，見 CONTENT 說明），最後列出各類題數：
缺圖、缺表、選項混入、題組材料遺失、上下標遺失、題幹混入解答、圖檔沒被引用。
--strict 時這些也算錯誤；--issues-csv 檔名 會把每一題的問題寫成 CSV（題目 id、類別、說明）。
只需要 pyyaml；有錯誤時結束碼為 1。
"""

from __future__ import annotations

import argparse
import gzip
import json
import re
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from apps.api.quality import evaluate  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
REQUIRED = ("id", "grade", "subject", "academic_year_roc", "semester", "exam_seq", "city", "school_short",
            "school", "title", "exam_name", "sections")
SUBJECTS = {"國文", "英語", "數學", "自然", "社會"}
SUBS = {"自然": {"理化", "生物", "地科"}, "社會": {"歷史", "地理", "公民"}}
# 高中（docs/HIGH_SCHOOL_HANDOFF.md）：自然分物理、化學、生物、地科，社會的公民是「公民與社會」
SUBS_SENIOR = {"自然": {"物理", "化學", "生物", "地科"}, "社會": {"歷史", "地理", "公民與社會"}}
TYPES = {"single", "multiple", "tf", "fill", "calc", "essay", "matching"}
STATUS = {"verified", "ai_generated", "disputed", "missing"}

# ── 內容完整性（格式對、但題目看不懂或答不出來）─────────────────────────
CONTENT = {
    "缺圖": "題目提到圖（下圖、如圖、附圖、圖(一)、照片…），但這題沒有 assets／shared_asset／選項圖",
    "缺表": "題目提到表（下表、附表、表(一)…），但題幹沒有 Markdown 表格，也沒有附圖",
    "選項混入": "選擇題選項裡有換行或超過 150 字，通常是表格或下一題的材料被併進最後一個選項",
    "題組材料遺失": "題幹一開頭就寫「由上述／根據上文…」，卻沒有 group_stem（材料不見了）",
    "上下標遺失": "數學、自然卷在 $…$ 以外出現 x2、10³ 寫成 103、H2O 這類上下標被攤平的寫法",
    "題幹混入解答": "題幹或題組文字裡出現「參考答案」「試題解析」「《答案》」",
    "圖檔沒被引用": "assets/{卷 id}/ 裡有 PNG，但 YAML 沒有任何一題引用它",
}
FIG_RE = re.compile(r"下圖|如圖|附圖|右圖|左圖|上圖|圖[一二三四五六七八九十\(（]|圖\s*[0-9]|照片|地圖所示")
TAB_RE = re.compile(r"下表|如表|附表|右表|上表|表[一二三四五六七八九十\(（]")
GROUP_RE = re.compile(r"(由|根據|依據|依|從|閱讀)(上述|上文|上列|上段|上方|此段|這段|該段)")
ANS_RE = re.compile(r"參考答案|試題解析|【解析】|《答案》")
# 數學式中被攤平的上下標：x2、y3、)2、×103、H2O；排除 $…$ 裡的 LaTeX 與年份等長數字
SCRIPT_RE = re.compile(r"(?<![A-Za-z0-9])[A-Za-z]\d(?![\d.．])"            # x2、a1、H2
                       r"|(?<![A-Za-z0-9甲乙丙丁戊己庚辛壬癸一二三四五六七八九十])[)）]\d(?![\d.．])"  # (x－1)3
                       r"|\d[.．]\d+\s*×\s*10[－\-]?\d"                     # 2.0×103（科學記號）
                       r"|[A-Z][a-z]?\d[A-Z(]")                              # Cr2O3、FeCl3(
SCRIPT_OK = re.compile(r"[pq]\d|[AB]\d(?![A-Za-z])|Q\d|H\dN\d")   # 頁碼、習作題號、A4 紙、四分位數、H1N1


def content_issues(q: dict, m: dict) -> list[tuple[str, str]]:
    """回傳 [(類別, 說明)]。只看內容是否完整，不重複 evaluate() 的格式規則。"""
    out = []
    stem, grp = q.get("stem") or "", q.get("group_stem") or ""
    txt = grp + "\n" + stem
    opts = [o for o in q.get("options") or [] if isinstance(o, dict)]
    has_fig = bool(q.get("assets") or q.get("shared_asset") or any(o.get("asset") for o in opts))
    if (hit := FIG_RE.search(txt)) and not has_fig:
        out.append(("缺圖", f"提到「{hit.group()}」"))
    if (hit := TAB_RE.search(txt)) and "|" not in txt and not has_fig:
        out.append(("缺表", f"提到「{hit.group()}」"))
    if q.get("type") in ("single", "multiple"):
        bad = [o.get("label") for o in opts if "\n" in (o.get("content") or "") or len(o.get("content") or "") > 150]
        if bad:
            out.append(("選項混入", f"選項 {','.join(map(str, bad))}"))
    if (hit := GROUP_RE.search(stem)) and hit.start() < 12 and not grp:   # 材料在題幹前段就不算
        out.append(("題組材料遺失", f"題幹寫「{hit.group()}」"))
    if m.get("subject") in ("數學", "自然") and m.get("sub_subject") not in ("生物", "地科"):
        plain = re.sub(r"\$[^$]*\$", " ", txt + "\n" + "\n".join(o.get("content") or "" for o in opts))
        for hit in SCRIPT_RE.finditer(plain):
            if SCRIPT_OK.match(plain, hit.start()):
                continue
            out.append(("上下標遺失", "「" + " ".join(plain[max(0, hit.start() - 6):hit.end() + 6].split()) + "」"))
            break
    if hit := ANS_RE.search(txt):
        out.append(("題幹混入解答", f"出現「{hit.group()}」"))
    return out


def doc_id(m: dict) -> str:
    subj = m["subject"] + (f"_{m['sub_subject']}" if m.get("sub_subject") else "")
    if m.get("course"):                              # 高中：數A／數B、選修課程
        subj += f"_{m['course']}"
    return (f"doc_{m['academic_year_roc']}_{m['city']}_{m['school_short']}_{subj}"
            f"_g{m['grade']}s{m['semester']}e{m['exam_seq']}")


def check(path: Path, assets: Path | None) -> tuple[list[str], list[str], int, int, list[tuple[str, str, str]]]:
    """回傳 (錯誤, 提醒, 題數, 會被剔除的題數, 內容問題 [(題目 id, 類別, 說明)])。"""
    err, warn, issues = [], [], []
    try:
        d = yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        return [f"YAML 讀不進來：{exc}"], [], 0, 0, []
    if not isinstance(d, dict) or not isinstance(d.get("document"), dict):
        return ["最上層要有 document（對照 docs/QUESTION_FORMAT.md 第 3 節）"], [], 0, 0, []
    bad = [a for a in d.get("shared_assets") or [] if not isinstance(a, dict)]
    if bad:
        err.append(f"shared_assets 每一項要寫成 {{key, file, used_by, crop}}，有 {len(bad)} 項只寫了字串")
        d["shared_assets"] = [a for a in d["shared_assets"] if isinstance(a, dict)]
    bad = [q for q in d.get("questions") or [] if not isinstance(q, dict)]
    if bad:
        err.append(f"questions 每一題要是一個物件，有 {len(bad)} 項不是")
        d["questions"] = [q for q in d["questions"] if isinstance(q, dict)]
    m = d["document"]
    miss = [k for k in REQUIRED if m.get(k) in (None, "", [])]
    if miss:
        err.append(f"document 缺少 {miss}")
    if m.get("subject") and m["subject"] not in SUBJECTS:
        err.append(f"subject 只能是 {sorted(SUBJECTS)}，寫的是「{m['subject']}」")
    for k in ("grade", "academic_year_roc", "semester", "exam_seq"):
        if m.get(k) is not None and not isinstance(m[k], int):
            err.append(f"{k} 要寫整數，寫的是「{m[k]}」")
            m[k] = None
    senior = m.get("level") == "senior" or (m.get("grade") or 0) >= 10
    subs = SUBS_SENIOR if senior else SUBS
    if m.get("sub_subject") and m.get("sub_subject") not in subs.get(m.get("subject"), set()):
        err.append(f"sub_subject「{m['sub_subject']}」不屬於 {m.get('subject')}")
    if m.get("grade") not in ((10, 11, 12) if senior else (7, 8, 9)):
        err.append(f"grade 應為 {'10、11、12' if senior else '7、8、9'}，寫的是 {m.get('grade')}")
    if senior and m.get("level") != "senior":
        err.append("高中卷 document 要加 level: senior")
    if m.get("semester") not in (1, 2) or m.get("exam_seq") not in (1, 2, 3):
        err.append(f"semester 應為 1、2，exam_seq 應為 1～3（{m.get('semester')}, {m.get('exam_seq')}）")
    if "臺" in str(m.get("city", "")):
        err.append("city 請用「台」不用「臺」")
    if not miss:
        want = doc_id(m)
        if m["id"] != want:
            err.append(f"卷 id 應為 {want}，寫的是 {m['id']}")
        if path.stem != m["id"]:
            err.append(f"檔名應為 {m['id']}.yaml")
    secs = {s.get("ord") for s in m.get("sections") or []}
    shared = {a.get("key"): a for a in d.get("shared_assets") or []}
    qs = d.get("questions") or []
    if not qs:
        err.append("沒有任何題目")
    seen, rejected, used = set(), 0, set()
    for q in qs:
        qid = q.get("id", "?")
        if q.get("id") in seen:
            err.append(f"{qid}：題目 id 重複")
        seen.add(q.get("id"))
        if m.get("id") and q.get("id") != f"{m['id']}_s{q.get('section')}_{q.get('number')}":
            err.append(f"{qid}：題目 id 應為 {m['id']}_s{q.get('section')}_{q.get('number')}")
        if q.get("section") not in secs:
            err.append(f"{qid}：section {q.get('section')} 不在 sections 裡")
        if q.get("type") not in TYPES:
            err.append(f"{qid}：type「{q.get('type')}」不是 {sorted(TYPES)} 之一")
        if not isinstance(q.get("page"), int):
            warn.append(f"{qid}：缺 page（原卷第幾頁）")
        if q.get("answer") is not None and not isinstance(q["answer"], list):
            err.append(f"{qid}：answer 要寫成清單，例如 [A]")
        if q.get("answer") and q.get("answer_status") not in STATUS:
            warn.append(f"{qid}：有答案時請填 answer_status（verified／ai_generated／disputed）")
        if q.get("shared_asset") and q["shared_asset"] in shared and \
                q["id"] not in (shared[q["shared_asset"]].get("used_by") or []):
            warn.append(f"{qid}：共用圖 {q['shared_asset']} 的 used_by 沒列這題")
        if re.search(r"<(?!/?u[ >])[a-zA-Z/]", (q.get("stem") or "") + (q.get("group_stem") or "")):
            warn.append(f"{qid}：文字裡有 <u> 以外的角括號標籤，網站會當成文字顯示")
        files = [a.get("file") for a in q.get("assets") or [] if isinstance(a, dict)]
        files += [(o.get("asset") or {}).get("file") for o in q.get("options") or [] if isinstance(o, dict)]
        if q.get("shared_asset") in shared:
            files.append(shared[q["shared_asset"]].get("file"))
        used.update(f for f in files if f)
        if assets:
            for f in files:
                if f and not (assets / f).is_file():
                    err.append(f"{qid}：附圖檔不存在 {f}")
        issues += [(qid, k, v) for k, v in content_issues(q, m)]
        try:
            ok, why = evaluate(q, d)
        except Exception as exc:  # noqa: BLE001  欄位型別不對時品管規則跑不動
            ok, why = False, [f"品管檢查無法執行：{exc}"]
        if not ok:
            rejected += 1
            warn.append(f"{qid}：會被品管剔除（{'；'.join(why)}）")
    if assets and m.get("id") and (assets / m["id"]).is_dir():
        for p in sorted((assets / m["id"]).rglob("*.png")):
            rel = p.relative_to(assets).as_posix()
            if rel not in used:
                issues.append((m["id"], "圖檔沒被引用", rel))
    return err, warn, len(qs), rejected, issues


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("target", type=Path)
    ap.add_argument("--assets", type=Path, help="附圖根目錄（YAML 裡的 file 相對於這裡）")
    ap.add_argument("--existing", action="store_true", help="比對題庫已有的卷（data/export/papers.jsonl.gz）")
    ap.add_argument("--strict", action="store_true", help="內容問題（◇）也算錯誤")
    ap.add_argument("--issues-csv", type=Path, help="把每一題的內容問題寫成 CSV")
    ap.add_argument("--quiet", action="store_true", help="只印總結")
    args = ap.parse_args()
    paths = sorted(args.target.glob("*.yaml")) if args.target.is_dir() else [args.target]
    have = set()
    if args.existing:
        with gzip.open(REPO / "data/export/papers.jsonl.gz", "rt", encoding="utf-8") as f:
            have = {json.loads(line)["id"] for line in f}
    n_err = n_q = n_rej = 0
    all_issues: list[tuple[str, str, str]] = []
    for p in paths:
        err, warn, nq, rej, issues = check(p, args.assets)
        n_q += nq
        n_rej += rej
        all_issues += issues
        if args.existing and p.stem in have:
            err.append("題庫已有這份卷（同一場考試），不要重複交")
        if (err or warn or issues) and not args.quiet:
            print(f"== {p.name}")
            for e in err:
                print(f"  ✗ {e}")
            for w in warn[:30]:
                print(f"  △ {w}")
            if len(warn) > 30:
                print(f"  △ …另有 {len(warn) - 30} 則提醒")
            for qid, k, v in issues[:30]:
                print(f"  ◇ {qid}：{k}（{v}）")
            if len(issues) > 30:
                print(f"  ◇ …另有 {len(issues) - 30} 則內容問題")
        n_err += len(err)
    if args.issues_csv:
        import csv
        with args.issues_csv.open("w", encoding="utf-8-sig", newline="") as f:
            w = csv.writer(f)
            w.writerow(["題目或卷 id", "類別", "說明"])
            w.writerows(all_issues)
    print(f"\n{len(paths)} 份卷、{n_q} 題：錯誤 {n_err} 項；會被品管剔除 {n_rej} 題")
    by = {}
    for qid, k, v in all_issues:
        by.setdefault(k, set()).add(v if k == "圖檔沒被引用" else qid)   # 沒被引用的圖以張數計
    if by:
        print("內容問題（◇）：" + "、".join(f"{k} {len(by[k])}" for k in CONTENT if k in by)
              + ("" if args.strict else "（--strict 時算錯誤）"))
    n_content = sum(len(v) for v in by.values())
    return 1 if n_err or (args.strict and n_content) else 0


if __name__ == "__main__":
    sys.exit(main())
