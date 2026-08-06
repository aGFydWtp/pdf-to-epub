# pdf-to-epub

書籍をスキャンした PDF を yomitoku で OCR し、章ごとに LLM 校正（`claude -p`）を
パイプライン並列で回してから、日本語縦書き・階層目次付きの EPUB3 を生成するツール群。

## 構成

| ファイル | 役割 |
|---|---|
| `ocr_book.py` | PDF をページ単位で OCR して JSON にキャッシュ |
| `book_ir.py` | OCR JSON → 中間表現（IR）。ルビ結合・見出し判定・図版切り出しの共有レイヤー |
| `to_epub.py` | IR → EPUB3（縦書き・nav.xhtml 階層目次・ruby/table/img・self-check 内蔵） |
| `to_aozora.py` | IR → 青空文庫形式テキスト（任意出力） |
| `llm_proofread.py` | `claude -p` による OCR 誤認識の校正（チャンク並列・キャッシュ・検証付き適用） |
| `pdf_to_epub.py` | 目次先行 OCR → 章境界確定 → 章ごとに OCR+校正を並列 → EPUB 生成のオーケストレーター |

## 使い方

書籍ごとの作業ディレクトリ（PDF を置いた場所）から実行する。キャッシュや出力はカレントに生成される。

```bash
PIPE=~/Documents/pdf-to-epub

# 1. dry-run で章境界を確認（課金なし）
uv run --project "$PIPE" python "$PIPE/pdf_to_epub.py" -p "BOOK.pdf" \
  --toc-pages 2-3 --dry-run

# 2. 本実行（LLM 校正に claude CLI の課金が発生）
uv run --project "$PIPE" python "$PIPE/pdf_to_epub.py" -p "BOOK.pdf" \
  --toc-pages 2-3 --page-offset 12 --title "書名" --author "著者" -o "書名.epub"

# 3. 検証
epubcheck "書名.epub"
```

詳細な手順・引数の決め方・中断再開の方法は
[.claude/skills/pdf-to-epub/SKILL.md](.claude/skills/pdf-to-epub/SKILL.md) を参照。
