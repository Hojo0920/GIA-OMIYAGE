#!/usr/bin/env python3
"""data/*.json から、上長確認用のスプレッドシート（xlsx）を作る。

使い方:
  # 写真・おすすめ案つきの完成版（private/ の写真と private/picks.json があれば使う。公開しないこと）
  python3 scripts/build_xlsx.py --out deliverables/anto_omiyage_candidates.xlsx

  # 写真・渡し先なしの公開版
  python3 scripts/build_xlsx.py --public --out /tmp/anto_omiyage_public.xlsx

判定（○△×）は数式で、「はじめに」シートの前提（予算・購入日など）を変えると更新される。
数式のキャッシュ値もPythonで計算して書くので、プレビューなど数式を計算しない画面でも同じ値が見える。
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import pathlib
import unicodedata

import xlsxwriter

# ---------------------------------------------------------------- 設定 -----------------------------------------
OK, WARN, NG, UNK, TOP = "○", "△", "×", "？", "◎"
FONT = "Yu Gothic"
C = dict(green="#1F5C45", chip="#E8EFE9", line="#D3D8DC", warn="#8A4B0F", ink="#1D2421", sub="#59625D",
         bg="#F6F5F1", input="#FFF8DC", link="#0B5CAD")
MARK_FILL = {TOP: "#BFE3CB", OK: "#E4F2E9", WARN: "#FFF0C2", NG: "#F8D3CF", UNK: "#E9E9E9"}
MARK_FONT = {TOP: "#0F5132", OK: "#1F5C45", WARN: "#8A5A00", NG: "#9B1C13", UNK: "#59625D"}

CATEGORY_ORDER = ["和菓子", "洋菓子", "食品・海産物", "酒", "茶", "惣菜・弁当・寿司", "工芸・雑貨", "その他"]
PARAMS = dict(budget=2000, tol=300, buy=dt.date(2026, 10, 7), give=dt.date(2026, 10, 13), lag=1, ok_days=7, warn_days=3)

WRAP_MARK = {"individual": OK, "pack": WARN, "none": NG, None: UNK}
WRAP_TEXT = {"individual": "1個ずつ個包装", "pack": "小分けパック（1個ずつではない）", "none": "個包装なし", None: "不明（未確認）"}
IMG_PX = 100          # 候補一覧の写真の表示サイズ（px）
IMG_SRC_PX = 240      # 写真ファイルの一辺（px）


# ---------------------------------------------------------------- 判定（数式と同じ論理をPythonでも持つ） ----------
def mark_budget(price: int) -> str:
    return OK if price <= PARAMS["budget"] else (WARN if price <= PARAMS["budget"] + PARAMS["tol"] else NG)


def remaining_days(days):
    if days is None:
        return None
    return days - (PARAMS["give"] - PARAMS["buy"]).days - PARAMS["lag"]


def mark_shelf(rem) -> str:
    if rem is None:
        return UNK
    return OK if rem >= PARAMS["ok_days"] else (WARN if rem >= PARAMS["warn_days"] else NG)


def mark_overall(j: str, n: str, o: str, k: str) -> str:
    if NG in (j, n, o):
        return NG
    if j == OK and n == OK and o == OK and k == OK:
        return TOP
    if WARN in (j, n) or UNK in (n, o, k):
        return WARN
    return OK


def round_half_up(x: float) -> int:
    return int(math.floor(x + 0.5))


# ---------------------------------------------------------------- 表示まわり ---------------------------------
def disp_width(s: str) -> int:
    return sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in s)


def n_lines(text, col_width: float) -> int:
    if text is None or text == "":
        return 1
    total = 0
    for para in str(text).split("\n"):
        total += max(1, math.ceil(disp_width(para) / max(1.0, col_width - 1.5)))
    return total


def short(text: str | None, n: int = 140) -> str:
    text = (text or "").strip()
    return text if len(text) <= n else text[: n - 1] + "…"


def yen(n) -> str:
    return f"{int(n):,}円"


def conf_text(p) -> str:
    lo, hi = p["confidence_min"], p["confidence_max"]
    return lo if lo == hi else f"{hi}（一部{lo}）"


class Book:
    def __init__(self, path: str):
        self.wb = xlsxwriter.Workbook(path)
        self._f: dict = {}

    def fmt(self, **kw):
        base = dict(font_name=FONT, font_size=10, valign="vcenter", font_color=C["ink"])
        base.update(kw)
        key = tuple(sorted(base.items()))
        if key not in self._f:
            self._f[key] = self.wb.add_format(base)
        return self._f[key]


# ---------------------------------------------------------------- メイン --------------------------------------
def build(out: str, data_dir: pathlib.Path, public: bool, private_dir: pathlib.Path) -> None:
    products = json.loads((data_dir / "products.json").read_text(encoding="utf-8"))["products"]
    shops_d = json.loads((data_dir / "shops.json").read_text(encoding="utf-8"))
    shops, popular = shops_d["shops"], shops_d.get("popular", [])
    ex_d = json.loads((data_dir / "excluded.json").read_text(encoding="utf-8"))
    excluded, unresolved = ex_d["excluded"], ex_d["unresolved"]

    img_dir = private_dir / "images"
    picks_path = private_dir / "picks.json"
    use_photos = (not public) and img_dir.exists()
    picks = json.loads(picks_path.read_text(encoding="utf-8")) if (not public and picks_path.exists()) else None
    aud = (picks or {}).get("audience", "ムスリムの方が多い渡し先")
    subtitle = (picks or {}).get("subtitle", "海外の訪問先へ渡す手土産（金沢で購入）の候補")

    def photo_of(p):
        # 前任が公式ページで確認した写真（image_key）→ scripts/fetch_photos.py が取得した写真（商品ID）の順に探す
        if not use_photos:
            return None
        for key in (p.get("image_key"), p["id"]):
            if key and (img_dir / f"{key}.jpg").exists():
                return img_dir / f"{key}.jpg"
        return None

    photo_log = {}
    if use_photos and (private_dir / "photo_log.json").exists():
        photo_log = json.loads((private_dir / "photo_log.json").read_text(encoding="utf-8")).get("items", {})

    def no_photo_label(p):
        """写真がない商品のセルに出す文言。scripts/fetch_photos.py の取得記録（photo_log.json）の理由から選ぶ。"""
        reason = (photo_log.get(p["id"]) or {}).get("reason", "")
        if any(k in reason for k in ("削除", "404", "見当たらない", "一覧にない")):
            why = "商品ページが見つからない"
        elif any(k in reason for k in ("拒否", "403")):
            why = "サイトが自動取得を拒否"
        elif "接続" in reason or "応答なし" in reason:
            why = "サイトに接続できない"
        else:
            why = "商品ページ参照"
        return f"写真なし\n（{why}）"

    # 並べ替え（既定の前提での総合評価が高い順 → 確認レベルが高い順 → 店の並び順）
    def best_overall(p):
        ranks = {TOP: 0, OK: 1, WARN: 2, NG: 3}
        vals = []
        for o in p["options"]:
            rem = remaining_days(p["shelf_life_days"])
            vals.append(mark_overall(mark_budget(o["price"]), mark_shelf(rem), p["ingredient_check"], WRAP_MARK.get(p["wrap_level"], UNK)))
        return min(ranks[v] for v in vals)

    products = sorted(products, key=lambda p: (best_overall(p), -{"A": 3, "B": 2, "C": 1}[p["confidence_max"]], p["id"]))

    n_by_shop = {}
    for p in products:
        n_by_shop[p["shop_key"]] = n_by_shop.get(p["shop_key"], 0) + 1
    shops = sorted(shops, key=lambda s: ({"yes": 0, "maybe": 1, "no": 2}.get(s.get("in_scope"), 1), 0 if n_by_shop.get(s["key"]) else 1,
                                         CATEGORY_ORDER.index(s["category"]) if s.get("category") in CATEGORY_ORDER else 99,
                                         s.get("short") or ""))

    def resolve(m):
        def nz(s):
            return unicodedata.normalize("NFKC", s or "").replace(" ", "").replace("　", "").lower()
        for p in products:
            if nz(m["shop"]) in nz(p["shop"]) or nz(p["shop"]) in nz(m["shop"]):
                if nz(m["product"]) in nz(p["name"]):
                    for o in p["options"]:
                        if not m.get("label") or nz(m["label"]) in nz(o["label"]):
                            return p, o
        return None, None

    def overall_of(p, o):
        return mark_overall(mark_budget(o["price"]), mark_shelf(remaining_days(p["shelf_life_days"])), p["ingredient_check"],
                            WRAP_MARK.get(p["wrap_level"], UNK))

    def key_points() -> list[str]:
        pts = []
        if picks:
            parts = []
            for pk in picks["picks"]:
                p, o = resolve(pk["match"])
                if p:
                    parts.append(f"{p['shop']}「{p['name'].split(' ')[0] if len(p['name']) > 18 else p['name']}」が{overall_of(p, o)}")
            if parts:
                pts.append("おすすめ案の4点は、" + "、".join(parts) + "です（△は日持ちや未確認項目があるもの）。")
        for p in products:
            if "金城巻" in p["name"]:
                o6 = next((o for o in p["options"] if o["label"].startswith("6個入")), p["options"][0])
                ng = "・".join(p["ingredient_hits"])
                if p["ingredient_check"] == NG:
                    pts.append(f"ご提案の金城巻（{p['shop']}）は、{o6['label'][:3]}が{o6['price']:,}円で予算内ですが、原材料に「{ng}」を含むため、"
                               f"{aud}には不向きです（日本人向けなら可）。日持ちは約{p['shelf_life_days']}日です。")
                else:
                    pts.append(f"ご提案の金城巻（{p['shop']}）は、{o6['label'][:3]}が{o6['price']:,}円です。")
                break
        tops = [(p, o) for p in products for o in p["options"] if overall_of(p, o) == TOP]
        if tops:
            top_products = list(dict.fromkeys(p["id"] for p, o in tops))
            top_shops = list(dict.fromkeys(p["shop"] for p, o in tops))
            n_a = len({p["id"] for p, o in tops if o.get("confidence") == "A"})
            pts.append(f"予算・日持ち・原材料・個包装をすべて満たす「◎」は{len(top_products)}商品（{len(tops)}件）です。確認レベルAは{n_a}商品で、残りは検索結果に基づく"
                       f"確認レベルBです。◎のある店: {'・'.join(top_shops)}。個包装や原材料表示が未確認の商品は「△」止まりなので、買う前に確認してください。")
        n_yes = sum(1 for s in shops if s.get("in_scope") == "yes")
        n_pend = sum(1 for s in shops if s.get("in_scope") == "yes" and not n_by_shop.get(s["key"]))
        pts.append(f"菓子・食品の土産を扱う店（対象）{n_yes}店のうち、商品と入数別の価格まで調べられたのは{n_yes - n_pend}店です。"
                   f"残り{n_pend}店は検索回数の上限で未調査のため、「除外・要確認」の2に一覧にしています。")
        if use_photos:
            n_photo = sum(1 for p in products if photo_of(p))
            pts.append(f"写真は{len(products)}商品のうち{n_photo}商品に入っています。写真のない商品は、「候補一覧」の「開く」から店の商品ページで確認できます。")
        return pts

    B = Book(out)
    wb = B.wb
    wb.set_properties({"title": "金沢百番街あんと お土産候補リスト", "subject": "菓子・食品の土産候補（入数別価格）",
                       "author": "Claude Code", "comments": "価格は税込。確認レベルはシート「はじめに」を参照"})
    wb.set_calc_mode("auto")

    s_intro = wb.add_worksheet("はじめに")
    s_pick = wb.add_worksheet("おすすめ案") if picks else None
    s_cat = wb.add_worksheet("候補一覧")
    s_flat = wb.add_worksheet("入数別価格")
    s_shop = wb.add_worksheet("店舗一覧")
    s_ex = wb.add_worksheet("除外・要確認")
    s_src = wb.add_worksheet("出典・調査メモ")
    for s in (s_intro, s_pick, s_cat, s_flat, s_shop, s_ex, s_src):
        if s:
            s.hide_gridlines(2)
            s.set_default_row(18)

    f = B.fmt
    f_title = f(bold=True, font_size=18, font_color=C["green"])
    f_sub = f(font_size=10, font_color=C["sub"], text_wrap=True, valign="top")
    f_h2 = f(bold=True, font_size=12, font_color=C["green"], bottom=2, bottom_color=C["green"])
    f_hdr = f(bold=True, font_color="#FFFFFF", bg_color=C["green"], text_wrap=True, align="center", border=1, border_color=C["line"])
    f_txt = f(text_wrap=True, valign="top", border=1, border_color=C["line"])
    f_txtc = f(text_wrap=True, align="center", border=1, border_color=C["line"])
    f_num = f(num_format="#,##0", align="right", border=1, border_color=C["line"])
    f_int = f(num_format="0", align="center", border=1, border_color=C["line"])
    f_mark = f(bold=True, align="center", border=1, border_color=C["line"], font_size=12)
    f_link = f(font_color=C["link"], underline=1, align="center", border=1, border_color=C["line"])
    f_note = f(font_color=C["warn"], text_wrap=True, valign="top")
    f_body = f(text_wrap=True, valign="top")
    f_bold = f(bold=True, text_wrap=True, valign="top")
    f_in_num = f(bg_color=C["input"], font_color="#1F3A93", num_format="#,##0", align="center", border=1, border_color=C["line"], bold=True)
    f_in_date = f(bg_color=C["input"], font_color="#1F3A93", num_format="yyyy/m/d", align="center", border=1, border_color=C["line"], bold=True)
    f_lbl = f(bold=True, bg_color=C["chip"], border=1, border_color=C["line"], text_wrap=True)
    f_cell = f(border=1, border_color=C["line"], text_wrap=True, valign="top")

    # 判定の条件付き書式
    def add_mark_cf(ws, rng):
        for mk in (TOP, OK, WARN, NG, UNK):
            ws.conditional_format(rng, {"type": "cell", "criteria": "==", "value": f'"{mk}"',
                                        "format": wb.add_format({"bg_color": MARK_FILL[mk], "font_color": MARK_FONT[mk], "bold": True})})

    # ================================================================ はじめに ==================================
    ws = s_intro
    ws.set_column("A:A", 2)
    ws.set_column("B:B", 46)
    ws.set_column("C:C", 18)
    ws.set_column("D:D", 84)
    ws.merge_range("B1:D1", "金沢百番街あんと お土産候補リスト（菓子・食品）", f_title)
    ws.set_row(0, 30)
    ws.merge_range("B2:D2", f"{subtitle}｜2026年10月7日作成（案）｜価格は税込、各店の公式表示（または公式ドメインの検索結果）ベース", f_sub)
    ws.set_row(1, 30)

    r = 3
    ws.merge_range(r, 1, r, 3, "要点（既定の前提での結果）", f_h2)
    r += 1
    for pt in key_points():
        ws.merge_range(r, 1, r, 3, "・" + pt, f_body)
        ws.set_row(r, max(20, 14 * n_lines(pt, 150) + 6))
        r += 1
    r += 1
    ws.merge_range(r, 1, r, 3, "シートの案内", f_h2)
    r += 1
    guide = [
        ("候補一覧", "写真と入数別の価格を見比べるシート（1商品1行）。並べ替えると写真がずれるため、絞り込み（フィルタ）だけ使ってください。"),
        ("入数別価格", "入数ごとに1行。並べ替え・絞り込み・計算はこのシートで（写真なし）。判定は数式です。"),
        ("店舗一覧", "あんとの店舗と、対象にした／しなかった理由。"),
        ("除外・要確認", "外した商品とその理由、まだ確認できていない項目。"),
        ("出典・調査メモ", "情報源と、調べ方の限界。"),
    ]
    if picks:
        guide.insert(0, ("おすすめ案", "上長に見てもらう4点の案と、差し替え候補。"))
    for name, desc in guide:
        ws.write_url(r, 1, f"internal:'{name}'!A1", f(font_color=C["link"], underline=1, bold=True), string=name)
        ws.merge_range(r, 2, r, 3, desc, f_body)
        ws.set_row(r, max(18, 14 * n_lines(desc, 96) + 4))
        r += 1

    r += 1
    ws.merge_range(r, 1, r, 3, "前提（黄色いセルを変えると、判定が更新されます）", f_h2)
    r += 1
    param_rows = [
        ("予算上限（税込・1点あたり）", PARAMS["budget"], "Budget", f_in_num, "これ以下を「○」とする。"),
        ("超過の許容幅（円）", PARAMS["tol"], "Tolerance", f_in_num, "上限＋この額までを「△」、超えると「×」。"),
        ("購入日", PARAMS["buy"], "BuyDate", f_in_date, "金沢駅で買う日。"),
        ("渡す日（最も遅い日）", PARAMS["give"], "GiveDate", f_in_date, "現地での最後の手渡し日。"),
        ("製造日から購入までの見込み日数", PARAMS["lag"], "MfgLag", f_in_num, "賞味期限は製造日から数えるため、購入日までに減る分を見込む。"),
        ("「○」にする残り日数", PARAMS["ok_days"], "OkDays", f_in_num, "渡す日にこの日数以上残っていれば日持ち「○」。"),
        ("「△」にする残り日数", PARAMS["warn_days"], "WarnDays", f_in_num, "この日数以上で「△」、未満は「×」。"),
    ]
    for label, val, nm, ff, desc in param_rows:
        ws.write(r, 1, label, f_lbl)
        if isinstance(val, dt.date):
            ws.write_datetime(r, 2, dt.datetime(val.year, val.month, val.day), ff)
        else:
            ws.write_number(r, 2, val, ff)
        ws.write(r, 3, desc, f_cell)
        wb.define_name(nm, f"='はじめに'!$C${r + 1}")
        r += 1

    r += 1
    ws.merge_range(r, 1, r, 3, "判定のルール", f_h2)
    r += 1
    rules = [
        ("予算", "税込価格が予算上限以下なら「○」、上限＋許容幅以下なら「△」、超えると「×」。"),
        ("日持ち", "渡す日の残り日数（＝日持ち日数－（渡す日－購入日）－見込み日数）が、○の基準以上で「○」、△の基準以上で「△」、未満は「×」。日持ちが分からなければ「？」。"),
        ("原材料（豚・酒）", "原材料表示に、豚由来の可能性がある成分（ゼラチン・乳化剤・ショートニング・ラード・マーガリン）やアルコール（酒精・洋酒・みりん・醤油ほか）があれば「×」、なければ「○」、原材料表示を確認できていなければ「？」。ハラール認証品ではありません。基準は観光庁「ベジタリアン・ヴィーガン／ムスリム旅行者おもてなしガイド」（2024年4月）に合わせています。"),
        ("個包装", "1個ずつ包装なら「○」、小分けパック（数個ずつ）なら「△」、個包装なしなら「×」、不明は「？」。"),
        ("総合", "予算・日持ち・原材料のどれかが「×」なら「×」。4項目すべて「○」なら「◎」。「△」か「？」が混じれば「△」（要確認）。それ以外（個包装だけ満たさない場合）は「○」。"),
        ("確認レベル", "A＝前任セッションが公式の商品ページを直接開いて確認／B＝公式ドメインなどの検索結果に価格・入数の記載があるが、商品ページ本文は未閲覧／C＝二次情報のみ、または情報が食い違う。B・Cは買う前に店舗か公式ページで確認してください。"),
    ]
    for k, v in rules:
        ws.write(r, 1, k, f_lbl)
        ws.merge_range(r, 2, r, 3, v, f_cell)
        ws.set_row(r, max(18, 14 * n_lines(v, 100) + 6))
        r += 1

    r += 1
    ws.merge_range(r, 1, r, 3, "集計", f_h2)
    r += 1
    n_prod, n_opt = len(products), sum(len(p["options"]) for p in products)
    intro_summary_row = r
    # 数式は各シートの範囲が決まってから書く（下で）
    r += 8

    ws.merge_range(r, 1, r, 3, "ご注意", f_h2)
    r += 1
    cautions = [
        "価格・入数・原材料・日持ちは2026年10月7日時点の各店の表示です。あんと店の在庫・取扱い・期間限定品の有無は、店舗へ電話で確認してください（営業8:30〜20:00）。",
        "確認レベルB・Cの項目は検索結果に基づくため、買う前の最終確認が必要です。調べ方の限界は「出典・調査メモ」にまとめています。",
        "写真は各社公式サイトの商品写真です（社内確認用。再配布しないでください）。" if use_photos else "この版には写真を入れていません。",
    ]
    for t in cautions:
        ws.merge_range(r, 1, r, 3, "・" + t, f_note)
        ws.set_row(r, max(18, 14 * n_lines(t, 130) + 4))
        r += 1

    ws.set_landscape()
    ws.set_paper(9)
    ws.fit_to_pages(1, 0)

    # ================================================================ 入数別価格（フラット）=====================
    ws = s_flat
    FH = 3  # ヘッダー行（0始まり）
    heads = ["オプションID", "商品ID", "区分", "店名", "商品名", "入数", "数量", "税込価格(円)", "1個あたり(円)", "予算", "個包装",
             "日持ち(日)", "渡す日の残り日数", "日持ち", "原材料(豚・酒)", "総合", "確認レベル", "包装メモ", "日持ち(表示)",
             "原材料の指摘語", "原材料(表示)", "期間・季節", "あんと店TEL", "商品ページ", "備考"]
    widths = [10, 7, 8, 12, 28, 16, 6, 10, 10, 6, 6, 8, 10, 7, 9, 6, 8, 26, 22, 16, 44, 18, 13, 10, 44]
    ws.merge_range(0, 0, 0, 12, "入数別価格（1行＝1つの入数）", f_title)
    ws.set_row(0, 28)
    ws.merge_range(1, 0, 1, 16, "並べ替え・絞り込みはこのシートで。○△×は「はじめに」の前提から数式で出しています。価格は税込。", f_sub)
    for ci, (h, w) in enumerate(zip(heads, widths)):
        ws.write(FH, ci, h, f_hdr)
        ws.set_column(ci, ci, w)
    ws.set_row(FH, 32)
    flat_rows = []
    for p in products:
        for o in p["options"]:
            flat_rows.append((p, o))
    first = FH + 1
    last = first + len(flat_rows) - 1
    rng = lambda col: f"'入数別価格'!${col}${first + 1}:${col}${last + 1}"  # noqa: E731
    flat_vals = {}
    for i, (p, o) in enumerate(flat_rows):
        R = first + i          # 0始まりの行
        X = R + 1              # Excelの行番号
        price = o["price"]
        qty = o["qty"]
        unit_price = round_half_up(price / qty) if qty else ""
        jb = mark_budget(price)
        rem = remaining_days(p["shelf_life_days"])
        nm = mark_shelf(rem)
        om = p["ingredient_check"]
        km = WRAP_MARK.get(p["wrap_level"], UNK)
        ov = mark_overall(jb, nm, om, km)
        flat_vals[o["id"]] = dict(price=price, overall=ov)
        ws.write(R, 0, o["id"], f_txtc)
        ws.write(R, 1, p["id"], f_txtc)
        ws.write(R, 2, p["shop_category"], f_txtc)
        ws.write(R, 3, p["shop"], f_txt)
        ws.write(R, 4, p["name"], f_txt)
        ws.write(R, 5, o["label"], f_txt)
        if qty:
            ws.write_number(R, 6, qty, f_int)
        else:
            ws.write_blank(R, 6, None, f_int)
        ws.write_number(R, 7, price, f_num)
        ws.write_formula(R, 8, f'=IF(AND(ISNUMBER(G{X}),G{X}>0),ROUND(H{X}/G{X},0),"")', f_num, unit_price)
        ws.write_formula(R, 9, f'=IF(H{X}<=Budget,"{OK}",IF(H{X}<=Budget+Tolerance,"{WARN}","{NG}"))', f_mark, jb)
        ws.write(R, 10, km, f_mark)
        if p["shelf_life_days"] is None:
            ws.write_blank(R, 11, None, f_int)
        else:
            ws.write_number(R, 11, p["shelf_life_days"], f_int)
        ws.write_formula(R, 12, f'=IF(L{X}="","",L{X}-(GiveDate-BuyDate)-MfgLag)', f_int, "" if rem is None else rem)
        ws.write_formula(R, 13, f'=IF(M{X}="","{UNK}",IF(M{X}>=OkDays,"{OK}",IF(M{X}>=WarnDays,"{WARN}","{NG}")))', f_mark, nm)
        ws.write(R, 14, om, f_mark)
        ws.write_formula(
            R, 15,
            f'=IF(OR(J{X}="{NG}",N{X}="{NG}",O{X}="{NG}"),"{NG}",IF(AND(J{X}="{OK}",N{X}="{OK}",O{X}="{OK}",K{X}="{OK}"),"{TOP}",'
            f'IF(OR(J{X}="{WARN}",N{X}="{WARN}",N{X}="{UNK}",O{X}="{UNK}",K{X}="{UNK}"),"{WARN}","{OK}")))', f_mark, ov)
        ws.write(R, 16, o.get("confidence") or p["confidence_min"], f_txtc)
        ws.write(R, 17, p["wrap_note"] or WRAP_TEXT.get(p["wrap_level"], ""), f_txt)
        ws.write(R, 18, p["shelf_life_text"], f_txt)
        ws.write(R, 19, "・".join(p["ingredient_hits"]) + (f"（参考: {'・'.join(p['ingredient_notes'])}）" if p["ingredient_notes"] else ""), f_txt)
        ws.write(R, 20, p["ingredients"] or "（原材料表示を確認できていない）", f_txt)
        ws.write(R, 21, p["seasonal"], f_txt)
        ws.write(R, 22, p["phone"] or "", f_txtc)
        url = o.get("source_url") or p.get("page_url")
        if url:
            ws.write_url(R, 23, url, f_link, string="開く")
        else:
            ws.write(R, 23, "", f_txtc)
        first_row = o is p["options"][0]
        opt_note = o.get("note") or ""
        body = p["notes"] if first_row else ""
        note_text = opt_note + (" ／" if opt_note and body else "") + body
        if not note_text and not first_row and p["notes"]:
            note_text = f"（備考は {p['id']}-1 の行に記載）"
        ws.write(R, 24, note_text, f_txt)
        lines = max(n_lines(p["name"], widths[4]), n_lines(note_text, widths[24]), n_lines(p["ingredients"] or "", widths[20]),
                    n_lines(p["wrap_note"], widths[17]))
        ws.set_row(R, min(150, max(24, 13.5 * lines + 4)))
    ws.autofilter(FH, 0, last, len(heads) - 1)
    ws.freeze_panes(FH + 1, 5)
    for col in ("J", "K", "N", "O", "P"):
        add_mark_cf(ws, f"{col}{first + 1}:{col}{last + 1}")
    ws.set_landscape()
    ws.set_paper(9)
    ws.fit_to_pages(1, 0)
    ws.repeat_rows(FH)

    # ================================================================ 候補一覧（写真つき）=======================
    ws = s_cat
    CH = 3
    cheads = ["No.", "写真", "店名", "区分", "商品名・説明", "入数別の税込価格", "総合", "個包装", "日持ち", "渡す日の残り日数",
              "原材料（表示）", "豚・酒チェック", "人気・評判", "確認レベル", "あんと店TEL", "商品ページ", "備考"]
    cw = [7, 17, 12, 8, 34, 26, 7, 18, 16, 9, 40, 14, 18, 9, 13, 9, 42]
    ws.merge_range(0, 0, 0, 10, "候補一覧（写真と入数別価格）", f_title)
    ws.set_row(0, 28)
    ws.merge_range(1, 0, 1, 12, "1商品1行。並べ替えると写真がずれるため、絞り込み（フィルタ）だけ使ってください。○△×は予算2,000円・購入日10/7の前提での判定です（変更は「はじめに」）。", f_sub)
    for ci, (h, w) in enumerate(zip(cheads, cw)):
        ws.write(CH, ci, h, f_hdr)
        ws.set_column(ci, ci, w)
    ws.set_row(CH, 32)
    cfirst = CH + 1
    clast = cfirst + len(products) - 1
    f_name_b = f(bold=True, text_wrap=True, valign="top")
    f_name_n = f(text_wrap=True, valign="top", font_color=C["sub"])
    min_h = (IMG_PX * 0.75) + 8 if use_photos else 24
    for i, p in enumerate(products):
        R = cfirst + i
        X = R + 1
        ws.write(R, 0, p["id"], f_txtc)
        photo = photo_of(p)
        if photo:
            sc = IMG_PX / IMG_SRC_PX
            ws.write_blank(R, 1, None, f_txtc)
            ws.insert_image(R, 1, str(photo), {"x_scale": sc, "y_scale": sc, "x_offset": 8, "y_offset": 4, "object_position": 1,
                                               "description": f"{p['shop']} {p['name']}"})
        else:
            ws.write(R, 1, no_photo_label(p) if use_photos else "", f(text_wrap=True, align="center", font_color=C["sub"], border=1, border_color=C["line"], font_size=9))
        ws.write(R, 2, p["shop"], f_txt)
        ws.write(R, 3, p["shop_category"], f_txtc)
        desc = short((p["kind"] + "｜" if p["kind"] else "") + (p["description"] or ""), 90)
        ws.write_rich_string(R, 4, f_name_b, p["name"], f_name_n, "\n" + desc, f_txt)
        price_lines = []
        for o in p["options"]:
            price_lines.append(f"{mark_budget(o['price'])} {o['label']}　{yen(o['price'])}" + ("※" if o.get("note") else ""))
        price_text = "\n".join(price_lines)
        ws.write(R, 5, price_text, f_txt)
        ids = f"'入数別価格'!$B${first + 1}:$B${last + 1}"
        pr = f"'入数別価格'!$P${first + 1}:$P${last + 1}"
        best = TOP if any(flat_vals[o["id"]]["overall"] == TOP for o in p["options"]) else (
            OK if any(flat_vals[o["id"]]["overall"] == OK for o in p["options"]) else (
                WARN if any(flat_vals[o["id"]]["overall"] == WARN for o in p["options"]) else NG))
        ws.write_formula(
            R, 6,
            f'=IF(COUNTIFS({ids},$A{X},{pr},"{TOP}")>0,"{TOP}",IF(COUNTIFS({ids},$A{X},{pr},"{OK}")>0,"{OK}",'
            f'IF(COUNTIFS({ids},$A{X},{pr},"{WARN}")>0,"{WARN}","{NG}")))', f_mark, best)
        wl = p["wrap_level"]
        wtxt = f"{WRAP_MARK[wl]} {WRAP_TEXT[wl]}" + (f"\n{p['wrap_note']}" if p["wrap_note"] and wl is not None else "")
        stext = short(p["shelf_life_text"] or (f"{p['shelf_life_days']}日" if p["shelf_life_days"] else "不明（未確認）"), 60)
        shelf_c = stext + (f"\n{short(p['seasonal'], 40)}" if p["seasonal"] and not p["seasonal"].startswith("通年") else "")
        ws.write(R, 8, shelf_c, f_txt)
        rem = remaining_days(p["shelf_life_days"])
        am = f"'入数別価格'!$A${first + 1}:$A${last + 1}"
        mm = f"'入数別価格'!$M${first + 1}:$M${last + 1}"
        ws.write_formula(R, 9, f'=IFERROR(INDEX({mm},MATCH($A{X}&"-1",{am},0)),"")', f_int, "" if rem is None else rem)
        chk = p["ingredient_check"]
        ctext = chk + ("\n" + "・".join(p["ingredient_hits"]) if p["ingredient_hits"] else "")
        ws.write(R, 11, ctext, f_txtc)
        ws.write(R, 12, "\n".join(p.get("popularity") or []), f_txt)
        ws.write(R, 13, conf_text(p), f_txtc)
        ws.write(R, 14, p["phone"] or "", f_txtc)
        if p.get("page_url"):
            ws.write_url(R, 15, p["page_url"], f_link, string="開く")
        else:
            ws.write(R, 15, "", f_txtc)
        opt_notes = list(dict.fromkeys(o["note"] for o in p["options"] if o.get("note")))
        pre = ("※" + "／".join(opt_notes) + " ／") if opt_notes else ""
        approx = (photo_log.get(p["id"]) or {}).get("approx")
        if approx and photo_of(p):
            pre = f"※写真は{approx} ／" + pre
        note_c = short(pre + p["notes"], 130) + ("（続きは「入数別価格」）" if len(pre + p["notes"]) > 130 else "")
        ws.write(R, 16, note_c, f_txt)
        ing_c = short(p["ingredients"], 150) if p["ingredients"] else "（原材料表示を確認できていない）"
        ws.write(R, 10, ing_c, f_txt)
        wtxt_c = wtxt if len(wtxt) <= 70 else short(wtxt, 70)
        ws.write(R, 7, wtxt_c, f_txt)
        lines = max(n_lines(p["name"] + "\n" + desc, cw[4]), n_lines(price_text, cw[5]), n_lines(wtxt_c, cw[7]),
                    n_lines(ing_c, cw[10]), n_lines(note_c, cw[16]), n_lines("\n".join(p.get("popularity") or []), cw[12]),
                    n_lines(shelf_c, cw[8]), n_lines(ctext, cw[11]))
        ws.set_row(R, min(170, max(min_h, 13.5 * lines + 6)))
    ws.autofilter(CH, 0, clast, len(cheads) - 1)
    ws.freeze_panes(CH + 1, 5)
    add_mark_cf(ws, f"G{cfirst + 1}:G{clast + 1}")
    ws.conditional_format(f"L{cfirst + 1}:L{clast + 1}", {"type": "text", "criteria": "begins with", "value": NG,
                                                        "format": wb.add_format({"bg_color": MARK_FILL[NG], "font_color": MARK_FONT[NG], "bold": True})})
    ws.conditional_format(f"L{cfirst + 1}:L{clast + 1}", {"type": "text", "criteria": "begins with", "value": UNK,
                                                        "format": wb.add_format({"bg_color": MARK_FILL[UNK], "font_color": MARK_FONT[UNK], "bold": True})})
    ws.set_landscape()
    ws.set_paper(9)
    ws.fit_to_pages(1, 0)
    ws.repeat_rows(CH)

    # ================================================================ 店舗一覧 ===================================
    ws = s_shop
    SH = 3
    sheads = ["区分", "店名", "通称", "あんと内の場所", "TEL", "あんと公式の店舗ページ", "対象", "判断の理由・メモ", "掲載商品数",
              "原材料が未確認の商品", "個包装が不明の商品", "公式サイト", "通販サイト"]
    sw = [10, 28, 14, 14, 13, 14, 8, 60, 9, 10, 10, 22, 22]
    ws.merge_range(0, 0, 0, 8, "店舗一覧（あんとの店舗と、対象にしたかどうか）", f_title)
    ws.set_row(0, 28)
    ws.merge_range(1, 0, 1, 9, "対象＝菓子・食品の土産を扱う店。商品まで調べられた店は「掲載商品数」に件数が出ます。あんと店の取扱い・在庫は電話で確認してください。", f_sub)
    for ci, (h, w) in enumerate(zip(sheads, sw)):
        ws.write(SH, ci, h, f_hdr)
        ws.set_column(ci, ci, w)
    ws.set_row(SH, 30)
    scope_text = {"yes": "対象", "maybe": "要確認", "no": "対象外"}
    shop_first = SH + 1
    for i, s in enumerate(shops):
        R = shop_first + i
        X = R + 1
        ws.write(R, 0, s.get("category") or "", f_txtc)
        ws.write(R, 1, s.get("name") or s.get("short") or "", f_txt)
        ws.write(R, 2, s.get("short") or "", f_txt)
        loc = " ".join(x for x in [s.get("area"), s.get("location")] if x)
        ws.write(R, 3, loc, f_txt)
        ws.write(R, 4, s.get("phone") or "", f_txtc)
        if s.get("anto_page_url"):
            ws.write_url(R, 5, s["anto_page_url"], f_link, string="開く")
        else:
            ws.write(R, 5, "", f_txtc)
        ws.write(R, 6, scope_text.get(s.get("in_scope"), s.get("in_scope") or ""), f_txtc)
        reason = short(s.get("reason") or "", 260)
        ws.write(R, 7, reason, f_txt)
        cnt = sum(1 for p in products if p["shop_key"] == s["key"])
        cat_shop = f"'候補一覧'!$C${cfirst + 1}:$C${clast + 1}"
        ws.write_formula(R, 8, f'=COUNTIF({cat_shop},C{X})', f_int, cnt)
        mine = [p for p in products if p["shop_key"] == s["key"]]
        ws.write_number(R, 9, sum(1 for p in mine if p["ingredient_check"] == UNK), f_int)
        ws.write_number(R, 10, sum(1 for p in mine if p["wrap_level"] is None), f_int)
        for ci, key in ((11, "official_site"), (12, "online_shop")):
            if s.get(key):
                ws.write_url(R, ci, s[key], f_link, string=s[key].replace("https://", "").replace("http://", "").rstrip("/")[:28])
            else:
                ws.write(R, ci, "", f_txtc)
        ws.set_row(R, min(120, max(20, 13.5 * n_lines(reason, sw[7]) + 4)))
    slast = shop_first + len(shops) - 1
    ws.autofilter(SH, 0, slast, len(sheads) - 1)
    ws.freeze_panes(SH + 1, 2)
    ws.conditional_format(f"G{shop_first + 1}:G{slast + 1}", {"type": "cell", "criteria": "==", "value": '"対象"',
                                                            "format": wb.add_format({"bg_color": MARK_FILL[OK], "font_color": MARK_FONT[OK], "bold": True})})
    ws.conditional_format(f"G{shop_first + 1}:G{slast + 1}", {"type": "cell", "criteria": "==", "value": '"要確認"',
                                                            "format": wb.add_format({"bg_color": MARK_FILL[WARN], "font_color": MARK_FONT[WARN], "bold": True})})
    ws.conditional_format(f"G{shop_first + 1}:G{slast + 1}", {"type": "cell", "criteria": "==", "value": '"対象外"',
                                                            "format": wb.add_format({"bg_color": MARK_FILL[UNK], "font_color": MARK_FONT[UNK], "bold": True})})
    ws.set_landscape()
    ws.set_paper(9)
    ws.fit_to_pages(1, 0)

    # ================================================================ 除外・要確認 ===============================
    ws = s_ex
    ws.set_column("A:A", 24)
    ws.set_column("B:B", 30)
    ws.set_column("C:C", 84)
    ws.set_column("D:D", 14)
    ws.merge_range(0, 0, 0, 3, "除外・要確認", f_title)
    ws.set_row(0, 28)
    r = 2
    ws.merge_range(r, 0, r, 3, "1. 外した商品・店とその理由", f_h2)
    r += 1
    for ci, h in enumerate(["店", "商品", "理由", "確認レベル"]):
        ws.write(r, ci, h, f_hdr)
    r += 1
    hit_rows = [(p["shop"], p["name"], "原材料表示に「" + "・".join(p["ingredient_hits"]) + "」があるため（豚由来の可能性またはアルコール）", conf_text(p))
                for p in products if p["ingredient_check"] == NG]
    ex_rows = [(e.get("shop", ""), e.get("item", ""), e.get("reason", ""), e.get("level", "B")) for e in excluded]
    for sh, it, rs, lv in ex_rows + hit_rows:
        ws.write(r, 0, sh, f_txt)
        ws.write(r, 1, it, f_txt)
        ws.write(r, 2, rs, f_txt)
        ws.write(r, 3, lv, f_txtc)
        ws.set_row(r, min(110, max(20, 13.5 * max(n_lines(rs, 84), n_lines(it, 30)) + 4)))
        r += 1

    r += 1
    ws.merge_range(r, 0, r, 3, "2. 商品をまだ調べられていない店（対象・要確認）", f_h2)
    r += 1
    ws.merge_range(r, 0, r, 3, "検索回数の上限に達したため、次の店は店舗の情報までで、商品・入数別の価格を調べられていません。次の調査で最優先の候補です。", f_note)
    ws.set_row(r, 20)
    r += 1
    for ci, h in enumerate(["店", "区分・対象", "メモ（店の特徴・分かっていること）", "あんと店TEL"]):
        ws.write(r, ci, h, f_hdr)
    r += 1
    n_pending = 0
    for s in shops:
        if s.get("in_scope") in ("yes", "maybe") and not n_by_shop.get(s["key"]):
            n_pending += 1
            ws.write(r, 0, s.get("short") or s.get("name"), f_txt)
            ws.write(r, 1, f"{s.get('category', '')}・{scope_text.get(s.get('in_scope'), '')}", f_txt)
            memo_s = short(s.get("reason") or "", 260)
            ws.write(r, 2, memo_s, f_txt)
            ws.write(r, 3, s.get("phone") or "", f_txtc)
            ws.set_row(r, min(100, max(20, 13.5 * n_lines(memo_s, 84) + 4)))
            r += 1

    r += 1
    ws.merge_range(r, 0, r, 3, "3. 調べた店で、まだ確認できていない項目", f_h2)
    r += 1
    ws.write(r, 0, "店", f_hdr)
    ws.merge_range(r, 1, r, 3, "内容", f_hdr)
    r += 1
    for u in unresolved:
        ws.write(r, 0, u.get("shop", ""), f_txt)
        item_u = short(u.get("item", ""), 420)
        ws.merge_range(r, 1, r, 3, item_u, f_txt)
        ws.set_row(r, min(120, max(20, 13.5 * n_lines(item_u, 125) + 4)))
        r += 1

    ws.set_landscape()
    ws.set_paper(9)
    ws.fit_to_pages(1, 0)

    # ================================================================ 出典・調査メモ ==============================
    ws = s_src
    ws.set_column("A:A", 20)
    ws.set_column("B:B", 16)
    ws.set_column("C:C", 100)
    ws.merge_range(0, 0, 0, 2, "出典・調査メモ", f_title)
    ws.set_row(0, 28)
    r = 2
    ws.merge_range(r, 0, r, 2, "調べ方と限界", f_h2)
    r += 1
    memo = [
        "確認レベルAのデータ（12商品）は、前任のセッション（Claude Desktop＋Claude in Chrome）が各店の公式の商品ページを直接開いて確認し、写真もそのときの公式写真です。",
        "それ以外（確認レベルB・C）は、公式ドメインに絞った検索（WebSearch）の結果に載っていた価格・入数・賞味期限・原材料です。検索結果は要約であり、商品ページ本文を直接見ていないため、買う前に店舗か公式ページで確認してください。",
        (f"写真は、確認レベルAの商品は前任が公式ページで取得したもの、それ以外は各店の商品ページ（公式・通販）から取得したものです。{sum(1 for p in products if photo_of(p))}／{len(products)}商品に付いています。"
         "商品名と写真が合っているかは一覧画像で目視確認していますが、通販ページの写真は入数や詰め合わせの内容と一致しないことがあります。"
         "同じ商品の写真が見つからず、近い商品・シリーズの写真を使ったものは、「候補一覧」の備考に「※写真は…」と書いています。"
         "商品ページが削除されている・サイトが自動取得を拒否している商品は、写真なしとして理由をセルに書いています。"
         if use_photos and sum(1 for p in products if photo_of(p)) > 12 else
         "今回の環境では、金沢百番街の公式サイトと各店の公式・通販サイトに直接アクセスできませんでした（実行環境の通信制限）。このため、写真はAレベルの商品にしか付いていません。"),
        "検索の回数に上限があり、調べきれなかった店・商品は「除外・要確認」と「店舗一覧」に未調査として残しています。",
        "あんと店舗の電話番号は、金沢百番街の公式の店舗ページの記載を検索で確認したものです（本店・通販の番号とは別）。",
    ]
    for t in memo:
        ws.merge_range(r, 0, r, 2, "・" + t, f_body)
        ws.set_row(r, max(18, 14 * n_lines(t, 150) + 4))
        r += 1
    r += 1
    ws.merge_range(r, 0, r, 2, "出典URL（店別）", f_h2)
    r += 1
    for ci, h in enumerate(["店", "種別", "URL"]):
        ws.write(r, ci, h, f_hdr)
    r += 1
    seen = set()
    for s in shops:
        rows = []
        if s.get("anto_page_url"):
            rows.append(("あんと公式の店舗ページ", s["anto_page_url"]))
        if s.get("official_site"):
            rows.append(("公式サイト", s["official_site"]))
        if s.get("online_shop") and s.get("online_shop") != s.get("official_site"):
            rows.append(("通販サイト", s["online_shop"]))
        for p in products:
            if p["shop_key"] == s["key"]:
                if p.get("page_url"):
                    rows.append((f"商品: {p['name'][:14]}", p["page_url"]))
                for o in p["options"]:
                    if o.get("source_url") and o["source_url"] != p.get("page_url"):
                        rows.append((f"商品: {p['name'][:14]}／{o['label'][:8]}", o["source_url"]))
        for kind, url in rows:
            if (s["key"], url) in seen:
                continue
            seen.add((s["key"], url))
            ws.write(r, 0, s.get("short") or s.get("name"), f_txt)
            ws.write(r, 1, kind, f_txt)
            ws.write_url(r, 2, url, f(font_color=C["link"], underline=1, border=1, border_color=C["line"]), string=url[:120])
            r += 1

    ws.set_landscape()
    ws.set_paper(9)
    ws.fit_to_pages(1, 0)

    # ================================================================ おすすめ案 ==================================
    if s_pick is not None and picks:
        ws = s_pick
        ws.set_column("A:A", 5)
        ws.set_column("B:B", 17)
        ws.set_column("C:C", 36)
        ws.set_column("D:D", 12)
        ws.set_column("E:E", 14)
        ws.set_column("F:F", 11)
        ws.set_column("G:G", 7)
        ws.set_column("H:H", 30)
        ws.set_column("I:I", 56)
        ws.merge_range(0, 0, 0, 8, picks.get("title", "おすすめ案"), f_title)
        ws.set_row(0, 28)
        ws.merge_range(1, 0, 1, 8, picks.get("note", ""), f_sub)
        ws.set_row(1, 30)
        heads = ["", "写真", "商品", "店", "入数", "税込価格", "総合", "渡し先（案）", "選んだ理由・注意"]
        PH = 3
        for ci, h in enumerate(heads):
            ws.write(PH, ci, h, f_hdr)
        ws.set_row(PH, 24)

        r = PH + 1
        pick_first = r
        am = f"'入数別価格'!$A${first + 1}:$A${last + 1}"
        for n, pk in enumerate(picks["picks"], 1):
            p, o = resolve(pk["match"])
            if not p:
                print("[warn] おすすめ案の商品が見つかりません:", pk["match"])
                continue
            photo = photo_of(p)
            ws.write(r, 0, "①②③④⑤⑥"[n - 1], f_mark)
            if photo:
                sc = IMG_PX / IMG_SRC_PX
                ws.write_blank(r, 1, None, f_txtc)
                ws.insert_image(r, 1, str(photo), {"x_scale": sc, "y_scale": sc, "x_offset": 8, "y_offset": 4, "object_position": 1})
            else:
                ws.write(r, 1, "", f_txtc)
            ws.write_rich_string(r, 2, f_name_b, p["name"], f_name_n, "\n" + (p["description"] or ""), f_txt)
            ws.write(r, 3, p["shop"], f_txt)
            ws.write(r, 4, o["label"], f_txt)
            ws.write_formula(r, 5, f'=INDEX(\'入数別価格\'!$H${first + 1}:$H${last + 1},MATCH("{o["id"]}",{am},0))', f_num, o["price"])
            ws.write_formula(r, 6, f'=INDEX(\'入数別価格\'!$P${first + 1}:$P${last + 1},MATCH("{o["id"]}",{am},0))', f_mark,
                             flat_vals[o["id"]]["overall"])
            ws.write(r, 7, pk.get("recipient", ""), f_txt)
            ws.write(r, 8, pk.get("reason", ""), f_txt)
            ws.set_row(r, max(IMG_PX * 0.75 + 8, 13.5 * max(n_lines(pk.get("reason", ""), 56), n_lines(p["description"] or "", 36) + 1) + 6))
            r += 1
        pick_last = r - 1
        ws.write(r, 4, "合計", f(bold=True, align="right"))
        total = sum(flat_vals[resolve(pk["match"])[1]["id"]]["price"] for pk in picks["picks"] if resolve(pk["match"])[0])
        ws.write_formula(r, 5, f"=SUM(F{pick_first + 1}:F{pick_last + 1})", f(bold=True, num_format="#,##0", align="right", top=1), total)
        add_mark_cf(ws, f"G{pick_first + 1}:G{pick_last + 1}")
        r += 2
        if picks.get("alternatives"):
            ws.merge_range(r, 0, r, 8, "差し替え候補・検討中", f_h2)
            r += 1
            for ci, h in enumerate(["", "写真", "商品", "店", "入数", "税込価格", "総合"]):
                ws.write(r, ci, h, f_hdr)
            ws.merge_range(r, 7, r, 8, "ひとこと", f_hdr)
            r += 1
            for alt in picks["alternatives"]:
                p, o = resolve(alt["match"])
                if not p:
                    print("[warn] 差し替え候補が見つかりません:", alt["match"])
                    continue
                photo = photo_of(p)
                ws.write(r, 0, "", f_txtc)
                if photo:
                    sc = IMG_PX / IMG_SRC_PX
                    ws.write_blank(r, 1, None, f_txtc)
                    ws.insert_image(r, 1, str(photo), {"x_scale": sc, "y_scale": sc, "x_offset": 8, "y_offset": 4, "object_position": 1})
                else:
                    ws.write(r, 1, no_photo_label(p) if use_photos else "", f(align="center", font_color=C["sub"], border=1, border_color=C["line"], font_size=9))
                ws.write_rich_string(r, 2, f_name_b, p["name"], f_name_n, "\n" + (p["description"] or ""), f_txt)
                ws.write(r, 3, p["shop"], f_txt)
                ws.write(r, 4, o["label"], f_txt)
                ws.write_formula(r, 5, f'=INDEX(\'入数別価格\'!$H${first + 1}:$H${last + 1},MATCH("{o["id"]}",{am},0))', f_num, o["price"])
                ws.write_formula(r, 6, f'=INDEX(\'入数別価格\'!$P${first + 1}:$P${last + 1},MATCH("{o["id"]}",{am},0))', f_mark,
                                 flat_vals[o["id"]]["overall"])
                ws.merge_range(r, 7, r, 8, alt.get("note", ""), f_txt)
                has_photo = bool(photo)
                ws.set_row(r, max((IMG_PX * 0.75 + 8) if has_photo else 40, 13.5 * max(n_lines(alt.get("note", ""), 86), n_lines(p["description"] or "", 36) + 1) + 6))
                r += 1
            add_mark_cf(ws, f"G{r - len(picks['alternatives']) + 1}:G{r}")
        ws.set_landscape()
        ws.set_paper(9)
        ws.fit_to_pages(1, 0)

    # ================================================================ はじめに: 集計の数式 ========================
    ws = s_intro
    r = intro_summary_row
    cat_shop = f"'候補一覧'!$C${cfirst + 1}:$C${clast + 1}"
    jr = f"'入数別価格'!$J${first + 1}:$J${last + 1}"
    pr_ = f"'入数別価格'!$P${first + 1}:$P${last + 1}"
    shop_scope = f"'店舗一覧'!$G${shop_first + 1}:$G${slast + 1}"
    n_scope_yes = sum(1 for s in shops if s.get("in_scope") == "yes")
    n_researched = len({p["shop_key"] for p in products})
    n_pending_yes = sum(1 for s in shops if s.get("in_scope") == "yes" and not n_by_shop.get(s["key"]))
    n_budget_ok = sum(1 for p in products for o in p["options"] if mark_budget(o["price"]) == OK)
    n_top = sum(1 for p in products for o in p["options"] if flat_vals[o["id"]]["overall"] == TOP)
    summary = [
        ("あんとの店舗（洗い出せた数）", f"=COUNTA('店舗一覧'!$B${shop_first + 1}:$B${slast + 1})", len(shops)),
        ("うち、菓子・食品の土産を扱う店（対象）", f'=COUNTIF({shop_scope},"対象")', n_scope_yes),
        ("商品まで調べられた店", f"=SUMPRODUCT(('店舗一覧'!$I${shop_first + 1}:$I${slast + 1}>0)*1)", n_researched),
        ("対象のうち、商品をまだ調べられていない店", f'=COUNTIFS({shop_scope},"対象",\'店舗一覧\'!$I${shop_first + 1}:$I${slast + 1},0)', n_pending_yes),
        ("掲載した商品数", f"=COUNTA('候補一覧'!$A${cfirst + 1}:$A${clast + 1})", n_prod),
        ("入数別の行数", f"=COUNTA('入数別価格'!$A${first + 1}:$A${last + 1})", n_opt),
        ("うち、予算内（○）の行数 ／ 総合◎の行数", f'=COUNTIF({jr},"{OK}")&" ／ "&COUNTIF({pr_},"{TOP}")', f"{n_budget_ok} ／ {n_top}"),
    ]
    for k, fm, val in summary:
        ws.write(r, 1, k, f_lbl)
        ws.merge_range(r, 2, r, 3, "", f_cell)
        ws.write_formula(r, 2, fm, f(bold=True, align="left", border=1, border_color=C["line"], num_format="#,##0"), val)
        r += 1

    ws.activate()
    wb.close()
    print(f"wrote {out}: products={n_prod} options={n_opt} shops={len(shops)} photos={'yes' if use_photos else 'no'} picks={'yes' if picks else 'no'}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default="data")
    ap.add_argument("--out", default="deliverables/anto_omiyage_candidates.xlsx")
    ap.add_argument("--private", default="private")
    ap.add_argument("--public", action="store_true", help="写真・おすすめ案なしの公開版")
    a = ap.parse_args()
    pathlib.Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    build(a.out, pathlib.Path(a.data), a.public, pathlib.Path(a.private))
