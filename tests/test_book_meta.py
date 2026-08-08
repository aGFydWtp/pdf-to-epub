"""書誌 API レスポンスのパースと ISBN 正規化の検証。

ネットワークには一切触れず、実際に openBD / 国立国会図書館サーチが返してきた
レスポンスを削ったものを固定入力として使う。特に押さえたい実データの癖:
  - 古い本の奥付は ISBN-10（例: 4-06-149293-4）。openBD は 13 桁でしか引けない
  - NDL は ISBN-10 の値を xsi:type="dcndl:ISBN13" で返すことがある
  - 著者が典拠形（「中島, 義道, 1946-」）や役割付き（「平井靖史 編」）で返る
  - 書名検索には雑誌記事（category=記事）が混ざり、同じ本が複数レコードで返る
"""

import json

import pytest

from book_meta import (
    BookMeta,
    _format_pubdate,
    _pick_exact_isbn,
    clean_author,
    normalize_isbn,
    parse_ndl_items,
    parse_openbd,
)

# 「時間」を哲学する（講談社現代新書, 1996）の openBD レスポンスを必要な項目だけに削ったもの
OPENBD_RECORD = json.loads("""
{
  "onix": {
    "DescriptiveDetail": {
      "TitleDetail": {
        "TitleElement": {"TitleText": {"content": "「時間」を哲学する : 過去はどこへ行ったのか"}}
      },
      "Contributor": [
        {"SequenceNumber": "1", "PersonName": {"content": "中島, 義道, 1946-"}}
      ]
    },
    "PublishingDetail": {"Imprint": {"ImprintName": "講談社"}}
  },
  "summary": {
    "isbn": "9784061492936",
    "title": "「時間」を哲学する : 過去はどこへ行ったのか",
    "series": "講談社現代新書",
    "publisher": "講談社",
    "pubdate": "199603",
    "author": "中島,義道,1946-"
  }
}
""")

# NDL OpenSearch の RSS。1 件目は雑誌記事、2・3 件目は同じ図書の重複レコード、4 件目は別の図書。
NDL_RSS = """<?xml version="1.0" encoding="UTF-8"?>
<rss xmlns:dc="http://purl.org/dc/elements/1.1/"
     xmlns:dcndl="http://ndl.go.jp/dcndl/terms/"
     xmlns:dcterms="http://purl.org/dc/terms/"
     xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" version="2.0">
  <channel>
    <item>
      <title>瞬間と偶然--時間を哲学する</title>
      <link>https://ndlsearch.ndl.go.jp/books/R000000004-I9803827</link>
      <category>記事</category>
      <dc:creator>入不二 基義</dc:creator>
    </item>
    <item>
      <title>「時間」を哲学する : 過去はどこへ行ったのか</title>
      <link>https://ndlsearch.ndl.go.jp/books/R100000002-I000002487873</link>
      <category>図書</category>
      <category>紙</category>
      <dc:creator>中島, 義道, 1946-</dc:creator>
      <dcndl:seriesTitle>講談社現代新書</dcndl:seriesTitle>
      <dc:publisher>講談社</dc:publisher>
      <dc:date xsi:type="dcterms:W3CDTF">1996</dc:date>
      <dcterms:issued>1996.3</dcterms:issued>
      <dc:extent>214p</dc:extent>
      <dc:identifier xsi:type="dcndl:ISBN">4-06-149293-4</dc:identifier>
      <dc:identifier xsi:type="dcndl:ISBN13">4-06-149293-4</dc:identifier>
      <dc:identifier xsi:type="dcndl:NDLBibID">000002487873</dc:identifier>
    </item>
    <item>
      <title>「時間」を哲学する : 過去はどこへ行ったのか</title>
      <link>https://ndlsearch.ndl.go.jp/books/R100000137-I000002487873</link>
      <category>図書</category>
      <dc:creator>中島, 義道, 1946-</dc:creator>
      <dc:identifier xsi:type="dcndl:ISBN">4-06-149293-4</dc:identifier>
    </item>
    <item>
      <title>時間を哲学する : 思考のためのツールボックス</title>
      <link>https://ndlsearch.ndl.go.jp/books/R100000002-I034656350</link>
      <category>図書</category>
      <dc:creator>平井, 靖史, 1971-</dc:creator>
      <dc:publisher>慶應義塾大学出版会</dc:publisher>
      <dcterms:issued>2026.4</dcterms:issued>
      <dc:extent>295p</dc:extent>
      <dc:identifier xsi:type="dcndl:ISBN">978-4-7664-3093-6</dc:identifier>
    </item>
  </channel>
</rss>
""".encode("utf-8")


# --------------------------------------------------------------------------
# ISBN の正規化
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "raw,expected",
    [
        ("9784061492936", "9784061492936"),
        ("978-4-06-149293-6", "9784061492936"),
        ("  978 4 06 149293 6 ", "9784061492936"),
        ("ISBN978-4-06-149293-6", "9784061492936"),
        # ISBN-10 → 13。古い本の奥付はこちらで、変換しないと openBD で引けない
        ("4061492934", "9784061492936"),
        ("4-06-149293-4", "9784061492936"),
        # チェックディジットが X の ISBN-10
        ("080442957X", "9780804429573"),
        ("0-8044-2957-x", "9780804429573"),
    ],
)
def test_normalize_isbn_valid(raw, expected):
    assert normalize_isbn(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "",
        None,
        "9784061492930",  # 13 桁だがチェックディジット不一致（OCR 誤読の想定）
        "4061492935",  # 10 桁だがチェックディジット不一致
        "978406149293",  # 12 桁
        "97840614929361",  # 14 桁
        "講談社現代新書",
    ],
)
def test_normalize_isbn_invalid(raw):
    assert normalize_isbn(raw) is None


def test_normalize_isbn_is_idempotent():
    once = normalize_isbn("4-06-149293-4")
    assert normalize_isbn(once) == once


# --------------------------------------------------------------------------
# 著者名の整形
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "raw,expected",
    [
        ("中島, 義道, 1946-", "中島義道"),
        ("中島,義道,1946-", "中島義道"),
        ("平井, 靖史, 1971-", "平井靖史"),
        ("平井靖史 編", "平井靖史"),
        ("中島義道 著", "中島義道"),
        ("　夏目漱石　", "夏目漱石"),
        ("三島, 由紀夫, 1925-1970", "三島由紀夫"),
        ("中島義道", "中島義道"),
        ("", ""),
        # 欧文名は詰めると読めなくなるのでカンマを空白にする
        ("Strunk, William", "Strunk William"),
    ],
)
def test_clean_author(raw, expected):
    assert clean_author(raw) == expected


# --------------------------------------------------------------------------
# openBD
# --------------------------------------------------------------------------

def test_parse_openbd():
    meta = parse_openbd(OPENBD_RECORD)
    assert meta == BookMeta(
        isbn="9784061492936",
        title="「時間」を哲学する : 過去はどこへ行ったのか",
        author="中島義道",
        publisher="講談社",
        series="講談社現代新書",
        pubdate="1996.3",
        extent="",
        source="openBD",
        link="https://api.openbd.jp/v1/get?isbn=9784061492936",
    )


def test_parse_openbd_handles_missing_record():
    # openBD は未収録の ISBN に対して [null] を返す
    assert parse_openbd(None) is None
    assert parse_openbd({}) is None
    assert parse_openbd({"summary": {"isbn": "9784061492936", "title": ""}}) is None


def test_parse_openbd_joins_multiple_contributors():
    record = {
        "onix": {
            "DescriptiveDetail": {
                "Contributor": [
                    {"PersonName": {"content": "中島, 義道, 1946-"}},
                    {"PersonName": {"content": "平井靖史 編"}},
                    {"PersonName": {"content": "中島, 義道, 1946-"}},  # 重複は落とす
                ]
            }
        },
        "summary": {"isbn": "9784061492936", "title": "テスト書名"},
    }
    assert parse_openbd(record).author == "中島義道、平井靖史"


@pytest.mark.parametrize(
    "raw,expected",
    [("199603", "1996.3"), ("1996-03", "1996.3"), ("20260401", "2026.4"), ("1996", "1996"), ("", "")],
)
def test_format_pubdate(raw, expected):
    assert _format_pubdate(raw) == expected


# --------------------------------------------------------------------------
# 国立国会図書館サーチ
# --------------------------------------------------------------------------

def test_parse_ndl_items_filters_articles_and_dedupes():
    items = parse_ndl_items(NDL_RSS)
    assert [m.title for m in items] == [
        "「時間」を哲学する : 過去はどこへ行ったのか",
        "時間を哲学する : 思考のためのツールボックス",
    ]


def test_parse_ndl_items_fields():
    first = parse_ndl_items(NDL_RSS)[0]
    assert first == BookMeta(
        # ISBN13 型で返ってきた ISBN-10 の値も 13 桁に正規化される
        isbn="9784061492936",
        title="「時間」を哲学する : 過去はどこへ行ったのか",
        author="中島義道",
        publisher="講談社",
        series="講談社現代新書",
        pubdate="1996.3",
        extent="214p",
        source="NDLサーチ",
        link="https://ndlsearch.ndl.go.jp/books/R100000002-I000002487873",
    )


def test_parse_ndl_items_keeps_articles_when_asked():
    items = parse_ndl_items(NDL_RSS, books_only=False)
    assert items[0].title == "瞬間と偶然--時間を哲学する"
    assert len(items) == 3


def test_parse_ndl_items_survives_broken_xml():
    assert parse_ndl_items(b"<rss><channel><item>") == []
    assert parse_ndl_items(b"") == []


def test_summary_lines_skips_empty_fields():
    lines = BookMeta(title="書名", author="著者", source="openBD").summary_lines()
    assert lines == ["書名: 書名", "著者: 著者"]


# --------------------------------------------------------------------------
# ISBN 検索結果の突き合わせ
# --------------------------------------------------------------------------

# NDL の isbn 検索は完全一致ではない。実際に存在しない ISBN で検索したときに
# 返ってきた並びがこれで、先頭を採ると別の本の書名が EPUB に入ってしまう。
NDL_FUZZY_HITS = [
    BookMeta(isbn="9784938853044", title="薬になる野山の草・花・木"),
    BookMeta(isbn="", title="初級点字楽譜解説"),
    BookMeta(isbn="9784931078031", title="伝承真桑文楽 : 写真集"),
]


def test_pick_exact_isbn_rejects_fuzzy_hits():
    assert _pick_exact_isbn(NDL_FUZZY_HITS, "9784061492936") is None


def test_pick_exact_isbn_finds_match_below_the_head():
    target = BookMeta(isbn="9784061492936", title="「時間」を哲学する : 過去はどこへ行ったのか")
    assert _pick_exact_isbn([*NDL_FUZZY_HITS, target], "9784061492936") is target


def test_pick_exact_isbn_on_empty_candidates():
    assert _pick_exact_isbn([], "9784061492936") is None
