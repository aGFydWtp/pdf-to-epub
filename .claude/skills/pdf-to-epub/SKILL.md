---
name: pdf-to-epub
description: 書籍をスキャンした PDF を OCR して日本語縦書き EPUB3 に変換したいときに使う。「この PDF を EPUB にして」「電子書籍化して」「本を OCR して EPUB にしたい」のように、書籍 PDF → EPUB 変換を頼まれたときに発動する。
---

# PDF → EPUB 変換

書籍 PDF を yomitoku で OCR し、章ごとに OCR と LLM 校正をパイプライン並列実行してから、
日本語縦書き・階層目次付きの EPUB3 を生成するワークフロー。

## 準備（パイプラインの取得）

パイプライン本体は https://github.com/aGFydWtp/pdf-to-epub にある。
ローカルにコードがなくても、キャッシュディレクトリへ取得して実行する。
まず以下を実行して `$PIPE` を用意すること:

```bash
PIPE="${XDG_CACHE_HOME:-$HOME/.cache}/pdf-to-epub"
if [ -d "$PIPE/.git" ]; then
  git -C "$PIPE" pull --ff-only
else
  git clone --depth 1 https://github.com/aGFydWtp/pdf-to-epub "$PIPE"
fi
```

- 以降のコマンドはすべて `uv run --project "$PIPE" python "$PIPE/<script>.py" ...` の形で
  実行する（素の `python` は使わない）
- 初回実行時は uv が `$PIPE/.venv` を作り yomitoku・torch 等（数 GB）を導入するため
  時間がかかる。2 回目以降はキャッシュされる
- 開発中のローカルチェックアウト（例: `~/Documents/pdf-to-epub`）で作業している場合は、
  clone せず `PIPE=そのディレクトリ` としてよい

## 前提

- **作業ディレクトリは書籍ごとに用意し、PDF を置いた場所で実行する**。
  `ocr_json/`・`proofread_cache/`・EPUB などの生成物はカレントディレクトリにできる
- OCR は 1 ページ数秒〜十数秒（CPU/MPS/CUDA）。ページ単位で JSON にキャッシュされ、
  既存 JSON があれば再 OCR しない
- 校正は `claude -p --strict-mcp-config --model <model>` をサブプロセス起動して行う。
  **課金が発生する**ので、本文全体を校正する前に必ずユーザーに実行してよいか確認すること。
  確認が取れない場面では `--dry-run` に留める
- 本のタイトル・著者は PDF から自動推定しない。**必ず引数（`--title` / `--author`）で
  ユーザーから受け取る**か、確認を取ってから渡す

## 手順

### 1. PDF の下見（ページ数・目次ページの確認）

```bash
uv run --project "$PIPE" python -c "import pypdfium2; print(len(pypdfium2.PdfDocument('BOOK.pdf')))"
```

目次のページ番号がわからない場合は、最初の 5〜10 ページだけ試しに OCR して
`ocr_json/` を目視して確認する。PDF のファイル名にスペースが入ることが多いので必ずクォートする。

```bash
uv run --project "$PIPE" python "$PIPE/ocr_book.py" "BOOK.pdf" -o ocr_json --first 1 --last 10
```

目次ページ（例: 2-3）と、表紙・奥付など本文から除きたいページ（例: 1,301）の
おおよその見当をつける。

### 2. pdf_to_epub.py の実行

まず必ず `--dry-run` で章境界の確定までを確認する（OCR 本実行・LLM 校正・EPUB 生成は
まだ行わない。課金なし）。

```bash
uv run --project "$PIPE" python "$PIPE/pdf_to_epub.py" \
  -p "BOOK.pdf" \
  --toc-pages 2-3 \
  --layout-pages 2-3,287-295 \
  --skip-pages 1,301 \
  --dry-run
```

引数の決め方:

- `--toc-pages`: 印刷目次があるページ範囲。目次だけを先に OCR して章境界を割り出す
- `--layout-pages`: 目次・索引など、段組みで幾何的に行を復元すべきページ
  （`--toc-pages` と重なってよい。索引ページなどもここに含める）
- `--skip-pages`: 表紙・白紙・奥付など、本文として出力しない PDF ページ
- `--page-offset`: 「書籍に印刷されたページ番号 + offset = PDF のページ番号」となる整数。
  省略すると先頭章のタイトルを手がかりに自動推定を試みるが、印刷目次の OCR 精度や、
  本文中に目次と同じ見出し文字列が別の意味（前書きでの紹介文など）で再度現れる本では
  誤推定しうる。**dry-run の出力（章境界の一覧）を人間が確認し、開始ページがおかしければ
  `--page-offset` を明示的に指定して再実行する**こと。自動推定に頼り切らない
- `--title` / `--author` / `--publisher`: 必ずユーザーに確認してから指定する
- `--horizontal`: 横書きにしたい場合のみ指定（既定は縦書き）
- `--proofread-workers`: 章の並列校正数（既定 2。4〜5 が実績値）

dry-run の章境界一覧に問題がなければ、校正ありで本実行する（**課金が発生するので
事前にユーザーへ確認**）。

```bash
uv run --project "$PIPE" python "$PIPE/pdf_to_epub.py" \
  -p "BOOK.pdf" \
  --toc-pages 2-3 \
  --layout-pages 2-3,287-295 \
  --skip-pages 1,301 \
  --page-offset 12 \
  --title "書名" --author "著者名" \
  -o "書名.epub"
```

校正を伴わずにまず OCR と EPUB 化だけ済ませたい場合は、`ocr_book.py` で全ページ OCR した
うえで `to_epub.py` を直接使う方法もある（校正機能を使わない最小構成）。

```bash
uv run --project "$PIPE" python "$PIPE/ocr_book.py" "BOOK.pdf" -o ocr_json
uv run --project "$PIPE" python "$PIPE/to_epub.py" -j ocr_json -p "BOOK.pdf" -o "書名.epub" \
  --title "書名" --author "著者名" \
  --layout-pages 2-3,287-295 --skip-pages 1,301
```

`to_epub.py` は生成のたびに自己検証（XHTML/OPF/nav の整形式チェック、
manifest と zip エントリの突合、spine の突合）を内蔵しており、末尾に
`self-check: PASS` / `FAIL` を出力する。FAIL の場合は原因を報告して停止する。

### 3. epubcheck での検証

```bash
which epubcheck && epubcheck "書名.epub"
```

`epubcheck` が入っていなければ `brew install epubcheck` が必要な旨をユーザーに伝える
（勝手にインストールしない）。エラーが出た場合は該当ファイル（`OEBPS/text/chNNN.xhtml` など）
を zip から取り出して内容を確認し、`to_epub.py` 側の生成ロジックを直す。

### 4. 中断・再開

- OCR: `ocr_json/pNNNN.json` がページ単位のキャッシュ。存在すれば再 OCR しない。
  途中で止まっても同じコマンドを再実行すれば未処理ページだけ進む
- 校正: `--proofread-cache`（既定 `proofread_cache/chNNN/`）に章・チャンク単位で
  `claude -p` の応答がキャッシュされる。同じコマンドを再実行すればキャッシュ済み
  チャンクは再送信しない。チャンクが失敗（認証切れ・タイムアウト等）した場合は
  キャッシュされず、パイプラインはエラーで中断する。原因（例: `claude /login`）を
  解消して再実行すればよい
- 適用済みの校正結果は `--fixes-output`（既定 `claudedocs/proofread_fixes.json`）に
  保存される。EPUB だけ作り直したい場合は、この fixes ファイルを
  `to_epub.py --proofread-fixes claudedocs/proofread_fixes.json` に渡せば
  OCR・校正をやり直さずに EPUB だけ再生成できる
- EPUB 自体は毎回まるごと作り直される（差分更新はしない）
- 中断（Ctrl-C）した場合、新規チャンクの投入は即座に止まるが、**そのとき実行中の
  校正チャンク（`claude -p` サブプロセス）自体の強制終了までは行わない**ため、
  実行中のチャンクの応答を待ってからプロセスが終了することがある。すぐに終了させたい
  場合は、ターミナルごと閉じるか `pkill -f "claude -p"` で該当プロセスを止めること

## 既知の限界

- 印刷目次の OCR は精度が不安定なため、章境界の自動推定は目安に留める。
  必ず dry-run の出力を人間が確認してから本実行すること
- 見出しの階層（部/大見出し/中見出し）は正規表現ベースの判定であり、
  OCR がタイトル行の役割（`section_headings`）を取りこぼすと、その見出しは
  章として独立せず直前の章に取り込まれる
- 校正 fixes は「ページ番号 + before 文字列の一意性」で適用される。同一ページに
  同じ文字列が複数ある場合は誤適用を避けて自動スキップされる（ログに理由が出る）
