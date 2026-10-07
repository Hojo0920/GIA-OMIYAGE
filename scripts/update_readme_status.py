#!/usr/bin/env python3
"""README.md の「現状」の表を、data/*.json の集計で更新する（<!-- status:start --> と <!-- status:end --> の間）。"""
import json
import pathlib
import re

d = pathlib.Path("data")
shops = json.loads((d / "shops.json").read_text(encoding="utf-8"))["shops"]
products = json.loads((d / "products.json").read_text(encoding="utf-8"))["products"]
have = {p["shop_key"] for p in products}
yes = [s for s in shops if s.get("in_scope") == "yes"]
pending = [s for s in yes if s["key"] not in have]
n_opt = sum(len(p["options"]) for p in products)
n_a = sum(1 for p in products if p["confidence_max"] == "A")
table = f"""<!-- status:start -->
| 項目 | 件数 |
|---|---|
| 洗い出せた店舗（あんと・あんと西・Rinto） | {len(shops)}店 |
| うち菓子・食品の土産を扱う店（対象） | {len(yes)}店（Rintoの店はあんと外のため対象外に分類） |
| 商品・入数別の価格まで調べられた店 | {len(have)}店（{len(products)}商品・{n_opt}入数） |
| 未調査の対象店 | {len(pending)}店（`python3 scripts/list_backlog.py --scope yes` で一覧） |
| 確認レベルA（公式ページを直接確認）の商品 | {n_a}商品 |
<!-- status:end -->"""
p = pathlib.Path("README.md")
s = p.read_text(encoding="utf-8")
s2 = re.sub(r"<!-- status:start -->.*?<!-- status:end -->", table, s, flags=re.S)
p.write_text(s2, encoding="utf-8")
print(f"README updated: shops={len(shops)} target={len(yes)} researched={len(have)} products={len(products)} options={n_opt} pending={len(pending)} A={n_a}")
