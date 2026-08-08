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


def join_lines(lines: list[str]) -> str:
    """折り返された行を 1 行に連結する（欧文どうしのみ空白で繋ぐ）"""

    out = ""
    for line in lines:
        line = line.strip()
        if not line:
            continue
        if out and re.search(r"[0-9A-Za-z]$", out) and re.match(r"[0-9A-Za-z]", line):
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
                "text": w["content"],
            }
        )
    lines.sort(key=lambda d: (d["y0"], d["x0"]))
    return lines


KANJI_RE = re.compile(r"[一-鿿々〆ヵヶ]")
TRIM_EDGE = "「」『』（）()〈〉《》、。，．・:：/／ 　"


def base_span(text: str, x0: float, x1: float, rx0: float, rx1: float, ruby: str):
    """ルビの矩形が覆う親文字の範囲 [start, end) を求める。

    行の矩形を各文字の字幅（和文は全角、欧文・数字は半角）で按分し、
    ルビと 60% 以上重なる文字を親文字とみなす。
    """

    widths = [1.0 if unicodedata.east_asian_width(c) in "WFA" else 0.5 for c in text]
    scale = (x1 - x0) / (sum(widths) or 1.0)
    bounds, acc = [], x0
    for w in widths:
        bounds.append((acc, acc + w * scale))
        acc += w * scale

    covered = [
        k
        for k, (a, b) in enumerate(bounds)
        if (min(rx1, b) - max(rx0, a)) > (b - a) * 0.6
    ]
    if not covered:
        mid = (rx0 + rx1) / 2
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
    """本文より小さい仮名行をルビとみなし、直下の親文字へ《》で結合する"""

    if len(lines) < 3:
        return lines
    med = statistics.median(l["h"] for l in lines)
    body = [i for i, l in enumerate(lines) if l["h"] >= med * 0.72]
    consumed: set[int] = set()
    # 親文字行ごとに (開始, 終了, ルビ) を集めてから、後ろ側から差し込む
    edits: dict[int, list[tuple[int, int, str]]] = {}

    for i, r in enumerate(lines):
        if r["h"] >= med * 0.72:
            continue
        text = r["text"].replace(" ", "").strip()
        if not text or not RUBY_RE.fullmatch(text):
            continue

        base_idx = None
        for j in body:
            b = lines[j]
            # 親文字行の矩形はルビと数 px 重なることがある
            gap = b["y0"] - r["y1"]
            if not (-med * 0.4 <= gap <= med * 0.9):
                continue
            overlap = min(r["x1"], b["x1"]) - max(r["x0"], b["x0"])
            if overlap < (r["x1"] - r["x0"]) * 0.5:
                continue
            if base_idx is None or b["y0"] < lines[base_idx]["y0"]:
                base_idx = j
        if base_idx is None:
            continue

        b = lines[base_idx]
        if not b["text"]:
            continue
        start, end = base_span(b["text"], b["x0"], b["x1"], r["x0"], r["x1"], text)
        if not b["text"][start:end].strip():
            continue
        edits.setdefault(base_idx, []).append((start, end, text))
        consumed.add(i)

    for idx, spans in edits.items():
        s = lines[idx]["text"]
        for start, end, ruby in sorted(spans, reverse=True):
            s = f"{s[:start]}｜{s[start:end]}《{ruby}》{s[end:]}"
        lines[idx]["text"] = s

    return [l for i, l in enumerate(lines) if i not in consumed]


def find_gutter(lines: list[dict], page_width: int) -> float | None:
    """段間の空白帯（ノド）を x 方向の被覆から探し、その中心を返す。

    1 段組でも行末は揃わないため、行頭 x や行末 x だけでは判別できない。
    どの行にも覆われない縦帯がページ中央付近にあるかどうかで判定する。
    """

    lo, hi = int(page_width * 0.35), int(page_width * 0.65)
    counts = [0] * (hi - lo)
    for l in lines:
        a, b = max(lo, int(l["x0"])), min(hi, int(l["x1"]))
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

    if best is None or (best[1] - best[0]) < page_width * 0.02:
        return None
    split = lo + (best[0] + best[1]) / 2
    left = sum(1 for l in lines if l["x0"] < split)
    if min(left, len(lines) - left) < len(lines) * 0.2:
        return None
    return split


def rows_of(lines: list[dict], page_width: int) -> list[str]:
    """段組・行位置から視覚的な行を復元する（目次・索引ページ向け）"""

    if not lines:
        return []

    split = find_gutter(lines, page_width)
    groups = (
        [[l for l in lines if l["x0"] < split], [l for l in lines if l["x0"] >= split]]
        if split
        else [lines]
    )

    out: list[str] = []
    for group in groups:
        rows: list[list[dict]] = []
        for l in sorted(group, key=lambda d: (d["y0"], d["x0"])):
            if rows and l["y0"] < rows[-1][-1]["y1"] - rows[-1][-1]["h"] * 0.4:
                rows[-1].append(l)
            else:
                rows.append([l])
        for row in rows:
            text = "　".join(x["text"].strip() for x in sorted(row, key=lambda d: d["x0"]))
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


def heading_level(text: str) -> str:
    head = text.split("\n")[0].strip()
    if re.match(r"^第\s*[ⅠⅡⅢⅣⅤ]\s*部", head):
        return "大"
    if re.match(r"^第\s*\d+\s*章", head):
        return "大"
    if re.match(r"^Column\s*\d+", head, re.IGNORECASE):
        return "大"
    if head in ("はじめに", "あとがき", "おわりに", "目次", "目 次", "索引", "事項索引", "人名索引", "文献一覧", "註", "注"):
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
            for row in rows_of(page_lines, width):
                if row in ("目次", "目 次", "事項索引", "人名索引", "索引"):
                    blocks.append({"kind": "heading", "level": "大", "lines": [row], "page": page_no})
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
            flush()
            titles = [l["text"] for l in page_lines]
            blocks.append({"kind": "heading", "level": "大", "lines": titles[:3], "page": page_no})
            for extra in titles[3:]:
                text = normalize_text(extra)
                if text:
                    blocks.append({"kind": "raw", "text": text, "page": page_no})
            continue

        # --- 章扉ページ（短いテキストだけで「第○章」「序章」からなる）---
        if len(page_text) < 120 and not data["tables"]:
            chapter_head = chapter_title_page(page_lines)
            if chapter_head:
                flush()
                blocks.append(
                    {"kind": "heading", "level": "大", "lines": chapter_head, "page": page_no}
                )
                continue

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

            lines = take_lines(el["box"])
            if lines:
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
            if el["role"] in ("page_header", "page_footer") or (
                (y_bottom < height * 0.06 or y_top > height * 0.93)
                and len(texts) == 1
                and "。" not in raw
                and len(raw) <= 40
            ):
                stats["除去"] += 1
                continue

            if el["role"] == "section_headings":
                flush()
                level = heading_level(raw)
                blocks.append({"kind": "heading", "level": level, "lines": texts, "page": page_no})
                first_content = False
                continue

            if re.match(r"^(図|表)\s*\d+", raw):
                flush()
                blocks.append({"kind": "raw", "text": normalize_text(join_lines(texts)), "page": page_no})
                first_content = False
                continue

            # --- 行頭字下げによる段落の切り出し ---
            if lines:
                left = min(l["x0"] for l in lines)
                unit = statistics.median(l["h"] for l in lines) * 0.45
                chunks: list[dict] = []
                for l in lines:
                    indented = (l["x0"] - left) > unit
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
