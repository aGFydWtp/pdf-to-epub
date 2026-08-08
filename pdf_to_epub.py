"""PDF から EPUB を生成するオーケストレーター。

処理の流れ:
  1. 目次ページを先に OCR する（既存 JSON があればスキップ）
  2. 目次を幾何再構成し、(階層, タイトル, 書籍ページ番号) を抽出する
  3. 書籍ページ番号→PDF ページ番号のオフセットを確定する
     （--page-offset で明示指定するか、先頭章のページを数ページ OCR して照合する）
  4. 章ごとに OCR を逐次実行し、1 章分の OCR が完了するたびに
     その章の LLM 校正ジョブを ThreadPoolExecutor に投入する
     （OCR は device を占有するため逐次、校正は章単位で重ねて並列実行する）
  5. 校正結果（fixes）を IR に適用し、to_epub.py で EPUB を生成する
     （--aozora-out を指定すれば青空文庫 txt も併せて出力する）

--dry-run を指定すると、目次 OCR〜章境界確定までを実行して計画を表示するだけで、
本文 OCR・LLM 校正（claude CLI 呼び出し）・EPUB 生成は行わない。
"""

import argparse
import json
import os
import re
import shutil
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import book_meta
import ocr_book
import to_aozora
import to_epub
from book_ir import DASHES, attach_ruby, build_ir, build_lines, normalize_text, parse_pages, rows_of
from llm_proofread import chunk_lines, run_chunk, validate


def load_json_or_die(path: Path) -> dict:
    """OCR JSON を読み込む。壊れている場合はファイル名入りのエラーで即終了する"""

    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError, OSError) as e:
        raise SystemExit(f"OCR JSON の読み込みに失敗しました: {path}: {e}") from e

# --------------------------------------------------------------------------
# 目次パース
# --------------------------------------------------------------------------

BU_TOC_RE = re.compile(r"^第\s*([0-9０-９IVXⅠ-Ⅴ]{1,4})\s*部")
CH_TOC_RE = re.compile(r"^第\s*([0-9０-９]{1,3})\s*章")
COL_TOC_RE = re.compile(r"^Column\s*\d+", re.IGNORECASE)
TRAILING_DIGITS_RE = re.compile(r"(\d+)\s*$")
TRAILING_ROMAN_RE = re.compile(r"([ivxlcIVXLC]{1,6})\s*$")


def roman_to_int(s: str) -> int | None:
    """小文字ローマ数字（前付けページ番号）を整数に変換する"""

    vals = {"i": 1, "v": 5, "x": 10, "l": 50, "c": 100}
    s = s.lower()
    if not s or any(ch not in vals for ch in s):
        return None
    total, prev = 0, 0
    for ch in reversed(s):
        v = vals[ch]
        total += -v if v < prev else v
        prev = max(prev, v)
    return total or None


def parse_toc_row(row: str) -> dict | None:
    """目次の 1 行から (階層, タイトル, 書籍ページ番号) を抽出する。

    OCR 由来のノイズが多いページなので、部/章/Column に一致する行だけを対象にし、
    それ以外（サブタイトルの折り返し行や中見出しなど）は無視する。
    """

    row = row.strip()
    if not row:
        return None
    m_bu, m_ch, m_col = BU_TOC_RE.match(row), CH_TOC_RE.match(row), COL_TOC_RE.match(row)
    if not (m_bu or m_ch or m_col):
        return None
    level = "部" if m_bu else "大"

    m_page = TRAILING_DIGITS_RE.search(row)
    if m_page:
        return {"level": level, "title": row, "page_num": int(m_page.group(1)), "is_roman": False}
    m_roman = TRAILING_ROMAN_RE.search(row)
    if m_roman:
        return {"level": level, "title": row, "page_num": roman_to_int(m_roman.group(1)), "is_roman": True}
    return {"level": level, "title": row, "page_num": None, "is_roman": False}


def extract_toc_entries(json_dir: Path, toc_pages: str) -> list[dict]:
    """目次ページから見出しエントリを抽出する。

    タイトルが長くて折り返された場合、ページ番号は続きの行（「――観点による区別73」
    のような副題＋ページ番号の行）にしか現れないことが多い。そこで、ページ番号が
    取れなかった直近のエントリを覚えておき、後続の非見出し行から数字が拾えたら
    そこで補完する。
    """

    entries: list[dict] = []
    pending_idx: int | None = None
    for page_no in sorted(parse_pages(toc_pages)):
        path = json_dir / f"p{page_no:04d}.json"
        if not path.exists():
            raise SystemExit(f"目次ページ p{page_no} の OCR JSON がありません: {path}")
        data = load_json_or_die(path)
        lines = attach_ruby(build_lines(data["words"]))
        for row in rows_of(lines, data["image_size"]["w"]):
            entry = parse_toc_row(row)
            if entry:
                entries.append(entry)
                pending_idx = len(entries) - 1 if entry["page_num"] is None else None
                continue
            if pending_idx is not None:
                m_page = TRAILING_DIGITS_RE.search(row)
                if m_page:
                    entries[pending_idx]["page_num"] = int(m_page.group(1))
                    entries[pending_idx]["is_roman"] = False
                    pending_idx = None
    return entries


# --------------------------------------------------------------------------
# OCR（キャッシュ経由）
# --------------------------------------------------------------------------


def ensure_page_ocr(pdf_doc, json_dir: Path, page_no: int, dpi: int, device: str | None, analyzer_holder: list):
    """該当ページの OCR JSON を返す。既存があれば読むだけで、なければその場で OCR する。

    device 上で動くモデルは analyzer_holder に一度だけ生成して使い回す
    （目次ページだけしか使わない --dry-run では一度も生成されない）。
    JSON の書き込みは同一ディレクトリに一時ファイルを作ってから os.replace() で
    原子的にリネームする（途中終了で壊れた不完全な JSON が残らないようにするため）。
    """

    path = json_dir / f"p{page_no:04d}.json"
    if path.exists():
        return load_json_or_die(path)

    if analyzer_holder[0] is None:
        from yomitoku import DocumentAnalyzer

        analyzer_holder[0] = DocumentAnalyzer(
            visualize=False, device=device or ocr_book.detect_device(), configs={}
        )
    img = ocr_book.render_page(pdf_doc, page_no - 1, dpi)
    results, _, _ = analyzer_holder[0](img)
    payload = json.loads(results.model_dump_json())
    payload["page"] = page_no
    payload["image_size"] = {"h": int(img.shape[0]), "w": int(img.shape[1])}

    json_dir.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=json_dir, prefix=f"p{page_no:04d}.", suffix=".json.tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(json.dumps(payload, ensure_ascii=False))
        os.replace(tmp_name, path)
    except BaseException:
        Path(tmp_name).unlink(missing_ok=True)
        raise
    return payload


# --------------------------------------------------------------------------
# ページオフセットの決定
# --------------------------------------------------------------------------


def resolve_offset(
    entries: list[dict],
    pdf_doc,
    json_dir: Path,
    dpi: int,
    device: str | None,
    explicit_offset: int | None,
    analyzer_holder: list,
    toc_page_set: set[int],
    max_probe: int = 60,
) -> int:
    """書籍ページ番号 + offset = PDF ページ番号 となる offset を決める。

    --page-offset が指定されていればそれを使う。未指定なら、算用数字ページを持つ
    先頭の目次エントリについて、offset の候補を総当たりでプローブし、対応する
    PDF ページの OCR テキストにタイトルの特徴部分が含まれるかどうかで照合する
    （二分探索ではなく単純な線形探索の簡易版）。
    """

    if explicit_offset is not None:
        return explicit_offset

    arabic = [e for e in entries if e["page_num"] is not None and not e["is_roman"]]
    if not arabic:
        raise SystemExit(
            "目次から算用数字のページ番号を抽出できませんでした。--page-offset を指定してください。"
        )
    target = min(arabic, key=lambda e: e["page_num"])

    # 見出し番号の接頭辞・末尾に貼り付いたページ番号・OCR ノイズの残り数字を落として
    # 「本文にもほぼ同じ形で現れるはずの説明部分」だけを照合キーワードにする。
    # 目次では装飾のダッシュで副題と結合されているが、本文の見出しでは別行（＝連結
    # されない）ことが多いため、ダッシュより前の最初の文字列だけを使う。
    title_desc = re.sub(r"^第\s*[0-9０-９IVXⅠ-Ⅴ]{1,4}\s*[部章]|^Column\s*\d+", "", target["title"], flags=re.IGNORECASE)
    title_desc = TRAILING_DIGITS_RE.sub("", title_desc)
    title_desc = re.sub(r"^[0-9０-９\s]+", "", title_desc)
    m_needle = re.match(f"[^{re.escape(DASHES)}\\s]+", title_desc)
    needle = normalize_text(m_needle.group(0)) if m_needle else ""
    if len(needle) < 2:
        raise SystemExit(
            f"目次エントリ「{target['title']}」のタイトルが短く自動照合できません。--page-offset を指定してください。"
        )

    n_pages = len(pdf_doc)
    for offset in range(0, max_probe):
        pdf_page = target["page_num"] + offset
        if not (1 <= pdf_page <= n_pages):
            continue
        if pdf_page in toc_page_set:
            # 目次ページ自身にはどの見出しの文字列も載っているので、照合対象から除く
            continue
        data = ensure_page_ocr(pdf_doc, json_dir, pdf_page, dpi, device, analyzer_holder)
        text = normalize_text("".join(w["content"] for w in data["words"]))
        if needle in text:
            print(
                f"ページオフセット自動推定: {offset}"
                f"（書籍ページ {target['page_num']} → PDF p{pdf_page}、「{target['title']}」で照合）",
                flush=True,
            )
            return offset

    raise SystemExit(
        f"ページオフセットを自動推定できませんでした（「{target['title']}」を"
        f" PDF p1〜p{max_probe} で照合できず）。--page-offset を指定してください。"
    )


# --------------------------------------------------------------------------
# 章境界の確定
# --------------------------------------------------------------------------


def build_chapter_plan(entries: list[dict], offset: int, n_pdf_pages: int) -> list[dict]:
    """目次の部/大見出しエントリから章ごとの PDF ページ範囲を確定する"""

    chapters = sorted(
        (e for e in entries if e["page_num"] is not None and not e["is_roman"]),
        key=lambda e: e["page_num"],
    )
    plan = []
    for i, e in enumerate(chapters):
        start = e["page_num"] + offset
        end = chapters[i + 1]["page_num"] + offset - 1 if i + 1 < len(chapters) else n_pdf_pages
        end = min(end, n_pdf_pages)
        if start < 1 or start > n_pdf_pages or start > end:
            continue
        plan.append({"title": e["title"], "level": e["level"], "start": start, "end": end})
    return plan


# --------------------------------------------------------------------------
# 章単位の校正
# --------------------------------------------------------------------------


def build_flat_lines(blocks: list[dict], page_range: tuple[int, int]) -> tuple[list[str], list[int]]:
    """章の IR ブロックから校正用の行リストと、各行の元ページ番号を作る。

    表・図版は校正対象外（本文行の校正のみを行う）。
    """

    lines: list[str] = []
    pages: list[int] = []
    lo, hi = page_range
    for b in blocks:
        if not (lo <= b["page"] <= hi):
            continue
        if b["kind"] in ("heading", "para"):
            # para は行ごとに実ページが異なりうる（book_ir.build_ir の "pages"）。
            # これが最終 IR の apply_fixes 側の by_page 構築とも対になっており、
            # 行の実ページを揃えることで fix の取りこぼしを防ぐ
            line_pages = b.get("pages") or [b["page"]] * len(b["lines"])
            for i, line in enumerate(b["lines"]):
                lines.append(line)
                pages.append(line_pages[i] if i < len(line_pages) else b["page"])
        elif b["kind"] == "raw":
            lines.append(b["text"])
            pages.append(b["page"])
    return lines, pages


def proofread_chapter(
    blocks: list[dict],
    page_range: tuple[int, int],
    cache_dir: Path,
    model: str,
    max_chars: int,
    max_lines: int,
    timeout: int,
) -> list[dict]:
    """章 1 つ分の校正を行い、fixes（{"page","before","after","why"}）のリストを返す。

    ブロック分割は章単位 OCR の都合で最終 IR と完全には一致しないことがあるため、
    fixes は行番号ではなく「ページ番号 + before の一意性」で表現する
    （to_epub.apply_fixes 側の適用ロジックと対になる）。
    """

    lines, pages = build_flat_lines(blocks, page_range)
    if not lines:
        return []

    cache_dir.mkdir(parents=True, exist_ok=True)
    chunks = chunk_lines(lines, max_chars, max_lines)
    fixes: list[dict] = []
    for ci, (a, b) in enumerate(chunks):
        try:
            _, edits = run_chunk((ci, lines[a:b], a, model, cache_dir, timeout))
        except FileNotFoundError as e:
            raise SystemExit(
                "claude CLI が見つかりません（校正には `claude` コマンドが PATH 上に必要です）。"
                f" 詳細: {e}"
            ) from e
        # run_chunk は失敗（rc≠0・タイムアウト）を空リストで返しキャッシュも書かない。
        # 「修正 0 件」と区別が付かないため、キャッシュ未生成なら失敗として中断する
        if not (cache_dir / f"c{ci:04d}.json").exists():
            raise RuntimeError(
                f"校正チャンク {cache_dir.name}/c{ci:04d} が失敗しました"
                "（claude CLI の認証切れ・タイムアウト等）。原因を解消して再実行すれば"
                "キャッシュ済みチャンクはスキップされます。"
            )
        for edit in edits:
            ok, reason, pos = validate(edit, lines)
            if not ok:
                continue
            flat_idx = edit["line"] - 1
            fixes.append(
                {
                    "page": pages[flat_idx],
                    "before": edit["before"],
                    "after": edit["after"],
                    "why": edit.get("why", ""),
                }
            )
            # 後続チャンクの整合性のため、検証用の行自体も更新しておく
            line = lines[flat_idx]
            lines[flat_idx] = line[:pos] + edit["after"] + line[pos + len(edit["before"]) :]
    return fixes


# --------------------------------------------------------------------------
# オーケストレーション本体
# --------------------------------------------------------------------------


def run(args):
    import pypdfium2

    pdf_path = Path(args.pdf)
    json_dir = Path(args.json_dir)
    pdf_doc = pypdfium2.PdfDocument(str(pdf_path))
    n_pages = len(pdf_doc)
    analyzer_holder = [None]

    print(f"PDF: {pdf_path.name} ({n_pages} ページ)", flush=True)

    # 1. 目次 OCR（既存 JSON があればスキップ）
    toc_pages = sorted(parse_pages(args.toc_pages))
    for p in toc_pages:
        ensure_page_ocr(pdf_doc, json_dir, p, args.dpi, args.device, analyzer_holder)
    print(f"目次 OCR: p{toc_pages[0]}〜p{toc_pages[-1]}（{len(toc_pages)} ページ）確認済み", flush=True)

    # 2. 目次パース
    entries = extract_toc_entries(json_dir, args.toc_pages)
    print(f"\n目次エントリ: {len(entries)} 件", flush=True)
    for e in entries:
        tag = "roman" if e["is_roman"] else "arabic"
        print(f"  [{e['level']}] {e['title']!r} page={e['page_num']}({tag})", flush=True)

    # 3. ページオフセット
    offset = resolve_offset(
        entries, pdf_doc, json_dir, args.dpi, args.device, args.page_offset, analyzer_holder,
        toc_page_set=set(toc_pages),
    )

    # 4. 章境界確定
    plan = build_chapter_plan(entries, offset, n_pages)
    print(f"\n章境界: {len(plan)} 章（オフセット {offset}）", flush=True)
    for i, c in enumerate(plan):
        print(f"  [{i}][{c['level']}] {c['title']!r}: PDF p{c['start']}〜p{c['end']} ({c['end'] - c['start'] + 1} ページ)", flush=True)

    if args.dry_run:
        print("\n--dry-run のため、本文 OCR・LLM 校正・EPUB 生成はスキップしました。", flush=True)
        return

    if shutil.which("claude") is None:
        raise SystemExit(
            "claude CLI が見つかりません。校正には `claude` コマンドが PATH 上に必要です"
            "（インストールされていれば `claude --version` で確認できます）。"
            " OCR だけ済ませたい場合は --dry-run を使うか、to_epub.py を直接使ってください。"
        )

    # 5. 章単位パイプライン: OCR は逐次、校正は章ごとに ThreadPoolExecutor へ投入
    proofread_cache_root = Path(args.proofread_cache)
    fixes_all: list[dict] = []
    print(f"\n章単位パイプライン開始（校正並列数 {args.proofread_workers}）", flush=True)
    pool = ThreadPoolExecutor(max_workers=args.proofread_workers)
    try:
        futures = []
        for i, c in enumerate(plan):
            n = c["end"] - c["start"] + 1
            print(f"[章 {i + 1}/{len(plan)}] 「{c['title']}」 OCR {n} ページ...", flush=True)
            for k, pn in enumerate(range(c["start"], c["end"] + 1), 1):
                ensure_page_ocr(pdf_doc, json_dir, pn, args.dpi, args.device, analyzer_holder)
                print(f"  OCR {k}/{n} ページ完了", flush=True)

            blocks, _ = build_ir(
                str(json_dir), str(pdf_path),
                layout_pages=args.layout_pages, skip_pages=args.skip_pages,
                page_filter=set(range(c["start"], c["end"] + 1)),
            )
            cache_dir = proofread_cache_root / f"ch{i:03d}"
            futures.append(
                pool.submit(
                    proofread_chapter, blocks, (c["start"], c["end"]), cache_dir,
                    args.model, args.max_chars, args.max_lines, args.timeout,
                )
            )

        for i, fut in enumerate(futures):
            fixes = fut.result()
            fixes_all.extend(fixes)
            print(f"[章 {i + 1}/{len(plan)}] 校正 done（{len(fixes)} 件）", flush=True)
    except KeyboardInterrupt:
        # 実行中の claude -p サブプロセス自体を即座に kill するところまでは行わないが、
        # 少なくとも「新規チャンクの投入」は止め、待機中の future はキャンセルする
        print("\n中断を検知しました。実行中の校正チャンクの終了を待ってから終了します…", flush=True)
        pool.shutdown(wait=False, cancel_futures=True)
        raise
    finally:
        pool.shutdown(wait=False)

    fixes_path = Path(args.fixes_output)
    fixes_path.parent.mkdir(parents=True, exist_ok=True)
    fixes_path.write_text(json.dumps(fixes_all, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n校正 fixes: {fixes_path}（{len(fixes_all)} 件）", flush=True)

    # 6. EPUB 生成
    to_epub.build_epub(
        json_dir=str(json_dir),
        pdf_path=str(pdf_path),
        out_path=args.output,
        title=args.title,
        author=args.author,
        publisher=args.publisher,
        horizontal=args.horizontal,
        layout_pages=args.layout_pages,
        skip_pages=args.skip_pages,
        dpi=args.dpi,
        cover_page=args.cover_page,
        proofread_fixes=str(fixes_path) if fixes_all else None,
    )

    # 7. 青空文庫 txt（任意）
    if args.aozora_out:
        to_aozora.convert(
            json_dir=str(json_dir),
            pdf_path=str(pdf_path),
            out_path=args.aozora_out,
            fig_dir=args.fig_dir,
            dpi=args.dpi,
            export_figures=not args.no_figures,
            layout_pages=args.layout_pages,
            skip_pages=args.skip_pages,
        )


def resolve_book_meta(args) -> None:
    """--isbn が指定されていれば書誌 API を引き、未指定のメタデータを補完する。

    引数で明示された値は常に API より優先する（API は空欄を埋めるだけ）。
    書誌が見つからない場合やネットワークが不通の場合は警告を出すだけで進み、
    従来どおり --title / --author / --publisher の指定値を使う。

    OCR を始める前に呼ぶこと。失敗を数時間の処理のあとで知ることになると困る。
    """

    if not args.isbn:
        return

    print(f"書誌 API を検索します（ISBN {args.isbn}）...", flush=True)
    meta = book_meta.lookup_isbn(args.isbn)
    if meta is None:
        print("  書誌が見つかりませんでした。--title / --author / --publisher の指定値を使います。", flush=True)
        return

    print(f"  取得元: {meta.source}", flush=True)
    for line in meta.summary_lines():
        print(f"    {line}", flush=True)

    filled = []
    if not args.title and meta.title:
        args.title = meta.title
        filled.append("--title")
    if not args.author and meta.author:
        args.author = meta.author
        filled.append("--author")
    if not args.publisher and meta.publisher:
        args.publisher = meta.publisher
        filled.append("--publisher")
    print(f"  書誌から補完: {', '.join(filled) if filled else 'なし（すべて引数で指定済み）'}", flush=True)


def main():
    parser = argparse.ArgumentParser(
        description="目次を先に OCR して章境界を確定し、章ごとに OCR と LLM 校正を"
        "パイプライン並列実行してから EPUB を生成する"
    )
    parser.add_argument("-p", "--pdf", required=True, help="入力 PDF")
    parser.add_argument("-j", "--json-dir", default="ocr_json", help="OCR JSON の保存/参照先")
    parser.add_argument("-o", "--output", default=None, help="出力 EPUB ファイル（--dry-run 時は不要）")
    parser.add_argument("--title", default=None, help="書名（--dry-run 時、または --isbn 指定時は不要）")
    parser.add_argument("--author", default="", help="著者")
    parser.add_argument("--publisher", default="", help="出版社")
    parser.add_argument(
        "--isbn", default=None,
        help="ISBN。書誌 API（openBD / 国立国会図書館サーチ）から書名・著者・出版社を補完する。"
             "取得できなかった項目と、明示指定した引数は従来どおりの手動指定が優先される",
    )
    parser.add_argument("--horizontal", action="store_true", help="横書きで出力する（既定は縦書き）")

    parser.add_argument("--toc-pages", required=True, help="目次のページ範囲（例: 2-3）")
    parser.add_argument("--page-offset", type=int, default=None, help="書籍ページ番号+offset=PDFページ番号")
    parser.add_argument("--layout-pages", default="", help="段組を幾何的に復元するページ（目次・索引など）")
    parser.add_argument("--skip-pages", default="", help="出力しないページ（表紙・奥付など）")

    parser.add_argument("--dpi", type=int, default=200, help="レンダリング DPI")
    parser.add_argument("-d", "--device", default=None, choices=["cuda", "cpu", "mps"], help="OCR デバイス")
    parser.add_argument("--cover-page", type=int, default=1, help="カバーに使う PDF ページ")

    parser.add_argument("--model", default="sonnet", help="校正に使う claude -p のモデル")
    parser.add_argument("--proofread-workers", type=int, default=2, help="章を並列校正する数")
    parser.add_argument("--max-chars", type=int, default=6000, help="校正 1 チャンクの文字数上限")
    parser.add_argument("--max-lines", type=int, default=50, help="校正 1 チャンクの行数上限")
    parser.add_argument("--timeout", type=int, default=900, help="校正 1 チャンクの制限時間（秒）")
    parser.add_argument("--proofread-cache", default="proofread_cache", help="章ごとの校正キャッシュ親ディレクトリ")
    parser.add_argument("--fixes-output", default="claudedocs/proofread_fixes.json", help="適用した校正の出力先")

    parser.add_argument("--fig-dir", default="aozora_fig", help="（--aozora-out 用）図版の出力先")
    parser.add_argument("--no-figures", action="store_true", help="（--aozora-out 用）図版を切り出さない")
    parser.add_argument("--aozora-out", default=None, help="指定すると青空文庫形式 txt も出力する")

    parser.add_argument(
        "--dry-run", action="store_true",
        help="目次OCR〜章境界確定までを行い、本文OCR・LLM校正・EPUB生成はスキップする",
    )
    args = parser.parse_args()

    if not args.dry_run and not args.output:
        parser.error("--dry-run を指定しない場合は --output が必須です")

    # 書誌の解決は OCR より前。--dry-run でも実行して、EPUB に入る書誌を人が確認できるようにする。
    resolve_book_meta(args)

    if not args.dry_run and not args.title:
        parser.error(
            "--dry-run を指定しない場合は --title が必須です"
            "（--isbn を指定しても書誌を取得できなかった場合は --title を明示してください）"
        )

    run(args)


if __name__ == "__main__":
    main()
