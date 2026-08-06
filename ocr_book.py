"""PDF を1ページずつ YomiToku で OCR し、ページ単位の JSON として保存する。

ページごとにレンダリング・保存するため、メモリ使用量を抑えられ、
途中で中断しても未処理ページだけを再実行できる（レジューム対応）。
"""

import argparse
import json
import time
from pathlib import Path

import numpy as np
import pypdfium2
from yomitoku import DocumentAnalyzer


def detect_device() -> str:
    """利用可能なデバイスを自動検出する"""
    import torch

    if torch.cuda.is_available():
        return "cuda"
    if torch.backends.mps.is_available():
        return "mps"
    return "cpu"


def render_page(doc: pypdfium2.PdfDocument, index: int, dpi: int) -> np.ndarray:
    """PDF の指定ページを BGR の ndarray としてレンダリングする"""
    pil = doc[index].render(scale=dpi / 72).to_pil().convert("RGB")
    return np.array(pil)[:, :, ::-1]


def ocr_pdf(
    pdf_path: str,
    out_dir: str,
    dpi: int = 200,
    device: str | None = None,
    first: int = 1,
    last: int | None = None,
    overwrite: bool = False,
):
    pdf_path = Path(pdf_path)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    if device is None:
        device = detect_device()

    doc = pypdfium2.PdfDocument(str(pdf_path))
    n_pages = len(doc)
    last = n_pages if last is None else min(last, n_pages)
    targets = [
        i
        for i in range(first - 1, last)
        if overwrite or not (out_dir / f"p{i + 1:04d}.json").exists()
    ]

    print(f"PDF: {pdf_path.name} ({n_pages} ページ)")
    print(f"対象: {len(targets)} ページ / デバイス: {device} / DPI: {dpi}")

    if not targets:
        print("処理対象のページはありません。")
        return

    analyzer = DocumentAnalyzer(visualize=False, device=device, configs={})

    started = time.time()
    for n, i in enumerate(targets, 1):
        page_no = i + 1
        img = render_page(doc, i, dpi)
        results, _, _ = analyzer(img)

        payload = json.loads(results.model_dump_json())
        payload["page"] = page_no
        payload["image_size"] = {"h": int(img.shape[0]), "w": int(img.shape[1])}
        (out_dir / f"p{page_no:04d}.json").write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8"
        )

        elapsed = time.time() - started
        eta = elapsed / n * (len(targets) - n)
        print(
            f"[{n}/{len(targets)}] p{page_no} 完了 "
            f"({elapsed / n:.1f}s/page, 残り約 {eta / 60:.1f}分)",
            flush=True,
        )

    print(f"完了: {out_dir}/ に {len(targets)} ページ分の JSON を保存しました。")


def main():
    parser = argparse.ArgumentParser(description="PDF をページ単位で OCR する")
    parser.add_argument("input", help="入力 PDF")
    parser.add_argument("-o", "--output", default="ocr_json", help="JSON 出力ディレクトリ")
    parser.add_argument("--dpi", type=int, default=200, help="レンダリング DPI")
    parser.add_argument("-d", "--device", default=None, choices=["cuda", "cpu", "mps"])
    parser.add_argument("--first", type=int, default=1, help="開始ページ（1始まり）")
    parser.add_argument("--last", type=int, default=None, help="終了ページ（1始まり）")
    parser.add_argument("--overwrite", action="store_true", help="既存 JSON も再処理する")
    args = parser.parse_args()

    ocr_pdf(
        pdf_path=args.input,
        out_dir=args.output,
        dpi=args.dpi,
        device=args.device,
        first=args.first,
        last=args.last,
        overwrite=args.overwrite,
    )


if __name__ == "__main__":
    main()
