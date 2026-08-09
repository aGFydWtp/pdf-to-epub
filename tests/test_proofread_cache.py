"""LLM 校正キャッシュがチャンクの内容で引かれることの検証。

キーがチャンク番号だけだった頃は、本文を直しても古い校正結果が返ってきた。
返ってくる修正は before と行番号で位置を指すので、本文が変わると before が
どこにも一致せず、修正が黙って捨てられる（読み順を直した際に、校正が
282 件から 56 件へ減り、ある章は 0 件になるという形で表面化した）。
ここでは claude CLI を偽物に差し替え、呼び出し回数でヒット／ミスを見る。
"""

import json
import subprocess

import pytest

import llm_proofread
import pdf_to_epub
from llm_proofread import chunk_cache_path, run_chunk

LINES = ["夏目漱石の木が仕上がりました。", "二行目の本文です。"]
EDIT = {"line": 1, "before": "木が仕上がりました", "after": "本が仕上がりました", "why": "字形が近い"}


class FakeClaude:
    """claude CLI の代わりに固定の修正候補を返し、呼ばれた回数を数える"""

    def __init__(self, edits: list[dict]):
        self.edits = edits
        self.calls = 0

    def __call__(self, cmd, **kwargs):
        self.calls += 1
        return subprocess.CompletedProcess(
            cmd, 0, stdout=json.dumps(self.edits, ensure_ascii=False), stderr=""
        )


@pytest.fixture
def claude(monkeypatch):
    fake = FakeClaude([EDIT])
    monkeypatch.setattr(llm_proofread.subprocess, "run", fake)
    return fake


def test_same_text_hits_the_cache(tmp_path, claude):
    """同じ本文・同じモデルなら二度目は CLI を呼ばずキャッシュを読む"""

    job = (0, LINES, 0, "sonnet", tmp_path, 900)
    assert run_chunk(job) == (0, [EDIT])
    assert claude.calls == 1

    assert run_chunk(job) == (0, [EDIT])
    assert claude.calls == 1


def test_changed_text_misses_the_cache(tmp_path, claude):
    """本文が 1 文字でも変われば、古い結果を流用せず引き直す（今回の回帰の核心）"""

    run_chunk((0, LINES, 0, "sonnet", tmp_path, 900))
    assert claude.calls == 1

    changed = ["夏目漱石の本が仕上がりました。", LINES[1]]
    run_chunk((0, changed, 0, "sonnet", tmp_path, 900))
    assert claude.calls == 2

    # 古いキャッシュは消さない。番号が同じでもハッシュが違うので共存し、参照されない
    assert len(list(tmp_path.glob("c0000_*.json"))) == 2


def test_changed_model_misses_the_cache(tmp_path, claude):
    """--model を変えたら校正しなおす（モデルが違えば指摘も変わる）"""

    run_chunk((0, LINES, 0, "sonnet", tmp_path, 900))
    run_chunk((0, LINES, 0, "opus", tmp_path, 900))
    assert claude.calls == 2
    assert len(list(tmp_path.glob("c0000_*.json"))) == 2


def test_changed_offset_misses_the_cache(tmp_path, claude):
    """行番号のオフセットもキーに含める。キャッシュ内の line は絶対行番号のため"""

    run_chunk((0, LINES, 0, "sonnet", tmp_path, 900))
    run_chunk((0, LINES, 40, "sonnet", tmp_path, 900))
    assert claude.calls == 2


def test_legacy_cache_file_is_ignored(tmp_path, claude):
    """ハッシュを含まない旧形式のファイルは自然にミスして無視される"""

    stale = [{"line": 1, "before": "存在しない文字列", "after": "誤った修正", "why": ""}]
    (tmp_path / "c0000.json").write_text(json.dumps(stale, ensure_ascii=False), encoding="utf-8")

    assert run_chunk((0, LINES, 0, "sonnet", tmp_path, 900)) == (0, [EDIT])
    assert claude.calls == 1


def test_failed_chunk_leaves_no_cache(tmp_path, monkeypatch):
    """claude が失敗したチャンクはキャッシュを書かない（0 件と失敗を取り違えない）"""

    def failing(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="auth error")

    monkeypatch.setattr(llm_proofread.subprocess, "run", failing)
    assert run_chunk((0, LINES, 0, "sonnet", tmp_path, 900)) == (0, [])
    assert not chunk_cache_path(tmp_path, 0, LINES, 0, "sonnet").exists()


# --------------------------------------------------------------------------
# 章単位パイプライン（pdf_to_epub）経由
# --------------------------------------------------------------------------


def proofread(blocks, cache_dir, model="sonnet"):
    return pdf_to_epub.proofread_chapter(blocks, (5, 5), cache_dir, model, 6000, 50, 900)


def test_chapter_pipeline_uses_the_content_keyed_cache(tmp_path, claude):
    """章ごとに --cache-dir を分ける pdf_to_epub 経由でも、内容でキーが決まる。

    pdf_to_epub 側はキャッシュの有無で成否を判定するので、命名を片方だけ
    変えると必ずここで RuntimeError になる。両者の結合を固定するテスト。
    """

    blocks = [{"kind": "para", "page": 5, "lines": list(LINES), "pages": [5, 5]}]
    cache_dir = tmp_path / "ch001"
    expected = [{"page": 5, "before": EDIT["before"], "after": EDIT["after"], "why": EDIT["why"]}]

    assert proofread(blocks, cache_dir) == expected
    assert claude.calls == 1

    # 同じ本文ならヒットする
    assert proofread(blocks, cache_dir) == expected
    assert claude.calls == 1

    # 冒頭に 1 行足すと引き直す。旧実装ではキャッシュに当たり、
    # line 1 を指す before が新しい 1 行目に一致せず修正が消えていた
    grown = [{"kind": "para", "page": 5, "lines": ["冒頭に一行足した。", *LINES], "pages": [5, 5, 5]}]
    claude.edits = [{**EDIT, "line": 2}]
    assert proofread(grown, cache_dir) == expected
    assert claude.calls == 2


def test_chapter_pipeline_raises_when_the_chunk_fails(tmp_path, monkeypatch):
    """キャッシュが書かれなければ失敗として中断する（メッセージに実ファイル名を出す）"""

    def failing(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="auth error")

    monkeypatch.setattr(llm_proofread.subprocess, "run", failing)
    blocks = [{"kind": "para", "page": 5, "lines": list(LINES), "pages": [5, 5]}]

    with pytest.raises(RuntimeError, match=r"ch001/c0000_[0-9a-f]{12}"):
        proofread(blocks, tmp_path / "ch001")
