"""見出しの階層判定（book_ir.heading_level）の検証。

大見出しは章として XHTML を分割する単位なので、ここで「小」に落ちた見出しは
章にならず、直前の章の本文に埋もれて nav からも消える。番号を持たない章
（序章・終章）と後付（引用・参考文献）はまさにそれで落ちていた。
"""

import pytest

from book_ir import heading_level
from to_epub import heading_title, split_chapters_and_nav


@pytest.mark.parametrize(
    "head",
    ["第Ⅰ部　総論", "第1章　町内会は必要です！", "第 12 章 まとめ", "Column 3 用語集"],
)
def test_numbered_headings_are_major(head):
    assert heading_level(head) == "大"


@pytest.mark.parametrize(
    "head",
    ["序章", "序章\n町内会って入らなくてもいいの？", "終章　親睦だけでもなんとかなる", "補章", "プロローグ"],
)
def test_unnumbered_chapters_are_major(head):
    assert heading_level(head) == "大"


@pytest.mark.parametrize(
    "head",
    [
        "はじめに", "あとがき", "引用・参考文献", "参考文献", "索引",
        "解説", "付録", "年表", "凡例", "謝辞", "補遺", "跋", "序",
    ],
)
def test_front_and_back_matter_is_major(head):
    assert heading_level(head) == "大"


@pytest.mark.parametrize("head", ["訳者あとがき", "文庫版あとがき", "編者解説"])
def test_front_back_matter_keeps_its_qualifier(head):
    """「訳者あとがき」のように書き手を冠した後付も章として立てる"""

    assert heading_level(head) == "大"


@pytest.mark.parametrize(
    "head",
    [
        "解説　佐藤太郎",
        "訳者あとがき　二〇二〇年",
        "序文　著者を代表して",
        "終章　親睦だけでもなんとかなる",
        "序章\n町内会って入らなくてもいいの？",
    ],
)
def test_title_or_author_after_the_word_still_counts(head):
    """語のあとに章題や解説者名が続く形（文庫の最頻形）を章として立てる。

    正規化は CJK 隣接空白を落とすので、この切れ目は正規化前にしか取れない。
    """

    assert heading_level(head) == "大"


@pytest.mark.parametrize(
    "head",
    ["終章に向けて", "序章的な考察", "序章的な考察12", "補章立てについて", "プロローグ風の導入"],
)
def test_unnumbered_chapter_words_inside_a_phrase_stay_minor(head):
    """章語で始まるだけの句を章に昇格させない（前方一致ではなく終端境界で見る）"""

    assert heading_level(head) == "小"


@pytest.mark.parametrize("head", ["引用· 参考文献", "引用 参考文献", "引用参考文献"])
def test_space_inside_a_vocabulary_word_is_tolerated(head):
    """OCR が中黒を空白として返しても後付として拾う（正規化後の行全体でも照合する）"""

    assert heading_level(head) == "大"


@pytest.mark.parametrize("head", ["あとがきにかえて", "参考文献一覧"])
def test_compound_front_back_words_are_not_eaten_by_shorter_ones(head):
    """「あとがき」「参考文献」に食われて終端境界で落ちないこと"""

    assert heading_level(head) == "大"


@pytest.mark.parametrize("head", ["解説　3つのポイント", "注　これは重要です", "付録　Aの使い方"])
def test_word_plus_space_is_promoted_even_when_it_is_a_section(head):
    """語＋空きで始まる節見出しが章に昇格しうるのは受容する。

    これは「解説　佐藤太郎」を大見出しとして救うことと不可分の対価で、先頭トークンで
    判定する以上どちらか一方だけは選べない。heading_level() は OCR が section_headings
    と判定したブロックにしか当たらないので、影響する範囲は限定的。
    """

    assert heading_level(head) == "大"


@pytest.mark.parametrize("head", ["はじめに12", "はじめにiii", "あとがき203"])
def test_page_number_glued_to_heading_is_tolerated(head):
    """級数の小さいページ番号が見出しに貼り付いたまま届いても取りこぼさない"""

    assert heading_level(head) == "大"


def test_raw_ocr_text_is_normalized_before_matching():
    """OCR 生テキストの中点・余分な空白のまま照合しない"""

    assert heading_level("引用· 参考文献") == "大"


def test_bibliography_section_label_is_not_a_chapter():
    """参考文献の区分ラベル「終章・あとがき」を章と誤認しない"""

    assert heading_level("終章·あとがき") != "大"


def test_numeric_section_is_middle():
    assert heading_level("2-3　町内会の会計") == "中"


@pytest.mark.parametrize(
    "head",
    ["町内会って何？", "参考にした資料", "解説を書くということ", "序論としての町内会", "注意したいこと"],
)
def test_plain_headings_stay_minor(head):
    """前付/後付の語で始まるだけの本文見出しは章にしない（終端境界が効いている）"""

    assert heading_level(head) == "小"


# --------------------------------------------------------------------------
# nav の階層（to_epub.split_chapters_and_nav）
# --------------------------------------------------------------------------


def heading(*lines: str) -> dict:
    return {"kind": "heading", "level": "大", "lines": list(lines), "page": 1}


def test_back_matter_escapes_the_part_but_notes_nest_under_it():
    """部構成のある本で、後注は部の下に残し、解説など他の後付は nav 第1階層へ出す"""

    blocks = [
        heading("第Ⅰ部　総論"),
        heading("第1章　町内会は必要です！"),
        heading("註"),
        heading("解説"),
        heading("訳者あとがき"),
    ]

    _, nav = split_chapters_and_nav(blocks)

    # nav の見出しは heading_title() を通るので、字間の全角空白は落ちた形で並ぶ
    assert [n["title"] for n in nav] == ["第Ⅰ部総論", "解説", "訳者あとがき"]
    assert [c["title"] for c in nav[0]["children"]] == ["第1章町内会は必要です！", "註"]


@pytest.mark.parametrize(
    ("lines", "expected"),
    [
        (["あとがき203"], "あとがき"),
        (["はじめにiii"], "はじめに"),
        (["解説203", "佐藤太郎"], "解説　佐藤太郎"),
        (["終章227"], "終章"),
        (["序ii"], "序"),
        # 落とすのは数字を除いた残りが語彙表の語に完全一致するときだけなので、
        # 末尾が数字でない正当な章題は影響を受けない
        (["終章2020年の町内会"], "終章2020年の町内会"),
        # 語彙表の語でなければ落とさない。無条件に末尾の数字を削ると Column が壊れる
        (["Column 3"], "Column 3"),
        (["第4章　町内会は今後どうしたらいい？"], "第4章町内会は今後どうしたらいい？"),
        (["2-3　町内会の会計"], "2-3町内会の会計"),
        # 序数を伴うのが普通の語は落とさない。落とすと nav に同じタイトルが並ぶ
        (["付録1"], "付録1"),
        (["付録2"], "付録2"),
        (["註1"], "註1"),
        (["注2"], "注2"),
    ],
)
def test_heading_title_drops_page_number_glued_to_the_word(lines, expected):
    assert heading_title(lines) == expected


def test_nav_title_has_no_page_number():
    """heading_level が番号ごと見出しを拾うので、nav に「あとがき203」を出さない"""

    _, nav = split_chapters_and_nav([heading("あとがき203")])

    assert [n["title"] for n in nav] == ["あとがき"]
