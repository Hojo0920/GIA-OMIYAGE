#!/usr/bin/env python3
"""店ごとに「いま分かっていること」と「まだ分からないこと」をまとめる（続きの調査の割り当て用）。

使い方: python3 scripts/gap_report.py --shops "森八,村上" [--data data]
"""
import argparse
import json
import pathlib

ap = argparse.ArgumentParser()
ap.add_argument("--data", default="data")
ap.add_argument("--shops", required=True, help="店名（通称）をカンマ区切りで。部分一致")
a = ap.parse_args()
d = pathlib.Path(a.data)
shops = json.loads((d / "shops.json").read_text(encoding="utf-8"))["shops"]
products = json.loads((d / "products.json").read_text(encoding="utf-8"))["products"]
WRAP = {"individual": "個包装あり", "pack": "小分けパック", "none": "個包装なし", None: "不明"}
for want in [w.strip() for w in a.shops.split(",") if w.strip()]:
    hit = [s for s in shops if want in (s.get("short") or "") or want in (s.get("name") or "")]
    if not hit:
        print(f"## {want}: 店舗一覧に見つかりません\n")
        continue
    s = hit[0]
    print(f"## {s.get('name') or s.get('short')}（通称: {s.get('short')}／{s.get('category')}／{ {'yes': '対象', 'maybe': '要確認', 'no': '対象外'}.get(s.get('in_scope')) }）")
    print(f"- あんと店舗ページ: {s.get('anto_page_url') or '未取得'} ／ あんと店TEL: {s.get('phone') or '未取得'}")
    print(f"- 公式サイト: {s.get('official_site') or '未特定'} ／ 通販サイト: {s.get('online_shop') or '未特定'}")
    reason = (s.get("reason") or "").replace("\n", " ")
    if reason:
        print(f"- 前回のメモ: {reason[:300]}")
    mine = [p for p in products if p["shop_key"] == s["key"]]
    if not mine:
        print("- 既知の商品: なし（商品・入数別価格は未調査）")
    for p in mine:
        opts = " / ".join(f"{o['label']}={o['price']:,}円({o['confidence']})" for o in p["options"])
        gaps = []
        if p["ingredient_check"] == "？":
            gaps.append("原材料表示")
        if p["wrap_level"] is None:
            gaps.append("個包装の有無")
        if p["shelf_life_days"] is None:
            gaps.append("日持ち")
        if not p.get("page_url"):
            gaps.append("商品ページURL")
        print(f"- 既知: {p['name']}｜{opts}｜{WRAP[p['wrap_level']]}｜日持ち{p['shelf_life_days'] or '不明'}日｜原材料{p['ingredient_check']}"
              + (f"｜未確認: {'・'.join(gaps)}" if gaps else ""))
    print()
