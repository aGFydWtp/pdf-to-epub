"""印刷目次を本文から除外する処理（to_epub.filter_printed_toc）の検証。

除外に失敗すると印刷目次の行が本文へ二重掲載される。それも先頭章の途中に紛れる形で
出るので、EPUB を開いて読み進めるまで気づけない。

かつては「先頭行が正規化後ちょうど『目次』の見出しブロック」からしか除外を始められず、
実測で 2 冊とも掛からなかった:
  - 『レベニューオペレーション(RevOps)の教科書』p6-12: 「目次」の行が 1 本も立たず、
    先頭行は柱と最初のエントリが連結した 'はじめに2'。見出しブロックが 0 個
  - 『イスラム教の論理』p10-13: 目次扉が 'イスラム教の論理目次'
ブロックの座標データは持たせず、rows_of() が復元したあとの IR ブロックを直接組む。
"""

import pytest

from to_epub import filter_printed_toc, printed_toc_warning


def raw(page: int, text: str) -> dict:
    return {"kind": "raw", "text": text, "page": page}


def heading(page: int, *lines: str) -> dict:
    return {"kind": "heading", "level": "大", "lines": list(lines), "page": page}


def para(page: int, *lines: str) -> dict:
    return {"kind": "para", "lines": list(lines), "page": page, "pages": [page] * len(lines)}


# 実測: RevOps p3-13。はじめに（p3-）の途中に印刷目次 p6-12 が挟まる構成で、
# 目次ページには見出しブロックが 1 つも立たない
REVOPS_BLOCKS = [
    heading(3, "はじめに"),
    para(3, "本書は、持続的に収益成長する生産性高い組織構築の方法論である"),
    raw(6, "はじめに2"),
    raw(6, "第1章RevOpsとは何か17"),
    raw(7, "1-1レベニュー組織の分断18"),
    raw(12, "索引327"),
    para(13, "それでは本編に入ろう。"),
]


def test_printed_toc_pages_are_dropped_without_a_matching_heading():
    """見出しが「目次」と一致しなくても、--toc-pages の範囲なら除外できる"""

    out = filter_printed_toc(REVOPS_BLOCKS, toc_pages="6-12")

    assert [b["page"] for b in out] == [3, 3, 13]
    assert not [b for b in out if b["kind"] == "raw"]


def test_printed_toc_is_kept_without_toc_pages_when_no_heading_matches():
    """回帰の起点。--toc-pages を渡さないと従来どおり 1 ブロックも除外できない"""

    assert filter_printed_toc(REVOPS_BLOCKS) == REVOPS_BLOCKS


def test_toc_page_range_beats_a_mismatched_toc_heading():
    """目次扉が『イスラム教の論理』のように書名込みでも、ページ範囲なら除外できる"""

    blocks = [
        heading(2, "まえがき"),
        para(2, "イスラム教は特殊な宗教ではない。"),
        heading(10, "イスラム教の論理目次"),
        raw(10, "まえがき3"),
        raw(13, "第7章イスラム社会の常識と日常209"),
        heading(14, "第1章 イスラム教徒は「イスラム国」を否定できない"),
    ]

    out = filter_printed_toc(blocks, toc_pages="10-13")

    assert [b["page"] for b in out] == [2, 2, 14]


def test_index_pages_survive():
    """索引は本文として残す。--layout-pages に入っていても除外の根拠にはしない"""

    blocks = [
        raw(6, "はじめに2"),
        heading(327, "索引"),
        raw(327, "ARR 42, 118"),
        raw(332, "レベニュー組織 18"),
    ]

    out = filter_printed_toc(blocks, toc_pages="6-12")

    assert [b["page"] for b in out] == [327, 327, 332]


def test_exact_toc_heading_still_works_without_toc_pages():
    """--toc-pages を持たない呼び出しでは、従来の「目次」見出し起点の除外を維持する"""

    blocks = [
        heading(12, "目次"),
        raw(12, "はじめに4"),
        raw(14, "あとがき243"),
        heading(15, "序章 町内会って入らなくてもいいの？"),
        para(15, "町内会は任意加入の団体である。"),
    ]

    out = filter_printed_toc(blocks)

    assert [b["page"] for b in out] == [15, 15]


def test_keep_printed_toc_wins_over_toc_pages():
    """--keep-printed-toc は --toc-pages より優先する（印刷目次を残す明示指定）"""

    assert filter_printed_toc(REVOPS_BLOCKS, keep=True, toc_pages="6-12") == REVOPS_BLOCKS


def test_page_straddling_paragraph_loses_only_its_toc_lines():
    """目次ページから本文ページへ跨いだ段落は、目次側の行だけ落として本文を残す

    段落ブロックの代表ページ（"page"）だけで判定すると本文側まで巻き添えで消える。
    """

    blocks = [
        {
            "kind": "para",
            "lines": ["索引327", "それでは本編に入ろう。", "まず用語を揃える。"],
            "page": 12,
            "pages": [12, 13, 13],
        }
    ]

    out = filter_printed_toc(blocks, toc_pages="6-12")

    assert len(out) == 1
    assert out[0]["lines"] == ["それでは本編に入ろう。", "まず用語を揃える。"]
    assert out[0]["pages"] == [13, 13]
    # 代表ページも残った行に合わせる（校正 fixes の突合はこのページ番号を見る）
    assert out[0]["page"] == 13
    # 入力を破壊しない（IR は他の出力先とも共有される）
    assert blocks[0]["lines"] == ["索引327", "それでは本編に入ろう。", "まず用語を揃える。"]


def test_skip_pages_workaround_does_not_conflict():
    """印刷目次を --skip-pages で回避している既存の指定と重ねても壊れない

    build_ir() が skip 指定のページを読む前に落とすので、filter_printed_toc から見ると
    目次ページのブロックは最初から存在しない。空振りの警告も出さない。
    """

    blocks = [heading(2, "まえがき"), para(2, "イスラム教は特殊な宗教ではない。")]

    assert filter_printed_toc(blocks, toc_pages="10-13") == blocks
    assert printed_toc_warning("10-13", "1,10-13,238,239", n_removed=0) is None


def test_warns_when_the_toc_range_removed_nothing():
    """--toc-pages がずれていて 0 件なら警告する（黙ると二重掲載に気づけない）"""

    warning = printed_toc_warning("6-12", "1,2", n_removed=0)

    assert warning is not None
    assert "p6〜p12" in warning


@pytest.mark.parametrize("n_removed", [1, 72])
def test_no_warning_once_blocks_were_removed(n_removed):
    assert printed_toc_warning("6-12", "1,2", n_removed) is None
