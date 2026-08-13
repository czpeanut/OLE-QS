#!/usr/bin/env python3
"""讓同一所學校在題庫裡只有一個校名。

擷取時已經會在**同一批**卷之內統一校名（見 extract.py 的 collect_school_names），
但題庫是跨批次累積的，批與批之間仍會分裂：

    屏東市中正國中          ← 113 批，卷面沒印校名，由縣市＋簡稱組出來
    屏東縣立中正國民中學    ← 111 批，卷面印了全名
    台中市立向上國中 / 臺中市立向上國民中學

對使用者來說這是同一所學校，但檢索、統計、去重都會把它們當成兩所。

判準：以 (縣市, 校名簡稱) 為同一所學校的鍵（臺／台差異在這一步已被抹平），
同一鍵之下取**最完整**的寫法 —— 卷面印出來的官方全名一定比組出來的長。

用法:
    python scripts/canonicalize_schools.py data/bank            # 實際改檔
    python scripts/canonicalize_schools.py data/bank --dry-run  # 只看會改什麼
"""

from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from apps.api.models import split_school  # noqa: E402


def canonical_names(docs: list[dict]) -> dict[tuple[str, str], str]:
    variants: dict[tuple[str, str], list[str]] = defaultdict(list)
    for meta in docs:
        school = meta.get("school")
        if school:
            variants[split_school(school)].append(school)
    # 最長的優先；一樣長時取出現次數多的，讓結果與檔案順序無關
    return {k: max(v, key=lambda s: (len(s), v.count(s))) for k, v in variants.items()}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("target", type=Path)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    paths = sorted(args.target.glob("*.y*ml")) if args.target.is_dir() else [args.target]
    loaded = [(p, yaml.safe_load(p.read_text(encoding="utf-8"))) for p in paths]
    names = canonical_names([d["document"] for _, d in loaded if d.get("document")])

    changed = 0
    for path, data in loaded:
        meta = data.get("document") or {}
        old = meta.get("school")
        new = names.get(split_school(old or ""))
        if not old or not new or new == old:
            continue
        print(f"  {old}  →  {new}   ({path.name})")
        changed += 1
        if args.dry_run:
            continue
        meta["school"] = new
        if meta.get("title", "").startswith(old):
            meta["title"] = new + meta["title"][len(old):]
        path.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False),
                        encoding="utf-8")

    kept = len({v for v in names.values()})
    print(f"\n{len(loaded)} 份文件、{kept} 所學校，"
          f"{'需要' if args.dry_run else '已'}統一 {changed} 份的校名")
    return 0


if __name__ == "__main__":
    sys.exit(main())
