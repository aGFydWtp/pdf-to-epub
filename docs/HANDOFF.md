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

### フェーズ2: 縦書きタイポグラフィ（着手中）

| # | 項目 | 状態 | 備考 |
|---|---|---|---|
| 2-1 | 縦中横 `text-combine-upright` | 🔜 未着手 | 最優先。下記参照 |
| 2-2 | ルビの `<rp>` フォールバック | 🔜 未着手 | ruby 非対応リーダー対策 |
| 2-3 | `ruby-position` の明示 | 🔜 未着手 | |
| 2-4 | 欧文の `text-orientation` 制御 | 🔜 未着手 | 既定に任せる判断もあり得る |
| 2-5 | 禁則・ぶら下げ | 🔜 未着手 | `line-break` / `hanging-punctuation` |

### フェーズ3: リーダー互換性（未着手）

| # | 項目 | 状態 | 備考 |
|---|---|---|---|
| 3-1 | `rendition:*` メタ＋`prefix` 宣言 | 🔜 未着手 | 縦書き固有ではない |
| 3-2 | NCX（toc.ncx）の生成 | 🔜 未着手 | EPUB2 互換リーダー向け |

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
を検証する。FAIL 時は `sys.exit(1)`。

---

## 既知の未対応事項

- `self_check()` は `style.css` を manifest ではなく固定名で探している。CSS 名を変えると
  「見つかりません」と偽陽性側に倒れるため実害は低いが、manifest から
  `media-type="text/css"` の href を引く方が堅牢。
- `--keep-printed-toc` が `to_epub.py` にはあるが `pdf_to_epub.py` に露出していない。
  オーケストレーター経由では印刷目次を残せない。
- `dc:language` と `xml:lang` が `ja` ハードコード。
- `to_aozora.py` の書名・出版社が特定書籍のハードコード。
