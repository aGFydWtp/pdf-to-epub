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
| 2-1 | 数字・英字の正立（全角化＋縦中横） | ✅ done | 最優先。下記参照 |
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

## 2-1 数字・英字の正立がなぜ最優先か

`book_ir.py` の `normalize_text()` は NFKC 正規化をかけるため、**全角数字・全角英字が
半角に変換される**。

```
'第２０章'   → '第20章'
'１９８０年代' → '1980年代'
'ＳＮＳ'     → 'SNS'
```

縦書きの既定 `text-orientation: mixed` では半角の数字・英字は 90 度横倒しで
レンダリングされる。つまり現在のパイプラインは、元が全角だった文字までわざわざ
横倒しになる形に変換している。章番号・年号・ページ番号は書籍に頻出するため、
縦書き出力で最も目につく不具合。

### 確定した規則（原書2冊の版面を実測）

| 対象 | 原書の組み方 | 実装 |
|---|---|---|
| 数字1桁 | 全角で正立 | 全角化 |
| 数字ちょうど2桁 | 縦中横（1字分に詰める） | `<span class="tcy">`（中身は半角） |
| 数字3桁以上 | 全角で1字ずつ正立 | 全角化 |
| 小数点 | 中黒。1字分を占めて中央に来る | `・` に変換 |
| 大文字だけの略語（2文字以上） | 桁数によらず全角で1字ずつ正立 | 全角化 |
| 小文字を含む欧文語 | 横倒しのまま（回転） | 変換しない |
| `!!` `!?` `?!` `??` | 縦中横 | `<span class="tcy">`（中身は半角） |

実測の根拠:

- 『イスラム教の論理』(新潮新書) p21「2011年」→ ２/０/１/１ が縦に4字
- 同 p20「コーラン第4章48節」→ `4` は全角1字、`48` は1字分に詰めた縦中横
- 同 p15「第2章216節」→ ２/１/６ が縦に3字
- 同 p113「2.2人」→ ２/・/２ で、中黒が1字分を占めて中央に来る
- 同 p11「SNS戦略」→ S/N/S が縦に3字。p2「EUからの離脱」→ E/U が縦に2字
  （**2文字の略語でも縦中横にはしない**。数字の2桁とは規則が違う）
- 同 p74「Telegram は」→ 横倒し（回転）のまま
- 『“町内会”は義務ですか?』(幻冬舎新書) も同じ規則。「27.8％」は
  `27`(縦中横)・`・`・`8`(全角)・`％`(全角)

「1桁は回転しないので不要」という当初の判断は**誤り**で、1桁が横倒しになっている
実機スクリーンショットで反証済み。3桁以上を縦中横にしないという判断は正しかったが、
正解は「対象外（半角のまま）」ではなく「全角化して1字ずつ正立」だった。
仕様の `text-combine-upright: digits <n>` はどのブラウザも未実装なので使えず、
縦中横は `<span class="tcy">` で囲む方式のみ。

### 実装上の制約

- **`normalize_text()` には触らない。** 触ると LLM 校正チャンクの本文が変わって
  キャッシュキーが全滅し、全章再校正＝再課金になる。変換は `to_epub.py` の
  レンダリング時にだけ行う
- **縦組みのときだけ変換する。** 縦中横は「横書きでは CSS を出さない」ことで
  無効化できたが、全角化は文字そのものを変えるので同じ手が使えない。縦組みフラグは
  `build_epub(horizontal=...)` を起点に `render_inline()` まで引数で貫通させる
  （`render_chapter_xhtml` → `render_block` → `render_heading`、および
  `render_nav_xhtml` → `render_nav_list`）。モジュールレベルのグローバル状態は使わない
- **表セルは組方向によらず変換しない。** 表は縦組みの本でも `build_css()` が
  `writing-mode: horizontal-tb` に戻すので、中身は常に横組みで表示される。
  中黒化は縦組み専用の約物で、横組みのセルに出ると数値の意味が壊れる
  （縦中横も同じ理由で `table.h-table .tcy` が無効化している）
- **トークンは1本の正規表現でまとめて取る。** 小数点だけを独立した正規表現
  （`(?<=[0-9])\.(?=[0-9])`）で処理すると `Web2.0` の点まで中黒になる。数字トークンを
  取ってから `.` で split し、桁数規則を適用して `・` で join する。前後の lookaround に
  `.` を含めるのは、英数字が混在するトークンを部分的に変換しないため
  （含めないと `Web2.0` → `Web2.０`、`1.5GB` → `１.5GB` と途中だけ全角になる）
- **単独の大文字1文字は変換しない。** 実データを調べたところ、単独大文字は OCR が語を
  割ったノイズ（`L GBT`、`UA E`、`PD F`）しか無かった。`[A-Z]{2,}` で確定してよい
- 適用は `render_inline()` の最後（ルビ変換より前にかけると `[漢字]+《》` の隣接が
  span や全角字で分断され `RUBY_BARE_RE` が壊れる）。タグとルビ要素は `SKIP_RE` で
  退避してから置換する。これを怠ると `<img src="../images/fig02.png"/>` が
  `fig<span class="tcy">02</span>.png` になり画像パスが壊れる
- NCX 用の `render_plain()` はタグを出せないので対象外。目次 UI は縦書きとは限らないため
  全角化も入れない

**感嘆符は全角も対象に含める必要がある。** `book_ir` の `PUNCT_MAP` が `!` `?` を
全角化するため、`render_inline()` に届く時点では `！？` になっている。半角だけを
対象にしていた当初の実装は、実パイプラインで一度も発動しないデッドコードだった
（レビューで発覚）。`span` の中身は半角に戻す（全角2字を1文字幅に組むと潰れる）。
**テストは必ず `normalize_text()` を通した文字列で書くこと。** 半角入力だけを
検査するテストは、製品で通らない経路を見ているだけで回帰を検出できない。

### ルビ記法を落とす箇所

`heading_title()` は `normalize_text()` を通すだけなので `｜《》` が残る。
マークアップを置けない場所ではこれを落とす必要がある。用途で2つ使い分ける:

- `render_plain()` — エスケープ**込み**。NCX の `navLabel/text` 用
- `strip_ruby()` — エスケープ**なし**。`<title>` や `alt` のように埋め込み側が
  自前でエスケープする箇所用（`render_plain()` を渡すと二重エスケープになる）

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
