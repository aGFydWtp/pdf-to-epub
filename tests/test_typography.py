"""縦書きタイポグラフィ（縦中横・ルビ・禁則）と nav のルビ化の検証。

to_epub は import 時に yomitoku/torch を必要としない（pypdfium2 は関数内で遅延
import）ため、OCR 環境なしで純粋関数レベルのテストが回る。
"""

import re
import xml.etree.ElementTree as ET

import pytest

from book_ir import normalize_text
from to_epub import (
    add_tcy,
    build_css,
    heading_title,
    render_chapter_xhtml,
    render_figure,
    render_inline,
    render_nav_list,
    render_plain,
    strip_ruby,
)

TCY_OPEN = '<span class="tcy">'


# --- 縦中横 -------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # 半角数字ちょうど2桁だけが対象
        ("第12章", f"第{TCY_OPEN}12</span>章"),
        # 1桁は回転しないので不要
        ("第3章", "第3章"),
        # 3桁以上は 1 文字幅に潰れて判読不能になるので絶対に対象にしない
        ("全123頁", "全123頁"),
        ("1980年", "1980年"),
        # 欧字と地続きの数字は識別子・単位の一部なので対象外
        ("a12b", "a12b"),
        ("12a", "12a"),
    ],
)
def test_tcy_targets_only_two_digit_runs(text, expected):
    assert render_inline(text) == expected


@pytest.mark.parametrize("mark", ["!!", "!?", "?!", "??"])
def test_tcy_covers_two_char_exclamation_marks(mark):
    assert render_inline(f"なんと{mark}") == f"なんと{TCY_OPEN}{mark}</span>"


def test_tcy_does_not_break_tag_attributes():
    """素朴な置換だと <img src="...fig02.png"> のパスや alt が壊れる"""

    html = '<img src="../images/fig02.png" alt="図12"/>'
    assert add_tcy(html) == html
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
def test_tcy_skips_ruby_contents(text):
    """ルビは親文字も <rt> も丸ごと退避されるので縦中横が入らない"""

    assert TCY_OPEN not in render_inline(text)


def test_tcy_applied_outside_ruby_only():
    out = render_inline("第12章｜天体《てんたい》の12")
    assert out == (
        f"第{TCY_OPEN}12</span>章"
        "<ruby>天体<rp>（</rp><rt>てんたい</rt><rp>）</rp></ruby>"
        f"の{TCY_OPEN}12</span>"
    )


def test_render_inline_still_escapes():
    assert render_inline('<&"') == "&lt;&amp;\""


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


def test_tcy_fires_on_normalized_digits():
    """全角数字は NFKC で半角化されるので、そのまま2桁の縦中横対象になる"""

    html = render_inline(normalize_text("第１２章と１９８０年と３人"))
    assert '<span class="tcy">12</span>' in html
    assert "1980" in html and '<span class="tcy">1980' not in html   # 4桁は対象外
    assert '<span class="tcy">3' not in html                          # 1桁は対象外


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
