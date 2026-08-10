"""OCR JSON から書籍の中間表現（IR）を構築する共通モジュール。

to_aozora.py（青空文庫形式）と to_epub.py（EPUB3）はどちらもこの IR を経由して
出力を組み立てる。ページ単位の幾何再構成・見出し判定・ルビ結合・図版切り出しなど、
フォーマットに依存しない処理をここに集約する。

IR ブロックは以下の種類を持つ dict のリスト（`build_ir` の戻り値）:
  - {"kind":"heading", "level":"大|中|小", "lines":[…], "page":n}
  - {"kind":"para", "lines":[…], "page":n}
      （lines は改行前の生の行。｜親《ルビ》記法はこの時点で行内に埋め込まれている）
  - {"kind":"raw", "text":"…", "page":n}
      （目次・索引ページの復元行、図表キャプションなど、既に 1 行に確定した文字列）
  - {"kind":"table", "n_row":…, "n_col":…, "cells":[…], "page":n}
      （cells は OCR JSON の table["cells"] をそのまま保持する。row/col は 1 始まり）
  - {"kind":"figure", "src":"p0015_fig01.png", "alt":"…", "box":[x1,y1,x2,y2], "page":n}
"""

import re
import statistics
import subprocess
import unicodedata
from pathlib import Path

CJK = r"々〆぀-ヿ㐀-䶿一-鿿！-｠　-〃〈-】〔-〟"
DASHES = "‐‑‒–—―−－-"
RUBY_RE = re.compile(r"[ぁ-んァ-ヶーゝゞ・·‧／/]{1,24}")

PUNCT_MAP = {
    # ［］は青空文庫の注記記号なので、本文の角括弧は亀甲括弧に置き換える
    "(": "（", ")": "）", "[": "〔", "]": "〕", "{": "｛", "}": "｝",
    "?": "？", "!": "！", "·": "・", "‧": "・", ";": "；",
    "%": "％", "&": "＆", "~": "〜", "∼": "〜",
}

ROMAN = {1: "Ⅰ", 2: "Ⅱ", 3: "Ⅲ", 4: "Ⅳ", 5: "Ⅴ"}
BU_RE = re.compile(r"第\s*([^\s部]{1,4})\s*部")


# --------------------------------------------------------------------------
# テキスト正規化
# --------------------------------------------------------------------------


def normalize_text(s: str) -> str:
    """OCR 由来の表記ゆれを青空文庫寄りに整える（誤認識の修正はしない）"""

    # 全角ローマ数字とルビ記号は NFKC で分解・半角化されるので退避する
    keep = "ⅠⅡⅢⅣⅤ｜《》"
    s = re.sub(f"[{keep}]", lambda m: f"\x00{m.group(0)}\x00", s)
    s = "".join(
        p if p in keep else unicodedata.normalize("NFKC", p) for p in s.split("\x00")
    )

    s = re.sub(f"[{DASHES}]{{2,}}", "――", s)
    s = "".join(PUNCT_MAP.get(ch, ch) for ch in s)

    s = re.sub(f"(?<=[{CJK}]):", "：", s)
    s = re.sub(f":(?=[{CJK}])", "：", s)
    s = re.sub(f"(?<=[{CJK}]),", "、", s)
    s = re.sub(f"(?<=[{CJK}])\\.(?=[{CJK}])", "。", s)

    s = re.sub(f"(?<=[{CJK}])[ 　]+", "", s)
    s = re.sub(f"[ 　]+(?=[{CJK}])", "", s)
    s = re.sub(r"[ \t]{2,}", " ", s)
    return s.strip()


# 行またぎで割れた語を見分けるための、行末・行頭の英数字トークン。
# 前後が和文であることを lookbehind/lookahead で必須にしている（欧文の語間と区別する）。
SPLIT_TAIL_RE = re.compile(f"(?<=[{CJK}])([0-9A-Za-z]+)$")
SPLIT_HEAD_RE = re.compile(f"^([0-9A-Za-z]+)(?=[{CJK}])")


def is_split_token(left: str, right: str) -> bool:
    """行末と次行頭が 1 つの語を割ったものか判定する（空白で繋いではいけない形）。

    版面は語の途中でも改行するので、行末と次行頭が英数字というだけで空白を入れると
    語が割れる（'…大きく、20' + '15年から…' → '20 15年'）。かといって空白を一律に
    やめると、行をまたぐ欧文の語間（'…研究所 The Institute' + 'for Policy…'）が
    潰れる。そこで、和文に挟まれていること（＝日本語の本文中に置かれた数字・略語で
    あること）を必須にしたうえで、3 冊のコーパスで確認できた 2 形だけを拾う。

      - 数字どうし          '…、20' + '15年から…'  → 2015年
      - 片方が単独の大文字   '…やUA' + 'Eなど…'    → UAE

    単独の大文字 1 文字は、OCR が語を割ったときにしか現れない（to_epub の UPRIGHT_RE と
    同じ実測に基づく）。この条件を外すと『THE』+『MODEL』のような正当な欧文の語間まで
    潰れる（『レベニューオペレーション(RevOps)の教科書』p91 で実在）。
    和文に挟まれる条件のほうは、図表を段落として拾ってしまったページの数値の並び
    （'70' + '60' + '50' …）や、索引の「見出し語＋ページ番号」を守っている。
    """

    m_left, m_right = SPLIT_TAIL_RE.search(left), SPLIT_HEAD_RE.match(right)
    if not m_left or not m_right:
        return False
    tail, head = m_left.group(1), m_right.group(1)
    if tail.isdigit() and head.isdigit():
        return True
    return (
        tail.isalpha()
        and head.isalpha()
        and tail.isupper()
        and head.isupper()
        and min(len(tail), len(head)) == 1
    )


def join_lines(lines: list[str]) -> str:
    """折り返された行を 1 行に連結する（欧文どうしのみ空白で繋ぐ）"""

    out = ""
    for line in lines:
        line = line.strip()
        if not line:
            continue
        if (
            out
            and re.search(r"[0-9A-Za-z]$", out)
            and re.match(r"[0-9A-Za-z]", line)
            and not is_split_token(out, line)
        ):
            out += " "
        out += line
    return out


# --------------------------------------------------------------------------
# 「第○部」のローマ数字補正
# --------------------------------------------------------------------------


def roman_value(token: str) -> tuple[int | None, bool]:
    """OCR された部番号を (値, 信頼できるか) に変換する"""

    t = token.strip().replace(" ", "")
    if t == "川":
        return 3, True

    normalized = t
    for ch in "lLi|｜ｌ[]f{}!":
        normalized = normalized.replace(ch, "I")
    normalized = normalized.replace("Ⅰ", "I").replace("Ⅱ", "II").replace("Ⅲ", "III")

    if re.fullmatch(r"I{1,5}", normalized):
        # ローマ数字の字形が読めていれば信頼できる
        return len(normalized), True
    if re.fullmatch(r"1{2,5}", t):
        # 「11」「111」はローマ数字を算用数字と誤読したもの
        return len(t), True
    if re.fullmatch(r"[1-5]", t):
        # 一桁の算用数字は Ⅰ の誤読かもしれず、単独では信頼できない
        return int(t), False
    return None, False


def load_acrobat_pages(pdf_path: Path) -> list[str]:
    """PDF 内蔵のテキスト層をページごとに取り出す（取得できなければ空リスト）"""

    try:
        out = subprocess.run(
            ["pdftotext", str(pdf_path), "-"], capture_output=True, text=True, timeout=180
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return []
    if out.returncode != 0:
        return []
    return out.stdout.split("\f")


def fix_bu_numerals(texts: list[str], acrobat_text: str) -> list[str]:
    """ページ内の「第○部」を、内蔵テキスト層と突き合わせて全角ローマ数字に直す"""

    hits = [(i, m) for i, t in enumerate(texts) for m in BU_RE.finditer(t)]
    if not hits:
        return texts

    others = [m.group(1) for m in BU_RE.finditer(acrobat_text)]
    use_other = len(others) == len(hits)

    edits: dict[int, list[tuple[int, int, str]]] = {}
    for k, (i, m) in enumerate(hits):
        value, confident = roman_value(m.group(1))
        alt_value, alt_confident = roman_value(others[k]) if use_other else (None, False)
        if alt_confident:
            if not confident:
                value = alt_value
            elif value != alt_value:
                # 同じ字を繰り返すローマ数字は、本数が減る方向に誤読されやすい
                value = max(value, alt_value)
        if value in ROMAN:
            edits.setdefault(i, []).append((m.start(), m.end(), f"第{ROMAN[value]}部"))

    out = list(texts)
    for i, spans in edits.items():
        s = out[i]
        for start, end, rep in sorted(spans, reverse=True):
            s = s[:start] + rep + s[end:]
        out[i] = s
    return out


# --------------------------------------------------------------------------
# 行の構築とルビの結合
# --------------------------------------------------------------------------


def build_lines(words: list[dict]) -> list[dict]:
    lines = []
    for w in words:
        xs = [p[0] for p in w["points"]]
        ys = [p[1] for p in w["points"]]
        lines.append(
            {
                "x0": min(xs), "x1": max(xs),
                "y0": min(ys), "y1": max(ys),
                "h": max(ys) - min(ys),
                "w": max(xs) - min(xs),
                "vertical": w.get("direction") == "vertical",
                "text": w["content"],
            }
        )
    lines.sort(key=lambda d: (d["y0"], d["x0"]))
    return lines


def reading_order(lines: list[dict], vertical: bool) -> list[dict]:
    """行を組方向の読み順に並べ替える。

    縦組みは右の行から左へ、横組みは上の行から下へ読む。build_lines() の既定順は
    横組みの読み順なので、縦組みの要素はここで組み直す必要がある。1 ページに縦組みと
    横組みが混在する版面があるため、並べ替えはページ単位ではなく要素単位で行う。
    """

    if vertical:
        return sorted(lines, key=lambda d: (-d["x0"], d["y0"]))
    return sorted(lines, key=lambda d: (d["y0"], d["x0"]))


KANJI_RE = re.compile(r"[一-鿿々〆ヵヶ]")
TRIM_EDGE = "「」『』（）()〈〉《》、。，．・:：/／ 　"


def base_span(text: str, t0: float, t1: float, r0: float, r1: float, ruby: str):
    """ルビの矩形が覆う親文字の範囲 [start, end) を求める。

    座標は行の長さ方向（横組みは x、縦組みは y）へ射影した 1 次元で与える。
    t0/t1 が親文字行、r0/r1 がルビ行の範囲。行の矩形を各文字の字送り（和文は全角、
    欧文・数字は半角）で按分し、ルビと 60% 以上重なる文字を親文字とみなす。
    """

    widths = [1.0 if unicodedata.east_asian_width(c) in "WFA" else 0.5 for c in text]
    scale = (t1 - t0) / (sum(widths) or 1.0)
    bounds, acc = [], t0
    for w in widths:
        bounds.append((acc, acc + w * scale))
        acc += w * scale

    covered = [
        k
        for k, (a, b) in enumerate(bounds)
        if (min(r1, b) - max(r0, a)) > (b - a) * 0.6
    ]
    if not covered:
        mid = (r0 + r1) / 2
        covered = [
            min(range(len(bounds)), key=lambda k: abs(sum(bounds[k]) / 2 - mid))
        ]
    start, end = covered[0], covered[-1] + 1

    # 平仮名ルビは漢字（熟字訓を含む）に付くので、前後の仮名を削る
    if re.fullmatch(r"[ぁ-ん]+", ruby):
        while start < end - 1 and not KANJI_RE.match(text[start]):
            start += 1
        while end - 1 > start and not KANJI_RE.match(text[end - 1]):
            end -= 1
    else:
        while start < end - 1 and text[start] in TRIM_EDGE:
            start += 1
        while end - 1 > start and text[end - 1] in TRIM_EDGE:
            end -= 1
    return start, end


def attach_ruby(lines: list[dict]) -> list[dict]:
    """本文より小さい仮名行をルビとみなし、隣の親文字へ《》で結合する。

    ルビは横組みなら親文字の上、縦組みなら親文字の右に付く。字の大きさは組方向と
    直交する辺（横組みは高さ、縦組みは幅）に出るので、本文との大小はその辺で見分け、
    親文字のどこに掛かるかは行の長さ方向（横組みは x、縦組みは y）の重なりで決める。

    1 ページに両方の組方向が混在する版面があるため、中央値も親文字探しも組方向ごとに
    閉じて行う。縦組みページで行の高さ（＝行の長さ）の中央値を取ると、横組みの行が
    もれなくルビ候補になり、仮名だけのキャプションを本文へ飲み込む。

    1 語のルビが OCR で複数の行に割れることがあるので、同じ親文字行に掛かる断片は
    行の長さ方向で近接するものをまとめてから 1 つのルビとして付ける。
    """

    consumed: set[int] = set()
    # 親文字行ごとに (開始, 終了, ルビ) を集めてから、後ろ側から差し込む
    edits: dict[int, list[tuple[int, int, str]]] = {}

    for vertical in (False, True):
        group = [i for i, l in enumerate(lines) if bool(l["vertical"]) == vertical]
        if len(group) < 3:
            continue
        size, t0, t1 = ("w", "y0", "y1") if vertical else ("h", "x0", "x1")
        med = statistics.median(lines[i][size] for i in group)
        body = [i for i in group if lines[i][size] >= med * 0.72]
        # 親文字行ごとに、掛かるルビ行を (始点, 終点, 文字列, 行番号) で集める
        found: dict[int, list[tuple[float, float, str, int]]] = {}

        for i in group:
            r = lines[i]
            if r[size] >= med * 0.72:
                continue
            text = r["text"].replace(" ", "").strip()
            if not text or not RUBY_RE.fullmatch(text):
                continue

            base_idx = None
            for j in body:
                b = lines[j]
                # 親文字行の矩形はルビと数 px 重なることがある
                gap = r["x0"] - b["x1"] if vertical else b["y0"] - r["y1"]
                if not (-med * 0.4 <= gap <= med * 0.9):
                    continue
                overlap = min(r[t1], b[t1]) - max(r[t0], b[t0])
                if overlap < (r[t1] - r[t0]) * 0.5:
                    continue
                if base_idx is None:
                    base_idx = j
                    continue
                # ルビに最も近い行を親文字とする（縦組みは左隣＝x1 が大きい方）
                prev = lines[base_idx]
                nearer = b["x1"] > prev["x1"] if vertical else b["y0"] < prev["y0"]
                if nearer:
                    base_idx = j
            if base_idx is None:
                continue

            if not lines[base_idx]["text"]:
                continue
            found.setdefault(base_idx, []).append((r[t0], r[t1], text, i))

        # 1 語のルビが複数の行に割れて出ることがある（引き伸ばして掛かるルビで顕著）。
        # 行の長さ方向で近接する断片は 1 つのルビに戻し、断片全体を覆う範囲の親文字に付ける。
        # 閾値 0.8med は実測から: 割れた断片の間隔は 0.61med（『時間を哲学する』p0089 の
        # 「テロス」）、別語のルビ同士は間に親文字が入るので 1 文字＝1.0med 以上離れる。
        for base_idx, frags in found.items():
            b = lines[base_idx]
            runs: list[list[tuple[float, float, str, int]]] = []
            for f in sorted(frags):
                if runs and f[0] - max(g[1] for g in runs[-1]) <= med * 0.8:
                    runs[-1].append(f)
                else:
                    runs.append([f])

            for run in runs:
                r0, r1 = min(f[0] for f in run), max(f[1] for f in run)
                ruby = "".join(f[2] for f in run)
                start, end = base_span(b["text"], b[t0], b[t1], r0, r1, ruby)
                if not b["text"][start:end].strip():
                    continue
                edits.setdefault(base_idx, []).append((start, end, ruby))
                consumed.update(f[3] for f in run)

    for idx, spans in edits.items():
        s = lines[idx]["text"]
        for start, end, ruby in sorted(spans, reverse=True):
            s = f"{s[:start]}｜{s[start:end]}《{ruby}》{s[end:]}"
        lines[idx]["text"] = s

    return [l for i, l in enumerate(lines) if i not in consumed]


def find_gutter(lines: list[dict], extent: int, vertical: bool = False) -> float | None:
    """段間の空白帯（ノド）を、段が並ぶ方向の被覆から探して中心を返す。

    1 段組でも行末は揃わないため、行頭・行末の座標だけでは判別できない。
    どの行にも覆われない帯がページ中央付近にあるかどうかで判定する。
    横組みは段が左右に並ぶので x 方向、縦組みは段が上下に並ぶので y 方向を見る
    （extent はその方向のページの大きさ＝横組みなら幅、縦組みなら高さ）。
    """

    k0, k1 = ("y0", "y1") if vertical else ("x0", "x1")
    lo, hi = int(extent * 0.35), int(extent * 0.65)
    counts = [0] * (hi - lo)
    for l in lines:
        a, b = max(lo, int(l[k0])), min(hi, int(l[k1]))
        for x in range(a, b):
            counts[x - lo] += 1

    # 見出しなど数本の行はノドをまたぐので、完全な空白は求めない
    limit = max(1, int(len(lines) * 0.05))
    best, run_start = None, None
    for i in range(len(counts) + 1):
        if i < len(counts) and counts[i] <= limit:
            run_start = i if run_start is None else run_start
        elif run_start is not None:
            if best is None or i - run_start > best[1] - best[0]:
                best = (run_start, i)
            run_start = None

    if best is None or (best[1] - best[0]) < extent * 0.02:
        return None
    split = lo + (best[0] + best[1]) / 2
    first = sum(1 for l in lines if l[k0] < split)
    if min(first, len(lines) - first) < len(lines) * 0.2:
        return None
    return split


def rows_of(lines: list[dict], page_width: int, page_height: int | None = None) -> list[str]:
    """段組・行位置から視覚的な行を復元する（目次・索引ページ向け）。

    横組みは y の近い語をひとつの行にまとめて x 昇順に連結し、縦組みは x の近い語を
    ひとつの行（＝1 カラム）にまとめて y 昇順に連結する。縦組みの目次は章タイトルの
    真下にページ番号が置かれるので、この軸の入れ替えでページ番号まで同じ行に入る。
    軸を入れ替えないと全カラムが 1 行に連結され、行頭の「第○章」を拾えなくなる。
    """

    if not lines:
        return []

    vertical = sum(1 for l in lines if l["vertical"]) * 2 > len(lines)
    extent = (page_height or page_width) if vertical else page_width
    split = find_gutter(lines, extent, vertical)
    key = "y0" if vertical else "x0"
    groups = (
        [[l for l in lines if l[key] < split], [l for l in lines if l[key] >= split]]
        if split
        else [lines]
    )

    out: list[str] = []
    for group in groups:
        rows: list[list[dict]] = []
        # 縦組みは右の行から左へ読むので x0 の降順、横組みは上の行から下へ読むので y0 の昇順
        order = (lambda d: (-d["x0"], d["y0"])) if vertical else (lambda d: (d["y0"], d["x0"]))
        for l in sorted(group, key=order):
            prev = rows[-1][-1] if rows else None
            same_row = prev is not None and (
                l["x1"] > prev["x0"] + prev["w"] * 0.4
                if vertical
                else l["y0"] < prev["y1"] - prev["h"] * 0.4
            )
            if same_row:
                rows[-1].append(l)
            else:
                rows.append([l])
        for row in rows:
            inner = sorted(row, key=(lambda d: d["y0"]) if vertical else (lambda d: d["x0"]))
            text = "　".join(x["text"].strip() for x in inner)
            text = normalize_text(text)
            if text:
                out.append(text)
    return out


# --------------------------------------------------------------------------
# 見出し判定
# --------------------------------------------------------------------------


def fix_subtitle_dash(lines: list[str]) -> list[str]:
    """章扉・部扉の副題行の先頭・末尾のダーシュを「――」に正規化する。

    底本では副題は「――……――」で囲まれるが、細い罫線は OCR で欠落しやすい。
    見出しが3行以上（本題＋副題が複数行）のときだけ、最終行に対して補う
    （to_aozora の青空文庫出力・to_epub の EPUB 出力の双方で見た目を揃えるため共通化）。
    """

    lines = list(lines)
    if len(lines) >= 3:
        lines[-1] = re.sub(f"[{DASHES}]+$", "――", lines[-1])
        if not lines[-1].startswith("――"):
            lines[-1] = "――" + lines[-1]
    return lines


CHAPTER_NUM_TOKEN_RE = re.compile(r"^(第|章|\d{1,2})$")
CHAPTER_HEAD_RE = re.compile(r"^第\d{1,2}章$")
SPECIAL_CHAPTER_RE = re.compile(r"^[序終]章$")


def chapter_title_page(page_lines: list[dict]) -> list[str] | None:
    """章扉ページなら ["第○章", "章題"] を返す。章扉でなければ None を返す。

    章扉では「第」「1」「章」が独立した要素として横に並び、章題は縦組みで
    折り返されるため、OCR の読み順のままでは「1第章」「Rev0ps価値収益拡大を…」の
    ように崩れて heading_level() の「^第○章」に当たらない。番号は x 昇順
    （左→右）、章題は x 降順（右→左）で組み直す。

    「第○章」が柱（page_header）と判定されて捨てられる版面もあるため、
    要素のロールではなくページ全体の行から判定する。
    """

    nums: list[dict] = []
    titles: list[dict] = []
    for line in page_lines:
        text = normalize_text(line["text"]).replace(" ", "")
        if not text:
            continue
        bucket = nums if (CHAPTER_NUM_TOKEN_RE.match(text) or SPECIAL_CHAPTER_RE.match(text)) else titles
        bucket.append({"x": line["x0"], "text": text})

    if not nums:
        return None
    head = "".join(t["text"] for t in sorted(nums, key=lambda t: t["x"]))
    if not (CHAPTER_HEAD_RE.match(head) or SPECIAL_CHAPTER_RE.match(head)):
        return None

    title = "".join(t["text"] for t in sorted(titles, key=lambda t: -t["x"]))
    return [head, title] if title else [head]


UNNUMBERED_CHAPTER_WORDS = ("序章", "終章", "補章", "プロローグ", "エピローグ")

# 前付/後付の見出し。本文の見出し（「解説を書くということ」「参考にした資料」）と
# 紛れるため、語の直後で切れていることまで確かめる。
# 「引用参考文献」は中黒を空白として読んだ OCR（「引用 参考文献」）が正規化で
# 詰められた形。正規化が CJK 隣接空白を落とすので、中黒版とは別の語になる
FRONT_BACK_WORDS = (
    "はじめに", "まえがき", "序文", "序論", "序説", "序", "凡例",
    "あとがきにかえて", "あとがき", "おわりに", "むすび", "結び", "結語", "跋",
    "解説", "補遺", "付録", "附録", "年表", "謝辞",
    "引用・参考文献", "引用参考文献", "参考文献一覧", "参考文献", "文献一覧",
    "参考図書", "初出一覧",
    "事項索引", "人名索引", "索引", "註", "注", "目次",
)

FRONT_BACK_PREFIX = r"(?:訳者|著者|編者|監修者|文庫版|新装版|増補版|新版|日本語版)?"

# 印刷目次ページ自身を指す語。目次側の語彙からは差し引く（pdf_to_epub 参照）
TOC_SELF_WORDS = ("目次",)

# 語の直後で切れていることを要求する境界。見出しには級数の小さいページ番号が
# 貼り付いたまま届くことがある（「はじめに12」「はじめにiii」）ので、行末に加えて
# 数字とローマ数字も境界として認める。範囲は TRAILING_ROMAN_RE と揃えてある
WORD_END = r"(?=$|\d|[ivxlcIVXLC])"


def _alternation(words) -> str:
    # 長い語から並べるのは可読性のため。Python の選択肢は後続の照合に失敗すると
    # 後戻りするので、並び順は判定結果も一致範囲も変えない
    return "|".join(re.escape(w) for w in sorted(words, key=len, reverse=True))


def front_back_re(words) -> re.Pattern:
    """前付/後付の語から、語の直後で行が切れていることを要求する正規表現を作る"""

    return re.compile(rf"^{FRONT_BACK_PREFIX}(?:{_alternation(words)}){WORD_END}")


FRONT_BACK_RE = front_back_re(FRONT_BACK_WORDS)

# 番号を持たない章。本文用と目次用で判定が非対称なのは、入力の形が違うため。
# 本文の見出しは heading_level() が先頭トークンを切り出してから当てるので、
# 終端境界を課しても章題を取りこぼさず、「終章に向けて」のような句を章に
# 昇格させずに済む（トークン全体が「終章・あとがき」になるので中黒の除外も不要）。
UNNUMBERED_CHAPTER_RE = re.compile(rf"^(?:{_alternation(UNNUMBERED_CHAPTER_WORDS)}){WORD_END}")

# 目次側は rows_of() が章題もページ番号も 1 行に連結して渡すので
# （「序章町内会って入らなくてもいいの？17」）、終端境界を課すと正当な序章エントリを
# 落とし、その範囲が章プランから丸ごと消える。前方一致のまま、参考文献の区分ラベル
# 「終章・あとがき」だけを直後の中黒で弾く。
# 代わりに目次行「序章的な考察12」を章と誤認する余地が残るが、締めて正当な章を
# 落とすほうが害が大きい（本文 OCR も校正もされずに欠落する）ので受容し、
# dry-run の章境界一覧を人が確認する運用で補う
UNNUMBERED_CHAPTER_TOC_RE = re.compile(
    rf"^(?:{_alternation(UNNUMBERED_CHAPTER_WORDS)})(?![・·])"
)

# 序数を伴うのが普通の語。「付録1」「註2」の数字が序数かページ番号かは
# 区別できないので、落とさないほうを選ぶ。落とすと付録が複数ある本や部ごとに
# 後注を置く本で、nav に同じタイトルが並んでしまう
ORDINAL_SUFFIX_WORDS = ("付録", "附録", "註", "注")

PAGE_NUMBER_WORDS = tuple(
    w for w in FRONT_BACK_WORDS if w not in ORDINAL_SUFFIX_WORDS
) + UNNUMBERED_CHAPTER_WORDS

# 「語彙表の語＋ページ番号」の形だけを捉える。無条件に末尾の数字を落とすと
# 「Column 3」が「Column」になって壊れるので、数字を取り除いた残りが語彙表の語に
# 完全一致するときだけ削る。「終章2020年の町内会」のような正当な章題は末尾が
# 数字ではないので影響を受けない
FRONT_BACK_PAGE_RE = re.compile(
    rf"^({FRONT_BACK_PREFIX}(?:{_alternation(PAGE_NUMBER_WORDS)}))(?:\d+|[ivxlcIVXLC]+)$"
)


# 柱から前付/後付の見出しを拾うときの語彙。「目次」だけは差し引く。
# 昇格させると to_epub.filter_printed_toc() の「目次見出し配下を本文から除く」
# ロジックが、今まで発火しなかった本で発火して挙動が変わる。
# 差し引きの理由は pdf_to_epub.FRONT_BACK_TOC_RE と同じ（あちらは目次パース側）
FRONT_BACK_HEADER_RE = front_back_re(w for w in FRONT_BACK_WORDS if w not in TOC_SELF_WORDS)


def strip_trailing_page_number(title: str) -> str:
    """見出しに貼り付いたページ番号を落とす（「あとがき203」「終章227」→「あとがき」「終章」）。

    級数の小さいページ番号は柱として除去しきれず見出しに連結されて届くことがある。
    heading_level() はそれを見込んで境界に数字を認めるので、そのままだと nav と
    <title> に「あとがき203」が出てしまう。
    """

    m = FRONT_BACK_PAGE_RE.match(title)
    return m.group(1) if m else title


def front_back_heading_title(text: str) -> str:
    """前付/後付の見出しとして扱える柱なら、表示に使える 1 行を返す（でなければ空文字）。

    柱にはページ番号が貼り付いて届くことがある（「あとがき203」）。番号を落とした形を
    返すのは、nav と <title> に番号を出さないためと、同じ見出しの重複判定に使うキーを
    ページごとにぶれさせないため（素の文字列だと「あとがき」と「あとがき203」が
    別物になり、同じ節の途中で二度目の昇格が起きて章が分裂する）。
    """

    first_line = normalize_text(text.split("\n")[0]).strip()
    title = strip_trailing_page_number(first_line)
    if title and (FRONT_BACK_HEADER_RE.match(title) or UNNUMBERED_CHAPTER_RE.match(title)):
        return title
    return ""


def heading_level(text: str) -> str:
    # 受け取るのは OCR の生テキストなので、中点や全角数字・余分な空白が
    # 混ざったまま（「引用· 参考文献」）照合すると前付/後付の判定を取りこぼす
    first_line = text.split("\n")[0].strip()
    head = normalize_text(first_line).strip()
    # 番号付きは行全体で見る。先頭トークンだと「第 12 章 まとめ」が「第」になって落ちる
    if re.match(r"^第\s*[ⅠⅡⅢⅣⅤ]\s*部", head):
        return "大"
    if re.match(r"^第\s*\d+\s*章", head):
        return "大"
    if re.match(r"^Column\s*\d+", head, re.IGNORECASE):
        return "大"

    # 番号を持たない章（序章・終章など）と前付/後付。章扉ページ側の判定
    # （chapter_title_page）は飾りの数字が紛れ込むと外れるので、文字列でも拾う。
    #
    # 章語と章題、後付の語と解説者名の間には必ず改行か空きが入る
    # （「解説　佐藤太郎」「終章　親睦だけでもなんとかなる」）が、「終章に向けて」の
    # ような句には入らない。正規化は CJK 隣接空白を落としてしまうので、この区別は
    # 正規化前の先頭トークンでしか取れない。
    # 正規化後の行全体でも照合するのは、逆に OCR が語の内側へ空白を入れることが
    # あるため（「引用· 参考文献」「引用 参考文献」）。どちらも終端境界は課すので、
    # 「解説佐藤太郎」のように語の直後で切れないものは昇格しない
    first_token = normalize_text(re.split(r"[ \t　]", first_line, maxsplit=1)[0]).strip()
    for candidate in (first_token, head):
        if UNNUMBERED_CHAPTER_RE.match(candidate) or FRONT_BACK_RE.match(candidate):
            return "大"
    if re.match(r"^\d+\s*[-‐−–—－]\s*\d+", head):
        return "中"
    return "小"


# --------------------------------------------------------------------------
# 図版切り出し
# --------------------------------------------------------------------------


def crop_figures(pdf_path: Path, jobs: list[dict], fig_dir: Path, dpi: int):
    """IR の figure ブロック（page, box, name）から画像を切り出して保存する"""

    if not jobs:
        return
    import pypdfium2

    fig_dir.mkdir(parents=True, exist_ok=True)
    doc = pypdfium2.PdfDocument(str(pdf_path))
    try:
        by_page: dict[int, list[dict]] = {}
        for job in jobs:
            by_page.setdefault(job["page"], []).append(job)

        for page_no, page_jobs in sorted(by_page.items()):
            pil = doc[page_no - 1].render(scale=dpi / 72).to_pil().convert("RGB")
            for job in page_jobs:
                x1, y1, x2, y2 = (int(v) for v in job["box"])
                pad = 6
                pil.crop(
                    (
                        max(0, x1 - pad), max(0, y1 - pad),
                        min(pil.width, x2 + pad), min(pil.height, y2 + pad),
                    )
                ).save(fig_dir / job["name"])
    finally:
        doc.close()
    print(f"図版 {len(jobs)} 点を {fig_dir}/ に保存しました。")


# --------------------------------------------------------------------------
# ページ範囲指定
# --------------------------------------------------------------------------


def parse_pages(spec: str) -> set[int]:
    pages: set[int] = set()
    for part in filter(None, (p.strip() for p in spec.split(","))):
        if "-" in part:
            a, b = part.split("-", 1)
            pages.update(range(int(a), int(b) + 1))
        else:
            pages.add(int(part))
    return pages


# --------------------------------------------------------------------------
# IR 構築本体
# --------------------------------------------------------------------------


def _figure_alt(paragraphs: list[dict]) -> str:
    """図中のラベル（figures[].paragraphs）を連結して alt テキストにする"""

    labels = []
    for p in paragraphs:
        text = normalize_text(join_lines(p.get("contents", "").split("\n")))
        if text:
            labels.append(text)
    return "、".join(labels)


def build_ir(
    json_dir: str,
    pdf_path: str,
    layout_pages: str = "",
    skip_pages: str = "",
    page_filter: set[int] | None = None,
) -> tuple[list[dict], dict]:
    """OCR JSON ディレクトリと PDF から IR ブロック列を構築する。

    page_filter を指定すると、そのページ番号集合だけを対象に処理する
    （pdf_to_epub.py が章単位で OCR 完了直後に部分 IR を組み立てるために使う）。
    ページ跨ぎ段落の連結は指定範囲内でのみ行われる。

    戻り値は (blocks, stats)。stats はブロック化できない副次的な計数
    （ルビとして吸収された行数・ヘッダ/フッタとして除去した要素数）を持つ。
    """

    json_dir, pdf_path = Path(json_dir), Path(pdf_path)
    files = sorted(json_dir.glob("p*.json"))
    if not files:
        raise SystemExit(f"JSON が見つかりません: {json_dir}")

    layout = parse_pages(layout_pages)
    skip = parse_pages(skip_pages)
    acrobat = load_acrobat_pages(pdf_path)
    if not acrobat:
        print("警告: PDF 内蔵テキスト層を取得できませんでした（部番号の突合せを省略します）")

    blocks: list[dict] = []
    pending: list[str] | None = None
    pending_pages: list[int] | None = None
    stats = {"ルビ": 0, "除去": 0}
    # 前付/後付の見出しとして本の中で既に立っているタイトル（柱からの昇格の重複ガード）。
    # 見出しが既に立っている節は、後続ページの柱を昇格させてはいけない
    seen_front_back: set[str] = set()

    def flush():
        nonlocal pending, pending_pages
        if pending:
            # "page" はブロックの先頭行のページ（後方互換・見出し等との統一表示用）。
            # 段落はページを跨いで連結されうるので、行ごとの実ページは "pages" に別途持つ
            # （校正 fixes をページ番号で当て直す際に、代表ページだけでは行の実際の
            # 所在ページとずれることがあるため）。
            blocks.append(
                {"kind": "para", "lines": list(pending), "page": pending_pages[0], "pages": list(pending_pages)}
            )
        pending = None
        pending_pages = None

    def add_heading(lines: list[str], page_no: int, level: str = "大"):
        """見出しブロックを積み、大見出しなら前付/後付のタイトルを既出として控える。

        大見出しを積む経路は複数ある（章扉・部扉・段組ページ・section_headings・柱からの
        昇格）。どれか 1 つでも既出の登録を落とすと、後続ページの柱が「まだ立っていない
        見出し」と誤認されて二重に昇格し、nav が二重化して節の途中で章が割れる。
        登録漏れが起きないよう、大見出しの追加は必ずここを通す。
        """

        flush()
        blocks.append({"kind": "heading", "level": level, "lines": list(lines), "page": page_no})
        if level != "大":
            return
        for line in lines:
            title = front_back_heading_title(line)
            if title:
                seen_front_back.add(title)

    for file in files:
        import json as jsonlib

        try:
            data = jsonlib.loads(file.read_text(encoding="utf-8"))
        except (jsonlib.JSONDecodeError, UnicodeDecodeError, OSError) as e:
            raise SystemExit(f"OCR JSON の読み込みに失敗しました: {file}: {e}") from e
        page_no = data["page"]
        if page_no in skip:
            continue
        if page_filter is not None and page_no not in page_filter:
            continue

        height = data["image_size"]["h"]
        width = data["image_size"]["w"]

        raw_lines = build_lines(data["words"])
        before = len(raw_lines)
        page_lines = attach_ruby(raw_lines)
        stats["ルビ"] += before - len(page_lines)

        # 「第○部」の補正はページ内の行順（＝おおむね読み順）で行う
        acrobat_text = acrobat[page_no - 1] if page_no - 1 < len(acrobat) else ""
        fixed = fix_bu_numerals([l["text"] for l in page_lines], acrobat_text)
        for line, text in zip(page_lines, fixed):
            line["text"] = text

        # --- 目次・索引など段組ページは幾何的に行を復元する ---
        if page_no in layout:
            flush()
            for row in rows_of(page_lines, width, height):
                if row in ("目次", "事項索引", "人名索引", "索引"):
                    add_heading([row], page_no)
                else:
                    blocks.append({"kind": "raw", "text": row, "page": page_no})
            continue

        elements: list[tuple[int, str, dict]] = []
        elements += [(p["order"], "p", p) for p in data["paragraphs"]]
        elements += [(t["order"], "t", t) for t in data["tables"]]
        elements += [(f["order"], "f", f) for f in data["figures"]]
        elements.sort(key=lambda e: e[0])

        # --- 部扉ページ（短いテキストだけで「第○部」から始まる）---
        page_text = "".join(l["text"] for l in page_lines)
        if (
            len(page_text) < 120
            and re.match(r"^第\s*[ⅠⅡⅢⅣⅤ]\s*部", normalize_text(page_text))
            and not data["tables"]
        ):
            titles = [l["text"] for l in page_lines]
            add_heading(titles[:3], page_no)
            for extra in titles[3:]:
                text = normalize_text(extra)
                if text:
                    blocks.append({"kind": "raw", "text": text, "page": page_no})
            continue

        # --- 章扉ページ（短いテキストだけで「第○章」「序章」からなる）---
        if len(page_text) < 120 and not data["tables"]:
            chapter_head = chapter_title_page(page_lines)
            if chapter_head:
                add_heading(chapter_head, page_no)
                continue

        # このページで section_headings として取れている前付/後付の見出しを先に控える。
        # 同じページの柱を昇格させると見出しが二重になり、後続ページの柱を昇格させると
        # 節の途中で章が切れる。要素を 1 つずつ見ていては手遅れなので先読みする
        for par in data["paragraphs"]:
            if par.get("role") != "section_headings":
                continue
            known = front_back_heading_title(par.get("contents") or "")
            if known:
                seen_front_back.add(known)

        consumed: list[bool] = [False] * len(page_lines)
        fig_index = 0
        first_content = True

        def take_lines(box) -> list[dict]:
            """矩形に含まれる未使用の行を読み順で取り出す"""
            x1, y1, x2, y2 = box
            got = []
            for idx, l in enumerate(page_lines):
                if consumed[idx]:
                    continue
                cx, cy = (l["x0"] + l["x1"]) / 2, (l["y0"] + l["y1"]) / 2
                if x1 - 2 <= cx <= x2 + 2 and y1 - 2 <= cy <= y2 + 2:
                    consumed[idx] = True
                    got.append(l)
            return got

        for _, kind, el in elements:
            if kind == "t":
                flush()
                take_lines(el["box"])
                blocks.append(
                    {
                        "kind": "table",
                        "n_row": el["n_row"],
                        "n_col": el["n_col"],
                        "cells": el["cells"],
                        "page": page_no,
                    }
                )
                first_content = False
                continue

            if kind == "f":
                flush()
                take_lines(el["box"])
                fig_index += 1
                name = f"p{page_no:04d}_fig{fig_index:02d}.png"
                blocks.append(
                    {
                        "kind": "figure",
                        "src": name,
                        "alt": _figure_alt(el.get("paragraphs", [])),
                        "box": el["box"],
                        "page": page_no,
                    }
                )
                first_content = False
                continue

            vertical = el.get("direction") == "vertical"
            lines = take_lines(el["box"])
            if lines:
                lines = reading_order(lines, vertical)
                texts = [l["text"] for l in lines]
            elif page_lines:
                # 行がすべてルビとして親文字に取り込まれた、あるいは
                # 先行要素に割り当て済み。二重出力を避けて読み飛ばす。
                continue
            else:
                texts = el["contents"].split("\n")
            raw = "\n".join(texts).strip()
            if not raw:
                continue

            y_top, y_bottom = el["box"][1], el["box"][3]

            # --- 柱としてしか現れない前付/後付の見出しを大見出しへ昇格させる ---
            # 前付の見出しが独立した要素として取れず、柱（page_header）としてだけ
            # 現れる本がある。柱ごと捨てると大見出しが 1 つも立たず、その範囲は
            # 本文としては出るのに nav から丸ごと落ちる。
            # フッタ柱（page_footer）は対象外: そのページの本文ブロックより後ろに
            # 見出しが来るため、split_chapters_and_nav がそのページの本文を
            # 前の章へ押し込んでしまう。
            # 昇格しなかった柱は下の除去へ落ちる（＝従来どおりの挙動）
            if el["role"] == "page_header":
                title = front_back_heading_title(raw)
                if title and title not in seen_front_back:
                    # level は heading_level() の戻り値に依らず「大」で固定する。
                    # 大でなければ split_chapters_and_nav が nav ノードを作らず、
                    # 昇格させた意味がなくなる
                    add_heading([title], page_no)
                    first_content = False
                    continue

            if el["role"] in ("page_header", "page_footer") or (
                (y_bottom < height * 0.06 or y_top > height * 0.93)
                and len(texts) == 1
                and "。" not in raw
                and len(raw) <= 40
            ):
                stats["除去"] += 1
                continue

            if el["role"] == "section_headings":
                add_heading(texts, page_no, heading_level(raw))
                first_content = False
                continue

            if re.match(r"^(図|表)\s*\d+", raw):
                flush()
                blocks.append({"kind": "raw", "text": normalize_text(join_lines(texts)), "page": page_no})
                first_content = False
                continue

            # --- 行頭字下げによる段落の切り出し ---
            # 字下げは行送りと直交する向き（横組みなら右、縦組みなら下）へずれる。
            # 閾値は 1 字ぶんなので、字の大きさにあたる辺（横組みは高さ、縦組みは幅）
            # の中央値から取る。縦組みで h を使うと行の長さが閾値になって発動しない。
            if lines:
                if vertical:
                    unit = statistics.median(l["w"] for l in lines) * 0.45
                    head = min(l["y0"] for l in lines)
                    offsets = [l["y0"] - head for l in lines]
                else:
                    unit = statistics.median(l["h"] for l in lines) * 0.45
                    head = min(l["x0"] for l in lines)
                    offsets = [l["x0"] - head for l in lines]
                chunks: list[dict] = []
                for l, offset in zip(lines, offsets):
                    indented = offset > unit
                    if not chunks or indented:
                        chunks.append({"lines": [l["text"]], "indented": indented})
                    else:
                        chunks[-1]["lines"].append(l["text"])
            else:
                chunks = [{"lines": texts, "indented": not first_content}]

            for i, chunk in enumerate(chunks):
                continues_prev = i == 0 and first_content and not chunk["indented"]
                if not continues_prev:
                    flush()
                pending = (pending or []) + chunk["lines"]
                pending_pages = (pending_pages or []) + [page_no] * len(chunk["lines"])
            first_content = False

    flush()
    return blocks, stats
