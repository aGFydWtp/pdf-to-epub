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
from llm_proofread import DEFAULT_BOOK_CONTEXT, ChunkJob, chunk_cache_path, run_chunk

LINES = ["夏目漱石の木が仕上がりました。", "二行目の本文です。"]
EDIT = {"line": 1, "before": "木が仕上がりました", "after": "本が仕上がりました", "why": "字形が近い"}
CONTEXT = "底本は B2B SaaS の営業組織を扱う実務書で、英略語が頻出します。"


def job(cache_dir, lines=LINES, *, index=0, offset=0, model="sonnet", **kwargs) -> ChunkJob:
    return ChunkJob(
        index=index, lines=lines, offset=offset, model=model,
        cache_dir=cache_dir, timeout=900, **kwargs,
    )


class FakeClaude:
    """claude CLI の代わりに固定の修正候補を返し、呼ばれた回数と渡された入力を記録する"""

    def __init__(self, edits: list[dict]):
        self.edits = edits
        self.calls = 0
        self.inputs: list[str] = []

    def __call__(self, cmd, **kwargs):
        self.calls += 1
        self.inputs.append(kwargs.get("input", ""))
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

    assert run_chunk(job(tmp_path)) == (0, [EDIT])
    assert claude.calls == 1

    assert run_chunk(job(tmp_path)) == (0, [EDIT])
    assert claude.calls == 1


def test_changed_text_misses_the_cache(tmp_path, claude):
    """本文が 1 文字でも変われば、古い結果を流用せず引き直す（今回の回帰の核心）"""

    run_chunk(job(tmp_path))
    assert claude.calls == 1

    changed = ["夏目漱石の本が仕上がりました。", LINES[1]]
    run_chunk(job(tmp_path, changed))
    assert claude.calls == 2

    # 古いキャッシュは消さない。番号が同じでもハッシュが違うので共存し、参照されない
    assert len(list(tmp_path.glob("c0000_*.json"))) == 2


def test_changed_model_misses_the_cache(tmp_path, claude):
    """--model を変えたら校正しなおす（モデルが違えば指摘も変わる）"""

    run_chunk(job(tmp_path, model="sonnet"))
    run_chunk(job(tmp_path, model="opus"))
    assert claude.calls == 2
    assert len(list(tmp_path.glob("c0000_*.json"))) == 2


def test_changed_offset_misses_the_cache(tmp_path, claude):
    """行番号のオフセットもキーに含める。キャッシュ内の line は絶対行番号のため"""

    run_chunk(job(tmp_path, offset=0))
    run_chunk(job(tmp_path, offset=40))
    assert claude.calls == 2


def test_changed_book_context_misses_the_cache(tmp_path, claude):
    """--book-context を変えたら校正しなおす。

    底本の説明はプロンプトの一部なので、変えれば指摘も変わる。キーに入れないと、
    分野を差し替えたつもりで、前の本の前提で得た結果が黙って返ってくる。
    """

    run_chunk(job(tmp_path))
    run_chunk(job(tmp_path, book_context=CONTEXT))
    assert claude.calls == 2
    assert len(list(tmp_path.glob("c0000_*.json"))) == 2

    # 同じ説明に戻せばキャッシュに当たる
    run_chunk(job(tmp_path, book_context=CONTEXT))
    assert claude.calls == 2


def test_book_context_reaches_the_prompt(tmp_path, claude):
    """指定した底本の説明が実際にプロンプトへ入る（キーだけ変えて配線を忘れる事故の検出）"""

    run_chunk(job(tmp_path, book_context=CONTEXT))
    assert CONTEXT in claude.inputs[0]
    assert DEFAULT_BOOK_CONTEXT not in claude.inputs[0]

    # 未指定なら分野中立の既定文言。特定ジャンルを決め打ちしない
    run_chunk(job(tmp_path, index=1))
    assert DEFAULT_BOOK_CONTEXT in claude.inputs[1]
    assert "哲学" not in claude.inputs[1]


def test_legacy_cache_file_is_ignored(tmp_path, claude):
    """ハッシュを含まない旧形式のファイルは自然にミスして無視される"""

    stale = [{"line": 1, "before": "存在しない文字列", "after": "誤った修正", "why": ""}]
    (tmp_path / "c0000.json").write_text(json.dumps(stale, ensure_ascii=False), encoding="utf-8")

    assert run_chunk(job(tmp_path)) == (0, [EDIT])
    assert claude.calls == 1


def test_failed_chunk_leaves_no_cache(tmp_path, monkeypatch):
    """claude が失敗したチャンクはキャッシュを書かない（0 件と失敗を取り違えない）"""

    def failing(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="auth error")

    monkeypatch.setattr(llm_proofread.subprocess, "run", failing)
    assert run_chunk(job(tmp_path)) == (0, [])
    assert not chunk_cache_path(job(tmp_path)).exists()


# --------------------------------------------------------------------------
# 章単位パイプライン（pdf_to_epub）経由
# --------------------------------------------------------------------------


def proofread(blocks, cache_dir, model="sonnet", book_context=DEFAULT_BOOK_CONTEXT):
    return pdf_to_epub.proofread_chapter(
        blocks, (5, 5), cache_dir, model, 6000, 50, 900, book_context=book_context
    )


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


def test_chapter_pipeline_separates_cache_by_book_context(tmp_path, claude):
    """--book-context は pdf_to_epub 経由でもプロンプトへ届き、キャッシュを分ける。

    キャッシュキーだけ直して CLI 引数の配線を忘れると、こちらの経路だけ
    既定文言のまま動いてしまう。argparse からプロンプトまでを通しで固定する。
    """

    blocks = [{"kind": "para", "page": 5, "lines": list(LINES), "pages": [5, 5]}]
    cache_dir = tmp_path / "ch001"

    proofread(blocks, cache_dir)
    proofread(blocks, cache_dir, book_context=CONTEXT)
    assert claude.calls == 2
    assert CONTEXT in claude.inputs[1]
    assert len(list(cache_dir.glob("c0000_*.json"))) == 2


def test_chapter_pipeline_raises_when_the_chunk_fails(tmp_path, monkeypatch):
    """キャッシュが書かれなければ失敗として中断する（メッセージに実ファイル名を出す）"""

    def failing(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="auth error")

    monkeypatch.setattr(llm_proofread.subprocess, "run", failing)
    blocks = [{"kind": "para", "page": 5, "lines": list(LINES), "pages": [5, 5]}]

    with pytest.raises(RuntimeError, match=r"ch001/c0000_[0-9a-f]{12}"):
        proofread(blocks, tmp_path / "ch001")
