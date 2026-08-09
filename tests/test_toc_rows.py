"""縦組みの目次ページから章エントリを復元できることの検証。

縦組みの目次は 1 カラムが 1 行で、章タイトルの真下（同じ x 帯）に書籍ページ番号が
縦中横で置かれる。rows_of() が横組みと同じ軸（y でグループ化・x 昇順で連結）のままだと
全カラムが 1 行に連結され、行頭の「第○章」を拾えず目次エントリが 0 件になる。

座標は『イスラム教の論理』（新潮新書）の実際の OCR 結果から採っている。
"""

import json

import pytest

from book_ir import attach_ruby, build_lines, find_gutter, normalize_text, rows_of
from pdf_to_epub import (
    apply_toc_fixes,
    build_chapter_plan,
    extract_toc_entries,
    parse_toc_row,
    uncovered_pages,
)


def word(x0: int, x1: int, y0: int, y1: int, text: str, vertical: bool = True) -> dict:
    return {
        "points": [[x0, y0], [x1, y0], [x1, y1], [x0, y1]],
        "content": text,
        "direction": "vertical" if vertical else "horizontal",
    }


# 実測: p11（W=800, H=1362）。第1章・第2章のページ番号は横組み、第3章のみ縦組みで検出される
TOC_P11 = [
    word(676, 700, 192, 342, "まえがき 3"),
    word(611, 634, 975, 994, "15", vertical=False),
    word(608, 635, 192, 944, "第1章 イスラム教徒は「イスラム国」を否定できない"),
    word(568, 596, 218, 748, "「イスラム国」のイスラム教解釈は「正しい」"),
    word(526, 552, 219, 587, "穏健派法学者と国家権力の癒着"),
    word(483, 509, 218, 862, "「イスラム国」の人気は「過激派組織」だからではない"),
    word(418, 441, 1035, 1056, "48", vertical=False),
    word(416, 444, 192, 1009, "第2章 インターネットで増殖する「正しい」イスラム教徒"),
    word(375, 403, 219, 879, "なぜインドネシアのイスラム教徒は「過激化」したのか"),
    word(333, 360, 219, 720, "SNS戦略の徹底と洗練された動画編集術"),
    word(290, 317, 217, 786, "「イスラム国」の支配地域こそ「理想郷」である"),
    word(223, 250, 878, 935, "18"),
    word(222, 251, 191, 886, "第3章 世界征服はイスラム教徒全員の義務である"),
    word(184, 211, 221, 985, "コーランを字義通り解釈すれば、日本人も「殺すべき敵」である"),
]

# 実測: p13（W=809, H=1364）。1 段組だが第6章の下端と「178」の間に約 31px の空白帯があり、
# ページ高の 2%（約 27px）を超えるため y 方向のノド候補として拾われてしまう
TOC_P13 = [
    word(683, 710, 188, 849, "第6章 民主主義とは絶対に両立しない価値体系"),
    word(681, 713, 880, 899, "178", vertical=False),
    word(644, 670, 217, 661, "イスラム圏に信教の自由は存在しない"),
    word(600, 627, 215, 690, "手首切断も石打ち刑も世論の大半が支持"),
    word(557, 585, 215, 743, "主権在民ではなく主権在神、人間は神の奴隷"),
    word(491, 519, 188, 667, "第7章 イスラム社会の常識と日常"),
    word(487, 520, 695, 714, "209", vertical=False),
    word(452, 478, 221, 584, "ハラール認証は誤解されている"),
    word(406, 436, 215, 699, "「人生を楽しむ」という発想はありえない"),
    word(366, 391, 216, 633, "急成長するイスラム·ファッション"),
]


# 実測: 『"町内会"は義務ですか?』（小学館新書）p14（W=820, H=1364）。
# ページ番号の縦中横だけが本文と離れた下端の帯に落ち、しかも隣り合う 2 章分が
# 1 語（'250 243'）に結合されている
TOC_P14 = [
    word(684, 709, 157, 234, "第4章"),
    word(682, 711, 274, 684, "町内会は今後どうしたらいい?"),
    word(679, 712, 1206, 1225, "193", vertical=False),
    word(629, 655, 274, 551, "1 基本はボランティア"),
    word(584, 613, 274, 655, "2 校区(連合体) から考えるな"),
    word(544, 567, 324, 628, "地域の声を代表するには?"),
    word(542, 567, 274, 328, "3"),
    word(504, 524, 275, 310, "4", vertical=False),
    word(501, 525, 321, 783, "くずれかけた町内会でお悩みのあなたに"),
    word(412, 438, 274, 623, "親睦だけでもなんとかなる"),
    word(412, 436, 156, 206, "終章"),
    word(404, 440, 1205, 1226, "227", vertical=False),
    word(313, 338, 275, 378, "あとがき"),
    word(269, 296, 274, 458, "引用· 参考文献"),
    word(263, 342, 1205, 1225, "250 243", vertical=False),
]


def rows(words: list[dict], w: int, h: int) -> list[str]:
    return rows_of(attach_ruby(build_lines(words)), w, h)


def test_vertical_toc_joins_page_number_below_heading():
    """章タイトルと、その真下に置かれたページ番号が同じ行に入る"""

    out = rows(TOC_P11, 800, 1362)

    assert out[0] == "まえがき3"
    assert out[1] == "第1章イスラム教徒は「イスラム国」を否定できない15"
    assert "第2章インターネットで増殖する「正しい」イスラム教徒48" in out
    assert "第3章世界征服はイスラム教徒全員の義務である18" in out


def test_vertical_toc_reads_right_to_left():
    """行は右のカラムから左へ並ぶ（＝目次の掲載順）"""

    out = rows(TOC_P11, 800, 1362)

    assert out.index("穏健派法学者と国家権力の癒着") < out.index(
        "なぜインドネシアのイスラム教徒は「過激化」したのか"
    )


def test_vertical_toc_rejects_false_gutter():
    """1 段組の縦組みページで、行間の空白帯をノドと誤検出しない"""

    lines = attach_ruby(build_lines(TOC_P13))

    assert find_gutter(lines, 1364, vertical=True) is None
    out = rows_of(lines, 809, 1364)
    assert out[0] == "第6章民主主義とは絶対に両立しない価値体系178"
    assert out[-1] == "急成長するイスラム・ファッション"


def test_horizontal_two_column_toc_still_splits():
    """横組みの 2 段組目次は従来どおり左段→右段の順に復元する"""

    words = [
        word(60, 300, 100, 130, "第1章 序論", vertical=False),
        word(340, 380, 100, 130, "10", vertical=False),
        word(60, 300, 160, 190, "第2章 本論", vertical=False),
        word(340, 380, 160, 190, "24", vertical=False),
        word(620, 860, 100, 130, "第3章 結論", vertical=False),
        word(900, 940, 100, 130, "48", vertical=False),
        word(620, 860, 160, 190, "あとがき", vertical=False),
        word(900, 940, 160, 190, "60", vertical=False),
    ]

    out = rows(words, 1000, 1400)

    # normalize_text が連結用の空白ごと空白を落とすので、行内は詰めて比較する
    assert out == ["第1章序論10", "第2章本論24", "第3章結論48", "あとがき60"]


# --------------------------------------------------------------------------
# 目次エントリの補正と検証（pdf_to_epub）
# --------------------------------------------------------------------------


def write_toc_page(tmp_path, page_no: int, words: list[dict], w: int, h: int):
    (tmp_path / f"p{page_no:04d}.json").write_text(
        json.dumps(
            {
                "paragraphs": [],
                "tables": [],
                "figures": [],
                "words": words,
                "page": page_no,
                "image_size": {"w": w, "h": h},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def test_extract_toc_entries_from_vertical_page(tmp_path):
    write_toc_page(tmp_path, 11, TOC_P11, 800, 1362)

    entries = extract_toc_entries(tmp_path, "11")

    # 「まえがき」も章プランに入れないと、前付が本文 OCR されないまま落ちる
    assert [(e["level"], e["page_num"]) for e in entries] == [
        ("大", 3), ("大", 15), ("大", 48), ("大", 18),
    ]
    assert entries[1]["title"] == "第1章イスラム教徒は「イスラム国」を否定できない"


def test_parse_toc_row_takes_unnumbered_headings():
    """「第○章」以外の大見出し（序章・終章・前付/後付）も目次エントリにする"""

    assert parse_toc_row("序章町内会って入らなくてもいいの？17") == {
        "level": "大",
        "title": "序章町内会って入らなくてもいいの？",
        "page_num": 17,
        "is_roman": False,
    }
    assert parse_toc_row("終章親睦だけでもなんとかなる")["level"] == "大"
    assert parse_toc_row("引用・参考文献")["page_num"] is None
    assert parse_toc_row("はじめに3") == {
        "level": "大",
        "title": "はじめに",
        "page_num": 3,
        "is_roman": False,
    }
    # 節（中見出し）は従来どおり拾わない
    assert parse_toc_row("1イヤ〜な顔をされる加入勧誘") is None


def test_parse_toc_row_ignores_the_toc_heading_itself():
    """印刷目次ページ自身の見出し「目次」をエントリにしない。

    拾ってしまうとページ番号を持たないエントリとして pending に積まれ、縦組みで
    本文と分離したページ番号が 1 つずつずれて割り当てられ、全章の範囲が狂う。
    """

    assert parse_toc_row("目次") is None


@pytest.mark.parametrize(
    "row",
    ["はじめに考えるべきこと12", "参考文献の探し方45", "序論としての町内会30", "注意事項8"],
)
def test_parse_toc_row_rejects_prefix_only_matches(row):
    """前付/後付の語で始まるだけの節タイトルを章と誤認しない"""

    assert parse_toc_row(row) is None


def test_parse_toc_row_rejects_bibliography_section_label():
    """参考文献の区分ラベル「終章・あとがき」を章と誤認しない"""

    assert parse_toc_row("終章・あとがき") is None


@pytest.mark.parametrize("row", ["引用・参考文献250", "引用 参考文献250", "引用· 参考文献250"])
def test_parse_toc_row_takes_bibliography_with_any_separator(row):
    """OCR が中黒を空白として返しても後付エントリとして拾う。

    parse_toc_row() には rows_of() を通った正規化済みの行が届くので、ここでも
    normalize_text() を通してから渡す（CJK 隣接空白はその時点で消えている）。
    """

    entry = parse_toc_row(normalize_text(row))

    assert entry is not None
    assert entry["page_num"] == 250


def test_parse_toc_row_keeps_unnumbered_chapter_with_its_title():
    """目次側は前方一致のまま。終端境界を課すと正当な序章エントリごと落ちる"""

    assert parse_toc_row("序章町内会って入らなくてもいいの？17")["page_num"] == 17


def test_parse_toc_row_accepts_false_positive_on_unnumbered_chapter_words():
    """章語で始まる節タイトルを章と誤認するのは受容する（本文側とは非対称）。

    目次行は rows_of() が章題もページ番号も 1 行に連結して渡すので、本文側のように
    終端境界で締めると「序章町内会って入らなくてもいいの？17」まで落ちる。誤検出は
    章が 1 つ増えるだけだが、取りこぼしは本文 OCR も校正もされない欠落になるため、
    緩いほうを選んでいる。dry-run の章境界一覧を人が確認する運用で補う。
    """

    assert parse_toc_row("序章的な考察12")["page_num"] == 12


# --------------------------------------------------------------------------
# 章プランが覆わないページの検出
# --------------------------------------------------------------------------


def test_uncovered_pages_reports_the_gap_before_the_first_chapter():
    """先頭エントリの取りこぼしは、そのページが丸ごと欠落するので検出する"""

    plan = [{"start": 4, "end": 15}]

    assert uncovered_pages(plan, 15, "", set()) == [1, 2, 3]


def test_uncovered_pages_has_no_off_by_one_at_the_boundaries():
    """章範囲は start・end とも内包。1 ページ目と最終ページも取りこぼさない"""

    plan = [{"start": 2, "end": 14}]

    assert uncovered_pages(plan, 15, "", set()) == [1, 15]
    assert uncovered_pages([{"start": 1, "end": 15}], 15, "", set()) == []


def test_uncovered_pages_excludes_skip_and_toc_pages():
    """--skip-pages と目次ページは章プランの外にあっても欠落ではない"""

    plan = [{"start": 4, "end": 15}]

    assert uncovered_pages(plan, 15, "1", {2, 3}) == []
    # 目次ページを除外しないと必ず警告に計上されてしまう
    assert uncovered_pages(plan, 15, "1", set()) == [2, 3]


def test_extract_toc_entries_assigns_detached_page_numbers(tmp_path):
    """ページ番号だけが別の帯に落ちた縦組み目次でも、昇順で各見出しに割り当てる"""

    write_toc_page(tmp_path, 14, TOC_P14, 820, 1364)

    entries = extract_toc_entries(tmp_path, "14")

    assert [(e["title"], e["page_num"]) for e in entries] == [
        ("第4章町内会は今後どうしたらいい？", 193),
        ("終章親睦だけでもなんとかなる", 227),
        ("あとがき", 243),
        ("引用・参考文献", 250),
    ]


def test_apply_toc_fixes_overrides_page_number(tmp_path):
    write_toc_page(tmp_path, 11, TOC_P11, 800, 1362)
    entries = extract_toc_entries(tmp_path, "11")

    apply_toc_fixes(entries, "第3章=81")

    assert [e["page_num"] for e in entries] == [3, 15, 48, 81]


def test_apply_toc_fixes_rejects_bad_spec(tmp_path):
    write_toc_page(tmp_path, 11, TOC_P11, 800, 1362)
    entries = extract_toc_entries(tmp_path, "11")

    with pytest.raises(SystemExit, match="0 件"):
        apply_toc_fixes(entries, "第9章=100")
    with pytest.raises(SystemExit, match="書式が不正"):
        apply_toc_fixes(entries, "第3章")


def test_build_chapter_plan_rejects_non_monotonic_pages():
    """目次の並び順とページ番号が矛盾したら、黙って章範囲を作らず落ちる"""

    entries = [
        {"level": "大", "title": "第1章 A", "page_num": 15, "is_roman": False},
        {"level": "大", "title": "第2章 B", "page_num": 48, "is_roman": False},
        {"level": "大", "title": "第3章 C", "page_num": 18, "is_roman": False},
    ]

    with pytest.raises(SystemExit, match="第3章 C"):
        build_chapter_plan(entries, -1, 239)


def test_build_chapter_plan_ranges():
    entries = [
        {"level": "大", "title": "第1章 A", "page_num": 15, "is_roman": False},
        {"level": "大", "title": "第2章 B", "page_num": 48, "is_roman": False},
        {"level": "大", "title": "第3章 C", "page_num": 81, "is_roman": False},
    ]

    plan = build_chapter_plan(entries, -1, 239)

    assert [(p["start"], p["end"]) for p in plan] == [(14, 46), (47, 79), (80, 239)]
