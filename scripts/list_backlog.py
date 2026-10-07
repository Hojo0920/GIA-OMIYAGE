#!/usr/bin/env python3
"""まだ商品を調べられていない「対象・要確認」の店を一覧にする（続きの調査の割り当て用）。

使い方: python3 scripts/list_backlog.py [--data data] [--scope yes,maybe]
"""
import argparse
import json
import pathlib

ap = argparse.ArgumentParser()
ap.add_argument("--data", default="data")
ap.add_argument("--scope", default="yes,maybe")
a = ap.parse_args()
d = pathlib.Path(a.data)
shops = json.loads((d / "shops.json").read_text(encoding="utf-8"))["shops"]
products = json.loads((d / "products.json").read_text(encoding="utf-8"))["products"]
have = {p["shop_key"] for p in products}
scopes = set(a.scope.split(","))
rows = [s for s in shops if s.get("in_scope") in scopes and s["key"] not in have]
print(f"未調査: {len(rows)}店（対象範囲: {a.scope}）\n")
for s in rows:
    print(f"- [{s.get('category')}] {s.get('short') or s.get('name')}  対象={s.get('in_scope')}  TEL={s.get('phone') or '-'}")
    print(f"    あんと店舗ページ: {s.get('anto_page_url') or '-'}  公式: {s.get('official_site') or '-'}  通販: {s.get('online_shop') or '-'}")
