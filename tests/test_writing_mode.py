"""縦書き／横書き切り替え（--horizontal）が CSS・OPF に正しく反映されるかの検証。

to_epub は import 時に yomitoku/torch を必要としない（pypdfium2 は関数内で遅延
import）ため、OCR 環境なしで純粋関数レベルのテストが回る。
"""

import xml.etree.ElementTree as ET
import zipfile

import pytest

from to_epub import (
    build_css,
    build_opf,
    primary_writing_mode,
    render_nav_xhtml,
    self_check,
    xhtml_page,
)

OPF_NS = {"opf": "http://www.idpf.org/2007/opf"}


def make_opf(horizontal: bool) -> str:
    """テスト用の最小構成 OPF を組み立てる"""

    return build_opf(
        title="テスト書名",
        author="テスト著者",
        publisher="テスト出版",
        book_uuid="00000000-0000-4000-8000-000000000000",
        modified="2026-01-01T00:00:00Z",
        manifest_items=[
            {"id": "css", "href": "style.css", "media_type": "text/css"},
            {"id": "nav", "href": "nav.xhtml", "media_type": "application/xhtml+xml", "properties": "nav"},
            {"id": "ch000", "href": "text/ch000.xhtml", "media_type": "application/xhtml+xml"},
        ],
        spine_items=[{"idref": "nav", "linear": "no"}, {"idref": "ch000"}],
        horizontal=horizontal,
    )


# --- CSS ---------------------------------------------------------------


def test_css_vertical_uses_vertical_rl_and_overrides_tables():
    css = build_css(horizontal=False)
    assert "writing-mode: vertical-rl;" in css
    # 表・図版は縦書き本文の中でも横組みに戻す
    assert "table.h-table, figure.h-figure {" in css
    assert "writing-mode: horizontal-tb;" in css


def test_css_horizontal_has_no_vertical_rl():
    css = build_css(horizontal=True)
    assert "writing-mode: horizontal-tb;" in css
    assert "vertical-rl" not in css
    # writing-mode の上書きだけを落とし、見た目用のスタイルは残す
    assert "table.h-table, figure.h-figure {" not in css
    assert "border-collapse: collapse;" in css
    assert "border: 1px solid #333;" in css


@pytest.mark.parametrize(
    ("horizontal", "physical"),
    # 物理フォールバックの対応は組方向で決まる。横書きは上下、縦書きは
    # vertical-rl なので block-start=右 / block-end=左。
    [(False, "margin: 0 1.2em 0 0.6em;"), (True, "margin: 1.2em 0 0.6em;")],
)
def test_css_headings_use_logical_block_margins(horizontal, physical):
    """見出しの前後空きは縦書きでも行内方向にならないよう論理プロパティで指定する"""

    css = build_css(horizontal=horizontal)
    for decl in ("margin-block-start: 1.2em;", "margin-block-end: 0.6em;"):
        assert decl in css
    # margin-block-* 非対応の古いエンジン向けに、組方向に合った物理指定を先に置く
    assert physical in css
    assert css.index(physical) < css.index("margin-block-start: 1.2em;")


# --- OPF ---------------------------------------------------------------


def test_opf_vertical_has_rtl_and_vertical_primary_writing_mode():
    opf = make_opf(horizontal=False)
    assert 'page-progression-direction="rtl"' in opf
    assert '<meta name="primary-writing-mode" content="vertical-rl"/>' in opf


def test_opf_horizontal_has_no_ppd_and_horizontal_primary_writing_mode():
    opf = make_opf(horizontal=True)
    assert "page-progression-direction" not in opf
    # 日本語横書きの Kindle 語彙は horizontal-tb ではなく horizontal-lr
    assert '<meta name="primary-writing-mode" content="horizontal-lr"/>' in opf


@pytest.mark.parametrize("horizontal", [False, True])
def test_opf_primary_writing_mode_is_not_epub3_property(horizontal):
    """接頭辞なしの property="primary-writing-mode" は epubcheck が OPF-027 で弾く"""

    assert 'property="primary-writing-mode"' not in make_opf(horizontal=horizontal)


@pytest.mark.parametrize(
    ("horizontal", "expected_ppd"), [(False, "rtl"), (True, None)]
)
def test_opf_is_well_formed_xml(horizontal, expected_ppd):
    root = ET.fromstring(make_opf(horizontal=horizontal))

    spine = root.find("opf:spine", OPF_NS)
    assert spine.get("page-progression-direction") == expected_ppd

    pwm = [
        (m.get("content") or "").strip()
        for m in root.iterfind("opf:metadata/opf:meta", OPF_NS)
        if m.get("name") == "primary-writing-mode"
    ]
    assert pwm == [primary_writing_mode(horizontal)]


# --- self_check ---------------------------------------------------------


CONTAINER_XML = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    '<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
    '<rootfiles><rootfile full-path="OEBPS/package.opf" '
    'media-type="application/oebps-package+xml"/></rootfiles></container>'
)


def write_epub(path, horizontal, *, css=None, opf=None):
    """縦横整合チェックにかけられる最小構成の EPUB を書き出す

    css / opf を渡すと、その内容で差し替える（壊れた EPUB を検出できるかの検証用）。
    """

    manifest_items = [
        {"id": "css", "href": "style.css", "media_type": "text/css"},
        {"id": "nav", "href": "nav.xhtml", "media_type": "application/xhtml+xml", "properties": "nav"},
        {"id": "ch000", "href": "ch000.xhtml", "media_type": "application/xhtml+xml"},
    ]
    spine_items = [{"idref": "nav", "linear": "no"}, {"idref": "ch000"}]
    if opf is None:
        opf = build_opf(
            title="テスト書名",
            author="テスト著者",
            publisher="テスト出版",
            book_uuid="00000000-0000-4000-8000-000000000000",
            modified="2026-01-01T00:00:00Z",
            manifest_items=manifest_items,
            spine_items=spine_items,
            horizontal=horizontal,
        )
    nav = render_nav_xhtml([{"title": "章1", "href": "ch000.xhtml", "children": []}], "ch000.xhtml")
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("mimetype", "application/epub+zip", zipfile.ZIP_STORED)
        z.writestr("META-INF/container.xml", CONTAINER_XML)
        z.writestr("OEBPS/package.opf", opf)
        z.writestr("OEBPS/style.css", build_css(horizontal) if css is None else css)
        z.writestr("OEBPS/nav.xhtml", nav)
        z.writestr("OEBPS/ch000.xhtml", xhtml_page("章1", "<p>本文</p>", "style.css", "chapter"))
    return path


@pytest.mark.parametrize("horizontal", [False, True])
def test_self_check_passes_for_matching_mode(tmp_path, horizontal):
    epub = write_epub(tmp_path / "book.epub", horizontal)
    assert self_check(epub, horizontal=horizontal) == []


@pytest.mark.parametrize("horizontal", [False, True])
def test_self_check_detects_all_three_mismatches(tmp_path, horizontal):
    """縦横を取り違えた EPUB では CSS・ppd・メタの3点すべてを報告する"""

    epub = write_epub(tmp_path / "book.epub", horizontal)
    problems = self_check(epub, horizontal=not horizontal)
    assert len(problems) == 3
    joined = "\n".join(problems)
    assert "writing-mode" in joined
    assert "page-progression-direction" in joined
    assert "primary-writing-mode" in joined


def test_self_check_detects_vendor_prefix_mismatch(tmp_path):
    """接頭辞付きの値だけがずれているケースも見逃さない"""

    css = build_css(horizontal=False).replace(
        "-webkit-writing-mode: vertical-rl;", "-webkit-writing-mode: horizontal-tb;"
    )
    epub = write_epub(tmp_path / "book.epub", False, css=css)
    problems = self_check(epub, horizontal=False)
    assert len(problems) == 1
    assert "writing-mode" in problems[0]


def test_self_check_reports_missing_spine(tmp_path):
    """spine 欠落で例外を投げず problems として報告する"""

    opf = build_opf(
        title="T",
        author="",
        publisher="",
        book_uuid="00000000-0000-4000-8000-000000000000",
        modified="2026-01-01T00:00:00Z",
        manifest_items=[{"id": "css", "href": "style.css", "media_type": "text/css"}],
        spine_items=[],
        horizontal=False,
    )
    opf = opf.replace("<spine page-progression-direction=\"rtl\">\n\n</spine>\n", "")
    epub = write_epub(tmp_path / "book.epub", False, opf=opf)
    problems = self_check(epub, horizontal=False)
    assert any("spine" in p for p in problems)
