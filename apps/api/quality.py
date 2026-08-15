"""品管閘門：判斷一題能不能進題庫。

策略是**寧缺勿濫，不修補**。題庫的經濟結構與教科書不同 —— 來源卷很多，
題目是可替換的，丟掉一題的成本遠低於人工修好它。因此擷取有疑慮的題目
直接標為 rejected，不進檢索與組卷。

但**不是刪除**。被剔除的題目連同原因一起留在資料庫裡，因為：
  - 剔除率是管線健康度的指標。突然從 8% 跳到 40%，代表管線壞了而不是卷變難了。
  - 剔除原因可以回頭改進擷取，而不是只知道「少了很多題」。

判準只看「這題能不能被正常作答」，不做內容品質judgement。
"""

from __future__ import annotations

import re

# 題幹短於此字數幾乎確定是擷取殘缺
MIN_STEM_CHARS = 6
# 各題型應有的選項數；None 表示不檢查
EXPECTED_OPTIONS = {"single": 4, "tf": 2}


def evaluate(q: dict, doc: dict) -> tuple[bool, list[str]]:
    """回傳 (是否收錄, 剔除原因)。"""
    reasons: list[str] = []

    stem = (q.get("stem") or "").strip()
    if len(stem) < MIN_STEM_CHARS:
        reasons.append("題幹過短或為空，可能擷取殘缺")

    # ── 辨識不清：擷取階段自己標記的不確定處 ──────────────────
    if q.get("uncertain_spans"):
        reasons.append(f"擷取時標記辨識不清（{len(q['uncertain_spans'])} 處）")

    # ── 公式未閉合：$ 必須成對，否則題幹會渲染錯亂 ──────────────
    # $ 兩種用途都存在：數學的公式界定符，與英語／社會卷的貨幣符號。
    # 兩種解讀都算成立，只有**兩種解讀都不成對**時才判定為未閉合 ——
    # 單看其中一種會誤殺：把 "$5^2$" 的 $5 當貨幣刪掉，公式就變成未閉合。
    def unbalanced(t: str) -> bool:
        return t.count("$") % 2 and re.sub(r"\$(?=\d)", "", t).count("$") % 2

    texts = [stem] + [(o.get("content") or "") for o in (q.get("options") or [])]
    if any(unbalanced(t) for t in texts):
        reasons.append("LaTeX 公式未閉合")

    # ── 括號未閉合：算式殘缺的徵兆 ────────────────────────────
    # 堆疊排版的算式（分數、指數）在還原時，括號可能跟著上下標一起被
    # 移到別處，留下「已知甲= (− 乙、丙之值最大為何?」這種半截式子。
    # 表面上是一段完整的中文句子，實際上算式已經不成立、無法作答。
    def unclosed_paren(t: str) -> bool:
        depth = 0
        for c in t:
            if c == "(":
                depth += 1
            elif c == ")":
                depth -= 1
                if depth < 0:
                    return True
        return depth != 0

    if unclosed_paren(stem):
        reasons.append("題幹括號未閉合，算式可能殘缺")

    # ── 選項完整性 ──────────────────────────────────────────
    qtype = q.get("type")
    opts = q.get("options") or []
    expected = EXPECTED_OPTIONS.get(qtype)
    if expected and len(opts) != expected:
        reasons.append(f"{qtype} 題應有 {expected} 個選項，實際 {len(opts)} 個")

    labels = [o.get("label") for o in opts]
    if len(set(labels)) != len(labels):
        reasons.append(f"選項標籤重複 {labels}")
    for o in opts:
        has_text = (o.get("content") or "").strip()
        has_img = (o.get("asset") or {}).get("file")
        if not has_text and not has_img:
            reasons.append(f"選項 {o.get('label')} 既無文字也無圖片")

    # ── 圖表資產：缺圖的題目無法作答 ────────────────────────────
    for a in q.get("assets") or []:
        kind = a.get("kind", "figure")
        has_payload = a.get("file") or a.get("source_file") or a.get("markdown")
        if not has_payload:
            reasons.append(f"資產 {a.get('key')} 沒有圖檔或結構化內容")

    # ── 共用素材：遺失時題目表面正常但無法作答 ────────────────────
    ref = q.get("shared_asset")
    if ref:
        pool = {a.get("key") for a in (doc.get("shared_assets") or [])}
        pool |= {p.get("key") for p in (doc.get("passages") or [])}
        if ref not in pool:
            reasons.append(f"引用的共用素材 {ref} 不存在")

    # ── 答案不明確 ──────────────────────────────────────────
    # 「沒有答案」與「答案不明確」是兩回事：
    # 前者是來源沒附答案卷，補得回來；後者是這題本身有疑義，補不回來。
    ans = q.get("answer")
    if ans is not None:
        flat = [str(a).strip() for a in (ans if isinstance(ans, list) else [ans])]
        if not any(flat):
            reasons.append("答案欄位存在但內容為空")
        # 問號只有在「答案很短」時才代表存疑；完整句子裡的問號是正常標點
        # （英語卷的答案就是整句英文，含 "Do you like sports?"）。
        elif any(re.search(r"待確認|存疑|送分|均給分|answer\s*unclear", a)
                 or (len(a) <= 8 and re.search(r"[?？]", a))
                 for a in flat):
            reasons.append(f"答案標示有疑義：{flat}")
        expect_n = q.get("answer_count")
        if expect_n and len(flat) != expect_n:
            reasons.append(f"應有 {expect_n} 個答案，實際 {len(flat)} 個")

        # 選擇題與是非題的答案只能是自己的選項代號。
        # 答案卷是表格，解析時只要對錯一行，整段答案就會平移 ——
        # 實測有整份卷的答案變成下一題的題號（第5題的答案是「7.」）。
        # 這種錯誤最危險的地方在於它看起來很正常：欄位有值、狀態是
        # verified，然後原封不動印在教師解答卷上當正解。
        labels = {o.get("label") for o in opts}
        if qtype in {"single", "tf", "multiple"} and labels:
            stray = [a for a in flat if a not in labels]
            if stray:
                reasons.append(f"{qtype} 題的答案 {stray} 不在選項代號 {sorted(labels)} 之中")

    return (not reasons), reasons


def summarize(results: list[tuple[bool, list[str]]]) -> dict:
    """統計收錄率與各剔除原因的次數，用來看管線健康度。"""
    kept = sum(1 for ok, _ in results if ok)
    counts: dict[str, int] = {}
    for ok, reasons in results:
        if ok:
            continue
        for r in reasons:
            # 把帶數字的原因歸併成同一類，統計才有意義
            key = re.sub(r"\d+", "N", r)
            counts[key] = counts.get(key, 0) + 1
    return {
        "total": len(results),
        "kept": kept,
        "rejected": len(results) - kept,
        "keep_rate": kept / len(results) if results else 0.0,
        "reasons": dict(sorted(counts.items(), key=lambda kv: -kv[1])),
    }
