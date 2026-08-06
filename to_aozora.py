"""ocr_book.py が出力したページ単位 JSON を青空文庫形式テキストへ変換する。

主な処理:
  - 行（word）を単位にページを再構成し、ルビを親文字に《》で結合
  - 行頭字下げを手がかりにした段落の再分割とページ跨ぎ段落の連結
  - 見出しの階層判定と青空文庫の見出し注記への変換
  - 「第○部」のローマ数字を PDF 内蔵テキスト層と突き合わせて補正
  - 目次・索引など段組ページの幾何的な行再構成
  - 表の罫囲み注記化、図版の切り出しと挿絵注記化

ページ走査とブロック抽出は book_ir.build_ir() に委譲し、このファイルは
IR ブロックから青空文庫テキストを組み立てる部分（emit_aozora）と CLI に専念する。
"""

import argparse
import re
from pathlib import Path

from book_ir import build_ir, crop_figures, fix_subtitle_dash, join_lines, normalize_text

# --------------------------------------------------------------------------
# 見出し
# --------------------------------------------------------------------------


def emit_heading(lines: list[str], level: str) -> list[str]:
    lines = [normalize_text(ln) for ln in lines]
    lines = [ln for ln in lines if ln]
    if not lines:
        return []

    # 章扉・部扉の副題のダーシュ補正は to_epub.py と共通のロジック（book_ir.fix_subtitle_dash）
    lines = fix_subtitle_dash(lines)

    if len(lines) == 1:
        return [f"{lines[0]}［＃「{lines[0]}」は{level}見出し］"]
    return [f"［＃ここから{level}見出し］", *lines, f"［＃ここで{level}見出し終わり］"]


# --------------------------------------------------------------------------
# 表
# --------------------------------------------------------------------------


def table_to_aozora(table: dict) -> list[str]:
    grid: dict[tuple[int, int], str] = {}
    for cell in table["cells"]:
        text = join_lines(cell["contents"].split("\n")) if cell["contents"] else ""
        grid[(cell["row"], cell["col"])] = normalize_text(text)

    out = ["［＃ここから罫囲み］"]
    for r in range(1, table["n_row"] + 1):
        row = [grid.get((r, c), "") for c in range(1, table["n_col"] + 1)]
        out.append("　".join(v for v in row if v))
    out.append("［＃ここで罫囲み終わり］")
    return out


# --------------------------------------------------------------------------
# IR → 青空文庫テキスト
# --------------------------------------------------------------------------

HEADER = """{title}
{subtitle}
{author}

-------------------------------------------------------
【テキスト中に現れる記号について】

《》：ルビ
（例）｜時《トキ》

｜：ルビの付く文字列の始まりを特定する記号
（例）｜必然性《アナンケ》

［＃］：入力者注　主に外字の説明や、傍点の位置の指定
（例）［＃改ページ］
-------------------------------------------------------

"""

FOOTER = """

底本：「時間を哲学する――思考のためのツールボックス」慶應義塾大学出版会
　　　2026（令和8）年4月25日初版第1刷発行
※このファイルは底本をスキャンした PDF を YomiToku 0.11.0 で OCR し、
　自動整形して青空文庫形式に変換したものです。
※ルビは字の大きさと位置から自動判定して付与しています。
※傍点・圏点は底本のスキャン画像から判別できないため再現していません。
※OCR 由来の誤認識が残っている可能性があります。
"""


def emit_aozora(blocks: list[dict], fig_dir_name: str) -> tuple[str, dict]:
    """IR ブロック列から青空文庫本文（HEADER/FOOTER を除く body）を組み立てる"""

    body: list[str] = []
    stats = {"段落": 0, "見出し": 0, "表": 0, "図版": 0}

    for b in blocks:
        kind = b["kind"]
        if kind == "para":
            text = normalize_text(join_lines(b["lines"]))
            if text:
                body.append("　" + text)
                stats["段落"] += 1
        elif kind == "raw":
            if b["text"]:
                body.append(b["text"])
        elif kind == "heading":
            if b["level"] == "大":
                body.append("［＃改ページ］")
            body.extend(emit_heading(b["lines"], b["level"]))
            stats["見出し"] += 1
        elif kind == "table":
            body.extend(table_to_aozora(b))
            stats["表"] += 1
        elif kind == "figure":
            body.append(f"［＃挿絵（{fig_dir_name}/{b['src']}）入る］")
            stats["図版"] += 1

    return "\n".join(body).lstrip("\n"), stats


# --------------------------------------------------------------------------
# 変換本体
# --------------------------------------------------------------------------


def convert(
    json_dir: str,
    pdf_path: str,
    out_path: str,
    fig_dir: str,
    dpi: int = 200,
    export_figures: bool = True,
    layout_pages: str = "",
    skip_pages: str = "",
):
    out_path, fig_dir_path = Path(out_path), Path(fig_dir)
    pdf_path = Path(pdf_path)

    blocks, ir_stats = build_ir(json_dir, pdf_path, layout_pages=layout_pages, skip_pages=skip_pages)
    body_text, stats = emit_aozora(blocks, fig_dir_path.name)
    stats["ルビ"] = ir_stats["ルビ"]
    stats["除去"] = ir_stats["除去"]

    if export_figures:
        fig_jobs = [
            {"page": b["page"], "box": b["box"], "name": b["src"]}
            for b in blocks
            if b["kind"] == "figure"
        ]
        crop_figures(pdf_path, fig_jobs, fig_dir_path, dpi)

    text = (
        HEADER.format(
            title="時間を哲学する",
            subtitle="思考のためのツールボックス",
            author="平井靖史　編",
        )
        + body_text
        + FOOTER
    )
    text = re.sub(r"\n{3,}", "\n\n", text)
    out_path.write_text(text, encoding="utf-8")

    print(f"出力: {out_path}")
    print("  " + " / ".join(f"{k} {v}" for k, v in stats.items()))
    print(f"  文字数: {len(text):,}")


def main():
    parser = argparse.ArgumentParser(description="OCR JSON を青空文庫形式へ変換する")
    parser.add_argument("-j", "--json-dir", default="ocr_json", help="OCR JSON ディレクトリ")
    parser.add_argument("-p", "--pdf", required=True, help="元 PDF（図版切り出し・部番号突合せ用）")
    parser.add_argument("-o", "--output", required=True, help="出力テキストファイル")
    parser.add_argument("--fig-dir", default="aozora_fig", help="図版の出力先")
    parser.add_argument("--dpi", type=int, default=200, help="OCR 時と同じ DPI")
    parser.add_argument("--no-figures", action="store_true", help="図版を切り出さない")
    parser.add_argument(
        "--layout-pages", default="", help="段組を幾何的に復元するページ（例: 2-3,287-295）"
    )
    parser.add_argument("--skip-pages", default="", help="出力しないページ（例: 1,301）")
    args = parser.parse_args()

    convert(
        json_dir=args.json_dir,
        pdf_path=args.pdf,
        out_path=args.output,
        fig_dir=args.fig_dir,
        dpi=args.dpi,
        export_figures=not args.no_figures,
        layout_pages=args.layout_pages,
        skip_pages=args.skip_pages,
    )


if __name__ == "__main__":
    main()
