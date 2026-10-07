#!/usr/bin/env python3
"""調査結果（data/raw/G*.json）を突き合わせ、スプレッドシートの元データを作る。

出力:
  data/products.json  商品 × 入数ごとの価格・包装・日持ち・原材料チェック
  data/shops.json     あんと（金沢百番街）の店舗一覧（対象 / 対象外の判定つき）と人気商品
  data/excluded.json  外した商品と、確認できていない項目

使い方:
  python3 scripts/merge_research.py [--raw data/raw] [--out data] [--overrides data/overrides.json]

G0 は前任セッションが公式の商品ページを直接開いて確認したデータ（確認レベルA）。
G1〜G6 は商品調査、G7 は店舗の洗い出しと人気商品。いずれも公式ドメインに絞った検索（WebSearch）の結果で、
商品ページ本文は見ていないため、確認レベルはB（またはC）に統一する。
"""
from __future__ import annotations

import argparse
import difflib
import json
import pathlib
import re
import unicodedata
from collections import defaultdict

CONF_RANK = {"A": 3, "B": 2, "C": 1}
CATEGORY_ORDER = ["和菓子", "洋菓子", "食品・海産物", "酒", "茶", "惣菜・弁当・寿司", "工芸・雑貨", "その他"]
SHOP_PREFIXES = ["加賀藩御用菓子司", "歳時和菓子", "兼六園本舗", "和菓子処", "和菓子", "菓匠", "落雁", "きんつば", "金沢", "金澤",
                 "洋菓子工房", "茶菓工房", "加賀棒茶"]
# 表記ゆれで別キーになる店を、正規のキー（shop_key の結果）へ寄せる
SHOP_ALIASES = {
    "filfilcacaofactorybyfildor": "filfilcacaofactory",
    "pisobyrespiración": "piso",
    "俵屋": "あめの俵屋",
    "ふらんどる": "しあわせお菓子ふらんどる",
    "ルコタンタン金沢": "ルコタンタン",
    "クルミのおやつ": "大畑食品",
    "クルミのおやつ大畑食品": "大畑食品",
    "ルミュゼドゥアッシュ金沢駅百番街店": "ルミュゼドゥアッシュ",
    "ルミュゼドゥアッシュフレ": "ルミュゼドゥアッシュ",   # 同じブランド・同じ通販。あんとには2店あるので電話番号は両方残す
}

# 原材料表示から機械的に拾う語。判断基準は観光庁「ベジタリアン・ヴィーガン／ムスリム旅行者おもてなしガイド」（2024年4月）に合わせ、
# 豚由来の可能性がある成分（ゼラチン・乳化剤・ショートニング・ラード）とアルコール（酒精・洋酒・みりん・醤油ほか）を「×」とする。
RED_PATTERNS = [
    ("ゼラチン", r"ゼラチン"), ("乳化剤", r"乳化剤|グリセリンエステル|グリセリン脂肪酸エステル"), ("ショートニング", r"ショートニング"),
    ("ラード", r"ラード|豚|ポーク"), ("マーガリン", r"マーガリン"),
    ("酒精", r"酒精"), ("洋酒", r"洋酒|ラム酒|ラムレーズン|ダークラム|ブランデー|ウイスキー|リキュール|ワイン|キルシュ|コニャック|グランマルニエ"),
    ("みりん", r"みりん|味醂|本みりん"), ("清酒・酒", r"清酒|日本酒|焼酎|梅酒|酒粕|酒(?!石)"), ("醤油", r"醤油|しょうゆ|ショウユ"),
    ("アルコール", r"アルコール"),
]
# 参考扱い（×にはしない。ハラール上の扱いが分かれるもの）
NOTE_PATTERNS = [("コチニール", r"コチニール|カルミン酸|ラック"), ("L-システイン", r"システイン")]


def norm(s: str | None) -> str:
    s = unicodedata.normalize("NFKC", s or "")
    s = re.sub(r"[\s・･·\-ー〜~＜＞<>\[\]（）()【】「」『』“”\"'、,.，．/／:：]", "", s)
    return s.lower()


def shop_key(name: str) -> str:
    n = norm(name)
    for p in SHOP_PREFIXES:
        np_ = norm(p)
        if n.startswith(np_) and len(n) > len(np_):
            n = n[len(np_):]
    return SHOP_ALIASES.get(n, n)


QTY_SUFFIX = re.compile(r"[（(]?\s*\d+\s*(?:入り?|個入り?|枚入り?|本入り?|袋入り?|粒入り?|缶入り?|切入り?)\s*[）)]?")
BRACKETS = re.compile(r"[（(〈][^）)〉]*[）)〉]")   # 丸括弧の中（季節・入数など）だけ除く。［黒豆］のような味の違いは残す


def base_name(s: str | None) -> str:
    """括弧内（季節・入数など）と入数の表記を除いた商品名"""
    t = unicodedata.normalize("NFKC", s or "")
    t = BRACKETS.sub("", t)
    return norm(QTY_SUFFIX.sub("", t))


def same_product(a: str, b: str) -> bool:
    """同じ商品か。誤統合を避けるため、完全一致（括弧・入数を除く）か、名前の長さが近い包含だけを認める"""
    na, nb = base_name(a), base_name(b)
    if not na or not nb:
        return False
    if na == nb:
        return True
    short, long_ = sorted([na, nb], key=len)
    return len(short) >= 3 and short in long_ and len(short) / len(long_) >= 0.85


def name_hit(rule_name: str, prod_name: str) -> bool:
    """data/overrides.json の指定用。名前の一部を書けば当たるようにする"""
    return norm(rule_name) in norm(prod_name) or same_product(rule_name, prod_name)


def clean_text(t: str | None) -> str:
    """調査の過程を表す語を、利用者向けの言い方に直す"""
    t = t or ""
    t = re.sub(r"【最重要】", "", t)
    t = re.sub(r"WebSearch\s*(?:の)?(?:共有)?(?:上限|予算)(?:（[^）]*）)?(?:に(?:到達|達)(?:し(?:た|て)?)?|到達|に達した)?(?:ため)?",
               "検索回数の上限に達したため", t)
    t = t.replace("WebSearch", "検索")
    return t.strip()


JARGON_SENT = re.compile(r"ingredient_flags|ingredients・flags|flagsは|\bflags\b|price_tax_incl|\bnull\b|JSON|schema|in_scope")


def join_notes(a: str, b: str) -> str:
    if not a:
        return b
    return a + ("" if not b else (" " if a.endswith("。") else " ／") + b)


def clean_note(t: str | None) -> str:
    """調査担当のメモから、内部のデータ項目名を含む文を除き、言い回しを整える"""
    t = clean_text(t)
    t = t.replace("conflict:", "【食い違い】").replace("【矛盾】", "【食い違い】")
    t = re.sub(r"confidence\s*(?:は|を)?\s*([ABC])", r"確認レベル\1", t)
    t = t.replace("confidence", "確認レベル")
    out: list[str] = []
    for sent in re.split(r"(?<=。)\s*|\s*／\s*", t):
        sent = sent.strip()
        if not sent:
            continue
        if JARGON_SENT.search(sent):
            continue   # 内部のデータ項目名を含む文（換算の注記は入数ごとの注記で出す）
        if (sent.startswith("8%換算の参考値") or "前任調査から引き継いだ" in sent or "本セッションの検索では再確認できていない" in sent
                or "既知データを転記" in sent or "商品ページURLは未取得" in sent or "前任調査の既知データ" in sent):
            continue
        out.append(sent)
    text = ""
    for sent in dict.fromkeys(out):
        text += ("" if not text else (" " if text.endswith("。") else " ／")) + sent
    return text


def is_cap_note(t: str) -> bool:
    return bool(re.search(r"上限", t)) and bool(re.search(r"検索|WebSearch", t))


def norm_category(cat: str | None, name: str = "") -> str:
    c = unicodedata.normalize("NFKC", f"{cat or ''}")
    if c in CATEGORY_ORDER:
        return c
    if "洋菓子" in c:
        return "洋菓子"
    if re.search(r"弁当|寿司|惣菜|飲食|レストラン|海鮮丼", c):
        return "惣菜・弁当・寿司"
    if "酒" in c:
        return "酒"
    if re.search(r"^茶|茶（|茶葉", c):
        return "茶"
    if re.search(r"麩|佃煮|食品|海産|珍味|調味|出汁|醤油|味噌", c):
        return "食品・海産物"
    if re.search(r"和菓子|豆菓子|飴|銘菓|菓子", c):
        return "和菓子"
    if re.search(r"工芸|雑貨", c):
        return "工芸・雑貨"
    return "その他"


def parse_days(text: str | None) -> int | None:
    if not text:
        return None
    t = unicodedata.normalize("NFKC", text)
    m = re.search(r"(\d+)\s*[〜~\-]\s*(\d+)\s*日", t)   # 「7〜10日」のような幅は短い方（保守的）を採る
    if m:
        return min(int(m.group(1)), int(m.group(2)))
    m = re.search(r"(\d+)\s*(?:か月|ヶ月|ケ月|カ月)", t)
    if m:
        return int(m.group(1)) * 30
    m = re.search(r"(\d+)\s*週間", t)
    if m:
        return int(m.group(1)) * 7
    m = re.search(r"(\d+)\s*日", t)
    if m:
        return int(m.group(1))
    return None


def ingredient_check(ingredients: str | None, agent_flags: list | None) -> tuple[str, list[str], list[str]]:
    """(記号, 指摘語, 参考語)。記号: ○=該当なし／×=該当あり／？=原材料表示を確認できていない"""
    if not ingredients:
        return "？", [], []
    hits: list[str] = []
    for label, pat in RED_PATTERNS:
        if re.search(pat, ingredients):
            hits.append(label)
    notes = [label for label, pat in NOTE_PATTERNS if re.search(pat, ingredients)]
    return ("×" if hits else "○"), hits, notes


def infer_wrap(p: dict) -> str | None:
    """individual（1個ずつ）／pack（小分けパック）／none（個包装なし）／None（不明）"""
    if p.get("wrap_level") in ("individual", "pack", "none"):
        return p["wrap_level"]
    w = p.get("individually_wrapped")
    note = unicodedata.normalize("NFKC", p.get("wrap_note") or "")
    if w is True:
        return "individual"
    if w is False:
        return "pack" if re.search(r"パック|小袋|袋|詰", note) and re.search(r"\d", note) else "none"
    return None


def to_int(v):
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return int(round(v))
    m = re.search(r"\d[\d,]*", str(v))
    return int(m.group(0).replace(",", "")) if m else None


def round_half_up(x: float) -> int:
    return int(x + 0.5)


def price_from_evidence(ev: str | None):
    """税込価格が null のとき、根拠文の「810円+税」「1,100円（税別）」から8%で換算する。(価格, 種別) か None"""
    if not ev:
        return None
    t = unicodedata.normalize("NFKC", ev)
    m = re.search(r"([\d,]{2,})\s*円\s*[（(]?\s*(?:\+税|税別|税抜)", t)
    if m:
        return round_half_up(int(m.group(1).replace(",", "")) * 1.08), "tax_excl"
    m = re.search(r"([\d,]{2,})\s*円", t)
    if m:
        return int(m.group(1).replace(",", "")), "ambiguous"
    return None


def qty_key(o: dict):
    q = o.get("qty")
    return (to_int(q), (o.get("unit") or "").strip()) if to_int(q) else (None, norm(o.get("qty_label")))


def load_group(path: pathlib.Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        print(f"[warn] {path.name}: 読み込めませんでした: {e}")
        return None


def short_popular(pp: dict) -> str:
    src = pp.get("source") or ""
    sec = pp.get("rank_or_section") or ""
    if "第2回" in src or "ランキング2026" in src:
        src = "総選挙2026"
    elif "2024" in src:
        src = "総選挙2024"
    elif "MRO" in src:
        src = "MRO"
    sec = sec.replace("総合ランキング（全年代）", "").strip()
    return f"{src} {sec}".strip()


def merge(raw_dir: pathlib.Path, out_dir: pathlib.Path, overrides_path: pathlib.Path | None) -> None:
    groups = {}
    for p in sorted(raw_dir.glob("G*.json")):
        d = load_group(p)
        if d:
            groups[d.get("group") or p.stem.split("_")[0]] = d

    shops: dict[str, dict] = {}
    products: list[dict] = []
    excluded: list[dict] = []
    unresolved: list[dict] = []
    warnings: list[str] = []

    def get_shop(s: dict, group: str) -> dict:
        k = shop_key(s.get("shop_short") or s.get("shop_name") or "")
        info = shops.setdefault(k, {"key": k})
        for fld_src, fld_dst in [("shop_name", "name"), ("shop_short", "short"), ("anto_page_url", "anto_page_url"),
                                 ("anto_phone", "phone"), ("anto_location", "location"), ("official_site", "official_site"),
                                 ("online_shop", "online_shop")]:
            v = s.get(fld_src)
            if v and (not info.get(fld_dst) or group == "G0"):
                info[fld_dst] = v
            elif fld_dst == "phone" and v and info.get(fld_dst) and v not in info[fld_dst]:
                info[fld_dst] = f"{info[fld_dst]}／{v}"   # 同じブランドの2店（本店とフレなど）
        if s.get("category") and not info.get("raw_category"):
            info["raw_category"] = s["category"]
        if "in_scope" in s:
            v = s["in_scope"]
            info["in_scope"] = "yes" if v is True else ("no" if v is False else ("maybe" if v is None else str(v)))
        if s.get("scope_note"):
            info["reason"] = clean_text(s["scope_note"])
        return info

    def gnum(g):
        m = re.search(r"\d+", g)
        return int(m.group(0)) if m else 999

    def absorb(existing: dict, prod: dict) -> None:
        """同じ商品の情報を統合する。同じ入数・同じ価格は先の値を優先し、食い違いは備考へ。別の入数は追加、空欄は埋める"""
        have_qty = {qty_key(o): o for o in existing["options"]}
        have_price = {o["price"] for o in existing["options"]}
        for o in prod["options"]:
            kk = qty_key(o)
            if kk in have_qty:
                a = have_qty[kk]
                if a["price"] != o["price"]:
                    if a["confidence"] == "A":
                        msg = f"検索結果では{o['label']}が{o['price']:,}円と出た（前任の確認値{a['price']:,}円と不一致・要確認）"
                    else:
                        msg = f"{o['label']}の価格が検索結果で2通り（{a['price']:,}円／{o['price']:,}円）・要確認"
                    existing["notes"] = join_notes(existing["notes"], msg)
            elif o["price"] in have_price:
                continue
            else:
                existing["options"].append(o)
                have_qty[kk] = o
                have_price.add(o["price"])
        for fld in ("description", "kind", "shelf_life_text", "seasonal", "page_url", "wrap_note", "allergens"):
            if not existing.get(fld) and prod.get(fld):
                existing[fld] = prod[fld]
        if existing.get("shelf_life_days") is None and prod.get("shelf_life_days") is not None:
            existing["shelf_life_days"] = prod["shelf_life_days"]
        elif (existing.get("shelf_life_days") and prod.get("shelf_life_days")
              and abs(existing["shelf_life_days"] - prod["shelf_life_days"]) > 0.2 * existing["shelf_life_days"]):
            existing["notes"] = join_notes(existing["notes"], f"日持ちが資料により異なる（{existing['shelf_life_days']}日／{prod['shelf_life_days']}日）")
        # 原材料: 空なら新しい方、両方あるときは長い（より完全な）方
        a_ing, b_ing = existing.get("ingredients"), prod.get("ingredients")
        if b_ing and (not a_ing or (existing["group"] != "G0" and len(b_ing) > len(a_ing) * 1.3)):
            existing["ingredients"] = b_ing
            existing["ingredient_check"], existing["ingredient_hits"], existing["ingredient_notes"] = ingredient_check(b_ing, None)
        if existing.get("wrap_level") is None and prod.get("wrap_level") is not None:
            existing["wrap_level"] = prod["wrap_level"]
            if prod.get("wrap_note"):
                existing["wrap_note"] = prod["wrap_note"]
        elif prod.get("wrap_level") is not None and prod["wrap_level"] != existing.get("wrap_level") and existing["group"] != "G0":
            existing["notes"] = join_notes(existing["notes"], f"個包装の有無が資料により異なる（{existing['wrap_level']}／{prod['wrap_level']}）")
        for sent in re.split(r"(?<=。)\s*|\s*／\s*", prod.get("notes") or ""):
            sent = sent.strip()
            if sent and sent not in existing["notes"]:
                existing["notes"] = join_notes(existing["notes"], sent)

    order = ["G0"] + [g for g in sorted(groups, key=gnum) if g not in ("G0", "G7")]
    by_shop_products: dict[str, list[dict]] = defaultdict(list)

    for g in order:
        d = groups.get(g)
        if not d:
            continue
        for s in d.get("shops", []):
            info = get_shop(s, g)
            k = info["key"]
            label = info.get("short") or info.get("name")
            for sk in s.get("skipped") or []:
                head, _, tail = str(sk).partition(":")
                excluded.append({"shop": label, "item": head.strip(), "reason": clean_text(tail) or "調査担当が対象外と判断", "level": "B"})
            for u in s.get("unresolved") or []:
                if not is_cap_note(str(u)) and not re.search(r"区画番号|anto_location", str(u)):
                    unresolved.append({"shop": label, "item": clean_text(str(u)), "group": g})
            for p in s.get("products") or []:
                opts = []
                for o in p.get("options") or []:
                    price = to_int(o.get("price_tax_incl", o.get("price")))
                    conf = o.get("confidence") or p.get("confidence") or "B"
                    conf = "B" if (g != "G0" and conf == "A") else conf
                    note_o = ""
                    if price is None:
                        got = price_from_evidence(o.get("evidence"))
                        if got:
                            price, kind = got
                            conf = "C"
                            note_o = ("税別表示の価格を8%で換算した参考値" if kind == "tax_excl" else "税込か税別かの記載が不明（表示の数字をそのまま採用）")
                    if price is None:
                        warnings.append(f"{label}／{p.get('product_name')}／{o.get('qty_label')}: 価格なし（スキップ）")
                        continue
                    opts.append({
                        "label": o.get("qty_label") or "", "qty": to_int(o.get("qty")), "unit": (o.get("unit") or "").strip(),
                        "price": price, "confidence": conf, "source_url": o.get("source_url"),
                        "evidence": o.get("evidence"), "note": note_o,
                    })
                if not opts:
                    continue
                ingredients = p.get("ingredients") or None
                mark, hits, notes_ = ingredient_check(ingredients, p.get("ingredient_flags"))
                days = to_int(p.get("shelf_life_days")) or parse_days(p.get("shelf_life_text"))
                pconf = p.get("confidence") or "B"
                pconf = "B" if (g != "G0" and pconf == "A") else pconf
                prod = {
                    "shop_key": k, "shop": label, "shop_full": info.get("name"), "phone": info.get("phone"),
                    "name": (p.get("product_name") or "").strip(), "kind": p.get("category") or "",
                    "description": p.get("description") or "", "options": opts,
                    "wrap_level": infer_wrap(p), "wrap_note": p.get("wrap_note") or "",
                    "shelf_life_days": days, "shelf_life_text": p.get("shelf_life_text") or "",
                    "ingredients": ingredients, "ingredient_check": mark, "ingredient_hits": hits, "ingredient_notes": notes_,
                    "allergens": p.get("allergens"), "seasonal": p.get("seasonal") or "",
                    "page_url": p.get("page_url"), "image_key": p.get("image_key"),
                    "confidence": pconf, "notes": clean_note(p.get("notes")), "group": g,
                }
                existing = None
                best = 0.0
                for q in by_shop_products[k]:
                    if same_product(q["name"], prod["name"]):
                        r = difflib.SequenceMatcher(None, base_name(q["name"]), base_name(prod["name"])).ratio()
                        if r >= best:
                            best, existing = r, q
                if existing is None:
                    by_shop_products[k].append(prod)
                    products.append(prod)
                else:
                    absorb(existing, prod)

    if "G0" in groups:
        excluded.extend(groups["G0"].get("excluded", []))

    # --- 店舗一覧（G7 の洗い出し結果を重ねる） ----------------------------------------------------------
    popular = []
    g7 = groups.get("G7")
    if g7:
        for s in g7.get("shops", []):
            nm = s.get("shop_name") or ""
            k = shop_key(nm)
            info = shops.setdefault(k, {"key": k, "name": nm, "short": nm})
            info.setdefault("name", nm)
            info.setdefault("short", nm)
            info["census_category"] = s.get("category")
            for src, dst in [("anto_page_url", "anto_page_url"), ("anto_phone", "phone"), ("anto_location", "location"),
                             ("area", "area"), ("shop_id", "shop_id"), ("source_url", "source_url")]:
                if s.get(src) and s.get(src) != "不明" and not info.get(dst):
                    info[dst] = s[src]
                elif dst == "phone" and s.get(src) and info.get(dst) and s[src] not in info[dst]:
                    info[dst] = f"{info[dst]}／{s[src]}"
            if not info.get("in_scope"):
                info["in_scope"] = s.get("in_scope") or "maybe"
                info["reason"] = clean_text(s.get("reason"))
            elif not info.get("reason") and s.get("reason"):
                info["reason"] = clean_text(s["reason"])
        popular = g7.get("popular", [])
        for u in g7.get("unresolved") or []:
            if not is_cap_note(str(u)):
                unresolved.append({"shop": "（店舗の洗い出し）", "item": clean_text(str(u)), "group": "G7"})
    for info in shops.values():
        info["category"] = norm_category(info.get("census_category") or info.get("raw_category"), info.get("name") or "")
        info.setdefault("area", "")
        info.setdefault("in_scope", "maybe")
    for info in shops.values():
        # Rinto（あんとの外のビル）の店は、あんとでは買えないので対象外にする
        if info.get("area") == "Rinto" and info["in_scope"] in ("yes", "maybe"):
            info["reason"] = ("Rintoの店（あんと外）。" + (info.get("reason") or "")).strip()
            info["in_scope"] = "no"
    for prod in products:
        prod["shop_category"] = shops[prod["shop_key"]]["category"]

    # --- 手動の上書き（食い違いの解消など。理由つきで data/overrides.json に残す） --------------------------
    applied = []
    if overrides_path and overrides_path.exists():
        ov = json.loads(overrides_path.read_text(encoding="utf-8"))
        for rule in ov.get("drop_options", []):
            for prod in products:
                if shop_key(rule["shop"]) == prod["shop_key"] and name_hit(rule["product"], prod["name"]):
                    before = len(prod["options"])
                    prod["options"] = [o for o in prod["options"]
                                       if not (norm(rule.get("label")) in norm(o["label"]) and (rule.get("price") in (None, o["price"])))]
                    if len(prod["options"]) != before:
                        prod["notes"] = (prod["notes"] + f" ／{rule['reason']}").strip(" ／")
                        applied.append(rule)
        for rule in ov.get("add_options", []):
            for prod in products:
                if shop_key(rule["shop"]) == prod["shop_key"] and name_hit(rule["product"], prod["name"]):
                    if not any(o["price"] == rule["price"] and norm(o["label"]) == norm(rule["label"]) for o in prod["options"]):
                        prod["options"].append({"label": rule["label"], "qty": rule.get("qty"), "unit": rule.get("unit", ""),
                                                "price": rule["price"], "confidence": rule.get("confidence", "B"),
                                                "source_url": rule.get("source_url"), "evidence": rule.get("evidence"),
                                                "note": rule.get("reason", "")})
                        applied.append(rule)
        for rule in ov.get("merge_products", []):
            keep = next((q for q in products if shop_key(rule["shop"]) == q["shop_key"] and name_hit(rule["keep"], q["name"])), None)
            gone = next((q for q in products if shop_key(rule["shop"]) == q["shop_key"] and q is not keep and name_hit(rule["absorb"], q["name"])), None)
            if keep and gone:
                absorb(keep, gone)
                products.remove(gone)
                applied.append(rule)
        for rule in ov.get("set_notes", []):
            for prod in products:
                if shop_key(rule["shop"]) == prod["shop_key"] and name_hit(rule["product"], prod["name"]):
                    prod["notes"] = rule["notes"]
        for rule in ov.get("append_notes", []):
            for prod in products:
                if shop_key(rule["shop"]) == prod["shop_key"] and name_hit(rule["product"], prod["name"]):
                    prod["notes"] = (prod["notes"] + f" ／{rule['note']}").strip(" ／")
        for rule in ov.get("drop_products", []):
            products[:] = [p for p in products if not (shop_key(rule["shop"]) == p["shop_key"] and name_hit(rule["product"], p["name"]))]
            excluded.append({"shop": rule["shop"], "item": rule["product"], "reason": rule["reason"], "level": "B"})
            applied.append(rule)

    # 人気の目安（店単位／商品単位）を商品へ付ける
    for prod in products:
        tags = []
        for pp in popular:
            if not pp.get("shop") and not pp.get("product"):
                continue
            shop_hit = bool(pp.get("shop")) and (shop_key(pp["shop"]) == prod["shop_key"] or norm(pp["shop"]) in norm(prod["shop_full"] or ""))
            if not shop_hit:
                continue
            if pp.get("product"):
                if same_product(pp["product"], prod["name"]):
                    tags.append("商品: " + short_popular(pp))
            else:
                tags.append("店: " + short_popular(pp))
        prod["popularity"] = list(dict.fromkeys(tags))

    # --- 並べ替えとID ---------------------------------------------------------------------------------------
    def cat_rank(c):
        return CATEGORY_ORDER.index(c) if c in CATEGORY_ORDER else len(CATEGORY_ORDER)

    shop_order = sorted(shops.values(), key=lambda s: (cat_rank(s["category"]), {"yes": 0, "maybe": 1, "no": 2}.get(s["in_scope"], 1),
                                                       s.get("short") or ""))
    shop_rank = {s["key"]: i for i, s in enumerate(shop_order)}
    products.sort(key=lambda p: (shop_rank.get(p["shop_key"], 999), p["name"]))
    for i, p in enumerate(products, 1):
        p["id"] = f"P{i:03d}"
        p["options"].sort(key=lambda o: (o["qty"] is None, o["qty"] or 0, o["price"]))
        for j, o in enumerate(p["options"], 1):
            o["id"] = f"{p['id']}-{j}"
        ranks = [CONF_RANK.get(o["confidence"], 1) for o in p["options"]]
        p["confidence_min"] = {3: "A", 2: "B", 1: "C"}[min(ranks)]
        p["confidence_max"] = {3: "A", 2: "B", 1: "C"}[max(ranks)]

    best: dict = {}
    for e in excluded:
        key = (shop_key(e.get("shop", "")), norm(e.get("item", "")))
        if key not in best or CONF_RANK.get(e.get("level", "B"), 1) > CONF_RANK.get(best[key].get("level", "B"), 1):
            best[key] = e
    excluded = sorted(best.values(), key=lambda e: -CONF_RANK.get(e.get("level", "B"), 1))

    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "products.json").write_text(json.dumps(
        {"generated_from": sorted(groups, key=lambda g: int(re.search(r"\d+", g).group(0)) if re.search(r"\d+", g) else 999), "products": products}, ensure_ascii=False, indent=1), encoding="utf-8")
    (out_dir / "shops.json").write_text(json.dumps(
        {"shops": shop_order, "popular": popular}, ensure_ascii=False, indent=1), encoding="utf-8")
    (out_dir / "excluded.json").write_text(json.dumps(
        {"excluded": excluded, "unresolved": unresolved}, ensure_ascii=False, indent=1), encoding="utf-8")

    # --- レポート ---------------------------------------------------------------------------------------------
    print(f"groups: {sorted(groups)}")
    print(f"shops: {len(shop_order)} / products: {len(products)} / options: {sum(len(p['options']) for p in products)}")
    per = defaultdict(lambda: [0, 0])
    for p in products:
        per[p["shop"]][0] += 1
        per[p["shop"]][1] += len(p["options"])
    for s, (n, m) in sorted(per.items(), key=lambda x: shop_rank.get(shop_key(x[0]), 999)):
        print(f"  {s}: 商品{n} / 入数{m}")
    seen_pairs = []
    for i, a in enumerate(products):
        for b in products[i + 1:]:
            if a["shop_key"] == b["shop_key"]:
                r = difflib.SequenceMatcher(None, base_name(a["name"]), base_name(b["name"])).ratio()
                if r >= 0.7:
                    seen_pairs.append((a["shop"], a["name"], b["name"], round(r, 2)))
    for sh, x, y, r in seen_pairs:
        print(f"[check] 似た名前の商品（別商品として扱っている）: {sh}｜{x}｜{y}｜類似度{r}")
    chk = defaultdict(int)
    for p in products:
        chk[p["ingredient_check"]] += 1
    print("原材料チェック:", dict(chk))
    print("対象判定:", dict(defaultdict(int, {k: sum(1 for s in shop_order if s['in_scope'] == k) for k in ('yes', 'maybe', 'no')})))
    if applied:
        print("上書きを適用:", len(applied))
    for w in warnings:
        print("[warn]", w)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--raw", default="data/raw")
    ap.add_argument("--out", default="data")
    ap.add_argument("--overrides", default="data/overrides.json")
    a = ap.parse_args()
    merge(pathlib.Path(a.raw), pathlib.Path(a.out), pathlib.Path(a.overrides))
