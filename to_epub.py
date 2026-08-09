"""OCR JSON（book_ir の IR 経由）から EPUB3（日本語縦書き既定）を生成する。

主な処理:
  - book_ir.build_ir() で IR ブロック列を構築し、印刷目次ページを既定で除外
  - 大見出しごとに XHTML を分割し、「部」「大見出し」「中見出し」の3階層 nav.xhtml を構築
  - 同じ目次ツリーから EPUB2 互換リーダー向けの toc.ncx を併載する
  - ｜親《ルビ》記法を <ruby><rp><rt> へ変換し、縦組みでは数字・略語を全角化／縦中横で立てる
  - 表セル構造を <table>（rowspan/colspan 付き）へ、図版を crop_figures で切り出して <img> へ
  - PDF 1ページ目をカバー画像としてレンダリング
  - 生成した EPUB を zip 構造・XML 整形式・manifest/spine 突合の観点で自己検証する
"""

import argparse
import datetime
import io
import re
import sys
import tempfile
import uuid
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path
from xml.sax.saxutils import escape

from book_ir import (
    FRONT_BACK_WORDS,
    KANJI_RE,
    build_ir,
    crop_figures,
    fix_subtitle_dash,
    front_back_re,
    join_lines,
    normalize_text,
    parse_pages,
    strip_trailing_page_number,
)

# --------------------------------------------------------------------------
# ルビ・インライン変換
# --------------------------------------------------------------------------

_KANJI_CLASS = KANJI_RE.pattern.strip("[]")
RUBY_PIPE_RE = re.compile(r"｜([^｜《》]+)《([^《》]+)》")
RUBY_BARE_RE = re.compile(f"([{_KANJI_CLASS}]+)《([^《》]+)》")

# 縦組みで数字・英字を正立させるための変換。normalize_text() の NFKC 正規化で
# 全角数字・全角英字はすべて半角になるため（'第１２章' → '第12章'、'ＳＮＳ' → 'SNS'）、
# 縦組みの既定 text-orientation: mixed ではそのまま 90 度横倒しでレンダリングされる。
# 規則は原書2冊の版面を実測して確定したもの:
#   - 数字1桁・3桁以上 …… 全角にして1字ずつ正立させる。『イスラム教の論理』p21
#     「2011年」は ２/０/１/１ が縦に4字、p15「第2章216節」は ２/１/６ が縦に3字。
#     「1桁は回転しないので不要」という当初の判断は実機で反証済み（1桁も横倒しになる）
#   - 数字ちょうど2桁 …… 縦中横（1字分に詰める）。同 p20「コーラン第4章48節」の 48。
#     span の中身は半角のまま入れる（全角を1字幅に組むと潰れる）
#   - 小数点 …… 中黒。1字分を占めて中央に来る。同 p113「2.2人」は ２/・/２。
#     『“町内会”は義務ですか?』の「27.8％」は 27(縦中横)・中黒・8(全角)・％(全角)
#   - 大文字だけの略語（2文字以上）…… 桁数によらず全角で1字ずつ正立。同 p11
#     「SNS戦略」は S/N/S が縦に3字、p2「EUからの離脱」は E/U が縦に2字。
#     2文字でも縦中横にはしない（数字の2桁とは規則が違う）
#   - 小文字を含む欧文語 …… 横倒しのまま。同 p74「Telegram は」。変換しない
#   - 「!!」「!?」「?!」「??」…… 縦中横。ただし book_ir の PUNCT_MAP が ! ? を
#     全角化するため、実際に render_inline() へ届くのは「！？」の形。全角も対象に
#     含めないとこの分岐は一度も発動しない。span の中身は半角に戻す
#
# トークンは1本の正規表現でまとめて取る。小数点だけを独立した regex で処理すると
# （(?<=[0-9])\.(?=[0-9]) を単独で走らせると）'Web2.0' の点まで中黒になる。
#
# lookbehind を2本に分けているのには理由がある。まとめて (?<![0-9A-Za-z.]) にしては
# いけない。
#   - (?<![0-9A-Za-z]) …… 'MP3' や 'iPhone' のように英数字が地続きのトークンを
#     部分的に拾わないための境界
#   - (?<![0-9]\.) …… 小数点の右側だけを拾わないための境界。これが無いと
#     'Web2.0' → 'Web2.０'、'1.5GB' → '１.5GB' と、トークンの途中だけが全角になる
#     （丸ごと素通りより見苦しい）
# ドットの前が数字のときだけに絞るのが要点で、ドットを一律に除外すると
# 'p.12'・'no.99'・'Fig.34' のような「英字＋ドット＋2桁」が縦中横にならなくなる。
# これらは従来から縦中横になっていた形なので、巻き込むと黙って退行する。
# 単独の大文字1文字は対象外。実データでは OCR が語を割ったノイズ（'L GBT'、'PD F'）
# しか無く、[A-Z]{2,} に絞って取りこぼしはない。
UPRIGHT_RE = re.compile(
    r"(?<![0-9A-Za-z])(?<![0-9]\.)(?:([0-9]+(?:\.[0-9]+)*)|([A-Z]{2,}))"
    r"(?![0-9A-Za-z]|\.[0-9A-Za-z])"
    r"|(?<![!?！？])([!?！？]{2})(?![!?！？])"
)
_TO_HALF_BANG = str.maketrans("！？", "!?")
_TO_FULLWIDTH = str.maketrans(
    "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ",
    "０１２３４５６７８９ＡＢＣＤＥＦＧＨＩＪＫＬＭＮＯＰＱＲＳＴＵＶＷＸＹＺ",
)
# 変換を適用してはいけない範囲（タグそのものと、ルビ要素の中身）を退避するための分割。
# 属性値にマッチすると <img src="../images/fig02.png"/> のようなパスが壊れる。
SKIP_RE = re.compile(r"(<ruby>.*?</ruby>|<[^>]+>)", re.S)


def apply_upright(html: str) -> str:
    """レンダリング済み HTML 断片のテキストノードだけに縦組みの正立処理をかける。

    SKIP_RE で分割すると奇数 index が退避対象（タグ・ルビ要素まるごと）、
    偶数 index がテキストノードになる。ルビは <rt> の中も含めて丸ごと退避される。

    数字トークンは '.' で分割し、各セグメントに桁数の規則（2桁なら縦中横、
    それ以外は全角化）を適用して中黒で連結する。
    """

    def convert(m: re.Match) -> str:
        digits, upper, marks = m.group(1), m.group(2), m.group(3)
        if marks:
            return f'<span class="tcy">{marks.translate(_TO_HALF_BANG)}</span>'
        if upper:
            return upper.translate(_TO_FULLWIDTH)
        return "・".join(
            f'<span class="tcy">{seg}</span>' if len(seg) == 2 else seg.translate(_TO_FULLWIDTH)
            for seg in digits.split(".")
        )

    parts = SKIP_RE.split(html)
    for i in range(0, len(parts), 2):
        parts[i] = UPRIGHT_RE.sub(convert, parts[i])
    return "".join(parts)


def render_inline(text: str, *, vertical: bool = True) -> str:
    """本文中の｜親《ルビ》記法を <ruby><rt> タグへ変換する（エスケープ込み）。

    エスケープはここで一度だけ行う。｜《》は XML エスケープの対象外の文字なので、
    エスケープ後にルビ変換を行っても二重エスケープや取りこぼしは起きない。

    <rp> はルビ非対応のリーダー向けのフォールバック。挿入するのは（）であって
    《》ではないので、後続の RUBY_BARE_RE に再マッチして二重変換されることはない。
    なお rp { display: none } は書かない（UA スタイルシートで既に非表示であり、
    自前で書くと「CSS は解釈するがルビ非対応」なリーダーで括弧まで消えてしまう）。

    正立処理は縦組みのときだけ、かつ最後に適用する。
      - vertical=False（--horizontal のビルド、および縦組み本文の中で
        horizontal-tb に戻す表セル）では一切かけない。全角化は文字そのものを
        変えてしまうので、CSS を出さないことでは無効化できない
      - ルビ変換より前にかけると [漢字]+《》 の隣接が span や全角字で分断されて
        RUBY_BARE_RE が壊れる
    """

    def ruby(m: re.Match) -> str:
        return f"<ruby>{m.group(1)}<rp>（</rp><rt>{m.group(2)}</rt><rp>）</rp></ruby>"

    escaped = escape(text)
    escaped = RUBY_PIPE_RE.sub(ruby, escaped)
    escaped = RUBY_BARE_RE.sub(ruby, escaped)
    return apply_upright(escaped) if vertical else escaped


def render_plain(text: str) -> str:
    """｜親《ルビ》記法をプレーンテキスト化する（エスケープ込み・タグを一切出さない）。

    NCX の navLabel/text はマークアップを一切許さない語彙で、<ruby> を入れると
    epubcheck が RSC-005 で弾く（実測）。nav.xhtml 用の render_inline() と違い、
    ルビ部《…》を捨てて親文字だけを残す。
      '第12章｜天体《てんたい》の運行' → '第12章天体の運行'

    RUBY_PIPE_RE は '｜親《ルビ》' 全体にマッチするので親文字への置換で ｜ ごと
    消える。ルビの開始記号として使われなかった裸の ｜ は正規表現に拾われずに
    残るため、最後にまとめて除去する（青空文庫記法では ｜ はルビ開始記号専用で、
    本文の文字として現れることはない）。
    """

    return escape(strip_ruby(text))


def strip_ruby(text: str) -> str:
    """｜親《ルビ》記法から親文字だけを残す（エスケープしない）。

    render_plain() と違いエスケープを行わないので、<title> や alt 属性のように
    埋め込み側が自前でエスケープする箇所に使う（render_plain() を渡すと二重
    エスケープになる）。

    RUBY_PIPE_RE は '｜親《ルビ》' 全体にマッチするので親文字への置換で ｜ ごと
    消える。ルビの開始記号として使われなかった裸の ｜ は正規表現に拾われずに
    残るため、最後にまとめて除去する（青空文庫記法では ｜ はルビ開始記号専用で、
    本文の文字として現れることはない）。
    """

    out = RUBY_PIPE_RE.sub(lambda m: m.group(1), text)
    out = RUBY_BARE_RE.sub(lambda m: m.group(1), out)
    return out.replace("｜", "")


def escape_attr(text: str) -> str:
    """XML 属性値へ埋め込むためのエスケープ。

    xml.sax.saxutils.escape() は既定で " をエスケープしないため、属性値に
    ダブルクォートを含むテキスト（OCR 由来の図版キャプション等）を埋め込むと
    非整形式になる。属性値に動的なテキストを差し込む箇所は必ずこちらを使う。
    """

    return escape(text, {'"': "&quot;"})


# --------------------------------------------------------------------------
# 印刷目次の除外
# --------------------------------------------------------------------------


def filter_printed_toc(blocks: list[dict], keep: bool = False, toc_pages: str = "") -> list[dict]:
    """既定では印刷目次を本文から除く（EPUB の目次は nav.xhtml が担うため）。

    除去区間は --toc-pages で与えられた PDF ページ番号を根拠に決める。見出し文字列を
    根拠にはしない。印刷目次ページの先頭に「目次」ちょうどの見出しが立つとは限らず、
    実測でも次のように外れるためである。
      - 『レベニューオペレーション(RevOps)の教科書』p6-12: 先頭行が 'はじめに2'
        （柱と最初のエントリが 1 行に連結される）。「目次」の行が 1 本も立たない
      - 『イスラム教の論理』p10-13: 目次扉が 'イスラム教の論理目次'
    どちらも見出し一致では 1 ブロックも除去できず、印刷目次がそのまま本文へ二重掲載される。

    --layout-pages は根拠に使えない。索引（本文として残す）も同じ引数で指定する運用
    だからで、実際 RevOps は `--layout-pages 6-12,327-332` の後半が索引である。

    --skip-pages との衝突はない。build_ir() が skip 指定のページを読む前に落とすので、
    印刷目次を --skip-pages で回避している既存の指定（『イスラム教の論理』の
    `--skip-pages 1,10-13,238,239`）と重ねても、除去対象のブロックが最初から
    存在しないだけで結果は変わらない。

    toc_pages が空のとき（--toc-pages を持たない呼び出し）だけ、従来どおり「目次」
    ちょうどの見出しから次の見出しまでを除去する経路にフォールバックする。
    """

    if keep:
        return blocks

    if toc_pages:
        return _drop_pages(blocks, parse_pages(toc_pages))

    out: list[dict] = []
    skipping = False
    for b in blocks:
        if b["kind"] == "heading":
            head = normalize_text(b["lines"][0]) if b["lines"] else ""
            skipping = head == "目次"
            if skipping:
                continue
        if skipping:
            continue
        out.append(b)
    return out


def _drop_pages(blocks: list[dict], pages: set[int]) -> list[dict]:
    """指定ページに載っているブロックを落とす。

    段落ブロックはページを跨いで連結されうるので、行ごとの実ページ（"pages"）を持つ
    ものは行単位で落とし、残った行があればブロックとして残す。ブロックの代表ページ
    （"page"）だけで判定すると、目次ページから本文ページへ跨いだ段落の本文側まで
    巻き添えで消える。
    """

    out: list[dict] = []
    for b in blocks:
        line_pages = b.get("pages") if b["kind"] == "para" else None
        if line_pages and len(line_pages) == len(b["lines"]):
            kept = [(line, p) for line, p in zip(b["lines"], line_pages) if p not in pages]
            if not kept:
                continue
            if len(kept) < len(b["lines"]):
                b = {**b, "lines": [k[0] for k in kept], "pages": [k[1] for k in kept]}
                b["page"] = b["pages"][0]
            out.append(b)
        elif b["page"] not in pages:
            out.append(b)
    return out


def printed_toc_warning(toc_pages: str, skip_pages: str, n_removed: int) -> str | None:
    """印刷目次を 1 ブロックも除去できなかったときに警告文を返す（正常なら None）。

    黙って 0 件だと印刷目次がそのまま本文へ二重掲載され、EPUB を開くまで気づけない。

    --skip-pages で先に落とされたページは除去対象になりようがないので、判定は
    「--toc-pages のうち --skip-pages に含まれないページ」が空かどうかで行う。
    印刷目次を --skip-pages で回避している既存の指定に対して空振りの警告を出さない。
    """

    effective = parse_pages(toc_pages) - parse_pages(skip_pages)
    if not effective or n_removed:
        return None
    return (
        f"警告: 印刷目次 p{min(effective)}〜p{max(effective)} から除去できたブロックが 0 件です。"
        "\n  --toc-pages のページ範囲が実際の印刷目次とずれている可能性があります"
        "（ずれていると印刷目次が本文へ二重掲載されます）。"
    )


# --------------------------------------------------------------------------
# 校正 fixes の適用
# --------------------------------------------------------------------------


def apply_fixes(blocks: list[dict], fixes: list[dict]) -> list[dict]:
    """pdf_to_epub.py が生成する校正 fixes を IR に適用する。

    fixes の各要素: {"page", "before", "after", "why"}（llm_proofread.py の適用結果と同形式）。
    ブロック分割は章単位 OCR の都合で pdf_to_epub 内部と最終 IR とで完全には一致しない
    ことがあるため、行番号ではなく「ページ番号 + before の一意性」で適用位置を特定する
    （llm_proofread.validate() と同じ安全策）。

    段落ブロックはページを跨いで連結されることがあり、ブロックの代表ページ（"page"）
    だけでは行ごとの実際の所在ページとずれる場合がある。そのため、行ごとの実ページが
    わかるブロック（"pages" を持つ para ブロック）はそちらを優先して使い、
    見出し／raw のように 1 ブロック＝単一ページのものはブロックの "page" を使う。
    それでも見つからない場合に限り、章単位 OCR とページ確定のわずかなずれを吸収する
    ためページ ±1 を最終フォールバックとして試す（一致が複数になる場合は適用しない）。
    """

    blocks = [dict(b) for b in blocks]
    by_page: dict[int, list[tuple[int, int]]] = {}
    for bi, b in enumerate(blocks):
        if b["kind"] in ("heading", "para"):
            pages = b.get("pages") or [b["page"]] * len(b["lines"])
            for li in range(len(b["lines"])):
                page = pages[li] if li < len(pages) else b["page"]
                by_page.setdefault(page, []).append((bi, li))
        elif b["kind"] == "raw":
            by_page.setdefault(b["page"], []).append((bi, -1))

    def find_matches(page: int, before: str) -> list[tuple[int, int]]:
        matches = []
        for bi, li in by_page.get(page, []):
            text = blocks[bi]["text"] if li == -1 else blocks[bi]["lines"][li]
            if text.count(before) == 1:
                matches.append((bi, li))
        return matches

    applied, skipped = 0, []
    for fix in fixes:
        page, before, after = fix.get("page"), fix.get("before", ""), fix.get("after", "")
        if not before or page is None:
            skipped.append((fix, "before が空、または page が未指定"))
            continue

        matches = find_matches(page, before)
        via_fallback = False
        if not matches:
            # 章単位ビルドとフル IR とでページ跨ぎ段落の代表ページがずれるケースの
            # 最終フォールバック。曖昧な適用を避けるため候補が複数出たら諦める。
            fallback = [m for p in (page - 1, page + 1) for m in find_matches(p, before)]
            if len(fallback) == 1:
                matches, via_fallback = fallback, True

        if len(matches) == 0:
            skipped.append((fix, f"ページ {page}（±1 含む）に before が見つからない: {before!r}"))
            continue
        if len(matches) > 1:
            skipped.append((fix, f"ページ {page} で before が複数箇所に一致: {before!r}"))
            continue

        bi, li = matches[0]
        if li == -1:
            blocks[bi]["text"] = blocks[bi]["text"].replace(before, after)
        else:
            lines = list(blocks[bi]["lines"])
            lines[li] = lines[li].replace(before, after)
            blocks[bi]["lines"] = lines
        applied += 1
        if via_fallback:
            print(f"  校正 fix: ページ {page} で見つからず ±1 で適用: {before!r} → {after!r}")

    print(f"校正 fixes 適用: {applied}/{len(fixes)} 件")
    for fix, reason in skipped:
        print(f"  スキップ: page={fix.get('page')} before={fix.get('before')!r} 理由={reason}")
    return blocks


# --------------------------------------------------------------------------
# 章分割と階層目次の構築
# --------------------------------------------------------------------------

BU_HEADING_RE = re.compile(r"^第[ⅠⅡⅢⅣⅤ]部")
# book_ir.heading_level() は「大見出しかどうか」（＝章として XHTML を分割すべきか）を
# 判定するためのものだが、ここでの判定は「大見出しの中でも部にぶら下げず常に nav の
# 第1階層に置くべきものか」という別基準であり、意図的に非対称。
# その非対称は共有語彙からの差集合で表す: 註・注は大見出しだが、直前の部の内容に
# 属する後注なので現在の部の下へネストさせたい。それ以外の前付/後付は部から独立させる。
NESTED_BACK_WORDS = ("註", "注")
FRONT_BACK_TITLE_RE = front_back_re(w for w in FRONT_BACK_WORDS if w not in NESTED_BACK_WORDS)


def heading_title(lines: list[str]) -> str:
    """見出し行を nav・<title> 表示用の 1 行に連結する（render_heading と見た目を揃える）。

    ダーシュで始まる副題行（fix_subtitle_dash が付与したもの）の前には全角空白を
    挟まない（「……時間――観点による区別」のように直接続ける）。

    heading_level() は柱として除去しきれなかったページ番号ごと見出しを拾う
    （「あとがき203」）ので、表示に回す前にその番号を落とす。
    """

    parts = [strip_trailing_page_number(normalize_text(l)) for l in lines]
    parts = [p for p in parts if p]
    parts = fix_subtitle_dash(parts)
    out = ""
    for i, p in enumerate(parts):
        if i > 0 and not p.startswith("――"):
            out += "　"
        out += p
    return out


def split_chapters_and_nav(blocks: list[dict]) -> tuple[list[dict], list[dict]]:
    """IR を大見出しごとの章に分割し、同時に階層目次ツリーを構築する。

    - 「第○部」見出しは第1階層。以後、次の部見出しが現れるまでの大見出しは
      その部の第2階層として nest する（部がなければ大見出しは第1階層になる）
    - はじめに・あとがき・目次・索引など前付/後付の大見出しは常に第1階層
    - 中見出しは現在の章の nav ノードの子（第3階層）として登録し、id を振る
    - 小見出しは id のみ振り、nav には含めない

    戻り値: (chapters, nav_tree)
      chapters: [{"file": "ch000.xhtml", "blocks": [...], "section_type": "part"|"chapter"}]
      nav_tree: [{"title", "href", "children": [...]}, ...]
    """

    chapters: list[dict] = []
    nav_tree: list[dict] = []
    current_bu: dict | None = None
    current_chapter_nav: dict | None = None
    current_chapter: dict | None = None
    heading_seq = 0

    def ensure_chapter() -> dict:
        nonlocal current_chapter
        if current_chapter is None:
            current_chapter = {
                "file": f"ch{len(chapters):03d}.xhtml",
                "blocks": [],
                "section_type": "chapter",
            }
            chapters.append(current_chapter)
        return current_chapter

    for b in blocks:
        if b["kind"] == "heading" and b["level"] == "大":
            current_chapter = {
                "file": f"ch{len(chapters):03d}.xhtml",
                "blocks": [b],
                "section_type": "chapter",
            }
            chapters.append(current_chapter)

            title = heading_title(b["lines"])
            node = {"title": title or "（無題）", "href": f"text/{current_chapter['file']}", "children": []}
            if BU_HEADING_RE.match(title):
                current_chapter["section_type"] = "part"
                nav_tree.append(node)
                current_bu = node
            elif FRONT_BACK_TITLE_RE.match(title):
                nav_tree.append(node)
            elif current_bu is not None:
                current_bu["children"].append(node)
            else:
                nav_tree.append(node)
            current_chapter_nav = node
            continue

        chapter = ensure_chapter()

        if b["kind"] == "heading" and b["level"] in ("中", "小"):
            heading_seq += 1
            hid = f"h{heading_seq:04d}"
            b = {**b, "id": hid}
            if b["level"] == "中" and current_chapter_nav is not None:
                title = heading_title(b["lines"])
                current_chapter_nav["children"].append(
                    {"title": title or "（無題）", "href": f"text/{chapter['file']}#{hid}", "children": []}
                )
            chapter["blocks"].append(b)
            continue

        chapter["blocks"].append(b)

    return chapters, nav_tree


def chapters_without_nav_entry(chapters: list[dict]) -> list[dict]:
    """大見出しを持たない章（＝nav にも toc.ncx にも現れない章）を返す。

    split_chapters_and_nav() は大見出しが立つ前に本文が来ると暗黙の章を作る。
    献辞や中扉だけの章なら正当だが、見出しが柱として捨てられたせいで章が丸ごと
    nav から消えているときも同じ形になり、静かに欠落する。
    見分けは人にしかつかないので、self_check の失敗にはせず警告だけ出す。
    """

    orphans = []
    for ch in chapters:
        head = ch["blocks"][0] if ch["blocks"] else None
        if head is not None and head["kind"] == "heading" and head["level"] == "大":
            continue
        orphans.append(ch)
    return orphans


def describe_chapter_pages(chapter: dict) -> str:
    """章のページ範囲を「p2〜p9」の形で表す（ページを持つブロックが無ければ「?」）"""

    pages = [b["page"] for b in chapter["blocks"] if b.get("page") is not None]
    if not pages:
        return "?"
    lo, hi = min(pages), max(pages)
    return f"p{lo}" if lo == hi else f"p{lo}〜p{hi}"


# --------------------------------------------------------------------------
# XHTML レンダリング
# --------------------------------------------------------------------------

HEADING_TAG = {"大": "h1", "中": "h2", "小": "h3"}


def render_heading(lines: list[str], level: str, hid: str | None, *, vertical: bool = True) -> str:
    parts = [normalize_text(l) for l in lines]
    parts = [p for p in parts if p]
    if not parts:
        return ""
    # 章扉・部扉の副題のダーシュ補正は to_aozora.emit_heading と共通のロジック
    parts = fix_subtitle_dash(parts)
    tag = HEADING_TAG[level]
    attr = f' id="{hid}"' if hid else ""
    out = [f"<{tag}{attr}>{render_inline(parts[0], vertical=vertical)}</{tag}>"]
    for extra in parts[1:]:
        out.append(f'<p class="subtitle">{render_inline(extra, vertical=vertical)}</p>')
    return "\n".join(out)


def render_table(block: dict) -> str:
    """表を <table class="h-table"> へ組む。セルの正立処理は組方向によらず行わない。

    表は縦組みの本にあっても build_css() が writing-mode: horizontal-tb に戻すので、
    セルの中身は常に横組みで表示される。横組みでは全角化は不格好だし、小数点の中黒化
    （'2.2' → '２・２'）は縦組み専用の約物で、横組みでは数値の意味を壊す。縦中横も
    同じ理由で CSS 側が table.h-table .tcy で無効化している。
    """

    grid: dict[tuple[int, int], dict] = {(c["row"], c["col"]): c for c in block["cells"]}
    covered: set[tuple[int, int]] = set()
    rows_html = []
    for r in range(1, block["n_row"] + 1):
        tds = []
        for c in range(1, block["n_col"] + 1):
            if (r, c) in covered:
                continue
            cell = grid.get((r, c))
            if cell is None:
                tds.append("<td></td>")
                continue
            rs, cs = cell.get("row_span", 1) or 1, cell.get("col_span", 1) or 1
            for dr in range(rs):
                for dc in range(cs):
                    if dr or dc:
                        covered.add((r + dr, c + dc))
            attrs = ""
            if rs > 1:
                attrs += f' rowspan="{rs}"'
            if cs > 1:
                attrs += f' colspan="{cs}"'
            text = join_lines(cell["contents"].split("\n")) if cell.get("contents") else ""
            tds.append(f"<td{attrs}>{render_inline(normalize_text(text), vertical=False)}</td>")
        rows_html.append("<tr>" + "".join(tds) + "</tr>")
    return '<table class="h-table">\n' + "\n".join(rows_html) + "\n</table>"


def render_figure(block: dict) -> str:
    # alt 属性もマークアップを置けないので、<title> と同様にルビ記法を落とす
    alt = escape_attr(strip_ruby(block.get("alt") or ""))
    return f'<figure class="h-figure"><img src="../images/{block["src"]}" alt="{alt}"/></figure>'


def render_block(b: dict, *, vertical: bool = True) -> str:
    kind = b["kind"]
    if kind == "para":
        text = normalize_text(join_lines(b["lines"]))
        return f"<p>{render_inline(text, vertical=vertical)}</p>" if text else ""
    if kind == "raw":
        return f"<p>{render_inline(b['text'], vertical=vertical)}</p>" if b["text"] else ""
    if kind == "heading":
        return render_heading(b["lines"], b["level"], b.get("id"), vertical=vertical)
    if kind == "table":
        return render_table(b)
    if kind == "figure":
        return render_figure(b)
    raise ValueError(f"未知の IR kind: {kind}")


def xhtml_page(title: str, body_inner: str, css_href: str, epub_type: str, lang: str = "ja") -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        "<!DOCTYPE html>\n"
        '<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops" '
        f'xml:lang="{lang}" lang="{lang}">\n'
        "<head><meta charset=\"UTF-8\"/><title>" + escape(title) + "</title>\n"
        f'<link rel="stylesheet" type="text/css" href="{css_href}"/></head>\n'
        f'<body><section epub:type="{epub_type}">\n{body_inner}\n</section></body>\n'
        "</html>\n"
    )


def render_chapter_xhtml(chapter: dict, *, vertical: bool = True) -> str:
    body = "\n".join(filter(None, (render_block(b, vertical=vertical) for b in chapter["blocks"])))
    first = chapter["blocks"][0] if chapter["blocks"] else None
    title = heading_title(first["lines"]) if first and first["kind"] == "heading" else "本文"
    # <title> はマークアップを置けないので、ルビ記法は親文字だけ残して落とす
    # （xhtml_page 側がエスケープするので strip_ruby の方を使う）
    return xhtml_page(strip_ruby(title) or "本文", body, "../style.css", chapter["section_type"])


# --------------------------------------------------------------------------
# nav.xhtml
# --------------------------------------------------------------------------


def render_nav_list(nodes: list[dict], *, vertical: bool = True) -> str:
    if not nodes:
        return ""
    items = []
    for node in nodes:
        # heading_title() は normalize_text() を通すだけで、normalize_text() は
        # ｜《》を退避するためルビ記法が生のまま残る。nav の <a> の中には <ruby> を
        # 置けるので、本文と同じ render_inline() でルビ化する。nav.xhtml は本文と同じ
        # style.css を読むので組方向も本文と同じ。正立処理も本文に合わせる。
        # render_inline() が自前でエスケープするため escape() の二重適用はしない。
        inner = (
            f'<a href="{escape_attr(node["href"])}">'
            f"{render_inline(node['title'], vertical=vertical)}</a>"
        )
        children = render_nav_list(node["children"], vertical=vertical)
        items.append(f"<li>{inner}{children}</li>")
    return "<ol>" + "".join(items) + "</ol>"


def render_nav_xhtml(
    nav_tree: list[dict], first_chapter_href: str | None, *, vertical: bool = True
) -> str:
    """nav.xhtml を組み立てる（toc は section で包まず nav 要素を body 直下に置く）"""

    toc_list = render_nav_list(nav_tree, vertical=vertical)
    if not toc_list:
        # EPUB3 の toc nav は最低 1 エントリを持つ <ol> が必要（空の <nav> は仕様違反）。
        # 章がまったく検出できなかった場合のフォールバックとして本文への1項目だけ出す。
        fallback_href = escape_attr(first_chapter_href) if first_chapter_href else "nav.xhtml"
        toc_list = f'<ol><li><a href="{fallback_href}">本文</a></li></ol>'
    toc = f'<nav epub:type="toc" id="toc"><h1>目次</h1>{toc_list}</nav>'
    landmarks_items = ['<li><a epub:type="cover" href="cover.xhtml">表紙</a></li>',
                        '<li><a epub:type="toc" href="nav.xhtml">目次</a></li>']
    if first_chapter_href:
        landmarks_items.append(
            f'<li><a epub:type="bodymatter" href="{escape_attr(first_chapter_href)}">本文</a></li>'
        )
    landmarks = (
        '<nav epub:type="landmarks" id="landmarks" hidden="">'
        f'<ol>{"".join(landmarks_items)}</ol></nav>'
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        "<!DOCTYPE html>\n"
        '<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops" '
        'xml:lang="ja" lang="ja">\n'
        "<head><meta charset=\"UTF-8\"/><title>目次</title>\n"
        '<link rel="stylesheet" type="text/css" href="style.css"/></head>\n'
        f"<body>\n{toc}\n{landmarks}\n</body>\n"
        "</html>\n"
    )


# --------------------------------------------------------------------------
# toc.ncx（EPUB2 互換）
# --------------------------------------------------------------------------

NCX_ID = "ncx"
NCX_HREF = "toc.ncx"
NCX_MEDIA_TYPE = "application/x-dtbncx+xml"


def nav_depth(nodes: list[dict]) -> int:
    """目次ツリーの最大階層数（dtb:depth 用）。空なら 0。"""

    return max((1 + nav_depth(n["children"]) for n in nodes), default=0)


def render_nav_points(
    nodes: list[dict], counter: list[int], indent: str = "  ", seen: dict[str, int] | None = None
) -> str:
    """nav_tree を navPoint の入れ子へ変換する（render_nav_list と同じ階層構造）。

    playOrder は仕様上は任意（無くても epubcheck は通る）だが慣習的に付ける。
    文書順（pre-order）に 1 から通し番号を振る。

    ただし NCX では「同じ target を指す navPoint は同じ playOrder でなければならない」
    という制約があり、素朴に通し番号を振ると同一 href が複数階層に現れたときに
    epubcheck が RSC-005（different playOrder values ... refer to same target）で落ちる。
    split_chapters_and_nav() は大見出しごとに別ファイル、中見出しには #id 断片を振るので
    通常は衝突しないが、仕様に合わせて href ごとに採番を共有する（seen）。
    id は navPoint 単位で一意である必要があるので、こちらは通し番号のまま。
    """

    if seen is None:
        seen = {}
    out = []
    for node in nodes:
        counter[0] += 1
        # id は再帰の前に確定させる。子を先に描画すると counter が進んでしまい、
        # 親の id が子の id と衝突する（RSC-005: id does not have a unique value）。
        nid = counter[0]
        n = seen.setdefault(node["href"], len(seen) + 1)
        # navLabel/text はプレーンテキストのみ。<ruby> を入れると RSC-005 で落ちる。
        label = render_plain(node["title"])
        children = render_nav_points(node["children"], counter, indent + "  ", seen)
        out.append(
            f'{indent}<navPoint id="navPoint-{nid}" playOrder="{n}">\n'
            f"{indent}  <navLabel><text>{label}</text></navLabel>\n"
            f'{indent}  <content src="{escape_attr(node["href"])}"/>\n'
            f"{children}"
            f"{indent}</navPoint>\n"
        )
    return "".join(out)


def render_ncx(nav_tree: list[dict], *, title: str, book_id: str, first_chapter_href: str | None) -> str:
    """toc.ncx を組み立てる。

    EPUB3 に NCX を併載しても epubcheck 5.1.0 は警告すら出さないが、2つの必須条件がある
    （いずれも実測）:
      - OPF の spine に toc="ncx" が必要（無いと RSC-005）
      - dtb:uid が dc:identifier と完全一致していること（不一致だと NCX-001）
    構造面では <head> に meta が最低1つ、<docTitle> が <navMap> より前に必要。
    dtb:depth / dtb:totalPageCount / dtb:maxPageNumber と playOrder は任意だが慣習的に書く。

    読み方向（縦書き）に相当する語彙は NCX 2005-1 には存在しない。組方向の指定箇所は
    OPF の spine page-progression-direction が唯一なので、ここでは何もしない。
    """

    counter = [0]
    nav_points = render_nav_points(nav_tree, counter)
    if not nav_points:
        # navMap は最低 1 つの navPoint が必要。nav.xhtml と同じフォールバックを出す。
        href = escape_attr(first_chapter_href) if first_chapter_href else "nav.xhtml"
        nav_points = (
            '  <navPoint id="navPoint-1" playOrder="1">\n'
            "    <navLabel><text>本文</text></navLabel>\n"
            f'    <content src="{href}"/>\n'
            "  </navPoint>\n"
        )
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE ncx PUBLIC "-//NISO//DTD ncx 2005-1//EN" "http://www.daisy.org/z3986/2005/ncx-2005-1.dtd">
<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1" xml:lang="ja">
<head>
<meta name="dtb:uid" content="{escape_attr(book_id)}"/>
<meta name="dtb:depth" content="{max(nav_depth(nav_tree), 1)}"/>
<meta name="dtb:totalPageCount" content="0"/>
<meta name="dtb:maxPageNumber" content="0"/>
</head>
<docTitle><text>{render_plain(title)}</text></docTitle>
<navMap>
{nav_points}</navMap>
</ncx>
"""


# --------------------------------------------------------------------------
# CSS
# --------------------------------------------------------------------------


def build_css(horizontal: bool) -> str:
    """縦書き（vertical-rl）／横書き（horizontal-tb）それぞれの style.css を組み立てる。

    見出しの前後空きを margin-top/bottom で書くと、縦書きでは行内方向の余白に
    なってしまい見出しの前後が詰まる。流れ方向に沿う論理プロパティ
    （margin-block-start / margin-block-end）で指定する。
    ただし -epub- / -webkit- 接頭辞を併記している通り古いエンジンも対象なので、
    margin-block-* 非対応の環境で前後空きが消えないよう、同値の物理指定を
    フォールバックとして先に置く（後続の論理指定が対応環境では上書きする）。
    組方向は固定なので物理側の対応は静的に決まる：横書きは上下、縦書きは
    vertical-rl なので block-start=右 / block-end=左。
    """

    mode = "horizontal-tb" if horizontal else "vertical-rl"

    def block_margin(start: str, end: str) -> str:
        """見出しの流れ方向の前後空き。物理フォールバック→論理指定の順で置く。"""

        # margin: 上 右 下 左
        fallback = f"{start} 0 {end}" if horizontal else f"0 {start} 0 {end}"
        return (
            f"margin: {fallback}; "
            f"margin-block-start: {start}; margin-block-end: {end};"
        )

    # 表・図版は縦書き本文の中でも横組みで読ませる。横書き時は本文と同じ組方向
    # なので上書き自体が不要（class 名は XHTML 側で常に付くが CSS 側で分岐する）。
    h_override = "" if horizontal else """table.h-table, figure.h-figure {
  writing-mode: horizontal-tb;
  -epub-writing-mode: horizontal-tb;
  -webkit-writing-mode: horizontal-tb;
}
"""
    # 表・図版は自身が horizontal-tb なので論理プロパティは本文の流れ方向と一致
    # しない。前後空きは本文の組方向に合わせた物理プロパティで直接指定する。
    block_gap = "1em 0" if horizontal else "0 1em"

    # 縦中横は縦書きのときだけ意味を持つので、横書き時は規則ごと出さない。
    # 宣言の順序は電書協標準 CSS に合わせる。レガシー接頭辞は値が horizontal で
    # 標準の text-combine-upright とは別語彙なので、標準版を後に置いて上書きさせる。
    # 表・図版は縦書き本文の中でも horizontal-tb に戻しているため、その中の縦中横は
    # かえって有害。無効化する規則も併せて出す。
    tcy_rules = "" if horizontal else """.tcy {
  -webkit-text-combine: horizontal;
  -webkit-text-combine-upright: all;
  text-combine-upright: all;
  -epub-text-combine: horizontal;
}
table.h-table .tcy, figure.h-figure .tcy {
  text-combine-upright: none;
  -webkit-text-combine: none;
  -epub-text-combine: none;
}
"""

    # 禁則・ぶら下げ。html, body ではなく別規則として置く（self_check は
    # html, body ブロックを丸ごと切り出して中の writing-mode を全部拾うので、
    # 無関係な宣言を紛れ込ませない）。どちらの宣言も継承するので body で足りる。
    #   - line-break: strict は CJK で最も厳しい行分割規則。小書き仮名（っゃゅょ）
    #     と長音符「ー」の行頭禁則が効く
    #   - hanging-punctuation: allow-end は句読点のぶら下げ。実質 WebKit のみだが
    #     未対応エンジンは無視するだけ
    # font-feature-settings の vert / vrt2 と text-orientation・text-spacing は
    # 意図的に書かない。vert は UA が自動適用する義務があり冗長、vrt2 は W3C 仕様が
    # "is not used by CSS" と排除しており UA の回転と二重にかかる恐れがある。
    # text-orientation は初期値 mixed が日本語縦組みの正解、text-spacing の normal は
    # 約物詰めを抑制する逆効果。

    return f"""html, body {{
  writing-mode: {mode};
  -epub-writing-mode: {mode};
  -webkit-writing-mode: {mode};
  font-family: "Noto Serif CJK JP", serif;
  line-height: 1.9;
}}
body {{
  line-break: strict;
  hanging-punctuation: allow-end;
}}
{tcy_rules}ruby {{ ruby-position: over; -webkit-ruby-position: over; -epub-ruby-position: over; }}
rt {{ font-size: 0.5em; line-height: 1; }}
h1 {{ font-size: 1.6em; {block_margin("1.2em", "0.6em")} }}
h2 {{ font-size: 1.3em; {block_margin("1em", "0.5em")} }}
h3 {{ font-size: 1.1em; {block_margin("0.8em", "0.4em")} }}
p {{ margin: 0; text-indent: 1em; }}
p.subtitle {{ text-indent: 0; font-size: 0.9em; }}
{h_override}table.h-table {{ border-collapse: collapse; margin: {block_gap}; }}
table.h-table th, table.h-table td {{ border: 1px solid #333; padding: 0.3em 0.6em; }}
figure.h-figure {{ margin: {block_gap}; text-align: center; }}
figure.h-figure img {{ max-width: 100%; height: auto; }}
"""
# landmarks nav は epub:type="landmarks" 属性に加え hidden="" 属性で隠しているため、
# CSS 側で明示的に非表示にする必要はない。@namespace 宣言なしの
# nav[epub|type~="landmarks"] セレクタは無効（属性セレクタの名前空間接頭辞が
# 未解決になる）なので、あえて追加しない。


# --------------------------------------------------------------------------
# package.opf
# --------------------------------------------------------------------------


def primary_writing_mode(horizontal: bool) -> str:
    """primary-writing-mode メタの値。

    Kindle Publishing Guidelines が定める語彙は horizontal-lr / horizontal-rl /
    vertical-lr / vertical-rl の4つで、CSS の writing-mode とは別物（進行方向を
    含む）。日本語の横書きは horizontal-tb ではなく horizontal-lr。
    """

    return "horizontal-lr" if horizontal else "vertical-rl"


def build_opf(
    *,
    title: str,
    author: str,
    publisher: str,
    book_uuid: str,
    modified: str,
    manifest_items: list[dict],
    spine_items: list[dict],
    horizontal: bool,
    ncx_id: str | None = None,
) -> str:
    meta = [
        f'<dc:identifier id="pub-id">urn:uuid:{book_uuid}</dc:identifier>',
        f"<dc:title>{escape(title)}</dc:title>",
        "<dc:language>ja</dc:language>",
    ]
    if author:
        meta.append(f"<dc:creator>{escape(author)}</dc:creator>")
    if publisher:
        meta.append(f"<dc:publisher>{escape(publisher)}</dc:publisher>")
    meta.append(f'<meta property="dcterms:modified">{modified}</meta>')
    # Kindle や日本語ビューアが本の既定の組方向を判断するための拡張メタ。
    # spine の page-progression-direction だけではページ送り方向しか伝わらない。
    # EPUB3 の property 属性は既定語彙にある名前しか取れず、接頭辞なしの
    # primary-writing-mode は epubcheck が OPF-027 で弾く。Kindle が実際に読むのも
    # OPF2 由来の name/content 形式なので、そちらで出力する。
    # 値は CSS の writing-mode ではなく Kindle の語彙（進行方向込み）に従う。
    meta.append(
        f'<meta name="primary-writing-mode" content="{primary_writing_mode(horizontal)}"/>'
    )

    manifest = "\n".join(
        f'<item id="{it["id"]}" href="{it["href"]}" media-type="{it["media_type"]}"'
        + (f' properties="{it["properties"]}"' if it.get("properties") else "")
        + "/>"
        for it in manifest_items
    )
    # NCX を manifest に入れたら spine の toc 属性が必須（無いと epubcheck が RSC-005）。
    # NCX 自体は spine に itemref として並べない。
    spine_attr = f' toc="{ncx_id}"' if ncx_id else ""
    spine_attr += "" if horizontal else ' page-progression-direction="rtl"'
    spine = "\n".join(
        f'<itemref idref="{it["idref"]}"'
        + (f' linear="{it["linear"]}"' if it.get("linear") else "")
        + "/>"
        for it in spine_items
    )
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="pub-id" xml:lang="ja">
<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
{chr(10).join(meta)}
</metadata>
<manifest>
{manifest}
</manifest>
<spine{spine_attr}>
{spine}
</spine>
</package>
"""


CONTAINER_XML = """<?xml version="1.0" encoding="UTF-8"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
   <rootfiles>
      <rootfile full-path="OEBPS/package.opf" media-type="application/oebps-package+xml"/>
   </rootfiles>
</container>
"""


# --------------------------------------------------------------------------
# カバー
# --------------------------------------------------------------------------


def render_cover_png(pdf_path: Path, page_no: int, dpi: int) -> bytes:
    import pypdfium2

    doc = pypdfium2.PdfDocument(str(pdf_path))
    try:
        pil = doc[page_no - 1].render(scale=dpi / 72).to_pil().convert("RGB")
    finally:
        doc.close()
    buf = io.BytesIO()
    pil.save(buf, format="PNG")
    return buf.getvalue()


# --------------------------------------------------------------------------
# 自己検証
# --------------------------------------------------------------------------


def self_check(epub_path: Path, horizontal: bool = False) -> list[str]:
    """生成した EPUB を zip 構造・XML 整形式・manifest/spine 突合・縦横整合の観点で検証する

    縦横整合は style.css の writing-mode、spine の page-progression-direction、
    primary-writing-mode メタの3点が `horizontal` と食い違っていないかを見る。
    """

    problems: list[str] = []
    with zipfile.ZipFile(epub_path) as zf:
        infos = zf.infolist()
        if not infos or infos[0].filename != "mimetype":
            problems.append("mimetype が zip の先頭エントリではありません")
        else:
            if infos[0].compress_type != zipfile.ZIP_STORED:
                problems.append("mimetype が無圧縮（ZIP_STORED）で格納されていません")
            if zf.read("mimetype") != b"application/epub+zip":
                problems.append("mimetype の内容が application/epub+zip ではありません")

        names = set(zf.namelist())
        opf_path = None
        for name in names:
            if name.endswith((".xhtml", ".opf", ".ncx")):
                try:
                    ET.fromstring(zf.read(name))
                except ET.ParseError as e:
                    problems.append(f"整形式エラー: {name}: {e}")
            if name.endswith(".opf"):
                opf_path = name

        if opf_path is None:
            problems.append("package.opf が見つかりません")
            return problems

        ns = {"opf": "http://www.idpf.org/2007/opf"}
        root = ET.fromstring(zf.read(opf_path))
        opf_dir = str(Path(opf_path).parent).replace("\\", "/")

        manifest_items = {
            item.get("id"): item.get("href") for item in root.find("opf:manifest", ns)
        }
        declared = {opf_path}
        for id_, href in manifest_items.items():
            full = f"{opf_dir}/{href}" if opf_dir not in ("", ".") else href
            declared.add(full)
            if full not in names:
                problems.append(f"manifest 項目 {id_} のファイルが zip にありません: {full}")

        spine = root.find("opf:spine", ns)
        if spine is None:
            problems.append("package.opf に spine がありません")
            return problems
        for itemref in spine:
            idref = itemref.get("idref")
            if idref not in manifest_items:
                problems.append(f"spine の idref が manifest にありません: {idref}")

        # --- NCX（EPUB2 互換）---
        # NCX を manifest に入れたら spine の toc 属性で指す必要があり（無いと RSC-005）、
        # dtb:uid は dc:identifier と完全一致していなければならない（不一致だと NCX-001）。
        ncx_ids = [
            item.get("id")
            for item in root.find("opf:manifest", ns)
            if item.get("media-type") == "application/x-dtbncx+xml"
        ]
        toc_attr = spine.get("toc")
        if ncx_ids and toc_attr not in ncx_ids:
            problems.append(
                f"NCX が manifest にあるのに spine の toc 属性が指していません: {toc_attr!r}"
            )
        elif not ncx_ids and toc_attr is not None:
            problems.append(f"spine の toc 属性が指す NCX が manifest にありません: {toc_attr!r}")

        if ncx_ids and toc_attr in ncx_ids:
            ncx_href = manifest_items[toc_attr]
            ncx_full = f"{opf_dir}/{ncx_href}" if opf_dir not in ("", ".") else ncx_href
            if ncx_full in names:
                uid_attr = root.get("unique-identifier")
                book_ids = [
                    (e.text or "").strip()
                    for e in root.iterfind(
                        "opf:metadata/{http://purl.org/dc/elements/1.1/}identifier", ns
                    )
                    if e.get("id") == uid_attr
                ]
                ncx_root = ET.fromstring(zf.read(ncx_full))
                dtb_uid = [
                    (m.get("content") or "").strip()
                    for m in ncx_root.iterfind(
                        "{http://www.daisy.org/z3986/2005/ncx/}head/"
                        "{http://www.daisy.org/z3986/2005/ncx/}meta"
                    )
                    if m.get("name") == "dtb:uid"
                ]
                if dtb_uid != book_ids:
                    problems.append(
                        f"NCX の dtb:uid が dc:identifier と一致しません: "
                        f"{dtb_uid}（期待: {book_ids}）"
                    )

        # --- 縦横整合 ---
        expected_mode = "horizontal-tb" if horizontal else "vertical-rl"

        css_path = f"{opf_dir}/style.css" if opf_dir not in ("", ".") else "style.css"
        if css_path not in names:
            problems.append(f"style.css が見つかりません: {css_path}")
        else:
            css = zf.read(css_path).decode("utf-8")
            body_rule = re.search(r"html,\s*body\s*\{([^}]*)\}", css)
            if body_rule is None:
                problems.append("style.css に html, body の規則がありません")
            else:
                # ベンダー接頭辞付き（-epub- / -webkit-）も含め、すべて同じ値であること
                modes = [v.strip() for v in re.findall(r"writing-mode:\s*([^;]+);", body_rule.group(1))]
                if not modes:
                    problems.append("style.css の html, body に writing-mode がありません")
                elif any(v != expected_mode for v in modes):
                    problems.append(
                        f"style.css の writing-mode が期待値と異なります: {modes}（期待: {expected_mode}）"
                    )

        expected_ppd = None if horizontal else "rtl"
        ppd = spine.get("page-progression-direction")
        if ppd != expected_ppd:
            problems.append(
                f"spine の page-progression-direction が期待値と異なります: "
                f"{ppd!r}（期待: {expected_ppd!r}）"
            )

        expected_pwm = primary_writing_mode(horizontal)
        pwm = [
            (m.get("content") or "").strip()
            for m in root.iterfind("opf:metadata/opf:meta", ns)
            if m.get("name") == "primary-writing-mode"
        ]
        if pwm != [expected_pwm]:
            problems.append(
                f"primary-writing-mode メタが期待値と異なります: {pwm}（期待: [{expected_pwm!r}]）"
            )

        for name in names:
            if name == "mimetype" or name.startswith("META-INF/"):
                continue
            if name not in declared:
                problems.append(f"zip にあるが manifest 未登録: {name}")

    return problems


# --------------------------------------------------------------------------
# 変換本体
# --------------------------------------------------------------------------


def build_epub(
    *,
    json_dir: str,
    pdf_path: str,
    out_path: str,
    title: str,
    author: str = "",
    publisher: str = "",
    horizontal: bool = False,
    layout_pages: str = "",
    skip_pages: str = "",
    toc_pages: str = "",
    dpi: int = 200,
    cover_page: int = 1,
    proofread_fixes: str | None = None,
    keep_printed_toc: bool = False,
):
    pdf_path, out_path = Path(pdf_path), Path(out_path)

    blocks, _ir_stats = build_ir(json_dir, pdf_path, layout_pages=layout_pages, skip_pages=skip_pages)
    n_before = len(blocks)
    blocks = filter_printed_toc(blocks, keep=keep_printed_toc, toc_pages=toc_pages)
    if toc_pages and not keep_printed_toc:
        print(f"印刷目次を本文から除外: {n_before - len(blocks)} ブロック（p{toc_pages}）")
        warning = printed_toc_warning(toc_pages, skip_pages, n_before - len(blocks))
        if warning:
            print(warning)

    if proofread_fixes:
        import json as jsonlib

        fixes_path = Path(proofread_fixes)
        try:
            fixes = jsonlib.loads(fixes_path.read_text(encoding="utf-8"))
        except (jsonlib.JSONDecodeError, OSError) as e:
            raise SystemExit(f"校正 fixes の読み込みに失敗しました: {fixes_path}: {e}") from e
        blocks = apply_fixes(blocks, fixes)

    chapters, nav_tree = split_chapters_and_nav(blocks)
    orphans = chapters_without_nav_entry(chapters)
    if orphans:
        print(
            f"警告: 大見出しを持たない章が {len(orphans)} 件あります（本文には出ますが nav・"
            "toc.ncx には現れません）: "
            + ", ".join(f"{c['file']}({describe_chapter_pages(c)})" for c in orphans)
            + "\n  献辞や中扉だけの章なら正常です。本文のある章なら、見出しが柱として"
            "除去されている可能性があります。"
        )
    book_uuid = str(uuid.uuid4())
    book_id = f"urn:uuid:{book_uuid}"
    n_tables = sum(1 for b in blocks if b["kind"] == "table")
    n_figures = sum(1 for b in blocks if b["kind"] == "figure")

    manifest_items: list[dict] = []
    spine_items: list[dict] = []
    files: dict[str, bytes] = {}

    css = build_css(horizontal)
    files["OEBPS/style.css"] = css.encode("utf-8")
    manifest_items.append({"id": "css", "href": "style.css", "media_type": "text/css"})

    # --- カバー ---
    cover_png = render_cover_png(pdf_path, cover_page, dpi)
    files["OEBPS/images/cover.png"] = cover_png
    manifest_items.append(
        {"id": "cover-img", "href": "images/cover.png", "media_type": "image/png", "properties": "cover-image"}
    )
    cover_html = xhtml_page(
        title, '<figure class="h-figure"><img src="images/cover.png" alt="表紙"/></figure>', "style.css", "cover"
    )
    files["OEBPS/cover.xhtml"] = cover_html.encode("utf-8")
    manifest_items.append({"id": "cover-page", "href": "cover.xhtml", "media_type": "application/xhtml+xml"})
    spine_items.append({"idref": "cover-page"})

    # --- nav ---
    # 数字・英字の正立処理は縦組みのときだけ通す（全角化は文字そのものを変えるので、
    # 縦中横のように「CSS を出さない」ことでは横組みビルドから外せない）。
    vertical = not horizontal
    first_href = f"text/{chapters[0]['file']}" if chapters else None
    nav_html = render_nav_xhtml(nav_tree, first_href, vertical=vertical)
    files["OEBPS/nav.xhtml"] = nav_html.encode("utf-8")
    manifest_items.append({"id": "nav", "href": "nav.xhtml", "media_type": "application/xhtml+xml", "properties": "nav"})
    spine_items.append({"idref": "nav", "linear": "no"})

    # --- toc.ncx（EPUB2 互換リーダー向け。nav.xhtml と同じツリーから作る）---
    ncx = render_ncx(nav_tree, title=title, book_id=book_id, first_chapter_href=first_href)
    files[f"OEBPS/{NCX_HREF}"] = ncx.encode("utf-8")
    manifest_items.append({"id": NCX_ID, "href": NCX_HREF, "media_type": NCX_MEDIA_TYPE})

    # --- 図版切り出し ---
    fig_jobs = [
        {"page": b["page"], "box": b["box"], "name": b["src"]} for b in blocks if b["kind"] == "figure"
    ]
    if fig_jobs:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            crop_figures(pdf_path, fig_jobs, tmp_dir, dpi)
            for job in fig_jobs:
                data = (tmp_dir / job["name"]).read_bytes()
                files[f"OEBPS/images/{job['name']}"] = data
                img_id = f"img-{Path(job['name']).stem}"
                manifest_items.append({"id": img_id, "href": f"images/{job['name']}", "media_type": "image/png"})

    # --- 章 ---
    for idx, chapter in enumerate(chapters):
        xhtml = render_chapter_xhtml(chapter, vertical=vertical)
        files[f"OEBPS/text/{chapter['file']}"] = xhtml.encode("utf-8")
        item_id = f"ch{idx:03d}"
        manifest_items.append({"id": item_id, "href": f"text/{chapter['file']}", "media_type": "application/xhtml+xml"})
        spine_items.append({"idref": item_id})

    # --- package.opf ---
    modified = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    opf = build_opf(
        title=title,
        author=author,
        publisher=publisher,
        book_uuid=book_uuid,
        modified=modified,
        manifest_items=manifest_items,
        spine_items=spine_items,
        horizontal=horizontal,
        ncx_id=NCX_ID,
    )
    files["OEBPS/package.opf"] = opf.encode("utf-8")

    # --- zip 書き出し ---
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(out_path, "w") as zf:
        mimetype_info = zipfile.ZipInfo("mimetype")
        mimetype_info.compress_type = zipfile.ZIP_STORED
        zf.writestr(mimetype_info, b"application/epub+zip")
        zf.writestr("META-INF/container.xml", CONTAINER_XML, compress_type=zipfile.ZIP_DEFLATED)
        for name, data in files.items():
            zf.writestr(name, data, compress_type=zipfile.ZIP_DEFLATED)

    size = out_path.stat().st_size
    print(f"出力: {out_path} ({size:,} bytes)")
    print(f"  章ファイル数: {len(chapters)} / 表: {n_tables} / 図版: {n_figures}")
    print(f"  nav 第1階層: {len(nav_tree)} 項目")

    problems = self_check(out_path, horizontal=horizontal)
    if problems:
        print("self-check: FAIL")
        for p in problems:
            print(f"  - {p}")
    else:
        print("self-check: PASS")

    return out_path, problems


def main():
    parser = argparse.ArgumentParser(description="OCR JSON から EPUB3 を生成する")
    parser.add_argument("-j", "--json-dir", default="ocr_json", help="OCR JSON ディレクトリ")
    parser.add_argument("-p", "--pdf", required=True, help="元 PDF（カバー・図版切り出し用）")
    parser.add_argument("-o", "--output", required=True, help="出力 EPUB ファイル")
    parser.add_argument("--title", required=True, help="書名")
    parser.add_argument("--author", default="", help="著者")
    parser.add_argument("--publisher", default="", help="出版社")
    parser.add_argument("--horizontal", action="store_true", help="横書きで出力する（既定は縦書き）")
    parser.add_argument("--layout-pages", default="", help="段組を幾何的に復元するページ（例: 2-3,287-295）")
    parser.add_argument("--skip-pages", default="", help="出力しないページ（例: 1,301）")
    parser.add_argument(
        "--toc-pages",
        default="",
        help="印刷目次のページ範囲（例: 2-3）。この範囲を本文から除外する"
        "（EPUB の目次は nav.xhtml が担うため）。省略すると「目次」見出しの検出に頼るので、"
        "目次ページがあるなら指定すること",
    )
    parser.add_argument("--dpi", type=int, default=200, help="OCR 時と同じ DPI（図版切り出し・カバー用）")
    parser.add_argument("--cover-page", type=int, default=1, help="カバーに使う PDF ページ（1始まり）")
    parser.add_argument("--proofread-fixes", default=None, help="適用する校正 fixes JSON")
    parser.add_argument(
        "--keep-printed-toc", action="store_true", help="印刷目次ページを本文から除外しない"
    )
    args = parser.parse_args()

    _out, problems = build_epub(
        json_dir=args.json_dir,
        pdf_path=args.pdf,
        out_path=args.output,
        title=args.title,
        author=args.author,
        publisher=args.publisher,
        horizontal=args.horizontal,
        layout_pages=args.layout_pages,
        skip_pages=args.skip_pages,
        toc_pages=args.toc_pages,
        dpi=args.dpi,
        cover_page=args.cover_page,
        proofread_fixes=args.proofread_fixes,
        keep_printed_toc=args.keep_printed_toc,
    )
    if problems:
        sys.exit(1)


if __name__ == "__main__":
    main()
