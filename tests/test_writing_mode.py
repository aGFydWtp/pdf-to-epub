"""縦書き／横書き切り替え（--horizontal）が CSS・OPF に正しく反映されるかの検証。

to_epub は import 時に yomitoku/torch を必要としない（pypdfium2 は関数内で遅延
import）ため、OCR 環境なしで純粋関数レベルのテストが回る。
"""

import xml.etree.ElementTree as ET

import pytest

from to_epub import build_css, build_opf

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


@pytest.mark.parametrize("horizontal", [False, True])
def test_css_headings_use_logical_block_margins(horizontal):
    """見出しの前後空きは縦書きでも行内方向にならないよう論理プロパティで指定する"""

    css = build_css(horizontal=horizontal)
    for decl in ("margin-block-start: 1.2em;", "margin-block-end: 0.6em;"):
        assert decl in css
    if not horizontal:
        # 縦書きでは物理プロパティのフォールバックを出さない（行内方向の余白になるため）
        assert "margin: 1.2em 0 0.6em;" not in css


# --- OPF ---------------------------------------------------------------


def test_opf_vertical_has_rtl_and_vertical_primary_writing_mode():
    opf = make_opf(horizontal=False)
    assert 'page-progression-direction="rtl"' in opf
    assert '<meta property="primary-writing-mode">vertical-rl</meta>' in opf


def test_opf_horizontal_has_no_ppd_and_horizontal_primary_writing_mode():
    opf = make_opf(horizontal=True)
    assert "page-progression-direction" not in opf
    assert '<meta property="primary-writing-mode">horizontal-tb</meta>' in opf


@pytest.mark.parametrize(
    ("horizontal", "expected_mode", "expected_ppd"),
    [(False, "vertical-rl", "rtl"), (True, "horizontal-tb", None)],
)
def test_opf_is_well_formed_xml(horizontal, expected_mode, expected_ppd):
    root = ET.fromstring(make_opf(horizontal=horizontal))

    spine = root.find("opf:spine", OPF_NS)
    assert spine.get("page-progression-direction") == expected_ppd

    pwm = [
        (m.text or "").strip()
        for m in root.iterfind("opf:metadata/opf:meta", OPF_NS)
        if m.get("property") == "primary-writing-mode"
    ]
    assert pwm == [expected_mode]
