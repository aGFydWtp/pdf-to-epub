"""NCX（toc.ncx）併載の検証。

EPUB3 に NCX を入れても epubcheck 5.1.0 は警告すら出さないが、必須条件が2つある
（実測）:
  - OPF の spine に toc="ncx" が必要（無いと RSC-005）
  - dtb:uid が dc:identifier と完全一致していること（不一致だと NCX-001）
さらに navLabel/text はマークアップを一切許さないため、nav.xhtml と同じ文字列
（ルビ入り）を流し込むと RSC-005 で落ちる。ここではその境界を固定する。

to_epub は import 時に yomitoku/torch を必要としない（pypdfium2 は関数内で遅延
import）ため、OCR 環境なしで純粋関数レベルのテストが回る。
"""

import re
import xml.etree.ElementTree as ET
import zipfile

import pytest

from to_epub import (
    NCX_HREF,
    NCX_ID,
    NCX_MEDIA_TYPE,
    build_css,
    build_opf,
    heading_title,
    nav_depth,
    render_nav_list,
    render_ncx,
    render_plain,
    render_nav_xhtml,
    self_check,
    xhtml_page,
)

NCX_NS = {"ncx": "http://www.daisy.org/z3986/2005/ncx/"}
OPF_NS = {"opf": "http://www.idpf.org/2007/opf"}
BOOK_ID = "urn:uuid:00000000-0000-4000-8000-000000000000"

# 部／大見出し／中見出しの3階層。ルビ記法を含む見出しを混ぜてある。
NAV_TREE = [
    {
        "title": "第Ⅰ部　基礎",
        "href": "text/ch000.xhtml",
        "children": [
            {
                "title": "第12章｜天体《てんたい》の運行",
                "href": "text/ch001.xhtml",
                "children": [
                    {"title": "1　序説", "href": "text/ch001.xhtml#h0001", "children": []}
                ],
            }
        ],
    },
    {"title": "あとがき", "href": "text/ch002.xhtml", "children": []},
]


def make_ncx(nav_tree=None, title="テスト書名"):
    return render_ncx(
        nav_tree if nav_tree is not None else NAV_TREE,
        title=title,
        book_id=BOOK_ID,
        first_chapter_href="text/ch000.xhtml",
    )


# --- render_plain -------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # ｜親《ルビ》は親文字だけを残す（｜ の除去漏れに注意）
        ("第12章｜天体《てんたい》の運行", "第12章天体の運行"),
        # ｜なしの漢字＋《》も同様
        ("天体《てんたい》の運行", "天体の運行"),
        # ルビの開始記号として使われなかった裸の ｜ も残さない
        ("ここに｜だけ", "ここにだけ"),
        # 縦中横の span も付かない（NCX にはタグを一切出せない）
        ("第12章", "第12章"),
    ],
)
def test_render_plain_strips_ruby_notation(text, expected):
    assert render_plain(text) == expected


def test_render_plain_escapes_xml():
    assert render_plain('A & B <c> "d"') == "A &amp; B &lt;c&gt; \"d\""


def test_render_plain_and_render_inline_differ_on_ruby():
    """nav.xhtml はルビ化、NCX はプレーン化。同じ文字列は流せない"""

    from to_epub import render_inline

    text = "｜天体《てんたい》"
    assert "<ruby>" in render_inline(text)
    assert render_plain(text) == "天体"


# --- NCX の構造 ---------------------------------------------------------


def test_ncx_is_well_formed_xml():
    root = ET.fromstring(make_ncx())
    assert root.tag == "{http://www.daisy.org/z3986/2005/ncx/}ncx"
    assert root.get("version") == "2005-1"


def test_ncx_uid_matches_opf_identifier():
    """不一致だと epubcheck が NCX-001 で落ちる"""

    ncx = ET.fromstring(make_ncx())
    uid = [
        m.get("content")
        for m in ncx.iterfind("ncx:head/ncx:meta", NCX_NS)
        if m.get("name") == "dtb:uid"
    ]
    assert uid == [BOOK_ID]

    opf = ET.fromstring(
        build_opf(
            title="テスト書名",
            author="",
            publisher="",
            book_uuid="00000000-0000-4000-8000-000000000000",
            modified="2026-01-01T00:00:00Z",
            manifest_items=[{"id": NCX_ID, "href": NCX_HREF, "media_type": NCX_MEDIA_TYPE}],
            spine_items=[],
            horizontal=False,
            ncx_id=NCX_ID,
        )
    )
    identifiers = [
        (e.text or "").strip()
        for e in opf.iterfind("opf:metadata/{http://purl.org/dc/elements/1.1/}identifier", OPF_NS)
        if e.get("id") == opf.get("unique-identifier")
    ]
    assert identifiers == uid


def test_ncx_head_has_at_least_one_meta():
    """空の <head> は RSC-005（missing required element "meta"）"""

    metas = ET.fromstring(make_ncx()).findall("ncx:head/ncx:meta", NCX_NS)
    assert len(metas) >= 1


def test_ncx_doctitle_precedes_navmap():
    """docTitle は必須で navMap より前。逆だと RSC-005"""

    ncx = make_ncx()
    assert "<docTitle>" in ncx
    assert ncx.index("<docTitle>") < ncx.index("<navMap>")

    names = [e.tag.split("}")[-1] for e in ET.fromstring(ncx)]
    assert names == ["head", "docTitle", "navMap"]


def test_ncx_nav_labels_have_no_markup_or_ruby_notation():
    """navLabel/text はプレーンテキストのみ。<ruby> を入れると RSC-005"""

    ncx = make_ncx()
    assert "<ruby>" not in ncx
    assert "<span" not in ncx
    for text in ET.fromstring(ncx).iterfind(".//ncx:navLabel/ncx:text", NCX_NS):
        assert len(text) == 0  # 子要素を持たない
        for mark in ("｜", "《", "》", "<", ">"):
            assert mark not in (text.text or "")


def test_ncx_nav_label_keeps_ruby_base_text():
    """ルビ部を落としても親文字は残す"""

    labels = [
        t.text for t in ET.fromstring(make_ncx()).iterfind(".//ncx:navLabel/ncx:text", NCX_NS)
    ]
    assert "第12章天体の運行" in labels


# --- 階層 ---------------------------------------------------------------


def test_ncx_hierarchy_matches_nav_tree():
    """navPoint の入れ子が nav_tree（＝nav.xhtml の <ol>）と対応する"""

    nav_map = ET.fromstring(make_ncx()).find("ncx:navMap", NCX_NS)

    def to_tree(parent):
        return [
            {
                "title": p.find("ncx:navLabel/ncx:text", NCX_NS).text,
                "href": p.find("ncx:content", NCX_NS).get("src"),
                "children": to_tree(p),
            }
            for p in parent.findall("ncx:navPoint", NCX_NS)
        ]

    def expected(nodes):
        return [
            {"title": render_plain(n["title"]), "href": n["href"], "children": expected(n["children"])}
            for n in nodes
        ]

    assert to_tree(nav_map) == expected(NAV_TREE)
    # nav.xhtml 側と同じ階層数（<ol> の数 = ネストを持つノードの数）
    assert render_nav_list(NAV_TREE).count("<ol>") == 3


def test_ncx_play_order_is_document_order_from_one():
    points = ET.fromstring(make_ncx()).iterfind(".//ncx:navPoint", NCX_NS)
    orders = [int(p.get("playOrder")) for p in points]
    # iterfind は文書順（pre-order）で返る
    assert orders == list(range(1, len(orders) + 1))
    assert len(orders) == 4


def test_ncx_depth_meta_matches_tree():
    depth = [
        m.get("content")
        for m in ET.fromstring(make_ncx()).iterfind("ncx:head/ncx:meta", NCX_NS)
        if m.get("name") == "dtb:depth"
    ]
    assert depth == [str(nav_depth(NAV_TREE))] == ["3"]


def test_ncx_empty_tree_falls_back_to_one_nav_point():
    """navMap は最低1つの navPoint が必要"""

    points = ET.fromstring(make_ncx([])).findall(".//ncx:navPoint", NCX_NS)
    assert len(points) == 1
    assert points[0].find("ncx:content", NCX_NS).get("src") == "text/ch000.xhtml"


# --- OPF 側 -------------------------------------------------------------


def make_opf(ncx_id=NCX_ID, *, with_ncx_item=True, horizontal=False):
    manifest_items = [
        {"id": "css", "href": "style.css", "media_type": "text/css"},
        {"id": "nav", "href": "nav.xhtml", "media_type": "application/xhtml+xml", "properties": "nav"},
        {"id": "ch000", "href": "ch000.xhtml", "media_type": "application/xhtml+xml"},
    ]
    if with_ncx_item:
        manifest_items.append({"id": NCX_ID, "href": NCX_HREF, "media_type": NCX_MEDIA_TYPE})
    return build_opf(
        title="テスト書名",
        author="テスト著者",
        publisher="テスト出版",
        book_uuid="00000000-0000-4000-8000-000000000000",
        modified="2026-01-01T00:00:00Z",
        manifest_items=manifest_items,
        spine_items=[{"idref": "nav", "linear": "no"}, {"idref": "ch000"}],
        horizontal=horizontal,
        ncx_id=ncx_id,
    )


def test_opf_registers_ncx_and_sets_spine_toc():
    opf = make_opf()
    assert f'<item id="{NCX_ID}" href="{NCX_HREF}" media-type="{NCX_MEDIA_TYPE}"/>' in opf
    assert f'<spine toc="{NCX_ID}" page-progression-direction="rtl">' in opf

    root = ET.fromstring(opf)
    spine = root.find("opf:spine", OPF_NS)
    assert spine.get("toc") == NCX_ID
    # NCX は spine に itemref として並べない
    assert NCX_ID not in [it.get("idref") for it in spine]


def test_opf_horizontal_keeps_toc_without_ppd():
    opf = make_opf(horizontal=True)
    assert f'<spine toc="{NCX_ID}">' in opf
    assert "page-progression-direction" not in opf


def test_opf_omits_toc_when_no_ncx():
    """既存の呼び出し側（ncx_id 未指定）は従来どおりの spine を出す"""

    assert 'toc="' not in make_opf(ncx_id=None, with_ncx_item=False)


def test_opf_has_no_rendition_metadata():
    """rendition:* はリフロー型では既定値と同じで実益がないため出さない"""

    opf = make_opf()
    assert "rendition:" not in opf
    assert "prefix=" not in opf


# --- self_check ---------------------------------------------------------

CONTAINER_XML = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    '<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
    '<rootfiles><rootfile full-path="OEBPS/package.opf" '
    'media-type="application/oebps-package+xml"/></rootfiles></container>'
)


def write_epub(path, *, ncx=None, opf=None):
    """NCX 入りの最小構成 EPUB を書き出す（ncx / opf を渡すと差し替える）"""

    with zipfile.ZipFile(path, "w") as z:
        z.writestr("mimetype", "application/epub+zip", zipfile.ZIP_STORED)
        z.writestr("META-INF/container.xml", CONTAINER_XML)
        z.writestr("OEBPS/package.opf", make_opf() if opf is None else opf)
        z.writestr("OEBPS/style.css", build_css(horizontal=False))
        z.writestr(
            "OEBPS/nav.xhtml",
            render_nav_xhtml([{"title": "章1", "href": "ch000.xhtml", "children": []}], "ch000.xhtml"),
        )
        z.writestr("OEBPS/ch000.xhtml", xhtml_page("章1", "<p>本文</p>", "style.css", "chapter"))
        z.writestr(
            f"OEBPS/{NCX_HREF}",
            render_ncx(
                [{"title": "章1", "href": "ch000.xhtml", "children": []}],
                title="テスト書名",
                book_id=BOOK_ID,
                first_chapter_href="ch000.xhtml",
            )
            if ncx is None
            else ncx,
        )
    return path


def test_self_check_passes_with_ncx(tmp_path):
    assert self_check(write_epub(tmp_path / "book.epub"), horizontal=False) == []


def test_self_check_detects_uid_mismatch(tmp_path):
    """dtb:uid のずれは epubcheck の NCX-001 相当なので self_check でも捕まえる"""

    ncx = render_ncx(
        [{"title": "章1", "href": "ch000.xhtml", "children": []}],
        title="テスト書名",
        book_id="urn:uuid:ffffffff-ffff-4fff-8fff-ffffffffffff",
        first_chapter_href="ch000.xhtml",
    )
    problems = self_check(write_epub(tmp_path / "book.epub", ncx=ncx), horizontal=False)
    assert any("dtb:uid" in p for p in problems)


def test_self_check_detects_missing_spine_toc(tmp_path):
    """NCX を manifest に入れて toc 属性を落とすと epubcheck が RSC-005 で落ちる"""

    opf = make_opf().replace(f'<spine toc="{NCX_ID}"', "<spine")
    problems = self_check(write_epub(tmp_path / "book.epub", opf=opf), horizontal=False)
    assert any("toc 属性" in p for p in problems)


def test_self_check_detects_toc_pointing_nowhere(tmp_path):
    opf = make_opf(with_ncx_item=True).replace(
        f'<item id="{NCX_ID}" href="{NCX_HREF}" media-type="{NCX_MEDIA_TYPE}"/>\n', ""
    )
    # NCX ファイル自体は zip に残るので manifest 未登録としても検出される
    problems = self_check(write_epub(tmp_path / "book.epub", opf=opf), horizontal=False)
    assert any("toc 属性" in p for p in problems)


def test_ncx_title_from_heading_title_is_plain(tmp_path):
    """heading_title() が返すルビ記法入りの文字列をそのまま流しても記法が漏れない"""

    title = heading_title(["第１章　｜天体《てんたい》の運行"])
    assert "《" in title
    ncx = render_ncx(
        [{"title": title, "href": "text/ch000.xhtml", "children": []}],
        title=title,
        book_id=BOOK_ID,
        first_chapter_href="text/ch000.xhtml",
    )
    for mark in ("｜", "《", "》"):
        assert mark not in ncx
    ET.fromstring(ncx)


# --- playOrder / id の一意性 --------------------------------------------


DUP_HREF_TREE = [
    # 部と配下の章が同じファイルを指す最悪ケース（playOrder 衝突の再現条件）
    {"title": "第Ⅰ部", "href": "text/ch000.xhtml", "children": [
        {"title": "第1章", "href": "text/ch000.xhtml", "children": [
            {"title": "1 節", "href": "text/ch000.xhtml#h0001", "children": []},
        ]},
    ]},
    {"title": "あとがき", "href": "text/ch001.xhtml", "children": []},
]


def nav_points(ncx):
    return re.findall(r'<navPoint id="([^"]+)" playOrder="(\d+)"', ncx)


def test_same_target_shares_play_order():
    """NCX は同じ target を指す navPoint に同じ playOrder を要求する

    素朴に文書順の通し番号を振ると epubcheck が
    RSC-005（different playOrder values ... refer to same target）で落ちる。
    """

    ncx = render_ncx(DUP_HREF_TREE, title="T", book_id="urn:uuid:x", first_chapter_href="text/ch000.xhtml")
    points = nav_points(ncx)
    order = {i: o for i, o in points}
    # 部と第1章は同じ href なので playOrder も同じでなければならない
    assert order["navPoint-1"] == order["navPoint-2"]
    # 断片付き・別ファイルはそれぞれ別の playOrder
    assert len({order["navPoint-2"], order["navPoint-3"], order["navPoint-4"]}) == 3


def test_nav_point_ids_are_unique():
    """id は navPoint ごとに一意（採番を再帰の後に行うと親子で衝突する）"""

    ncx = render_ncx(DUP_HREF_TREE, title="T", book_id="urn:uuid:x", first_chapter_href="text/ch000.xhtml")
    ids = [i for i, _ in nav_points(ncx)]
    assert len(ids) == len(set(ids)) == 4
