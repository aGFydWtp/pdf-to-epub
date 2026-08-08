"""書誌 API から書籍のメタデータ（書名・著者・出版社など）を取得する。

使う API はどちらも登録・API キー不要:
  - openBD (https://api.openbd.jp/)  ISBN 検索のみ。版元ドットコム＋NDL 由来の書誌
  - 国立国会図書館サーチ OpenSearch  ISBN 検索・書名検索の両方に対応

ISBN 検索は openBD → NDL の順にフォールバックする（openBD の方が応答が速く
レート制限も緩いが、収録が薄い本があるため）。書名検索は NDL のみ。

目次データはどちらの API でも提供されていないため、章立ては従来どおり印刷目次の
OCR から取る。ここで取得するのは書誌情報だけ。

ネットワーク不通・該当なしの場合は None / 空リストを返す。呼び出し側は
`--title` などの手動指定にフォールバックすること。

CLI としても使える:
    python book_meta.py --isbn 9784061492936
    python book_meta.py --title "時間を哲学する"
    python book_meta.py --title "時間を哲学する" --json
"""

import argparse
import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass

OPENBD_ENDPOINT = "https://api.openbd.jp/v1/get"
NDL_ENDPOINT = "https://ndlsearch.ndl.go.jp/api/opensearch"
USER_AGENT = "pdf-to-epub/0.1 (+https://github.com/aGFydWtp/pdf-to-epub)"
# NDL は同時アクセス数の上限が厳しく、混んでいると応答が数秒〜十数秒かかる（実測）。
DEFAULT_TIMEOUT = 15

# NDL は図書のほかに雑誌記事なども返す。書籍 PDF の書誌として使えるのはこれだけ。
BOOK_CATEGORIES = {"図書"}


@dataclass
class BookMeta:
    """書誌 API から取得した書籍メタデータ（取得できなかった項目は空文字）"""

    isbn: str = ""
    title: str = ""
    author: str = ""
    publisher: str = ""
    series: str = ""
    pubdate: str = ""
    extent: str = ""
    source: str = ""
    link: str = ""

    def summary_lines(self) -> list[str]:
        """人間が確認するための表示用の行（値のある項目だけ）"""

        labels = [
            ("書名", self.title),
            ("著者", self.author),
            ("出版社", self.publisher),
            ("シリーズ", self.series),
            ("刊行", self.pubdate),
            ("ページ数", self.extent),
            ("ISBN", self.isbn),
        ]
        return [f"{k}: {v}" for k, v in labels if v]


# --------------------------------------------------------------------------
# ISBN の正規化
# --------------------------------------------------------------------------

def _isbn10_check_digit(body: str) -> str:
    """ISBN-10 の先頭 9 桁からチェックディジットを求める（10 は 'X'）"""

    total = sum((10 - i) * int(c) for i, c in enumerate(body))
    rem = (11 - total % 11) % 11
    return "X" if rem == 10 else str(rem)


def _isbn13_check_digit(body: str) -> str:
    """ISBN-13 の先頭 12 桁からチェックディジットを求める"""

    total = sum((3 if i % 2 else 1) * int(c) for i, c in enumerate(body))
    return str((10 - total % 10) % 10)


def normalize_isbn(raw: str) -> str | None:
    """ISBN をハイフンなしの 13 桁に正規化する。

    ISBN-10 は 978 を付けてチェックディジットを計算し直す。スキャン対象になるような
    古い本は奥付の ISBN が 10 桁であり、この変換がないと openBD で引けない。
    チェックディジットが合わない文字列には None を返す（OCR 誤読の検出に使える）。
    """

    if not raw:
        return None
    s = re.sub(r"[\s　\-‐‑–—―ー]", "", str(raw)).upper()
    s = re.sub(r"^ISBN", "", s)

    if re.fullmatch(r"\d{9}[\dX]", s):
        if s[9] != _isbn10_check_digit(s[:9]):
            return None
        body = "978" + s[:9]
        return body + _isbn13_check_digit(body)

    if re.fullmatch(r"\d{13}", s):
        if s[12] != _isbn13_check_digit(s[:12]):
            return None
        return s

    return None


# --------------------------------------------------------------------------
# 著者名の整形
# --------------------------------------------------------------------------

# NDL 由来の典拠形は「中島, 義道, 1946-」のように生没年が付く。
_AUTHOR_YEARS_RE = re.compile(r"[,、]?[\s　]*\d{3,4}[\s　]*[-–—~〜][\s　]*\d{0,4}[\s　]*$")
# 責任表示の役割（「平井靖史 編」など）。dc:creator には人名だけ入れる。
_AUTHOR_ROLE_RE = re.compile(r"[\s　]*(著述|編著|共著|監修|編集|著|編|訳|撰|画)[\s　]*$")
_JA_CHAR_RE = re.compile(r"[ぁ-んァ-ヶ一-龥]")


def clean_author(raw: str) -> str:
    """API が返す著者表記を dc:creator に入れられる形に整える。

    「中島, 義道, 1946-」「中島,義道,1946-」→「中島義道」、「平井靖史 編」→「平井靖史」。
    欧文名は姓名を詰めると読めなくなるので、カンマを空白に置き換えるに留める。
    """

    s = (raw or "").strip()
    if not s:
        return ""
    prev = None
    while prev != s:
        prev = s
        s = _AUTHOR_YEARS_RE.sub("", s).strip()
        s = _AUTHOR_ROLE_RE.sub("", s).strip()
    if _JA_CHAR_RE.search(s):
        s = re.sub(r"[\s　]*[,、][\s　]*", "", s)
    else:
        s = re.sub(r"\s*,\s*", " ", s)
    return s.strip()


def _join_authors(names: list[str]) -> str:
    """複数著者を「、」でつなぐ（重複と空文字は落とす）"""

    out: list[str] = []
    for n in names:
        c = clean_author(n)
        if c and c not in out:
            out.append(c)
    return "、".join(out)


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------

def _http_get(url: str, timeout: int = DEFAULT_TIMEOUT) -> bytes | None:
    """GET してボディを返す。失敗したら理由を表示して None（呼び出し側でフォールバック）"""

    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as res:
            return res.read()
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        print(f"  書誌 API への接続に失敗しました（{url.split('?')[0]}）: {e}", flush=True)
        return None


# --------------------------------------------------------------------------
# openBD
# --------------------------------------------------------------------------

def _format_pubdate(raw: str) -> str:
    """openBD の pubdate（199603 / 1996-03）を「1996.3」形式に寄せる"""

    s = re.sub(r"\D", "", raw or "")
    if len(s) >= 6:
        return f"{s[:4]}.{int(s[4:6])}"
    if len(s) == 4:
        return s
    return (raw or "").strip()


def parse_openbd(record: dict | None) -> BookMeta | None:
    """openBD の 1 レコード（onix + summary）を BookMeta にする"""

    if not record:
        return None
    summary = record.get("summary") or {}
    onix = record.get("onix") or {}

    # 著者は summary.author（「中島,義道,1946-」）より ONIX の Contributor の方が
    # 構造化されていて複数著者も取れる。無ければ summary にフォールバックする。
    contributors = (onix.get("DescriptiveDetail") or {}).get("Contributor") or []
    names = [((c.get("PersonName") or {}).get("content") or "") for c in contributors]
    author = _join_authors(names) or clean_author(summary.get("author", ""))

    title = (summary.get("title") or "").strip()
    if not title:
        return None

    isbn = normalize_isbn(summary.get("isbn", "")) or ""
    return BookMeta(
        isbn=isbn,
        title=title,
        author=author,
        publisher=(summary.get("publisher") or "").strip(),
        series=(summary.get("series") or "").strip(),
        pubdate=_format_pubdate(summary.get("pubdate", "")),
        source="openBD",
        link=f"https://api.openbd.jp/v1/get?isbn={isbn}" if isbn else "",
    )


def _from_openbd(isbn13: str, timeout: int = DEFAULT_TIMEOUT) -> BookMeta | None:
    body = _http_get(f"{OPENBD_ENDPOINT}?isbn={isbn13}", timeout=timeout)
    if body is None:
        return None
    try:
        records = json.loads(body)
    except json.JSONDecodeError:
        return None
    if not isinstance(records, list) or not records:
        return None
    return parse_openbd(records[0])


# --------------------------------------------------------------------------
# 国立国会図書館サーチ（OpenSearch / RSS）
# --------------------------------------------------------------------------

def _local(tag: str) -> str:
    """`{namespace}creator` → `creator`（RSS 内の名前空間を意識せずに扱う）"""

    return tag.split("}")[-1]


def _child_texts(item: ET.Element, name: str) -> list[str]:
    return [(e.text or "").strip() for e in item if _local(e.tag) == name and (e.text or "").strip()]


def _child_text(item: ET.Element, name: str) -> str:
    values = _child_texts(item, name)
    return values[0] if values else ""


def parse_ndl_items(xml_bytes: bytes, books_only: bool = True) -> list[BookMeta]:
    """NDL OpenSearch の RSS から BookMeta のリストを作る。

    同じ本が複数の所蔵館から返ることがあるので ISBN（無ければ link）で重複を落とす。
    books_only=True のときは category が「図書」のものだけを残す（雑誌記事を除く）。
    """

    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError:
        return []

    out: list[BookMeta] = []
    seen: set[str] = set()
    for item in root.iter():
        if _local(item.tag) != "item":
            continue
        categories = _child_texts(item, "category")
        if books_only and not (set(categories) & BOOK_CATEGORIES):
            continue

        title = _child_text(item, "title")
        if not title:
            continue

        isbn = ""
        for ident in _child_texts(item, "identifier"):
            # NDL は ISBN-10 の値を ISBN13 型で返すことがあるので、型ではなく値を見る。
            normalized = normalize_isbn(ident)
            if normalized:
                isbn = normalized
                break

        meta = BookMeta(
            isbn=isbn,
            title=title,
            author=_join_authors(_child_texts(item, "creator")),
            publisher=_child_text(item, "publisher"),
            series=_child_text(item, "seriesTitle"),
            pubdate=_child_text(item, "issued") or _child_text(item, "date"),
            extent=_child_text(item, "extent"),
            source="NDLサーチ",
            link=_child_text(item, "link"),
        )
        key = meta.isbn or meta.link or meta.title
        if key in seen:
            continue
        seen.add(key)
        out.append(meta)
    return out


def _from_ndl(params: dict[str, str], timeout: int = DEFAULT_TIMEOUT) -> list[BookMeta]:
    url = f"{NDL_ENDPOINT}?{urllib.parse.urlencode(params)}"
    body = _http_get(url, timeout=timeout)
    if body is None:
        return []
    return parse_ndl_items(body)


# --------------------------------------------------------------------------
# 公開 API
# --------------------------------------------------------------------------

def _pick_exact_isbn(candidates: list[BookMeta], isbn13: str) -> BookMeta | None:
    """候補のうち ISBN が完全一致するものだけを採る。

    NDL の `isbn=` 検索は完全一致ではなく、まったく無関係な書誌が混じって返ってくる
    （実測: 存在しない ISBN で検索しても別の本が数件返る）。先頭を鵜呑みにすると
    別の本の書名が EPUB に入るため、ここで必ず突き合わせる。
    """

    for cand in candidates:
        if cand.isbn == isbn13:
            return cand
    return None


def lookup_isbn(isbn: str, timeout: int = DEFAULT_TIMEOUT) -> BookMeta | None:
    """ISBN から書誌を引く（openBD → NDL の順にフォールバック）。

    ISBN として不正な文字列、どちらの API にも無い場合は None を返す。
    """

    isbn13 = normalize_isbn(isbn)
    if not isbn13:
        print(f"  ISBN として解釈できません（チェックディジット不一致か桁数違い）: {isbn}", flush=True)
        return None

    meta = _from_openbd(isbn13, timeout=timeout)
    if meta:
        return meta

    candidates = _from_ndl({"isbn": isbn13, "cnt": "10"}, timeout=timeout)
    return _pick_exact_isbn(candidates, isbn13)


def search_title(query: str, limit: int = 10, timeout: int = DEFAULT_TIMEOUT) -> list[BookMeta]:
    """書名で候補を検索する（NDLサーチ）。

    同名異書が普通にあるため、どれを使うかの判断は呼び出し側（人間）に委ねる。
    """

    if not query.strip():
        return []
    return _from_ndl({"title": query, "cnt": str(limit)}, timeout=timeout)[:limit]


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def _print_candidates(candidates: list[BookMeta]) -> None:
    for i, m in enumerate(candidates, 1):
        print(f"[{i}] {m.title}")
        for line in m.summary_lines():
            if line.startswith("書名: "):
                continue
            print(f"    {line}")
        if m.link:
            print(f"    URL: {m.link}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="書誌 API（openBD / 国立国会図書館サーチ）から書籍情報を取得する"
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--isbn", help="ISBN（10 桁・13 桁、ハイフンの有無どちらでも可）")
    group.add_argument("--title", help="書名（部分一致。複数候補が出たら人が選ぶ）")
    parser.add_argument("--limit", type=int, default=10, help="書名検索で表示する候補数")
    parser.add_argument("--json", action="store_true", help="JSON で出力する")
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT, help="API のタイムアウト秒")
    args = parser.parse_args()

    if args.isbn:
        meta = lookup_isbn(args.isbn, timeout=args.timeout)
        if meta is None:
            print("[]" if args.json else f"ISBN {args.isbn} の書誌は見つかりませんでした。")
            return 1
        if args.json:
            print(json.dumps(asdict(meta), ensure_ascii=False, indent=2))
        else:
            print(f"取得元: {meta.source}")
            for line in meta.summary_lines():
                print(f"  {line}")
        return 0

    candidates = search_title(args.title, limit=args.limit, timeout=args.timeout)
    if not candidates:
        print("[]" if args.json else f"「{args.title}」に該当する書籍は見つかりませんでした。")
        return 1
    if args.json:
        print(json.dumps([asdict(m) for m in candidates], ensure_ascii=False, indent=2))
    else:
        print(f"「{args.title}」の候補 {len(candidates)} 件:")
        _print_candidates(candidates)
    return 0


if __name__ == "__main__":
    sys.exit(main())
