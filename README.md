# pdf-to-epub

書籍をスキャンした PDF を yomitoku で OCR し、章ごとに LLM 校正（`claude -p`）を
パイプライン並列で回してから、日本語縦書き・階層目次付きの EPUB3 を生成するツール群。

## 構成

| ファイル | 役割 |
|---|---|
| `ocr_book.py` | PDF をページ単位で OCR して JSON にキャッシュ |
| `book_ir.py` | OCR JSON → 中間表現（IR）。ルビ結合・見出し判定・図版切り出しの共有レイヤー |
| `to_epub.py` | IR → EPUB3（縦書き・nav.xhtml 階層目次＋EPUB2 互換 toc.ncx・ruby/table/img・self-check 内蔵） |
| `to_aozora.py` | IR → 青空文庫形式テキスト（任意出力） |
| `llm_proofread.py` | `claude -p` による OCR 誤認識の校正（チャンク並列・キャッシュ・検証付き適用） |
| `book_meta.py` | 書誌 API（openBD / 国立国会図書館サーチ）から書名・著者・出版社を取得（API キー不要） |
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
  --toc-pages 2-3 --page-offset 12 --isbn "4-06-149293-4" -o "書名.epub"

# 3. 検証
epubcheck "書名.epub"
```

書誌は ISBN から引ける。ISBN が分からないときは書名で候補を検索する（同名異書があるので選ぶのは人間）。

```bash
uv run --project "$PIPE" python "$PIPE/book_meta.py" --isbn "4-06-149293-4"
uv run --project "$PIPE" python "$PIPE/book_meta.py" --title "時間を哲学する"
```

### 主なオプション

- `--isbn`: 書誌 API から書名・著者・出版社を補完する（10 桁・13 桁、ハイフン有無どちらでも可）。
  見つからない場合は警告だけ出して `--title` などの指定値にフォールバックする
- `--title` / `--author` / `--publisher`: 書誌情報。API より常に優先される。
  `--title` は `--dry-run` 以外で必須（`--isbn` で取得できた場合を除く）
- `--toc-pages` / `--page-offset`: 印刷目次のページ範囲と、書籍ページ番号→PDF ページ番号の補正。
  `--toc-pages` は章境界の割り出しと、そのページを本文から除外する根拠の両方に使う
  （EPUB の目次は nav.xhtml が担うため。範囲がずれると印刷目次が本文へ二重掲載される）
- `--horizontal`: 横書き（`horizontal-tb`）で出力する。既定は縦書き（`vertical-rl`）。
  縦書きでは spine に `page-progression-direction="rtl"` が付き、
  `primary-writing-mode` メタが `vertical-rl` になる。`--horizontal` 指定時は
  `page-progression-direction` を付けず（既定の `ltr`）、メタは `horizontal-lr` になる
- `--dry-run`: 章境界の確認まで（LLM 校正を行わないので課金なし）

`--horizontal` と `--toc-pages` は `pdf_to_epub.py` と `to_epub.py` の両方で同じ意味で使える
（`to_epub.py` を直接使って本文だけ組み直すときも `--toc-pages` を付けること）。

詳細な手順・引数の決め方・中断再開の方法は
[.claude/skills/pdf-to-epub/SKILL.md](.claude/skills/pdf-to-epub/SKILL.md) を参照。
