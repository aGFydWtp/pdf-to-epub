"""柱としてしか現れない前付/後付の見出しと、章プランの先頭側の穴の検証。

前付の見出しが独立した要素として取れず、柱（role="page_header"）としてだけ現れる本が
ある。柱は無条件に捨てられていたため、その範囲は本文としては EPUB に入るのに大見出しが
1 つも立たず、nav からだけ丸ごと消えていた（『イスラム教の論理』のまえがき）。

章プランの側にも対になる穴がある。最終章の end は PDF の最終ページに張り付くので後付は
必ずどれかの章に吸収されるが、先頭側は plan[0]["start"] より前が無主地のままで、本文
OCR も校正もされずに落ちる。
"""

import json

import pytest

from book_ir import build_ir, front_back_heading_title
from pdf_to_epub import (
    build_chapter_plan,
    cover_front_matter,
    is_front_back_entry,
    resolve_offset,
    uncovered_pages,
)
from to_epub import chapters_without_nav_entry, describe_chapter_pages, split_chapters_and_nav

CHAR = 30
PAGE_H, PAGE_W = 1470, 940

# 章扉ページの判定（book_ir の len(page_text) < 120 の分岐）に吸い込まれないよう、
# どのフィクスチャにも十分な長さの本文を置く
BODY_LINES = ["本文の行がここに続く。" * 4] * 4


def hword(x0: int, y0: int, text: str) -> dict:
    """横組みの 1 行（1 文字 CHAR px 角）"""

    x1 = x0 + CHAR * len(text)
    return {
        "points": [[x0, y0], [x1, y0], [x1, y0 + CHAR], [x0, y0 + CHAR]],
        "content": text,
        "direction": "horizontal",
    }


def element(order: int, role: str | None, lines: list[str], y0: int) -> tuple[dict, list[dict]]:
    """1 要素ぶんの段落メタと words を作る"""

    words = [hword(100, y0 + i * CHAR * 2, t) for i, t in enumerate(lines)]
    xs = [p[0] for w in words for p in w["points"]]
    ys = [p[1] for w in words for p in w["points"]]
    para = {
        "box": [min(xs) - 5, min(ys) - 5, max(xs) + 5, max(ys) + 5],
        "contents": "\n".join(lines),
        "direction": "horizontal",
        "order": order,
        "role": role,
    }
    return para, words


def write_page(json_dir, page_no: int, specs: list[tuple[str | None, list[str], int]]):
    """(role, 行, y0) の並びから 1 ページぶんの OCR JSON を書き出す"""

    json_dir.mkdir(exist_ok=True)
    paragraphs, words = [], []
    for order, (role, lines, y0) in enumerate(specs):
        para, w = element(order, role, lines, y0)
        paragraphs.append(para)
        words += w
    page = {
        "paragraphs": paragraphs,
        "tables": [],
        "figures": [],
        "words": words,
        "page": page_no,
        "image_size": {"h": PAGE_H, "w": PAGE_W},
    }
    (json_dir / f"p{page_no:04d}.json").write_text(
        json.dumps(page, ensure_ascii=False), encoding="utf-8"
    )


def body(y0: int = 300) -> tuple[None, list[str], int]:
    return (None, BODY_LINES, y0)


def major_headings(json_dir, layout_pages: str = "") -> list[list[str]]:
    blocks, _ = build_ir(str(json_dir), str(json_dir / "no-such.pdf"), layout_pages=layout_pages)
    return [b["lines"] for b in blocks if b["kind"] == "heading" and b["level"] == "大"]


# --------------------------------------------------------------------------
# 柱からの昇格（book_ir.build_ir）
# --------------------------------------------------------------------------


def test_front_matter_page_header_becomes_a_major_heading(tmp_path):
    """柱としてしか現れない「まえがき」を大見出しに昇格させる。

    大でなければ split_chapters_and_nav が nav ノードを作らないので、
    昇格の level は heading_level() の戻り値に依存させず固定する。
    """

    json_dir = tmp_path / "ocr_json"
    write_page(json_dir, 1, [("page_header", ["まえがき"], 120), body()])

    assert major_headings(json_dir) == [["まえがき"]]


def test_promoted_heading_reaches_the_first_level_of_the_nav(tmp_path):
    """昇格の目的は nav に出ること。章分割まで通して確かめる"""

    json_dir = tmp_path / "ocr_json"
    write_page(json_dir, 1, [("page_header", ["まえがき"], 120), body()])

    blocks, _ = build_ir(str(json_dir), str(json_dir / "no-such.pdf"))
    chapters, nav = split_chapters_and_nav(blocks)

    assert [n["title"] for n in nav] == ["まえがき"]
    assert chapters[0]["blocks"][0]["lines"] == ["まえがき"]


def test_the_same_title_is_promoted_only_once(tmp_path):
    """柱は節の全ページに出る。2 回目以降を昇格させると節の途中で章が割れる"""

    json_dir = tmp_path / "ocr_json"
    for page_no in (1, 2, 3):
        write_page(json_dir, page_no, [("page_header", ["まえがき"], 120), body()])

    assert major_headings(json_dir) == [["まえがき"]]


def test_the_page_number_stuck_to_the_title_is_normalized_away(tmp_path):
    """柱にはページ番号が貼り付いて届く（「あとがき203」）。

    素の文字列を重複キーにすると「あとがき」と「あとがき203」が別物になり、
    同じ節の途中で 2 回目の昇格が起きて章が分裂し nav が二重化する。
    """

    json_dir = tmp_path / "ocr_json"
    write_page(json_dir, 1, [("page_header", ["あとがき"], 120), body()])
    write_page(json_dir, 2, [("page_header", ["あとがき203"], 120), body()])
    write_page(json_dir, 3, [("page_header", ["あとがき205"], 120), body()])

    assert major_headings(json_dir) == [["あとがき"]]


def test_no_promotion_when_the_same_page_already_has_the_heading(tmp_path):
    """同じページに section_headings で同じ見出しがあれば見出しが二重になる。

    『レベニューオペレーション(RevOps)の教科書』の「はじめに」「おわりに」「索引」が
    この形（節の初出ページに見出しと柱の両方がある）。
    """

    json_dir = tmp_path / "ocr_json"
    write_page(
        json_dir,
        1,
        [("page_header", ["はじめに"], 120), ("section_headings", ["はじめに"], 250), body(400)],
    )

    assert major_headings(json_dir) == [["はじめに"]]


def test_no_promotion_when_an_earlier_page_already_carried_the_heading(tmp_path):
    """節の初出ページで見出しが取れていれば、後続ページの柱は昇格させない。

    同ページのガードだけでは、初出ページに柱が無い版面（＝ガードが発火しない）で
    次のページの柱が昇格し、節の途中で章が割れて nav が二重化する。
    """

    json_dir = tmp_path / "ocr_json"
    write_page(json_dir, 1, [("section_headings", ["はじめに"], 250), body(400)])
    write_page(json_dir, 2, [("page_header", ["はじめに"], 120), body()])

    assert major_headings(json_dir) == [["はじめに"]]


def test_no_promotion_after_a_chapter_title_page_took_the_word(tmp_path):
    """章扉ページが立てた「序章」を、後続ページの柱が二度目の章にしない。

    章扉の経路（chapter_title_page）は前付/後付の先読みより前で continue するため、
    登録を怠ると柱が「まだ立っていない見出し」と誤認されて章が割れる。
    危ないのは ^[序終]章$ に当たる語だけで、「第N章」は昇格の語彙に無い。
    """

    json_dir = tmp_path / "ocr_json"
    # 章扉の判定を踏ませるため、意図的に本文の無い短いページにする
    write_page(json_dir, 1, [(None, ["序章"], 400)])
    write_page(json_dir, 2, [("page_header", ["序章"], 120), body()])

    assert major_headings(json_dir) == [["序章"]]


def test_no_promotion_after_a_layout_page_took_the_word(tmp_path):
    """段組ページが立てた「索引」を、後続ページの柱が二度目の章にしない。

    --layout-pages の経路も先読みより前で continue する。「目次」は語彙から
    差し引かれているので、ここで危ないのは索引の 3 語だけ。
    """

    json_dir = tmp_path / "ocr_json"
    write_page(json_dir, 1, [(None, ["索引"], 400)])
    write_page(json_dir, 2, [("page_header", ["索引"], 120), body()])

    assert major_headings(json_dir, layout_pages="1") == [["索引"]]


def test_page_footer_is_never_promoted(tmp_path):
    """フッタ柱を昇格させると、そのページの本文より後ろに見出しが来る。

    split_chapters_and_nav はブロック順で章を切るので、そのページの本文が
    前の章へ押し込まれてしまう。
    """

    json_dir = tmp_path / "ocr_json"
    write_page(json_dir, 1, [body(200), ("page_footer", ["あとがき"], 1380)])

    assert major_headings(json_dir) == []


def test_the_toc_word_is_not_promoted(tmp_path):
    """「目次」は昇格させない。

    昇格させると to_epub.filter_printed_toc() の「目次見出し配下を本文から除く」が
    今まで発火しなかった本で発火し、本文が消える。
    """

    json_dir = tmp_path / "ocr_json"
    write_page(json_dir, 1, [("page_header", ["目次"], 120), body()])

    assert major_headings(json_dir) == []


def test_body_page_headers_are_still_dropped(tmp_path):
    """語彙表に一致しない柱は従来どおり捨てる（昇格は厳密に加算的）"""

    json_dir = tmp_path / "ocr_json"
    write_page(json_dir, 1, [("page_header", ["第1章イスラム教徒は"], 120), body()])
    write_page(json_dir, 2, [("page_header", ["解説を書くということ"], 120), body()])

    assert major_headings(json_dir) == []


@pytest.mark.parametrize(
    "text, expected",
    [
        ("まえがき", "まえがき"),
        ("あとがき203", "あとがき"),
        ("訳者あとがき", "訳者あとがき"),
        ("終章227", "終章"),
        ("目次", ""),
        ("イスラム教の論理目次", ""),
        ("第1章イスラム教徒は", ""),
        ("解説を書くということ", ""),
        ("", ""),
    ],
)
def test_front_back_heading_title(text, expected):
    assert front_back_heading_title(text) == expected


# --------------------------------------------------------------------------
# 章プランの先頭側（pdf_to_epub.cover_front_matter）
# --------------------------------------------------------------------------


def test_cover_front_matter_adds_a_leading_segment():
    """plan[0]["start"] より前は無主地なので、1 章ぶんのセグメントで覆う"""

    plan = [{"title": "第1章", "level": "大", "start": 14, "end": 239}]

    covered = cover_front_matter(plan, 239, "1", {10, 11, 12, 13})

    assert covered[0] == {"title": "前付", "level": "大", "start": 2, "end": 13}
    assert covered[1:] == plan


def test_cover_front_matter_is_a_no_op_when_the_head_is_already_covered():
    """『イスラム教の論理』の本番引数はこちら（p1 は --skip-pages なので穴が無い）"""

    plan = [
        {"title": "まえがき", "level": "大", "start": 2, "end": 13},
        {"title": "第1章", "level": "大", "start": 14, "end": 239},
    ]

    assert cover_front_matter(plan, 239, "1,10-13,238,239", {10, 11, 12, 13}) == plan


def test_cover_front_matter_covers_everything_when_the_plan_is_empty():
    """目次エントリが 1 件も章にならなかった本。現状は 1 ページも OCR されない"""

    covered = cover_front_matter([], 20, "1", {2, 3})

    assert covered == [{"title": "前付", "level": "大", "start": 4, "end": 20}]


@pytest.mark.parametrize(
    "plan, n_pages, skip_spec, toc_pages",
    [
        ([{"start": 14, "end": 239}], 239, "1", {10, 11, 12, 13}),
        ([{"start": 4, "end": 15}], 15, "", set()),
        ([{"start": 1, "end": 15}], 15, "", set()),
        ([], 20, "1", {2, 3}),
        ([], 20, "", set()),
    ],
)
def test_cover_front_matter_leaves_no_uncovered_page(plan, n_pages, skip_spec, toc_pages):
    """事後不変条件。ここが崩れると、そのページは本文 OCR も校正もされずに落ちる"""

    covered = cover_front_matter(plan, n_pages, skip_spec, toc_pages)

    assert uncovered_pages(covered, n_pages, skip_spec, toc_pages) == []


def test_cover_front_matter_does_not_fill_holes_after_the_first_chapter():
    """途中や末尾の穴は直前の章に吸収されるのが正常で、埋めると章の範囲を偽る"""

    plan = [{"title": "第1章", "level": "大", "start": 1, "end": 10}]

    assert cover_front_matter(plan, 15, "", set()) == plan


# --------------------------------------------------------------------------
# 章エントリの無言脱落（pdf_to_epub.build_chapter_plan）
# --------------------------------------------------------------------------


def test_build_chapter_plan_rejects_equal_page_numbers():
    """同値も並び順の矛盾として検出する。

    sorted() は安定なので同値では並びが変わらず、並び替えの有無で判定すると素通りする。
    素通りすると前のエントリが start > end の零長範囲になって黙って消え、しかも
    後続エントリが同じページを覆うので uncovered_pages でも警告されない。
    """

    entries = [
        {"level": "大", "title": "前付", "page_num": 1, "is_roman": False},
        {"level": "大", "title": "第1章 A", "page_num": 1, "is_roman": False},
    ]

    with pytest.raises(SystemExit, match="第1章 A"):
        build_chapter_plan(entries, 0, 239)


def test_build_chapter_plan_keeps_the_toc_fix_hint_in_the_error():
    entries = [
        {"level": "大", "title": "第1章 A", "page_num": 15, "is_roman": False},
        {"level": "大", "title": "第2章 B", "page_num": 15, "is_roman": False},
    ]

    with pytest.raises(SystemExit, match="--toc-fix"):
        build_chapter_plan(entries, -1, 239)


def test_build_chapter_plan_warns_when_it_drops_an_entry(capsys):
    """範囲外のエントリを黙って捨てると、その章が丸ごと欠落したことに気づけない"""

    entries = [
        {"level": "大", "title": "まえがき", "page_num": 3, "is_roman": False},
        {"level": "大", "title": "第1章 A", "page_num": 15, "is_roman": False},
    ]

    plan = build_chapter_plan(entries, -10, 239)

    assert [c["title"] for c in plan] == ["第1章 A"]
    out = capsys.readouterr().out
    assert "まえがき" in out and "警告" in out


# --------------------------------------------------------------------------
# オフセット照合のターゲット（pdf_to_epub.resolve_offset）
# --------------------------------------------------------------------------


def write_text_page(json_dir, page_no: int, text: str):
    """resolve_offset が読む words だけの最小 JSON（OCR は走らせない）"""

    json_dir.mkdir(exist_ok=True)
    (json_dir / f"p{page_no:04d}.json").write_text(
        json.dumps({"words": [{"content": text}]}, ensure_ascii=False), encoding="utf-8"
    )


@pytest.mark.parametrize(
    "entry, expected",
    [
        ({"title": "はじめに"}, True),
        ({"title": "あとがき"}, True),
        ({"title": "序章注目されるRevOps"}, True),
        ({"title": "索引"}, True),
        ({"title": "第1章 収益拡大を実現する"}, False),
        ({"title": "Column 3 用語集"}, False),
    ],
)
def test_is_front_back_entry(entry, expected):
    assert is_front_back_entry(entry) is expected


def test_resolve_offset_prefers_a_body_entry_over_front_matter(tmp_path):
    """前付は本文と別ノンブルのことが多い。

    前付を照合ターゲットにすると前付の領域でだけ正しい offset を返し、本文の
    全章がずれる。しかも黙って通るので気づけない。
    """

    json_dir = tmp_path / "ocr_json"
    write_text_page(json_dir, 2, "扉のページ")
    write_text_page(json_dir, 3, "はじめに")
    write_text_page(json_dir, 10, "本文がはじまる")
    entries = [
        {"title": "はじめに", "page_num": 2, "is_roman": False},
        {"title": "第1章　本文がはじまる", "page_num": 10, "is_roman": False},
    ]

    offset = resolve_offset(entries, [None] * 12, json_dir, 200, None, None, [None], set())

    # 前付を選ぶと p3 で当たって 1 を返す（本文の実測は 0 なので全章が 1 ページずれる）
    assert offset == 0


def test_resolve_offset_falls_back_to_front_matter_when_there_is_nothing_else(tmp_path, capsys):
    """前付しか無い本を即死させない。ただし当てにならない旨は出す"""

    json_dir = tmp_path / "ocr_json"
    write_text_page(json_dir, 2, "扉のページ")
    write_text_page(json_dir, 3, "はじめに")
    entries = [{"title": "はじめに", "page_num": 2, "is_roman": False}]

    offset = resolve_offset(entries, [None] * 12, json_dir, 200, None, None, [None], set())

    assert offset == 1
    assert "前付" in capsys.readouterr().out


# --------------------------------------------------------------------------
# 見出しを持たない章の検出（to_epub）
# --------------------------------------------------------------------------


def test_chapters_without_nav_entry_finds_the_implicit_chapter():
    """大見出しが立つ前に本文が来ると暗黙の章ができ、nav にも ncx にも出ない"""

    blocks = [
        {"kind": "para", "lines": ["献辞"], "page": 1},
        {"kind": "heading", "level": "大", "lines": ["第1章"], "page": 3},
        {"kind": "para", "lines": ["本文"], "page": 3},
    ]
    chapters, _ = split_chapters_and_nav(blocks)

    orphans = chapters_without_nav_entry(chapters)

    assert [c["file"] for c in orphans] == ["ch000.xhtml"]
    assert describe_chapter_pages(orphans[0]) == "p1"


def test_chapters_without_nav_entry_is_empty_when_every_chapter_has_a_heading():
    blocks = [
        {"kind": "heading", "level": "大", "lines": ["まえがき"], "page": 2},
        {"kind": "para", "lines": ["本文"], "page": 2},
        {"kind": "heading", "level": "大", "lines": ["第1章"], "page": 14},
    ]
    chapters, _ = split_chapters_and_nav(blocks)

    assert chapters_without_nav_entry(chapters) == []


def test_describe_chapter_pages_spans_the_whole_chapter():
    chapter = {
        "file": "ch000.xhtml",
        "blocks": [
            {"kind": "para", "lines": ["a"], "page": 2},
            {"kind": "para", "lines": ["b"], "page": 9},
        ],
    }

    assert describe_chapter_pages(chapter) == "p2〜p9"
