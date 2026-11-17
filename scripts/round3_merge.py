"""高中第三輪交件合庫：照 data/high_school/round3_final_packages.json 下載各批定版壓縮包、核對 SHA-256、合成一份題庫。

  python scripts/round3_merge.py --out out/high_school            # 下載＋合併（約 1.8GB）
  python scripts/round3_merge.py --out out/high_school --zips DIR # 已下載的 <批次>.zip 放在 DIR

輸出 <out>/{bank,assets,sources,manifest.json}。附圖用原卷裁切圖（不是重繪圖）。
定版清單記錄每批取哪一組雲端分段、整包 SHA-256；同一卷出現在兩批、或同一原卷 PDF 內容不同都會停下來。
"""
import argparse, glob, hashlib, json, os, shutil, subprocess, sys, tempfile, zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LIST = ROOT / "data/high_school/round3_final_packages.json"
URL = "https://drive.usercontent.google.com/download?id={}&export=download&confirm=t"


def fetch(batch: str, info: dict, zdir: Path) -> Path:
    z = zdir / f"{batch}.zip"
    if not z.exists():
        with open(z, "wb") as w:
            for pid in info["parts"]:
                r = subprocess.run(["curl", "-sSL", "--fail", "--retry", "4", URL.format(pid)], capture_output=True)
                if r.returncode:
                    sys.exit(f"{batch}：分段 {pid} 下載失敗")
                w.write(r.stdout)
    sha = hashlib.sha256(z.read_bytes()).hexdigest()
    if sha != info["sha256"]:
        sys.exit(f"{batch}：SHA-256 不符（{sha}），拿錯版本或下載不完整")
    return z


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--zips", type=Path, help="已下載的壓縮包資料夾（檔名 <批次>.zip）；沒有的會下載到這裡")
    a = ap.parse_args()
    pk = json.loads(LIST.read_text())["packages"]
    zdir = a.zips or a.out.parent / "round3_zips"
    zdir.mkdir(parents=True, exist_ok=True)
    out = a.out
    shutil.rmtree(out, ignore_errors=True)
    for d in ("bank", "assets", "sources"):
        (out / d).mkdir(parents=True)
    seen, papers, skipped = {}, [], []
    for batch, info in sorted(pk.items()):
        z = fetch(batch, info, zdir)
        with tempfile.TemporaryDirectory() as td:
            zipfile.ZipFile(z).extractall(td)
            man = [p for p in glob.glob(f"{td}/**/manifest.json", recursive=True) if "/rejected/" not in p][0]
            root = Path(man).parent
            M = json.loads(Path(man).read_text())
            skipped += [dict(s, batch=batch) for s in M.get("skipped", [])]
            meta = {p["id"]: p for p in M["papers"]}
            for y in sorted((root / "bank").glob("*.yaml")):
                pid = y.stem
                if pid in seen:
                    sys.exit(f"{pid} 同時出現在 {seen[pid]} 與 {batch}")
                seen[pid] = batch
                shutil.copy(y, out / "bank")
                if (root / "assets" / pid).is_dir():
                    shutil.copytree(root / "assets" / pid, out / "assets" / pid)
                papers.append({**meta.get(pid, {"id": pid}), "batch": batch, "package": info["title"]})
            for s in (root / "sources").rglob("*.pdf"):
                t = out / "sources" / s.relative_to(root / "sources")
                t.parent.mkdir(parents=True, exist_ok=True)
                if t.exists() and t.read_bytes() != s.read_bytes():
                    sys.exit(f"原卷 PDF 衝突：{t}")
                shutil.copy(s, t)
        print(f"{batch}: 累計 {len(papers)} 卷")
    (out / "manifest.json").write_text(json.dumps(
        {"produced_by": "scripts/round3_merge.py", "figures": "original_crop", "papers": papers, "skipped": skipped},
        ensure_ascii=False, indent=1))
    print(f"完成：{len(papers)} 卷 → {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
