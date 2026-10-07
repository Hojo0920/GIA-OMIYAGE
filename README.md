# 金沢百番街あんと お土産候補リスト（菓子・食品）

海外の訪問先へ渡す手土産を、金沢駅直結の「金沢百番街 あんと」で買うための**候補リスト**を作るツール一式です。
商品ごとに、入数別の税込価格・個包装・日持ち・原材料（豚由来の可能性がある成分とアルコール）のチェックをまとめ、上長に見せるスプレッドシート（xlsx）を作ります。

## 現状（2026-10-07 時点）

<!-- status:start -->
| 項目 | 件数 |
|---|---|
| 洗い出せた店舗（あんと・あんと西・Rinto） | 157店 |
| うち菓子・食品の土産を扱う店（対象） | 39店（Rintoの店はあんと外のため対象外に分類） |
| 商品・入数別の価格まで調べられた店 | 34店（201商品・439入数） |
| 未調査の対象店 | 5店（`python3 scripts/list_backlog.py --scope yes` で一覧） |
| 確認レベルA（公式ページを直接確認）の商品 | 12商品 |
<!-- status:end -->

- **確認レベルA**: 前任のセッションが各店の公式の商品ページを直接開いて確認した12商品（写真つき）。
- **確認レベルB/C**: 公式ドメインに絞った検索（WebSearch）の結果。検索結果は要約で、商品ページ本文は見ていないため、買う前に店舗か公式ページで確認が必要です。
- 実行環境の通信制限（egress ポリシー）で、金沢百番街や各店の公式サイトには直接アクセスできませんでした。また検索には回数の上限（1ターン200回、調査担当すべてで共有）があり、調べきれなかった店は「除外・要確認」シートの2に残しています。

## このリポジトリに入れていないもの（公開リポジトリのため）

- 商品写真（各社の著作物）と、完成版の写真入りスプレッドシート
- 渡し先の割り当て（社内の計画）と、おすすめ案
- これらは `private/` と `deliverables/` に置き、`.gitignore` で除外しています。`scripts/build_xlsx.py` は `private/images/` と `private/picks.json` があればそれを使い、なければ写真・おすすめ案なしの版を作ります。

## 構成

```
data/raw/            調査結果（G0=前任が確認したデータ、G1〜G6=商品調査、G7=店舗の洗い出し・人気商品）
data/overrides.json  食い違いの手動解消（理由つき）
data/products.json   統合後の商品 × 入数ごとのデータ（merge_research.py の出力）
data/shops.json      店舗一覧と人気商品
data/excluded.json   外した商品と、確認できていない項目
docs/research_instructions.md   調査担当に渡した指示書（続きの調査にそのまま使える）
scripts/merge_research.py       data/raw → data/*.json（店名の表記ゆれ・重複の統合、原材料チェック）
scripts/build_xlsx.py           data/*.json → xlsx（数式・条件付き書式つき）
scripts/verify_xlsx.py          LibreOfficeで数式を再計算し、キャッシュ値と全数比較
scripts/list_backlog.py         まだ商品を調べていない店の一覧
scripts/gap_report.py           店ごとに「分かっていること」と「未確認の項目」をまとめる（調査の割り当て用）
scripts/fetch_photos.py         商品ページから商品写真を取得して 240px 四方のJPEGにする（private/images/<商品ID>.jpg）
tests/test_fetch_photos.py      fetch_photos.py の試験（手元のHTTPサーバの疑似ページ。外部通信なし）
docs/photo_hosts.txt            写真の取得で通信が必要なドメイン（実行環境の Network access を Custom にするとき用）
```

## 使い方

```bash
pip install -r requirements.txt
python3 scripts/merge_research.py
python3 scripts/build_xlsx.py --public --out /tmp/anto_omiyage_public.xlsx   # 写真・おすすめ案なし
python3 scripts/verify_xlsx.py /tmp/anto_omiyage_public.xlsx                  # 要 LibreOffice
```

xlsx の判定（○△×）は数式で、「はじめに」シートの黄色いセル（予算・購入日・渡す日など）を変えると更新されます。

## 商品写真の取得

商品写真は各店の商品ページから `scripts/fetch_photos.py` で取得します。店のサイトへ通信できる環境が必要です（Claude Code の cloud 環境では、Network access を Full にするか、Custom にして `docs/photo_hosts.txt` のドメインを入れます。写真の配信用に別のドメインが必要な店もあり、拒否されたホストは実行結果の最後に一覧されます）。

```bash
pip install -r requirements.txt
python3 scripts/fetch_photos.py --only-picks   # private/picks.json のおすすめ案・差し替え候補だけ先に
python3 scripts/fetch_photos.py                # 写真のない商品すべて
python3 scripts/fetch_photos.py --sheet        # 取得した写真を商品名つきの一覧画像にして、商品と写真が合っているか目で確かめる
python3 scripts/fetch_photos.py --status       # 写真の有無の一覧
python3 scripts/build_xlsx.py --out deliverables/anto_omiyage_candidates.xlsx
```

- 写真は `private/images/<商品ID>.jpg`（240px四方・白背景）に保存し、`build_xlsx.py` が読みます。`image_key` を持つ商品（前任が公式ページで確認した12商品）の写真は取り直しません。
- 商品ページの JSON-LD → og:image → twitter:image → 本文中の画像の順に探します。ロゴ・バナーらしい画像と、題名が商品名と合わないページの og:image は使いません。複数の商品が同じページを指す店（一覧ページ）は、画像の alt や周辺の文字と商品名を照合します。決め手がなければ写真は付けません（誤った写真より空欄のほうがよいため）。
- 結果は `private/photo_log.json`（元ページ・画像URL・方法・要確認かどうか）に残ります。確からしさが低い方法（本文中の画像・一覧ページの照合）は「要確認」になります。
- うまく取れない商品は `private/photo_overrides.json` で手動指定できます: `{"P017": {"image_url": "https://..."}, "P038": {"page_url": "https://..."}, "P086": {"skip": true}}`。
- 同じ写真が別のページの商品に使われていたら店の共通画像の疑いがあるため、3商品以上なら自動で不採用にします。
- 同じホストへは1秒以上あけ、`robots.txt` で禁止されたページは取得しません。写真は各社の著作物なので、社内確認用に限り、公開リポジトリには入れません（`private/` は `.gitignore` で除外）。

## 判定のルール

- **予算**: 税込価格が予算上限（既定2,000円）以下で「○」、上限＋300円以下で「△」、超えると「×」。
- **日持ち**: 渡す日の残り日数＝日持ち日数－（渡す日－購入日）－製造から購入までの見込み日数。7日以上で「○」、3日以上で「△」。
- **原材料（豚・酒）**: ゼラチン・乳化剤・ショートニング・ラード・マーガリン、酒精・洋酒・みりん・醤油など酒類・アルコールが原材料表示にあれば「×」。表示が確認できなければ「？」。ハラール認証品ではありません（観光庁「ベジタリアン・ヴィーガン／ムスリム旅行者おもてなしガイド」2024年4月に合わせた基準）。
- **個包装**: 1個ずつ「○」、小分けパック「△」、なし「×」、不明「？」。
- **総合**: 予算・日持ち・原材料のどれかが「×」なら「×」。4項目すべて「○」で「◎」。「△」「？」が混じれば「△」、それ以外は「○」。

## 続きの調査の進め方

1. `python3 scripts/list_backlog.py` で未調査の店を確認する。
2. `docs/research_instructions.md`（基本ルール）と `docs/research_instructions_round2.md`（追加ルール）を読ませて、店を数店ずつのグループに分けて調査担当に割り当てる（出力は `data/raw/Gn_*.json`）。担当ごとに `python3 scripts/gap_report.py --shops "店名,店名"` で既知データと未確認項目の資料を作って渡すと、穴を狙って調べられる。上位候補の裏取りだけを行うグループは、`scripts/merge_research.py` の `VERIFY_GROUPS` に番号を足すと、前のグループの価格・原材料・個包装より優先される。
3. `python3 scripts/merge_research.py` で統合し、`python3 scripts/build_xlsx.py` で作り直す。
4. 検索の回数上限に達したら、次のターンで続ける。店舗サイトへ直接アクセスできる環境なら、公式の商品ページ本文を読んで確認レベルAに上げられる。
5. 食い違いは `data/overrides.json` に理由つきで残す。
