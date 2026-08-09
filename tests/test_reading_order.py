"""組方向（縦組み／横組み）に応じた読み順・字下げ判定・ルビ結合の検証。

縦組みページで本文の行が横組みの読み順（上→下、左→右）に並べ替えられ、段落内の文が
ばらばらになる不具合の回帰テスト。実際の底本では複数行の縦組み段落の 86% で行順が
壊れていた。

book_ir は import 時に yomitoku を必要としない（PDF 内蔵テキスト層の取得も pdftotext の
subprocess なので、失敗すれば空リストにフォールバックする）ため、OCR 環境なしで回る。
"""

import json

import pytest

from book_ir import attach_ruby, build_ir, build_lines, reading_order

CHAR = 30  # 1 文字の一辺（px）


def vword(x0: int, y0: int, text: str) -> dict:
    """縦組みの 1 行。x0 が右にあるほど先に読む"""

    y1 = y0 + CHAR * len(text)
    return {
        "points": [[x0, y0], [x0 + CHAR, y0], [x0 + CHAR, y1], [x0, y1]],
        "content": text,
        "direction": "vertical",
    }


def hword(x0: int, y0: int, text: str, size: int = CHAR) -> dict:
    """横組みの 1 行。y0 が上にあるほど先に読む"""

    x1 = x0 + size * len(text)
    return {
        "points": [[x0, y0], [x1, y0], [x1, y0 + size], [x0, y0 + size]],
        "content": text,
        "direction": "horizontal",
    }


def box_of(words: list[dict]) -> list[int]:
    xs = [p[0] for w in words for p in w["points"]]
    ys = [p[1] for w in words for p in w["points"]]
    return [min(xs) - 5, min(ys) - 5, max(xs) + 5, max(ys) + 5]


def write_page(tmp_path, words: list[dict], direction: str, contents: str, box=None):
    """1 段落だけのページを OCR JSON として書き出し、そのディレクトリを返す"""

    page = {
        "paragraphs": [
            {
                "box": box or box_of(words),
                "contents": contents,
                "direction": direction,
                "order": 0,
                "role": None,
            }
        ],
        "tables": [],
        "words": words,
        "figures": [],
        "page": 1,
        "image_size": {"h": 1470, "w": 940},
    }
    json_dir = tmp_path / "ocr_json"
    json_dir.mkdir()
    (json_dir / "p0001.json").write_text(json.dumps(page, ensure_ascii=False), encoding="utf-8")
    return json_dir


def paragraphs_of(json_dir) -> list[list[str]]:
    blocks, _ = build_ir(str(json_dir), str(json_dir / "no-such.pdf"))
    return [b["lines"] for b in blocks if b["kind"] == "para"]


# --- 読み順 -------------------------------------------------------------


def test_縦組みは右の行から読む():
    lines = build_lines([vword(100, 50, "みぎ"), vword(300, 50, "ひだり")])
    assert [l["text"] for l in reading_order(lines, vertical=True)] == ["ひだり", "みぎ"]


def test_横組みは上の行から読む():
    lines = build_lines([hword(50, 300, "した"), hword(50, 100, "うえ")])
    assert [l["text"] for l in reading_order(lines, vertical=False)] == ["うえ", "した"]


def test_同じ列に分かれた行は上から順に並ぶ():
    """1 列が複数の検出矩形に割れても、列のなかの順序は上→下を保つ"""

    lines = build_lines([vword(300, 400, "あと"), vword(300, 50, "さき"), vword(500, 50, "みぎ")])
    assert [l["text"] for l in reading_order(lines, vertical=True)] == ["みぎ", "さき", "あと"]


def test_build_lines_が組方向と字の大きさを保持する():
    (v,) = build_lines([vword(100, 50, "たて")])
    assert v["vertical"] is True
    # 縦組みでは幅が字の大きさ、高さは行の長さになる
    assert (v["w"], v["h"]) == (CHAR, CHAR * 2)

    (h,) = build_lines([hword(100, 50, "よこ")])
    assert h["vertical"] is False


# --- 段落の組み立て -----------------------------------------------------


def test_縦組み段落の行が原文どおりの順序で連結される(tmp_path):
    """OCR の words を順不同で与えても、右の列から読み直される"""

    words = [
        vword(200, 100, "みっつめの行。"),
        vword(400, 100, "ひとつめの行、"),
        vword(300, 100, "ふたつめの行、"),
    ]
    json_dir = write_page(tmp_path, words, "vertical", "ひとつめの行、\nふたつめの行、\nみっつめの行。")
    assert paragraphs_of(json_dir) == [["ひとつめの行、", "ふたつめの行、", "みっつめの行。"]]


def test_縦組みは行の_x_のずれで段落を割らない(tmp_path):
    """縦組みでは行頭 x が読み順に沿って単調に減る。これを字下げと誤読しない"""

    words = [vword(400 - i * CHAR, 100, f"{i}行目のながいほんぶん、") for i in range(6)]
    json_dir = write_page(tmp_path, words, "vertical", "\n".join(w["content"] for w in words))
    assert len(paragraphs_of(json_dir)) == 1


def test_縦組みの字下げは行頭の_y_で判定する(tmp_path):
    """縦組みの字下げは下方向。2 行目だけ 1 字ぶん下げて新しい段落を始める"""

    words = [
        vword(400, 100, "まえの段落のおわり。"),
        vword(360, 100 + CHAR, "つぎの段落のはじまり、"),
        vword(320, 100, "そのつづき。"),
    ]
    json_dir = write_page(tmp_path, words, "vertical", "\n".join(w["content"] for w in words))
    assert paragraphs_of(json_dir) == [
        ["まえの段落のおわり。"],
        ["つぎの段落のはじまり、", "そのつづき。"],
    ]


def test_横組みの字下げは行頭の_x_で判定する(tmp_path):
    """横組みの挙動は従来どおり（字下げは右方向）"""

    words = [
        hword(100, 100, "まえの段落のおわり。"),
        hword(100 + CHAR, 200, "つぎの段落のはじまり、"),
        hword(100, 300, "そのつづき。"),
    ]
    json_dir = write_page(tmp_path, words, "horizontal", "\n".join(w["content"] for w in words))
    assert paragraphs_of(json_dir) == [
        ["まえの段落のおわり。"],
        ["つぎの段落のはじまり、", "そのつづき。"],
    ]


@pytest.mark.parametrize("direction", ["vertical", "horizontal"])
def test_行が取れないときは_contents_に退避する(tmp_path, direction):
    """words が空でも、段落の contents から原文の順序を復元できる"""

    json_dir = write_page(
        tmp_path, [], direction, "いちぎょうめ、\nにぎょうめ。", box=[100, 100, 400, 400]
    )
    assert paragraphs_of(json_dir) == [["いちぎょうめ、", "にぎょうめ。"]]


# --- ルビ ---------------------------------------------------------------


def test_横組みのルビが親文字に結合される():
    """ルビは親文字の上に載る小さい仮名行。行の高さの中央値で本文と見分ける"""

    lines = build_lines(
        [
            hword(100, 100, "かんじ", size=20),  # 親文字 2 字ぶんの幅に収まる
            hword(100, 118, "漢字"),
            hword(100, 500, "ほかのほんぶん"),
        ]
    )
    assert [l["text"] for l in attach_ruby(lines)] == ["｜漢字《かんじ》", "ほかのほんぶん"]


def test_縦組みページの横組み行をルビとして飲み込まない():
    """縦組みの行の高さ（＝行の長さ）で中央値を取ると、横組みの行がそろって
    「本文より小さい行」に見え、目次の項目まで親文字に吸い込まれてしまう。
    """

    body = [vword(400 - i * CHAR, 100, "たてぐみのほんぶんがつづく") for i in range(4)]
    caption = hword(100, 60, "みだし")  # 仮名だけの横組み行
    lines = build_lines([*body, caption])
    assert "みだし" in [l["text"] for l in attach_ruby(lines)]
