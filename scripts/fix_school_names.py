#!/usr/bin/env python3
"""修正擷取時被模型寫壞的校名（夾帶卷頭文字、重複校名，甚至整段題目）。

同一所學校（考卷 id 裡的「縣市_簡稱」）的各份卷中，取最常見的「乾淨」校名當標準：
乾淨 = 以國民中學／國中／國中部／中學結尾、長度合理、含簡稱。
不乾淨的卷改成標準校名，標題依同樣格式重組：「校名 考試名稱 N年級科目科試題」。

    python scripts/fix_school_names.py data/bank [--dry-run]
"""

from __future__ import annotations

import argparse
import collections
import re
import sys
from pathlib import Path

import yaml

SUFFIX = re.compile(r"(國民中學|國中|國中部|中學)$")


def clean(name: str, short: str) -> bool:
    name = name or ""
    return bool(SUFFIX.search(name)) and len(name) <= 20 and short in name and name.count(short) == 1


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("bank", type=Path)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    docs = {}
    names: dict[tuple[str, str], collections.Counter] = collections.defaultdict(collections.Counter)
    for p in sorted(args.bank.glob("*.yaml")):
        d = yaml.safe_load(p.read_text(encoding="utf-8"))
        m = d["document"]
        parts = m["id"].split("_")
        key = (parts[2], parts[3])
        docs[p] = (d, key)
        if clean(m.get("school"), key[1]):
            names[key][m["school"]] += 1

    fixed = 0
    for p, (d, key) in docs.items():
        m = d["document"]
        if clean(m.get("school"), key[1]):
            continue
        if not names[key]:
            print(f"  找不到標準校名，略過：{m['id']}（{m.get('school', '')[:40]}）")
            continue
        std = names[key].most_common(1)[0][0]
        subject = m["subject"] + (f"（{m['sub_subject']}）" if m.get("sub_subject") else "")
        title = f"{std} {m.get('exam_name', '')} {m['grade']}年級{subject}科試題"
        print(f"  {m['id']}：{m.get('school', '')[:40]!r} → {std}")
        if not args.dry_run:
            m["school"], m["title"] = std, title
            p.write_text(yaml.safe_dump(d, allow_unicode=True, sort_keys=False), encoding="utf-8")
        fixed += 1
    print(f"{'（試跑）' if args.dry_run else ''}修正 {fixed} 份卷")
    return 0


if __name__ == "__main__":
    sys.exit(main())
