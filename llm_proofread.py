"""青空文庫形式テキストの OCR 誤認識を claude CLI で校正する。

本文そのものを LLM に書き直させると、行の欠落や無断の言い換えが混ざりうる。
そこでこのスクリプトは「修正リスト（JSON）」だけを出力させ、
before が実際にその行へ一意に存在することを確かめてから機械的に置換する。
適用・却下の内訳はログに残るため、あとから監査できる。
"""

import argparse
import hashlib
import json
import re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

PROMPT = """あなたは日本語の書籍を校正する校正者です。
以下は、スキャンした書籍を OCR し、青空文庫形式に変換したテキストの一部です。
行番号を付けて示します。底本は時間論を扱う哲学の論集で、哲学・心理学・物理学の
専門用語と、欧文の人名・書名が頻出します。

【仕事】
OCR の誤認識だけを指摘してください。文章の書き換え・要約・表現の改善はしないでください。

【修正してよいもの】
- 字形が似ていることによる漢字の誤認識（例:「挟出」→「傑出」、「木が仕上がりました」→「本が仕上がりました」）
- 仮名の誤認識（例:「そのなかの」つに」→「そのなかの一つに」）
- 前後の文脈から一意に決まる、明らかな脱字・衍字
- 数字・記号の誤認識（例:「*!」→「*1」、「1」と「l」、「O」と「0」の取り違え）
- 欧文の人名・術語のつづりの誤認識（例:「lames 1890」→「James 1890」、「Witmann」→「Wittmann」）
- ルビ ｜親文字《ルビ》 の親文字の範囲がずれているもの
  （例:「1つの具｜体的な存《コンクリート》在者」→「1つの｜具体的《コンクリート》な存在者」）

【修正してはいけないもの】
- 著者の用字・言い回し・漢字とかなの使い分け
- 人名・専門用語・書名の表記（確実に OCR 誤認識と言えない限り触らない）
- ［＃…］ という注記そのもの
- 行の分割・結合・追加・削除
- 判断に迷うもの（迷ったら修正しない。見逃しより誤修正のほうが有害です）

【出力形式】
JSON 配列だけを出力してください。前置きも説明文も不要です。

[{{"line": 行番号, "before": "元の文字列", "after": "修正後の文字列", "why": "根拠を20字以内で"}}]

- "before" はその行に実際にある文字列を、その行で一意に特定できる最小限の長さで、そのまま書いてください。
- 修正すべき箇所がなければ [] とだけ出力してください。

【テキスト】
{text}
"""

HEADING_RE = re.compile(r"^(.*)［＃「(.*)」は(大|中|小)見出し］$")
ANNOT_RE = re.compile(r"［＃[^］]*］")
# LLM は行の途中までしか引用しないことがあるので、閉じない注記も注記として守る
ANNOT_LOOSE_RE = re.compile(r"［＃[^］]*(?:］|$)")
# 角括弧の異体。LLM が引用しなおすときに揺れるので、照合時は同一視する
BRACKETS = str.maketrans({"［": "〔", "］": "〕", "[": "〔", "]": "〕"})


def chunk_lines(lines: list[str], max_chars: int, max_lines: int) -> list[tuple[int, int]]:
    """(開始行, 終了行) の組に分割する（行番号は 0 始まり、終了は含まない）"""

    chunks, start, size = [], 0, 0
    for i, line in enumerate(lines):
        size += len(line) + 1
        if i - start + 1 >= max_lines or size >= max_chars:
            chunks.append((start, i + 1))
            start, size = i + 1, 0
    if start < len(lines):
        chunks.append((start, len(lines)))
    return chunks


def extract_json(output: str) -> list[dict]:
    """claude の応答から JSON 配列を取り出す"""

    text = output.strip()
    fence = re.search(r"```(?:json)?\s*(.+?)```", text, re.S)
    if fence:
        text = fence.group(1).strip()
    start, end = text.find("["), text.rfind("]")
    if start == -1 or end == -1:
        return []
    try:
        data = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return []
    return data if isinstance(data, list) else []


def chunk_cache_path(cache_dir: Path, index: int, lines: list[str], offset: int, model: str) -> Path:
    """そのチャンクの校正結果を置くパスを返す。

    チャンク番号だけをキーにすると、本文を直したのに古い結果が返ってきてしまう。
    キャッシュされた修正は before と行番号で位置を指すので、本文が変われば
    before はどこにも一致せず、修正が黙って捨てられる（実際に本文の読み順を
    直したとき、校正が 282 件から 56 件へ激減して顕在化した）。
    そこで、プロンプトを決めるもの（モデル・行番号のオフセット・本文）を
    すべてハッシュに入れる。合わないキャッシュは自然にミスして無視される。
    """

    payload = "\x00".join([model, str(offset), *lines])
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return cache_dir / f"c{index:04d}_{digest[:12]}.json"


def run_chunk(job) -> tuple[int, list[dict]]:
    index, lines, offset, model, cache_dir, timeout = job
    cache = chunk_cache_path(cache_dir, index, lines, offset, model)
    if cache.exists():
        return index, json.loads(cache.read_text(encoding="utf-8"))

    numbered = "\n".join(f"{offset + i + 1}: {l}" for i, l in enumerate(lines))
    try:
        proc = subprocess.run(
            # --strict-mcp-config: 校正に MCP は不要。全 MCP 設定を無視して起動を軽くする
            ["claude", "-p", "--strict-mcp-config", "--model", model],
            input=PROMPT.format(text=numbered),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        print(f"  chunk {index}: タイムアウト", file=sys.stderr)
        return index, []

    if proc.returncode != 0:
        print(
            f"  chunk {index}: claude 失敗 rc={proc.returncode} {proc.stderr[:200]}",
            file=sys.stderr,
        )
        return index, []

    edits = extract_json(proc.stdout)
    cache.write_text(json.dumps(edits, ensure_ascii=False), encoding="utf-8")
    return index, edits


def sanitize(text: str) -> str:
    """本文に紛れ込んだ角括弧を亀甲括弧へ直す（青空文庫の注記だけは残す）"""

    parts, last = [], 0
    for m in ANNOT_LOOSE_RE.finditer(text):
        parts.append(text[last : m.start()].translate(BRACKETS))
        parts.append(m.group(0))
        last = m.end()
    parts.append(text[last:].translate(BRACKETS))
    return "".join(parts)


def locate(line: str, before: str) -> int | None:
    """before が行のどこにあるかを返す。一意に決まらなければ None。"""

    if line.count(before) == 1:
        return line.index(before)
    if before in line:
        return None  # 複数該当。どれを直すか決められない
    # 1 文字ずつの置換なので、変換しても位置はずれない
    canon_line, canon_before = line.translate(BRACKETS), before.translate(BRACKETS)
    if canon_line.count(canon_before) == 1:
        return canon_line.index(canon_before)
    return None


def validate(edit: dict, lines: list[str]) -> tuple[bool, str, int]:
    """修正候補が安全に適用できるか確かめ、適用位置を返す"""

    if not isinstance(edit, dict):
        return False, "形式不正", -1
    line_no, before, after = edit.get("line"), edit.get("before"), edit.get("after")
    if not isinstance(line_no, int) or not isinstance(before, str) or not isinstance(after, str):
        return False, "形式不正", -1
    if not 1 <= line_no <= len(lines):
        return False, "行番号が範囲外", -1
    if "\n" in after or "\r" in after:
        return False, "改行を含む", -1
    after = sanitize(after)
    edit["after"] = after  # ログにも実際に適用した文字列を残す
    if not before or before == after:
        return False, "変更なし", -1
    if abs(len(after) - len(before)) > max(4, len(before)):
        return False, "変更量が大きすぎる", -1
    if after.count("《") != after.count("》"):
        return False, "ルビ記号が不整合", -1

    line = lines[line_no - 1]
    pos = locate(line, before)
    if pos is None:
        return False, "before が行に一意に見つからない", -1

    applied = line[:pos] + after + line[pos + len(before) :]
    for m in ANNOT_RE.finditer(line):
        if m.start() < pos + len(before) and pos < m.end():
            # 見出し行は本文と注記が同じ文字列を持つため、行全体にまたがる指摘もある。
            # 直した結果が見出しの形を保っていれば受け入れ、注記はあとで再生成する。
            if HEADING_RE.match(line) and HEADING_RE.match(applied):
                break
            return False, "注記に触れている", -1
    return True, "", pos


def write_log(path: Path, meta: list[str], applied: list[dict], rejected: list[tuple[dict, str]]):
    def cell(value) -> str:
        return str(value).replace("|", "\\|")

    out = ["# OCR 校正ログ", "", *meta, "", "## 適用した修正", "",
           "| 行 | 修正前 | 修正後 | 根拠 |", "|---|---|---|---|"]
    for e in applied:
        out.append(f"| {e['line']} | {cell(e['before'])} | {cell(e['after'])} | {cell(e.get('why', ''))} |")
    out += ["", "## 却下した候補", "", "| 行 | 修正前 | 修正後 | 却下理由 |", "|---|---|---|---|"]
    for e, reason in rejected:
        out.append(f"| {cell(e.get('line'))} | {cell(e.get('before'))} | {cell(e.get('after'))} | {reason} |")
    path.write_text("\n".join(out) + "\n", encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description="青空文庫テキストの OCR 誤認識を LLM で校正する")
    parser.add_argument("-i", "--input", required=True, help="校正前のテキスト")
    parser.add_argument("-o", "--output", required=True, help="校正後のテキスト")
    parser.add_argument("--log", default="claudedocs/proofread_log.md", help="校正ログ")
    parser.add_argument("--cache-dir", default="proofread_cache", help="応答のキャッシュ先")
    parser.add_argument("--model", default="sonnet", help="claude -p に渡すモデル")
    parser.add_argument("--max-chars", type=int, default=6000, help="1 チャンクの文字数上限")
    parser.add_argument("--max-lines", type=int, default=50, help="1 チャンクの行数上限")
    parser.add_argument("--workers", type=int, default=4, help="並列実行数")
    parser.add_argument("--timeout", type=int, default=900, help="1 チャンクの制限時間（秒）")
    parser.add_argument("--limit", type=int, default=0, help="先頭 N チャンクだけ処理（動作確認用）")
    args = parser.parse_args()

    lines = Path(args.input).read_text(encoding="utf-8").split("\n")
    cache_dir = Path(args.cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)

    chunks = chunk_lines(lines, args.max_chars, args.max_lines)
    if args.limit:
        chunks = chunks[: args.limit]
    print(f"{len(lines):,} 行 / {len(chunks)} チャンク / モデル {args.model} / 並列 {args.workers}")

    jobs = [
        (i, lines[a:b], a, args.model, cache_dir, args.timeout)
        for i, (a, b) in enumerate(chunks)
    ]
    results: dict[int, list[dict]] = {}
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        for done, (index, edits) in enumerate(pool.map(run_chunk, jobs), 1):
            results[index] = edits
            print(f"  [{done}/{len(jobs)}] chunk {index}: 候補 {len(edits)} 件", flush=True)

    applied, rejected = [], []
    for index in sorted(results):
        for edit in results[index]:
            ok, reason, pos = validate(edit, lines)
            if not ok:
                rejected.append((edit, reason))
                continue
            n = edit["line"] - 1
            line, before = lines[n], edit["before"]
            lines[n] = line[:pos] + edit["after"] + line[pos + len(before) :]
            applied.append(edit)

    # 見出し行は本文と［＃「…」は◯見出し］の複製が一致していなければならない
    fixed_headings = 0
    for i, line in enumerate(lines):
        m = HEADING_RE.match(line)
        if m and m.group(1) != m.group(2):
            lines[i] = f"{m.group(1)}［＃「{m.group(1)}」は{m.group(3)}見出し］"
            fixed_headings += 1

    Path(args.output).write_text("\n".join(lines), encoding="utf-8")

    log = Path(args.log)
    log.parent.mkdir(parents=True, exist_ok=True)
    write_log(
        log,
        [
            f"- 入力: `{args.input}`",
            f"- 出力: `{args.output}`",
            f"- モデル: `{args.model}` / チャンク数: {len(chunks)}",
            f"- 適用: {len(applied)} 件 / 却下: {len(rejected)} 件 / 見出し注記の再生成: {fixed_headings} 件",
        ],
        applied,
        rejected,
    )

    print(f"\n出力: {args.output}")
    print(f"  適用 {len(applied)} 件 / 却下 {len(rejected)} 件 / 見出し注記 {fixed_headings} 件")
    print(f"  ログ: {log}")


if __name__ == "__main__":
    main()
