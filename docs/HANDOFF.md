# ハンドオフ資料: 縦書き／横書き対応

`--horizontal` オプションの整備と、縦書き固有機能の実装に関する作業記録。
別のセッションや担当者が引き継げるよう、**何が done で何が未着手か**を第一に書く。

作業ブランチ: `claude/ge-2hrrzc`

---

## 背景

このツールは既定で日本語縦書き EPUB3 を出力する。`--horizontal`（横書き）フラグは
初期実装から存在したが、縦横で切り替わるのは実質2箇所（CSS の `writing-mode` と
spine の `page-progression-direction`）だけで、**縦書き側にも実バグが残っていた**。

縦書き固有の組版機能（縦中横・ルビ・禁則など）はほぼ未実装だった。

---

## 進捗

### フェーズ1: `--horizontal` の基盤整備（完了）

| # | 項目 | 状態 | コミット |
|---|---|---|---|
| 1-1 | 見出しマージンの論理プロパティ化 | ✅ done | `0666fde` |
| 1-2 | 表・図版の `writing-mode` 上書きを縦書き時のみに | ✅ done | `0666fde` |
| 1-3 | `primary-writing-mode` メタの追加 | ✅ done | `0666fde` |
| 1-4 | `self_check()` に縦横整合の検証を追加 | ✅ done | `0666fde` |
| 1-5 | `tests/` 新設（リポジトリ初のテスト） | ✅ done | `0666fde` |
| 1-6 | `primary-writing-mode` を epubcheck が通る形式へ修正 | ✅ done | `2c7fb32` |
| 1-7 | 縦書きにも物理マージンのフォールバックを追加 | ✅ done | `2c7fb32` |
| 1-8 | `self_check()` の spine 欠落ガード＋テスト | ✅ done | `2c7fb32` |

**1-1 は横書き対応ではなく縦書きのバグ修正だった。** 見出しマージンが物理プロパティ
`margin: 1.2em 0` だったため、縦書きでは上下＝行内方向の余白になり、見出しの前後空きと
して機能していなかった。既定が縦書きなので影響は大きい。

**1-6 は 1-3 で持ち込んだ退行の修正。** 接頭辞なしの `property="primary-writing-mode"`
は EPUB3 の既定語彙にないため epubcheck が `OPF-027` で必ず弾き、README が最終工程に
記載している epubcheck が通らない状態になっていた。Kindle が実際に読むのも OPF2 由来の
`name`/`content` 形式なので、そちらに変更した。値も Kindle の語彙（進行方向込みの4値）に
合わせ、横書きは `horizontal-tb` ではなく `horizontal-lr`。

### フェーズ2: 縦書きタイポグラフィ（完了）

| # | 項目 | 状態 | 備考 |
|---|---|---|---|
| 2-1 | 縦中横 `text-combine-upright` | ✅ done | 最優先。下記参照 |
| 2-2 | ルビの `<rp>` フォールバック | ✅ done | ruby 非対応リーダー対策 |
| 2-3 | `ruby-position` の明示 | ✅ done | 初期値は `over` ではなく `alternate` |
| 2-4 | 欧文の `text-orientation` 制御 | ✅ 指定しない判断 | 初期値 `mixed` が日本語縦組みの正解 |
| 2-5 | 禁則・ぶら下げ | ✅ done | `line-break: strict` / `hanging-punctuation: allow-end` |
| 2-6 | nav の目次にルビ記法が生で出るバグ修正 | ✅ done | 下記参照 |

**書かない CSS を明示的に選んでいる。** `font-feature-settings` の `vert` は UA が
自動適用する義務があるので冗長、`vrt2` は W3C 仕様が "is not used by CSS" と排除して
おり UA の回転と二重にかかる恐れがある。`text-orientation` は初期値 `mixed` が正解、
`text-spacing` の `normal` は約物詰めを抑制する逆効果。`rp { display: none }` も
書かない（UA スタイルシートで既に非表示であり、自前で書くと「CSS は解釈するが
ルビ非対応」なリーダーで括弧まで消え `漢字かんじ` という最悪の表示になる）。

**2-6 は既存バグ。** `heading_title()` は `normalize_text()` を通すだけだが、
`normalize_text()` は `｜《》` を保護するためルビ記法が残る。`render_nav_list()` は
`escape()` するだけだったので、目次に `｜天体《てんたい》` が生のまま表示されていた。
nav.xhtml の `<a>` 内には `<ruby>` を置けるので `render_inline()` を通すよう変更した
（epubcheck 実測済み）。

### フェーズ3: リーダー互換性（完了）

| # | 項目 | 状態 | 備考 |
|---|---|---|---|
| 3-1 | `rendition:*` メタ＋`prefix` 宣言 | ❌ 実装しない | 全部 EPUB3 の既定値。下記参照 |
| 3-2 | NCX（toc.ncx）の生成 | ✅ done | EPUB2 互換リーダー向け。下記参照 |
| 3-3 | `self_check()` に NCX の検証を追加 | ✅ done | toc 属性と `dtb:uid` 一致を見る |
| 3-4 | `playOrder` / `id` の一意性修正 | ✅ done | epubcheck で検出。下記参照 |

**3-4 は epubcheck の実測で見つけた退行。** NCX には「**同じ target を指す navPoint は
同じ `playOrder` でなければならない**」という制約があり、文書順の通し番号を素朴に振ると
同一 href が複数階層に現れたとき `RSC-005`（different playOrder values ... refer to same
target）で落ちる。`split_chapters_and_nav()` は大見出しごとに別ファイル・中見出しには
`#id` 断片を振るので通常は衝突しないが、仕様に合わせて href ごとに採番を共有するよう修正した。

併せて `id` の採番位置も修正。子を先に描画してから `counter` を読むと親の id が子と衝突し
`RSC-005`（id does not have a unique value）になる。id は再帰の**前**に確定させる。

---

## 3-1 `rendition:*` を実装しない理由

epubcheck 5.1.0 で対照実験した結果、**書く実益がない**と判断した。

- `rendition:` は EPUB3 の**予約接頭辞**なので `prefix` 属性は不要。独自接頭辞
  （`foo:`）は未宣言だと `OPF-028` で落ちるが、`rendition:` は宣言なしで通る
- リフロー型で妥当な値は `rendition:layout=reflowable` / `rendition:spread=auto` /
  `rendition:orientation=auto` の3つだが、**いずれも EPUB3 の既定値そのもの**。
  明示してもリーダーの挙動は変わらない
- `pre-paginated` は**指定してはいけない**。全 XHTML に viewport meta が必要になり
  `ERROR(HTM-046)` が出る。OCR 由来のリフロー書籍とは非互換
- itemref の `rendition:page-spread-left` / `-right` は固定レイアウト専用で、
  リフローでは効果がない

縦書きの見開き方向を伝えるのは spine の `page-progression-direction="rtl"` と
`primary-writing-mode` メタ（フェーズ1で実装済み）で足りている。

---

## 3-2 NCX の実装メモ

EPUB3 の `nav.xhtml` は EPUB2 世代の日本語ビューアが読まないため、目次が出ない。
同じ `nav_tree` から `toc.ncx` を生成して併載する（`render_ncx()`）。
**EPUB3 に NCX を入れても epubcheck 5.1.0 は警告すら出さない**（実測）。ただし条件がある。

### 必須条件（外すと epubcheck が落ちる。実測済み）

| 条件 | 外したときのエラー |
|---|---|
| spine に `toc="ncx"` 属性 | `ERROR(RSC-005)` spine element toc attribute must be set… |
| `dtb:uid` が `dc:identifier` と完全一致 | `ERROR(NCX-001)` NCX identifier does not match OPF identifier |
| `<head>` に meta が最低1つ | `ERROR(RSC-005)` element "head" incomplete; missing required element "meta" |
| `<docTitle>` が `<navMap>` より前 | `ERROR(RSC-005)` element "navMap" not allowed yet; missing required element "docTitle" |

manifest は `media-type="application/x-dtbncx+xml"` で登録する。**NCX を spine に
itemref として並べる必要はなく、`properties` も不要。**
`dtb:depth` / `dtb:totalPageCount` / `dtb:maxPageNumber` と `playOrder` は任意
（`dtb:uid` だけでも通る）だが慣習的に書いている。

### 最重要: nav.xhtml と同じ文字列を流してはいけない

**`navLabel/text` はプレーンテキストのみで、マークアップを一切許さない。**

```
ERROR(RSC-005): element "ruby" not allowed anywhere; expected the element end-tag or text
```

フェーズ2 の 2-6 で `render_nav_list()` を `render_inline()` 経由に変えたため、
nav.xhtml の `<a>` の中には `<ruby>` と縦中横の `<span>` が入っている。NCX にはこれを
流せないので、テキスト化の関数を分けている。

| 出力先 | 関数 | 挙動 |
|---|---|---|
| nav.xhtml | `render_inline()` | `｜天体《てんたい》` → `<ruby>天体<rt>てんたい</rt></ruby>` |
| toc.ncx | `render_plain()` | `｜天体《てんたい》` → `天体`（ルビ部を捨て親文字だけ残す） |

`render_plain()` は `RUBY_PIPE_RE` / `RUBY_BARE_RE` を再利用する。`RUBY_PIPE_RE` は
`｜親《ルビ》` 全体にマッチするので親文字への置換で `｜` ごと消えるが、ルビ開始記号
として使われなかった裸の `｜` は残るため最後にまとめて除去している。

### NCX に縦書き固有の指定はない

NCX（DAISY 2005-1）の語彙に `page-progression-direction` 相当は**存在しない**。
読み方向の指定箇所は OPF の spine `page-progression-direction="rtl"` が唯一で、
NCX 側は何もしなくてよい。

---

## 2-1 縦中横がなぜ最優先か

`book_ir.py` の `normalize_text()` は NFKC 正規化をかけるため、**全角数字が半角に変換される**。

```
'第２０章'   → '第20章'
'１９８０年代' → '1980年代'
```

縦書きの既定 `text-orientation: mixed` では半角数字は 90 度横倒しでレンダリングされる。
つまり現在のパイプラインは、元が全角だった数字までわざわざ横倒しになる形に変換している。
章番号・年号・ページ番号は書籍に頻出するため、縦書き出力で最も目につく不具合。

対象は**半角数字ちょうど2桁**と `!!` `!?` `?!` `??` のみ。1桁は回転しないので不要、
3桁以上は 1 文字幅に潰れて判読不能になるため 4 桁の年号（`1980`）も含めて対象外。
仕様の `text-combine-upright: digits <n>` はどのブラウザも未実装なので使えず、
`<span class="tcy">` で囲む方式のみ。適用は `render_inline()` の最後（ルビ変換より
前にかけると `[漢字]+《》` の隣接が span で分断され `RUBY_BARE_RE` が壊れる）。
タグとルビ要素は `SKIP_RE` で退避してから置換する。これを怠ると
`<img src="../images/fig02.png"/>` が `fig<span class="tcy">02</span>.png` になり
画像パスが壊れる。

---

## 実装できないもの（データが存在しない）

**圏点・傍点・太字・斜体・割注**は出力層の問題ではない。IR のブロック種別は
`heading` / `para` / `raw` / `table` / `figure` のみで、**インラインの装飾情報を一切
持たない**。OCR（yomitoku）が圏点を検出しないため、データそのものが無い。

`to_aozora.py:89` にも既に明記されている:

> ※傍点・圏点は底本のスキャン画像から判別できないため再現していません。

対応するには OCR 層かページ画像の解析から手を入れる必要があり、規模が別物になる。

---

## 検証方法

### テスト

```
uv run --group dev pytest
```

`to_epub` は import 時に yomitoku/torch を必要としない（`pypdfium2` は関数内で遅延
import）ため、OCR 環境なしで純粋関数レベルのテストが回る。

### epubcheck

README の最終工程。**縦書き系の変更を入れたら必ず通すこと。**
`primary-writing-mode` の件（1-6）のように、整形式でも仕様違反で弾かれる変更がある。

```
epubcheck "書名.epub"
```

### self_check()

`build_epub()` が毎回自動実行する。zip 構造・XML 整形式・manifest/spine 突合に加え、
縦横整合（CSS の `writing-mode` / `page-progression-direction` / `primary-writing-mode`）
と NCX 整合（manifest 登録・spine の `toc` 属性・`dtb:uid` と `dc:identifier` の一致）
を検証する。NCX の2項目は epubcheck の `RSC-005` / `NCX-001` に対応しており、
epubcheck を回す前にローカルで同じ退行を捕まえられる。FAIL 時は `sys.exit(1)`。

---

## 既知の未対応事項

- `self_check()` は `style.css` を manifest ではなく固定名で探している。CSS 名を変えると
  「見つかりません」と偽陽性側に倒れるため実害は低いが、manifest から
  `media-type="text/css"` の href を引く方が堅牢。
- `--keep-printed-toc` が `to_epub.py` にはあるが `pdf_to_epub.py` に露出していない。
  オーケストレーター経由では印刷目次を残せない。
- `dc:language` と `xml:lang` が `ja` ハードコード。
- `to_aozora.py` の書名・出版社が特定書籍のハードコード。
