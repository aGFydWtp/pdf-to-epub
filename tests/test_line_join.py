"""行またぎで割れた語の連結（book_ir.join_lines / is_split_token）の検証。

版面は語の途中でも改行するので、OCR は 1 つの語を 2 行に分けて返す。
join_lines() が行末・行頭の英数字を無条件に空白で繋いでいたため、'2015年' が
'20 15年'、'UAE' が 'UA E' として EPUB に出ていた（docs/HANDOFF.md 参照）。

**入力は生の OCR 行で書く。** join_lines() は正規化の前段にあり、本番の呼び出しも
`normalize_text(join_lines(b["lines"]))`（to_epub.py の render_block）という順序で、
normalize_text() を通した文字列が join_lines() へ入ることはない。
HANDOFF の「テストは normalize_text() を通した文字列で書くこと」は render_inline() 以降
（正立処理）に掛かる規則なので、レンダリングまで見る統合テストの側でそれに従う。
"""

import pytest

from book_ir import is_split_token, join_lines, normalize_text
from to_epub import render_inline

TCY_OPEN = '<span class="tcy">'


# --- 連結する（空白を入れてはいけない）形 -------------------------------
#
# 期待値は『イスラム教の論理』『“町内会”は義務ですか?』の OCR JSON 実測。
# 割れた側の行頭・行末は、いずれも和文に挟まれた数字か略語になっている。


@pytest.mark.parametrize(
    ("lines", "expected"),
    [
        # 『イスラム教の論理』p113。数字どうし
        (["するところが大きく、20", "15年から2020年にかけて"], "するところが大きく、2015年から2020年にかけて"),
        # 同 p27。割れる位置は桁の途中でもよい
        (["アッラームも2", "017年8月に"], "アッラームも2017年8月に"),
        # 同 p156「約9500円」。行頭側が 1 桁
        (["10万ディーナール(約950", "0円)、31歳から"], "10万ディーナール(約9500円)、31歳から"),
        # 同 p227「コーラン第3章195節」。章・節の番号
        (["コーラン第3章1", "95節には"], "コーラン第3章195節には"),
        # 同 p22「UAE」。行頭が単独の大文字
        (["サウジアラビアやUA", "Eなどでテロ組織"], "サウジアラビアやUAEなどでテロ組織"),
        # 同 p75「PDF版雑誌」
        (["映像や写真、声明文、PD", "F版雑誌などを"], "映像や写真、声明文、PDF版雑誌などを"),
        # 同 p103「LGBT活動家」。行末が単独の大文字。
        # 生 OCR は 'し' の誤読で、'L' は LLM 校正が入れた形（校正が空白挿入を誘発した）
        (["殺害や、L", "GBT活動家の殺害"], "殺害や、LGBT活動家の殺害"),
        # 『“町内会”は義務ですか?』p213「PTA」
        (["かならずこのP", "TAに入らなければ"], "かならずこのPTAに入らなければ"),
    ],
)
def test_split_token_is_joined_without_space(lines, expected):
    assert join_lines(lines) == expected


# --- 空白を残す（正当な語間・語の並び）形 -------------------------------


@pytest.mark.parametrize(
    ("lines", "expected"),
    [
        # 『イスラム教の論理』p88。行をまたぐ欧文の語間はこの空白が正しい
        (
            ["ある紛争政策分析研究所 The Institute", "for Policy Analysis of Conflict"],
            "ある紛争政策分析研究所 The Institute for Policy Analysis of Conflict",
        ),
        # 『レベニューオペレーション(RevOps)の教科書』p91。和文に挟まれていても、
        # 単独の大文字を含まない大文字語どうしは正当な語間
        (["進みました。当初『THE", "MODEL』(翔泳社)は"], "進みました。当初『THE MODEL』(翔泳社)は"),
        # 同 p138。大文字始まりでも小文字を含む語は欧文語
        (["月より代表取締役。Ask", "Oneは、あらゆる顧客"], "月より代表取締役。Ask Oneは、あらゆる顧客"),
        # 図表を段落として拾ったページの数値の並び。和文に挟まれていないので連結しない
        (["70", "60", "50"], "70 60 50"),
        # 索引の「見出し語＋ページ番号」（同 p327）
        (["ABM", "88"], "ABM 88"),
        # 行末側だけが和文に接している（ノンブルが次行に来た形）。片側だけでは連結しない
        (["タリバンは2", "017"], "タリバンは2 017"),
        # 行頭側だけが和文に接している（前行がノンブル）
        (["82", "2014年と比較すると"], "82 2014年と比較すると"),
    ],
)
def test_word_boundary_keeps_space(lines, expected):
    assert join_lines(lines) == expected


def test_is_split_token_needs_kanji_on_both_sides():
    assert is_split_token("大きく、20", "15年から")
    assert not is_split_token("20", "15年から")
    assert not is_split_token("大きく、20", "15")


def test_join_lines_leaves_non_alnum_boundaries_untouched():
    # 従来どおり、和文どうしは空白なしで連結する
    assert join_lines(["イスラム教の", "論理"]) == "イスラム教の論理"


# --- レンダリングまで通した統合テスト -----------------------------------
#
# 分断が残ると正立処理まで壊れる。'L GBT' は 'L' が単独大文字で対象外・'GBT' だけが
# 全角になり、'20 15年' は 2 桁ずつの縦中横が 2 つ並ぶ。連結した形でだけ正しく組める。
# ここは HANDOFF の規則どおり normalize_text() を通した文字列を render_inline() へ渡す。


@pytest.mark.parametrize(
    ("lines", "expected"),
    [
        (["するところが大きく、20", "15年から"], "するところが大きく、２０１５年から"),
        (["サウジアラビアやUA", "Eなどで"], "サウジアラビアやＵＡＥなどで"),
        (["殺害や、L", "GBT活動家"], "殺害や、ＬＧＢＴ活動家"),
        # 連結の結果ちょうど 2 桁になったものは縦中横になる（桁数規則は正立処理側）
        (["コーラン第3章1", "0節"], f"コーラン第３章{TCY_OPEN}10</span>節"),
    ],
)
def test_joined_token_renders_upright(lines, expected):
    assert render_inline(normalize_text(join_lines(lines)), vertical=True) == expected


def test_english_phrase_is_not_uprighted_after_join():
    # 空白を残した欧文はそのまま横倒し（小文字を含む語なので正立させない）
    src = normalize_text(join_lines(["研究所 The Institute", "for Policy Analysis"]))
    assert render_inline(src, vertical=True) == "研究所The Institute for Policy Analysis"
