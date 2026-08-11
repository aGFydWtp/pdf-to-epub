"""nav の構造（部への nest・同名大見出しの統合・偽の部見出しの降格・閲覧順の検査）

『時間を哲学する 思考のためのツールボックス』（慶應義塾大学出版会、301ページ）で
nav 第1階層が 37 項目まで膨らみ、epubcheck WARNING(NAV-011) が 3 件出た欠陥の回帰。
数値はすべて同書の ocr_json（`/Users/haruki/Documents/ebooks/時間を哲学する/ocr_json`）の
実測値で、推測値は使っていない。

入力は normalize_text() を通した文字列で書く（docs/HANDOFF.md の house rule）。
"""

import sys
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from to_epub import (  # noqa: E402
    PART_TITLE_PAGE_MAX_CHARS,
    build_css,
    build_opf,
    demote_false_part_headings,
    nav_order_problems,
    render_nav_xhtml,
    self_check,
    split_chapters_and_nav,
    xhtml_page,
)


def heading(*lines: str, page: int = 1) -> dict:
    return {"kind": "heading", "level": "大", "lines": list(lines), "page": page}


def para(text: str, page: int = 1) -> dict:
    return {"kind": "para", "lines": [text], "page": page}


# --------------------------------------------------------------------------
# 問題1: 章末の後付が部を飛び越えて第1階層へ上がる（NAV-011 の原因）
# --------------------------------------------------------------------------


def test_chapter_end_back_matter_nests_under_the_part():
    """章末の参考文献・謝辞は部の子。第1階層へ上げると nav が spine 順と食い違う

    実測: 同書は各章末に「註」「参考文献」を、第Ⅱ部の章末に「謝辞」を持つ。
    これらが第1階層に出ると epubcheck が ch005 / ch022 / ch035 で NAV-011 を出した。
    """

    blocks = [
        heading("第Ⅰ部　概念のツールボックス"),
        heading("第1章　主観的時間、客観的時間"),
        heading("註"),
        heading("参考文献"),
        heading("第2章　物理の時間、生物の時間"),
        heading("謝辞"),
        heading("参考文献"),
    ]

    _, nav = split_chapters_and_nav(blocks)

    assert [n["title"] for n in nav] == ["第Ⅰ部概念のツールボックス"]
    assert [c["title"] for c in nav[0]["children"]] == [
        "第1章主観的時間、客観的時間",
        "註",
        "参考文献",
        "第2章物理の時間、生物の時間",
        "謝辞",
        "参考文献",
    ]


def test_nav_order_matches_spine_order_for_a_book_with_parts():
    """部・章末の後付・巻末索引が混ざっても nav のリンク順が章の並び順を追い越さない"""

    blocks = [
        heading("はじめに"),
        heading("第Ⅰ部　概念のツールボックス"),
        heading("第1章　主観的時間、客観的時間"),
        heading("参考文献"),
        heading("第Ⅱ部　実践編"),
        heading("第1章　ベルクソンの時間論"),
        heading("謝辞"),
        heading("参考文献"),
        heading("事項索引"),
        heading("あとがき"),
    ]

    chapters, nav = split_chapters_and_nav(blocks)

    spine_order = {f"text/{c['file']}": i for i, c in enumerate(chapters)}
    nav_xml = render_nav_xhtml(nav, f"text/{chapters[0]['file']}")
    assert nav_order_problems(nav_xml, "nav.xhtml", spine_order) == []


def test_notes_still_nest_under_the_part():
    """既存の挙動（註は部の子、解説など他の後付は第1階層）を変えない"""

    blocks = [
        heading("第Ⅰ部　総論"),
        heading("第1章　町内会は必要です！"),
        heading("註"),
        heading("解説"),
        heading("訳者あとがき"),
    ]

    _, nav = split_chapters_and_nav(blocks)

    assert [n["title"] for n in nav] == ["第Ⅰ部総論", "解説", "訳者あとがき"]
    assert [c["title"] for c in nav[0]["children"]] == ["第1章町内会は必要です！", "註"]


def test_standalone_back_matter_bibliography_nests_under_the_last_part():
    """トレードオフの明示: 巻末の独立した参考文献は最終の部の子になる（順序は正しい）"""

    blocks = [
        heading("第Ⅰ部　総論"),
        heading("第1章　町内会は必要です！"),
        heading("引用・参考文献"),
    ]

    _, nav = split_chapters_and_nav(blocks)

    assert [n["title"] for n in nav] == ["第Ⅰ部総論"]
    assert [c["title"] for c in nav[0]["children"]] == ["第1章町内会は必要です！", "引用・参考文献"]


def test_back_matter_without_parts_stays_at_top_level():
    """部を持たない本（『“町内会”は義務ですか?』『イスラム教の論理』）は影響を受けない"""

    blocks = [
        heading("はじめに"),
        heading("第1章　町内会って何？"),
        heading("あとがき"),
        heading("引用・参考文献"),
    ]

    _, nav = split_chapters_and_nav(blocks)

    assert [n["title"] for n in nav] == [
        "はじめに",
        "第1章町内会って何？",
        "あとがき",
        "引用・参考文献",
    ]


# --------------------------------------------------------------------------
# 問題2: 索引がページ単位で章に割れる
# --------------------------------------------------------------------------


def test_repeated_heading_merges_into_one_chapter_without_losing_body():
    """ページごとに柱が昇格した索引は 1 章に統合する。本文ブロックは 1 つも落とさない

    実測: 事項索引が 4 章（ch055〜ch058）、人名索引が 2 章（ch059〜ch060）に割れていた。
    """

    blocks = [
        heading("事項索引", page=287),
        para("アリストテレス　12", page=287),
        heading("事項索引", page=288),
        para("イデア　34", page=288),
        heading("事項索引", page=290),
        para("運動　56", page=290),
        heading("人名索引", page=293),
        para("カント　78", page=293),
    ]

    chapters, nav = split_chapters_and_nav(blocks)

    assert [n["title"] for n in nav] == ["事項索引", "人名索引"]
    assert len(chapters) == 2
    # 落とすのは重複した見出しブロックだけ。本文はすべて統合先の章に残る
    assert [b["lines"][0] for b in chapters[0]["blocks"] if b["kind"] == "para"] == [
        "アリストテレス　12",
        "イデア　34",
        "運動　56",
    ]
    assert [b["lines"][0] for b in chapters[1]["blocks"] if b["kind"] == "para"] == ["カント　78"]
    assert sum(1 for b in chapters[0]["blocks"] if b["kind"] == "heading") == 1


def test_same_title_headings_apart_are_not_merged():
    """離れた位置の同名見出し（各章末の参考文献）は統合しない"""

    blocks = [
        heading("第1章　主観的時間"),
        heading("参考文献"),
        heading("第2章　物理の時間"),
        heading("参考文献"),
    ]

    chapters, _ = split_chapters_and_nav(blocks)

    assert len(chapters) == 4


def test_merged_chapter_keeps_hosting_sub_headings():
    """統合したあとの中見出しのアンカーは統合先の章ファイルを指す"""

    blocks = [
        heading("事項索引", page=287),
        heading("事項索引", page=288),
        {"kind": "heading", "level": "中", "lines": ["あ行"], "page": 288},
    ]

    chapters, nav = split_chapters_and_nav(blocks)

    assert len(chapters) == 1
    assert nav[0]["children"][0]["href"].startswith(f"text/{chapters[0]['file']}#")


# --------------------------------------------------------------------------
# 問題3: 前書き中の紹介文が部見出しとして誤検出され、本文を吸い込む
# --------------------------------------------------------------------------

# 実測: 偽の部見出しのページは本文 para が 650 字 / 617 字、本物の部扉は 0 字。
# この本でいちばん薄い本文ページでも 434 字あるので、閾値 100 字は両者の間にある。
BODY_PAGE_TEXT = "あ" * 650
THIN_LEAD_TEXT = "あ" * 40


def test_false_part_heading_in_the_preface_is_demoted_to_a_paragraph():
    """本文の乗ったページの部見出しは para へ降格し、その本文は前の章に残る

    降格しないと大見出しとして章ファイルを切ってしまい、「はじめに」の本文後半が
    別の章へ吸い込まれる（実測: ch001 / ch002 に分かれていた）。
    """

    blocks = [
        heading("はじめに", page=4),
        para(BODY_PAGE_TEXT, page=9),
        heading("第Ⅰ部　「概念のツールボックス」考えるための道具を揃える", page=9),
        para(BODY_PAGE_TEXT, page=9),
        heading("第Ⅰ部　概念のツールボックス", page=13),
        heading("第1章　主観的時間、客観的時間", page=14),
        para(BODY_PAGE_TEXT, page=14),
    ]

    fixed = demote_false_part_headings(blocks)
    chapters, nav = split_chapters_and_nav(fixed)

    assert [b["kind"] for b in fixed] == [
        "heading", "para", "para", "para", "heading", "heading", "para",
    ]
    assert "level" not in fixed[2]
    # 吸い込まれていた本文が「はじめに」に戻る
    assert [n["title"] for n in nav] == ["はじめに", "第Ⅰ部概念のツールボックス"]
    assert len(chapters) == 3
    assert len(chapters[0]["blocks"]) == 4  # 見出し + 本文3（うち1つは降格した見出し）


def test_real_part_title_pages_survive():
    """本物の部扉（本文のほとんど無いページ）は部として残る"""

    blocks = [
        heading("第Ⅰ部　概念のツールボックス", page=13),
        para("第1章　主観的時間", page=14),
        heading("第Ⅱ部　実践編", page=103),
        para(BODY_PAGE_TEXT, page=104),
        heading("第Ⅲ部　広げるためのコラム", page=252),
    ]

    fixed = demote_false_part_headings(blocks)

    assert [b["kind"] for b in fixed] == ["heading", "para", "heading", "para", "heading"]


def test_part_title_page_with_a_short_lead_survives():
    """扉に短いリード文が乗っていても閾値（100 字）以内なら扉として残す"""

    blocks = [
        heading("第Ⅰ部　概念のツールボックス", page=13),
        para(THIN_LEAD_TEXT, page=13),
        heading("第Ⅱ部　実践編", page=103),
        para(BODY_PAGE_TEXT, page=104),
    ]

    assert len(THIN_LEAD_TEXT) <= PART_TITLE_PAGE_MAX_CHARS
    assert [b["kind"] for b in demote_false_part_headings(blocks)] == [
        "heading", "para", "heading", "para",
    ]


def test_books_without_a_clean_part_title_page_are_left_alone():
    """部扉を立てず本文の頭に部名を置く体裁の本には触らない（判断材料が無い）"""

    blocks = [
        heading("第Ⅰ部　概念のツールボックス", page=13),
        para(BODY_PAGE_TEXT, page=13),
        heading("第Ⅱ部　実践編", page=103),
        para(BODY_PAGE_TEXT, page=103),
    ]

    assert [b["kind"] for b in demote_false_part_headings(blocks)] == [
        "heading", "para", "heading", "para",
    ]


def test_a_single_part_heading_is_never_demoted():
    """部見出しが 1 つしかなければ比較相手がいないので降格しない"""

    blocks = [
        heading("第Ⅰ部　概念のツールボックス", page=9),
        para(BODY_PAGE_TEXT, page=9),
        heading("はじめに", page=4),
        para(BODY_PAGE_TEXT, page=4),
    ]

    assert [b["kind"] for b in demote_false_part_headings(blocks)] == [
        "heading", "para", "heading", "para",
    ]


# --------------------------------------------------------------------------
# 問題4: self_check に nav 順序の検査を追加
# --------------------------------------------------------------------------

CONTAINER_XML = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    '<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
    '<rootfiles><rootfile full-path="OEBPS/package.opf" '
    'media-type="application/oebps-package+xml"/></rootfiles></container>'
)


def write_epub(path: Path, nav_tree: list[dict]) -> Path:
    """ch000 / ch001 の 2 章だけの最小構成 EPUB を、渡した nav ツリーで書き出す"""

    manifest_items = [
        {"id": "css", "href": "style.css", "media_type": "text/css"},
        {"id": "nav", "href": "nav.xhtml", "media_type": "application/xhtml+xml", "properties": "nav"},
        {"id": "ch000", "href": "text/ch000.xhtml", "media_type": "application/xhtml+xml"},
        {"id": "ch001", "href": "text/ch001.xhtml", "media_type": "application/xhtml+xml"},
    ]
    opf = build_opf(
        title="テスト書名",
        author="テスト著者",
        publisher="テスト出版",
        book_uuid="00000000-0000-4000-8000-000000000000",
        modified="2026-01-01T00:00:00Z",
        manifest_items=manifest_items,
        spine_items=[{"idref": "nav", "linear": "no"}, {"idref": "ch000"}, {"idref": "ch001"}],
        horizontal=False,
    )
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("mimetype", "application/epub+zip", zipfile.ZIP_STORED)
        z.writestr("META-INF/container.xml", CONTAINER_XML)
        z.writestr("OEBPS/package.opf", opf)
        z.writestr("OEBPS/style.css", build_css(horizontal=False))
        z.writestr("OEBPS/nav.xhtml", render_nav_xhtml(nav_tree, "text/ch000.xhtml"))
        z.writestr("OEBPS/text/ch000.xhtml", xhtml_page("章1", "<p>本文</p>", "../style.css", "chapter"))
        z.writestr("OEBPS/text/ch001.xhtml", xhtml_page("章2", "<p>本文</p>", "../style.css", "chapter"))
    return path


def node(title: str, href: str, children: list[dict] | None = None) -> dict:
    return {"title": title, "href": href, "children": children or []}


def test_self_check_passes_when_nav_follows_spine_order(tmp_path):
    nav_tree = [node("章1", "text/ch000.xhtml"), node("章2", "text/ch001.xhtml")]

    assert self_check(write_epub(tmp_path / "book.epub", nav_tree)) == []


def test_self_check_detects_broken_nav_order(tmp_path):
    """部の子を出し切ってから前の章が現れる形（NAV-011 と同じ崩れ）を捕まえる"""

    nav_tree = [node("部", "text/ch001.xhtml"), node("章1", "text/ch000.xhtml")]

    problems = self_check(write_epub(tmp_path / "book.epub", nav_tree))

    assert any("nav の順序が spine 順と食い違っています" in p for p in problems)


def test_self_check_detects_a_child_overtaken_by_the_next_top_level_entry(tmp_path):
    """実際の崩れ方: 部の children を全部出したあとに、部の途中の章が第1階層で現れる"""

    nav_tree = [node("部", "text/ch000.xhtml", [node("章2", "text/ch001.xhtml")]),
                node("参考文献", "text/ch000.xhtml")]

    problems = self_check(write_epub(tmp_path / "book.epub", nav_tree))

    assert any("nav の順序が spine 順と食い違っています" in p for p in problems)


def test_nav_order_ignores_fragments_and_non_spine_links():
    """中見出しのフラグメントは同じ章とみなす。spine に無い先（表紙）は比較しない"""

    nav_xml = render_nav_xhtml(
        [node("章1", "text/ch000.xhtml", [node("節", "text/ch000.xhtml#h0001")]),
         node("章2", "text/ch001.xhtml")],
        "text/ch000.xhtml",
    )
    spine_order = {"text/ch000.xhtml": 1, "text/ch001.xhtml": 2}

    assert nav_order_problems(nav_xml, "nav.xhtml", spine_order) == []
