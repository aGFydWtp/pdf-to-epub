"""縦書きタイポグラフィ（数字・英字の正立、ルビ、禁則）と nav のルビ化の検証。

to_epub は import 時に yomitoku/torch を必要としない（pypdfium2 は関数内で遅延
import）ため、OCR 環境なしで純粋関数レベルのテストが回る。
"""

import re
import xml.etree.ElementTree as ET

import pytest

from book_ir import normalize_text
from to_epub import (
    apply_upright,
    build_css,
    heading_title,
    render_chapter_xhtml,
    render_figure,
    render_inline,
    render_nav_list,
    render_nav_xhtml,
    render_plain,
    render_table,
    strip_ruby,
)

TCY_OPEN = '<span class="tcy">'


# --- 縦組みの正立処理（全角化・縦中横） ---------------------------------
#
# 入力は必ず normalize_text() を通した文字列で書く。normalize_text() の NFKC 正規化で
# 全角数字・全角英字はすべて半角になる（'第１２章' → '第12章'）ため、半角を直書きした
# テストは製品で通らない経路を見ているだけになり回帰を検出できない
# （感嘆符の分岐がデッドコードだった実績がある。docs/HANDOFF.md §2-1 参照）。
# 期待値は原書2冊の版面の実測から来ている。


@pytest.mark.parametrize(
    ("src", "expected"),
    [
        # 数字ちょうど2桁だけが縦中横（『イスラム教の論理』p20「第4章48節」の 48）
        ("第１２章", f"第{TCY_OPEN}12</span>章"),
        ("コーラン第４章４８節", f"コーラン第４章{TCY_OPEN}48</span>節"),
        # 1桁は全角にして正立させる。同 p20「第4章」の 4 は全角1字分を占める
        ("第３章", "第３章"),
        # 3桁以上も1字ずつ全角で正立。縦中横にすると1字幅に潰れて読めない
        ("２１６節", "２１６節"),          # 同 p15「第2章216節」→ ２/１/６ が縦に3字
        ("２０１１年", "２０１１年"),       # 同 p21「2011年」→ ２/０/１/１ が縦に4字
        ("全１２３頁", "全１２３頁"),
        # 小数点は中黒。1字分を占めて中央に来る（同 p113「2.2人」→ ２/・/２）
        ("２.２人", "２・２人"),
        # 『“町内会”は義務ですか?』の「27.8％」→ 27(縦中横)・中黒・8(全角)・％(全角)
        ("２７.８％", f"{TCY_OPEN}27</span>・８％"),
        # 大文字だけの略語は桁数によらず全角で1字ずつ。2文字でも縦中横にはしない
        # （同 p11「SNS戦略」→ S/N/S が縦に3字、p2「EUからの離脱」→ E/U が縦に2字）
        ("ＳＮＳ戦略", "ＳＮＳ戦略"),
        ("ＥＵからの離脱", "ＥＵからの離脱"),
        # 小文字を含む欧文語は横倒しのまま（同 p74「Telegram は」）
        ("Ｔｅｌｅｇｒａｍ", "Telegram"),
        # 単独の大文字1字は変換しない（実データでは OCR が語を割ったノイズしかない）
        ("Ａ社", "A社"),
    ],
)
def test_upright_follows_measured_typesetting_rules(src, expected):
    assert render_inline(normalize_text(src)) == expected


@pytest.mark.parametrize(
    "src",
    ["Web2.0", "Web2.0.3", "1.5GB", "2.0Web", "v1.5", "ＭＰ3", "iPhone", "a12b", "12a", "www.ABC.com"],
)
def test_upright_leaves_mixed_alphanumeric_tokens_whole(src):
    """英数字が混在するトークンは丸ごと素通りさせる

    途中だけ全角になる（'Web2.0' → 'Web2.０'、'1.5GB' → '１.5GB'）ほうが、
    丸ごと横倒しよりも見苦しい。小数点を独立した正規表現で処理すると 'Web2.0' の
    点まで中黒になるため、数字トークンは1本の正規表現でまとめて取る。
    """

    normalized = normalize_text(src)
    assert render_inline(normalized) == normalized


@pytest.mark.parametrize(
    ("src", "expected"),
    [
        # 変更前から縦中横になっていた形。ここを巻き込むと黙って退行する
        ("p.12", f"p.{TCY_OPEN}12</span>"),
        ("no.99", f"no.{TCY_OPEN}99</span>"),
        ("Fig.34", f"Fig.{TCY_OPEN}34</span>"),
        ("Ｖｏｌ.12", f"Vol.{TCY_OPEN}12</span>"),
        ("第１章p.12", f"第１章p.{TCY_OPEN}12</span>"),
        # 桁数の規則はドットの後でも変わらない（2桁以外は全角化して正立させる）
        ("p.5", "p.５"),
        ("p.123", "p.１２３"),
    ],
)
def test_digits_after_a_latin_period_still_stand_upright(src, expected):
    """ドットの除外は「直前が数字のとき」だけに絞る

    除外の目的は小数点の右側だけを拾わないこと（'Web2.0' → 'Web2.０' の防止）。
    ドットを一律に除外すると 'p.12'・'no.99'・'Fig.34' のような「英字＋ドット＋2桁」
    まで巻き込み、従来効いていた縦中横が消える。
    """

    assert render_inline(normalize_text(src)) == expected


@pytest.mark.parametrize("dot", ["·", "‧"])  # OCR が出す中点。PUNCT_MAP がどちらも ・ にする
@pytest.mark.parametrize(
    ("proofread", "raw_template", "expected"),
    [
        ("4.8%", "4{dot}8%", "４・８％"),
        # 2桁は縦中横のまま。中黒で切れても桁数の規則は同じ結果に落ちる
        ("27.8%", "27{dot}8%", f"{TCY_OPEN}27</span>・８％"),
        ("2.2人", "2{dot}2人", "２・２人"),
    ],
)
def test_decimal_point_reaches_the_same_output_by_both_paths(proofread, raw_template, expected, dot):
    """小数がレンダリングへ届く2経路が同値であることを固定する

    LLM 校正は正規化前の生テキストを見ており、OCR が読んだ '4·8%' を「小数点を
    ナカグロに誤認識」として '4.8%' に直す。そのため経路が2つある。
      1. 校正が当たった場合 …… '4.8％' が届く。数字トークンを '.' で split し、
         桁数規則を適用して '・' で join する
      2. 校正が当たらなかった場合 …… '4·8%' のまま normalize_text() に入り、
         PUNCT_MAP の '·' → '・' で '4・8％' が届く。中黒がトークンの区切りになり、
         '4' と '8' が独立した1桁トークンとして各々全角化される
    どちらも同じ出力に落ちなければ、校正キャッシュの当たり外れで版面が変わる。
    このテストの主張は「キャッシュヒット状況によって出力が変わらない」こと。
    """

    raw = raw_template.format(dot=dot)
    # 前提: レンダリングへ届く文字列そのものは経路によって違う
    assert normalize_text(proofread) != normalize_text(raw)

    assert render_inline(normalize_text(proofread)) == expected
    assert render_inline(normalize_text(raw)) == expected


@pytest.mark.parametrize("mark", ["!!", "!?", "?!", "??"])
def test_tcy_covers_two_char_exclamation_marks(mark):
    assert render_inline(f"なんと{mark}") == f"なんと{TCY_OPEN}{mark}</span>"


def test_upright_does_not_break_tag_attributes():
    """素朴な置換だと <img src="...fig02.png"> のパスや alt が壊れる"""

    html = '<img src="../images/fig02.png" alt="図12"/>'
    assert apply_upright(html) == html
    assert render_figure({"src": "fig02.png", "alt": "図12"}) == (
        '<figure class="h-figure"><img src="../images/fig02.png" alt="図12"/></figure>'
    )


@pytest.mark.parametrize(
    "text",
    [
        "｜第12章《12しょう》",  # 親文字にも <rt> にも数字がある
        "X線《12》",
    ],
)
def test_upright_skips_ruby_contents(text):
    """ルビは親文字も <rt> も丸ごと退避されるので、縦中横も全角化も入らない"""

    out = render_inline(text)
    assert TCY_OPEN not in out
    assert "12" in out and "１２" not in out


def test_tcy_applied_outside_ruby_only():
    out = render_inline("第12章｜天体《てんたい》の12")
    assert out == (
        f"第{TCY_OPEN}12</span>章"
        "<ruby>天体<rp>（</rp><rt>てんたい</rt><rp>）</rp></ruby>"
        f"の{TCY_OPEN}12</span>"
    )


def test_render_inline_still_escapes():
    assert render_inline('<&"') == "&lt;&amp;\""


# --- 正立処理は縦組みのときだけ通す -------------------------------------
#
# 縦中横は「横書きでは CSS を出さない」ことで無効化できたが、全角化は文字そのものを
# 変えてしまうので同じ手が使えない。縦組みフラグを render_inline() まで引数で
# 貫通させ、経路の途中で落ちていないことを本文・nav の両方で押さえる。

HORIZONTAL_CHAPTER = {
    "blocks": [
        {"kind": "heading", "level": "大", "lines": ["第１２章"], "page": 1},
        {"kind": "para", "lines": ["２０１１年は２.２人、ＳＮＳと１字"], "page": 1},
    ],
    "section_type": "chapter",
}


def test_horizontal_chapter_gets_no_conversion():
    """--horizontal のビルドでは全角化も縦中横も通さない（横書きで不格好なため）"""

    html = render_chapter_xhtml(HORIZONTAL_CHAPTER, vertical=False)
    assert "<h1>第12章</h1>" in html
    assert "<p>2011年は2.2人、SNSと1字</p>" in html
    assert "tcy" not in html


def test_vertical_chapter_gets_conversion():
    """同じ入力でも縦組みなら本文・見出しの両方に正立処理がかかる"""

    html = render_chapter_xhtml(HORIZONTAL_CHAPTER, vertical=True)
    assert f"<h1>第{TCY_OPEN}12</span>章</h1>" in html
    assert "<p>２０１１年は２・２人、ＳＮＳと１字</p>" in html


@pytest.mark.parametrize(
    ("vertical", "expected"),
    [(True, f"第{TCY_OPEN}12</span>章　２０１１年"), (False, "第12章　2011年")],
)
def test_nav_follows_the_build_direction(vertical, expected):
    """nav.xhtml は本文と同じ style.css を読むので、組方向も正立処理も本文と揃える"""

    title = heading_title(["第１２章", "２０１１年"])
    assert title == "第12章　2011年"  # 前提: normalize_text 済みで半角のまま届く

    html = render_nav_xhtml(
        [{"title": title, "href": "text/ch000.xhtml", "children": []}],
        "text/ch000.xhtml",
        vertical=vertical,
    )
    assert f">{expected}</a>" in html
    if not vertical:
        assert "tcy" not in html
    ET.fromstring(html)


def test_table_cells_are_never_converted():
    """表は縦組みの本でも horizontal-tb に戻すので、セルは横組みの規則に従う

    中黒化は縦組み専用の約物で、横組みのセルに出ると数値の意味が壊れる。
    縦中横も同じ理由で build_css() が table.h-table .tcy で無効化している。
    """

    html = render_table(
        {
            "n_row": 1,
            "n_col": 2,
            "cells": [
                {"row": 1, "col": 1, "contents": "２.２人"},
                {"row": 1, "col": 2, "contents": "第１２章"},
            ],
        }
    )
    assert "<td>2.2人</td>" in html
    assert "<td>第12章</td>" in html
    assert "tcy" not in html


# --- ルビ ---------------------------------------------------------------


@pytest.mark.parametrize("text", ["｜天体《てんたい》", "天体《てんたい》"])
def test_ruby_has_rp_fallback(text):
    assert render_inline(text) == "<ruby>天体<rp>（</rp><rt>てんたい</rt><rp>）</rp></ruby>"


def test_ruby_rp_parens_do_not_retrigger_conversion():
    """挿入する（）は《》ではないので RUBY_BARE_RE に再マッチせず二重変換されない"""

    out = render_inline("｜天体《てんたい》")
    assert out.count("<ruby>") == 1
    assert out.count("<rt>") == 1
    ET.fromstring(f"<p>{out}</p>")


def test_css_declares_ruby_position_over():
    """初期値は alternate なので、複数ルビで左右交互になるのを防ぐには明示が要る"""

    for horizontal in (False, True):
        css = build_css(horizontal=horizontal)
        assert "ruby-position: over;" in css
        assert "-webkit-ruby-position: over;" in css
        assert "-epub-ruby-position: over;" in css


def test_css_never_hides_rp():
    """rp を自前で隠すと、ルビ非対応で CSS だけ効くリーダーで括弧まで消える"""

    for horizontal in (False, True):
        assert "rp {" not in build_css(horizontal=horizontal)
        assert "display: none" not in build_css(horizontal=horizontal)


# --- CSS ----------------------------------------------------------------


def test_css_vertical_has_tcy_rules():
    css = build_css(horizontal=False)
    # レガシー接頭辞は値が horizontal、標準は all。標準版が後に来て上書きするのが正しい
    assert "-webkit-text-combine: horizontal;" in css
    assert "text-combine-upright: all;" in css
    assert "-epub-text-combine: horizontal;" in css
    assert css.index("-webkit-text-combine: horizontal;") < css.index("text-combine-upright: all;")
    # 表・図版は既に horizontal-tb なので、その中の縦中横は無効化する
    assert "table.h-table .tcy, figure.h-figure .tcy {" in css


def test_css_horizontal_has_no_tcy_rules():
    """横書きでは .tcy 自体が無意味なので規則ごと出さない"""

    css = build_css(horizontal=True)
    assert ".tcy" not in css
    assert "text-combine" not in css


def test_css_tcy_rules_are_outside_html_body_block():
    """self_check は html, body ブロックを丸ごと切り出すので、中に紛れ込ませない"""

    css = build_css(horizontal=False)
    body_block = css[css.index("html, body {") : css.index("}")]
    assert "text-combine" not in body_block
    assert "tcy" not in body_block


@pytest.mark.parametrize("horizontal", [False, True])
def test_css_has_kinsoku_and_hanging_punctuation(horizontal):
    css = build_css(horizontal=horizontal)
    assert "line-break: strict;" in css
    assert "hanging-punctuation: allow-end;" in css


@pytest.mark.parametrize("horizontal", [False, True])
def test_css_omits_harmful_vertical_declarations(horizontal):
    """UA の縦組み処理と二重にかかる／初期値のほうが正しい指定は書かない"""

    css = build_css(horizontal=horizontal)
    for decl in ("vrt2", "font-feature-settings", "text-orientation", "text-spacing"):
        assert decl not in css


# --- nav ----------------------------------------------------------------


def test_nav_title_has_no_raw_ruby_notation():
    """normalize_text() は ｜《》 を残すので、nav でも render_inline を通す必要がある"""

    title = heading_title(["第１章　｜天体《てんたい》の運行"])
    assert "《" in title  # 前提: heading_title 段階では記法が生で残っている

    html = render_nav_list([{"title": title, "href": "text/ch000.xhtml", "children": []}])
    for mark in ("｜", "《", "》"):
        assert mark not in html
    assert "<ruby>天体<rp>（</rp><rt>てんたい</rt><rp>）</rp></ruby>" in html
    ET.fromstring(html)


def test_nav_title_is_not_double_escaped():
    """render_inline が自前でエスケープするので escape() を重ねてはいけない"""

    html = render_nav_list([{"title": "A & B", "href": "text/ch000.xhtml", "children": []}])
    assert "A &amp; B" in html
    assert "&amp;amp;" not in html


def test_nav_children_are_rendered_recursively():
    tree = [
        {
            "title": "第Ⅰ部",
            "href": "text/ch000.xhtml",
            "children": [{"title": "｜天体《てんたい》", "href": "text/ch001.xhtml", "children": []}],
        }
    ]
    html = render_nav_list(tree)
    assert "《" not in html
    assert html.count("<ol>") == 2
    ET.fromstring(html)


# --- normalize_text 経由の統合（実パイプラインの経路） ------------------


def test_tcy_fires_on_normalized_exclamations():
    """book_ir の PUNCT_MAP が ! ? を全角化するので、全角も対象でないと発動しない

    半角入力だけを検査するテストは、実パイプラインで一度も通らない経路を
    見ているだけで回帰を検出できない。
    """

    normalized = normalize_text("えっ!?　まさか!!")
    assert normalized == "えっ！？まさか！！"     # 前提: 全角化されている
    html = render_inline(normalized)
    # 全角のまま span に入れると1文字幅で潰れるため半角に戻す
    assert '<span class="tcy">!?</span>' in html
    assert '<span class="tcy">!!</span>' in html
    assert "！？" not in html


def test_upright_fires_on_normalized_digits():
    """全角数字は NFKC で半角化されるので、そのまま正立処理の対象になる

    半角のまま残すと縦組みで 90 度横倒しになる。2桁だけが縦中横で、
    それ以外の桁数は全角へ戻して1字ずつ正立させる。
    """

    html = render_inline(normalize_text("第１２章と１９８０年と３人"))
    assert '<span class="tcy">12</span>' in html
    assert "１９８０年" in html and "1980" not in html   # 4桁は全角で1字ずつ
    assert "３人" in html and '<span class="tcy">3' not in html   # 1桁も全角で正立


def test_single_exclamation_is_not_combined():
    assert "tcy" not in render_inline(normalize_text("すごい!"))


# --- <title> / alt のルビ記法 ------------------------------------------


def test_chapter_title_has_no_ruby_notation():
    """<title> はマークアップを置けないので親文字だけ残す"""

    chapter = {
        "blocks": [{"kind": "heading", "level": "大", "lines": ["第１２章　｜天体《てんたい》の運行"], "page": 1}],
        "section_type": "chapter",
    }
    html = render_chapter_xhtml(chapter)
    title = re.search(r"<title>(.*?)</title>", html).group(1)
    assert "｜" not in title and "《" not in title and "》" not in title
    assert "天体" in title and "てんたい" not in title


def test_figure_alt_has_no_ruby_notation():
    html = render_figure({"src": "p0015_fig01.png", "alt": "図１２　｜天体《てんたい》の図"})
    alt = re.search(r'alt="(.*?)"', html).group(1)
    assert "｜" not in alt and "《" not in alt
    assert "天体" in alt and "てんたい" not in alt
    assert "fig01.png" in html          # 画像パスは無傷


def test_strip_ruby_does_not_escape():
    """strip_ruby はエスケープしない（埋め込み側が escape するため二重適用を避ける）"""

    assert strip_ruby("A & B｜漢字《かんじ》") == "A & B漢字"
    assert render_plain("A & B｜漢字《かんじ》") == "A &amp; B漢字"
