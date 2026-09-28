#!/usr/bin/env python3
"""對已擷取的題庫套用擷取流程後來補上的修正，不必重新呼叫模型。

目前的修正：
  - 選項被寫進題幹（見 vlm_extract.lift_inline_options）
  - 字詞題的作答指示在大題標題上，題目本身只有兩三個字（帶上大題名稱）

只處理視覺擷取產生的卷；規則式（extract.py）的舊卷不動。

用法:
    python scripts/repair_bank.py data/bank [--dry-run]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from vlm_extract import lift_inline_options  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("bank", type=Path)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    lifted = carried = files = 0
    for p in sorted(args.bank.glob("*.yaml")):
        d = yaml.safe_load(p.read_text(encoding="utf-8"))
        if not str((d.get("document") or {}).get("extractor", "")).startswith("vlm:"):
            continue
        secs = {s["ord"]: s["name"] for s in d["document"].get("sections") or []}
        dirty = False
        for q in d.get("questions") or []:
            if lift_inline_options(q):
                lifted += 1
                dirty = True
            if len(q.get("stem") or "") < 6 and not q.get("group_stem") and secs.get(q["section"]):
                q["group_stem"] = secs[q["section"]]
                carried += 1
                dirty = True
        if dirty:
            files += 1
            if not args.dry_run:
                p.write_text(yaml.safe_dump(d, allow_unicode=True, sort_keys=False),
                             encoding="utf-8")
    print(f"修正 {files} 份卷：選項從題幹拆出 {lifted} 題、帶上大題作答指示 {carried} 題"
          + ("（試跑，未寫入）" if args.dry_run else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
