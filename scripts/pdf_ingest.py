#!/usr/bin/env python3
"""把一整個目錄的考卷 PDF 轉成管線可吃的輸入，並判斷該走哪條擷取路線。

關鍵：考卷 PDF 有兩種，處理方式完全不同，混為一談會白白損失精度與金錢。

  A. 原生數位 PDF（born-digital）— 由 Word / LaTeX 直接匯出，內含文字圖層。
     文字可以「無損」取出，不需要 OCR，也不會有辨識錯誤。
     → 轉成圖片再 OCR 等於把已經正確的文字弄髒一次再猜回來，是負向操作。
     → 正確做法：抽出文字圖層當骨架，只把「圖形」送 VLM 看。

  B. 掃描 PDF（scanned）— 整頁就是一張影像，沒有文字圖層。
     → 這種才需要轉成 PNG 走 VLM 擷取。

本腳本會自動分類，兩種都輸出，並在 manifest 標明每份檔案該走哪條路線。

用法（Windows）:
    pip install pymupdf
    python scripts/pdf_ingest.py "C:\\Users\\USER\\OneDrive\\共同編輯區\\UC\\考古題\\國中" -o out/ingest

用法（macOS / Linux）:
    python scripts/pdf_ingest.py ~/考古題/國中 -o out/ingest

輸出:
    out/ingest/<pdf相對路徑>/p01.png ...   每頁影像
    out/ingest/<pdf相對路徑>/text.json     原生數位 PDF 的文字圖層（含座標）
    out/ingest/manifest.json               全部檔案的清單與路線判斷
"""

from __future__ import annotations

import argparse
import json
import sys
import traceback
from dataclasses import asdict, dataclass, field
from pathlib import Path

try:
    import pymupdf as fitz
except ImportError:  # PyMuPDF < 1.24 只有舊的 fitz 名稱
    try:
        import fitz
    except ImportError:
        sys.exit("需要 PyMuPDF，請先執行：pip install pymupdf")

# 一頁至少要有這麼多個字元，才算「有文字圖層」。
# 掃描 PDF 有時會夾帶頁碼或浮水印的少量文字，用門檻擋掉。
TEXT_LAYER_MIN_CHARS = 120


@dataclass
class FigureInfo:
    key: str
    file: str
    bbox: list[float]          # 頁面座標 (pt)
    column: int                # 0=左欄 1=右欄，單欄頁一律 0


@dataclass
class PageInfo:
    page_no: int
    image: str
    width: int
    height: int
    char_count: int
    columns: int
    figures: list[FigureInfo] = field(default_factory=list)


@dataclass
class DocInfo:
    source: str
    out_dir: str
    page_count: int
    route: str                      # 'digital' | 'scanned' | 'mixed' | 'error'
    digital_pages: int
    scanned_pages: int
    figure_count: int = 0
    pages: list[PageInfo] = field(default_factory=list)
    error: str | None = None


def detect_columns(page) -> tuple[int, float]:
    """判斷頁面是單欄還是雙欄，回傳 (欄數, 分欄線 x 座標)。

    作法：看文字區塊的左緣分布。雙欄版面的左緣會明顯聚成兩堆，
    且右堆的起點大於頁寬中線。比訓練版面模型簡單得多，對考卷夠用。
    """
    mid = (page.rect.x0 + page.rect.x1) / 2
    lefts = [b[0] for b in page.get_text("blocks") if (b[4] or "").strip()]
    if len(lefts) < 6:
        return 1, mid
    right_side = [x for x in lefts if x > mid]
    # 右半邊要有夠多的獨立區塊起點，才算真的有第二欄
    return (2, mid) if len(right_side) >= max(3, len(lefts) * 0.2) else (1, mid)


def extract_figures(page, out_dir: Path, page_no: int, dpi: int,
                    mid: float, columns: int, pad: float = 2.0) -> list[FigureInfo]:
    """把頁面上的圖形切出來。

    關鍵：**渲染頁面區域**，不要抽出內嵌的影像物件（XObject）。理由有二 ——

      1. 透明度：PDF 的圖幾乎都帶 SMask（軟遮罩）。直接抽 XObject 會丟掉遮罩，
         原本透明的地方變成純黑，圖等於毀損。實測某份自然科考卷，
         14 張圖裡有 2 張（燒杯圖、長條圖）整張變全黑，完全不可用。
      2. 重複使用：同一個 XObject 可以在頁面上被放置多次（旋轉、翻轉）。
         實測該卷某題的 4 個選項圖就是同一張圖擺 4 次 ——
         照 xref 去抽只會得到 1 張，少掉 3 個選項。

    改成從頁面渲染指定矩形，兩個問題同時消失，得到的就是讀者看到的樣子。
    """
    placements: list = []
    for info in page.get_images(full=True):
        placements.extend(page.get_image_rects(info[0]))

    # 依閱讀順序排序：雙欄時先左欄由上而下，再右欄
    def order(r):
        col = 1 if (columns == 2 and r.x0 >= mid) else 0
        return (col, round(r.y0, 1), round(r.x0, 1))

    placements.sort(key=order)

    figures: list[FigureInfo] = []
    for i, r in enumerate(placements, start=1):
        clip = fitz.Rect(r.x0 - pad, r.y0 - pad, r.x1 + pad, r.y1 + pad) & page.rect
        if clip.is_empty:
            continue
        key = f"p{page_no}_f{i:02d}"
        path = out_dir / f"{key}.png"
        page.get_pixmap(dpi=dpi, clip=clip).save(path)
        figures.append(FigureInfo(
            key=key, file=str(path),
            bbox=[round(v, 1) for v in (r.x0, r.y0, r.x1, r.y1)],
            column=1 if (columns == 2 and r.x0 >= mid) else 0,
        ))
    return figures


def render(pdf_path: Path, root: Path, out_root: Path, dpi: int,
           skip_existing: bool) -> DocInfo:
    rel = pdf_path.relative_to(root).with_suffix("")
    out_dir = out_root / rel
    info = DocInfo(source=str(pdf_path), out_dir=str(out_dir), page_count=0,
                   route="error", digital_pages=0, scanned_pages=0)

    try:
        doc = fitz.open(pdf_path)
    except Exception as exc:
        # OneDrive 的「僅線上可用」佔位檔在這裡會失敗
        info.error = f"{type(exc).__name__}: {exc}"
        return info

    out_dir.mkdir(parents=True, exist_ok=True)
    info.page_count = doc.page_count
    text_layer: dict[str, list] = {}

    fig_dir = out_dir / "figures"
    for i, page in enumerate(doc, start=1):
        img_path = out_dir / f"p{i:02d}.png"
        text = page.get_text("text")
        chars = len(text.strip())
        columns, mid = detect_columns(page)

        if not (skip_existing and img_path.exists()):
            pix = page.get_pixmap(dpi=dpi)
            pix.save(img_path)
            w, h = pix.width, pix.height
        else:
            w = h = 0

        figures = []
        if page.get_images(full=True):
            fig_dir.mkdir(parents=True, exist_ok=True)
            figures = extract_figures(page, fig_dir, i, dpi, mid, columns)
        info.figure_count += len(figures)

        if chars >= TEXT_LAYER_MIN_CHARS:
            info.digital_pages += 1
            # 保留座標，之後可用來對齊 VLM 的版面判讀
            text_layer[str(i)] = page.get_text("blocks")
        else:
            info.scanned_pages += 1

        info.pages.append(PageInfo(i, str(img_path), w, h, chars, columns, figures))

    doc.close()

    if info.digital_pages and not info.scanned_pages:
        info.route = "digital"
    elif info.scanned_pages and not info.digital_pages:
        info.route = "scanned"
    else:
        info.route = "mixed"

    if text_layer:
        (out_dir / "text.json").write_text(
            json.dumps(text_layer, ensure_ascii=False, indent=2), encoding="utf-8")

    return info


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("input_dir", type=Path, help="含 PDF 的目錄（會遞迴掃描）")
    ap.add_argument("-o", "--out", type=Path, default=Path("out/ingest"))
    ap.add_argument("--dpi", type=int, default=300,
                    help="渲染解析度，預設 300（掃描件建議 300，再高只是變慢）")
    ap.add_argument("--skip-existing", action="store_true", help="已存在的頁面影像不重畫")
    args = ap.parse_args()

    root = args.input_dir.expanduser().resolve()
    if not root.is_dir():
        return sys.exit(f"找不到目錄：{root}")

    pdfs = sorted(p for p in root.rglob("*.pdf") if p.is_file())
    if not pdfs:
        return sys.exit(f"{root} 底下找不到任何 PDF")

    print(f"找到 {len(pdfs)} 份 PDF，輸出至 {args.out.resolve()}\n")
    args.out.mkdir(parents=True, exist_ok=True)

    docs: list[DocInfo] = []
    for n, pdf in enumerate(pdfs, start=1):
        try:
            info = render(pdf, root, args.out, args.dpi, args.skip_existing)
        except Exception:
            info = DocInfo(source=str(pdf), out_dir="", page_count=0, route="error",
                           digital_pages=0, scanned_pages=0,
                           error=traceback.format_exc(limit=1))
        docs.append(info)
        mark = {"digital": "文字", "scanned": "掃描", "mixed": "混合", "error": "失敗"}[info.route]
        cols = "/".join(str(p.columns) for p in info.pages) or "-"
        print(f"[{n:>3}/{len(pdfs)}] {mark}  {info.page_count:>2}頁  圖{info.figure_count:>3}  欄{cols}  "
              f"{pdf.relative_to(root)}" + (f"\n        ⚠ {info.error}" if info.error else ""))

    (args.out / "manifest.json").write_text(
        json.dumps([asdict(d) for d in docs], ensure_ascii=False, indent=2),
        encoding="utf-8")

    # ── 摘要：這份摘要決定管線要怎麼配置 ──────────────────────────
    by_route: dict[str, int] = {}
    for d in docs:
        by_route[d.route] = by_route.get(d.route, 0) + 1
    total_pages = sum(d.page_count for d in docs)

    total_figs = sum(d.figure_count for d in docs)
    print(f"\n{'=' * 56}")
    print(f"總計 {len(docs)} 份、{total_pages} 頁、{total_figs} 張圖")
    for route, label in [("digital", "原生數位（有文字圖層，免 OCR）"),
                         ("scanned", "掃描影像（需走 VLM 擷取）"),
                         ("mixed", "混合（需逐頁判斷）"),
                         ("error", "讀取失敗")]:
        if by_route.get(route):
            print(f"  {label:<28} {by_route[route]:>3} 份")

    if by_route.get("error"):
        print("\n⚠ 有檔案讀取失敗。若來源在 OneDrive，多半是「僅線上可用」的佔位檔：")
        print("  在檔案總管對資料夾按右鍵 →「一律保留在此裝置上」，等同步完成後重跑。")

    if by_route.get("digital"):
        print("\n💡 有原生數位 PDF —— 這些檔案的文字可無損取出（text.json），")
        print("   不需要 OCR，擷取正確率接近 100%，成本也低一個數量級。")
        print("   不要把它們當掃描件處理。")

    return 0


if __name__ == "__main__":
    sys.exit(main())
