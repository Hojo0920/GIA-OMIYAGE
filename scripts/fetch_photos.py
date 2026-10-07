#!/usr/bin/env python3
"""商品ページから商品写真を取得し、スプレッドシート用の小さな画像（240px四方のJPEG）にする。

使い方:
  python3 scripts/fetch_photos.py --list-hosts               # 通信が必要なドメインの一覧（ネットワーク設定用）
  python3 scripts/fetch_photos.py --only-picks               # private/picks.json のおすすめ案・差し替え候補だけ
  python3 scripts/fetch_photos.py                            # 写真のない商品すべて（おすすめ案を先に）
  python3 scripts/fetch_photos.py --ids P001,P045 --force    # 指定した商品を取り直す
  python3 scripts/fetch_photos.py --probe URL --name 商品名   # 1ページ分の候補だけを表示する（保存しない）
  python3 scripts/fetch_photos.py --status                   # 写真の有無の一覧
  python3 scripts/fetch_photos.py --sheet                    # 取得済みの写真の一覧画像（商品名との対応の目視確認用）

出力（private/ は公開リポジトリに入れない。商品写真は各社の著作物）:
  private/images/<商品ID>.jpg     240px四方・白背景（build_xlsx.py が読む）
  private/photo_log.json          商品ごとの取得結果（元ページ・画像URL・方法・要確認かどうか）
  private/photo_overrides.json    手動の指定（任意）: {"P017": {"image_url": "..."}, "P038": {"page_url": "..."}, "P086": {"skip": true}}
  private/photo_sheets/*.png      --sheet の出力

写真の選び方: 商品ページの JSON-LD（Product）→ og:image → twitter:image → 本文中の画像の順に探す。
ロゴ・バナー・アイコンらしい画像は除き、ページの題名が商品名と合わないページでは og:image を使わない。
複数の商品が同じページを指している店（一覧ページ）は、画像の alt・周辺の文字と商品名を照合する。
どれも決め手がなければ写真を付けない（誤った写真より空欄のほうがよい）。
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import hashlib
import io
import json
import pathlib
import re
import sys
import threading
import time
import unicodedata
import urllib.parse
import urllib.robotparser
from collections import defaultdict
from html.parser import HTMLParser

import requests
from PIL import Image, ImageDraw, ImageFont, ImageOps

ROOT = pathlib.Path(__file__).resolve().parent.parent
UA = "Mozilla/5.0 (compatible; omiyage-photo-fetch/1.0)"
ROBOTS_TOKEN = "omiyage-photo-fetch"
OUT_PX = 240                 # 出力画像の一辺（build_xlsx.py の IMG_SRC_PX と同じ）
MIN_LONG, MIN_SHORT = 160, 40   # 長辺が160px未満、または短辺が40px未満の画像はアイコン・ボタンとみなして使わない
MAX_ASPECT = 3.0             # 縦横比がこれより極端な画像（バナー）は使わない
IMG_MAX_BYTES = 12 * 1024 * 1024
HTML_MAX_BYTES = 3 * 1024 * 1024
REVIEW_BELOW = 0.8           # この確からしさ未満は「要確認」にする

HARD_BAD_URL = re.compile(r"(logo|favicon|apple-touch|sprite|spacer|blank|no[-_]?image|no[-_]?photo|loading|/icons?/|icon[-_.]|btn[-_.]|button|arrow|banner|bnr[-_.]|\.(svg|gif|ico|webm|mp4)(\?|$))", re.I)
GENERIC_STEM = re.compile(r"^(ogp|og|og[-_]?image|ogimage|default|share|sns|site|main[-_]?visual)$", re.I)
LIST_URL = re.compile(r"(/list\.html|/category/|/categories/|/collections/[^/?#]+/?$|/sweets/?$|/products/?$|/items/?$)", re.I)
DETAIL_URL = re.compile(r"(/items?/\d|/products?/|/product-page/|/goods/|/shop/g/|/SHOP/\d|[?&](pid|product_id|products_id|item_id|goods_id|id)=|ProductDetail|/view/item/|/detail/|/fs/[^/]+/\d)", re.I)

_BRACKETS = re.compile(r"[（(\[［【〈<《].*?[）)\]］】〉>》]")
_KEEP = re.compile(r"[^0-9a-z々぀-ヿ㐀-鿿]")
_LEAD = re.compile(r"^[々぀-ヿ㐀-鿿]+")

_VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}
_CONTAINER_TAGS = {"li", "article", "figure", "tr", "dl", "td"}
_CONTAINER_CLS = re.compile(r"item|product|goods|card|entry|cell|box")
_CHROME_TAGS = {"header", "footer", "nav", "aside"}
_CHROME_CLS = re.compile(r"header|footer|navi?\b|menu|sidebar|banner|breadcrumb|related|recommend|ranking|sns|share|modal")
_GALLERY_CLS = re.compile(r"product|item|goods|main|gallery|photo|detail|slide|swiper|slick|zoom")


class FetchError(Exception):
    def __init__(self, kind: str, msg: str):
        super().__init__(msg)
        self.kind = kind
        self.msg = msg


# ---------------------------------------------------------------- 名前の照合 ---------------------------------------
def squash(s: str) -> str:
    return _KEEP.sub("", unicodedata.normalize("NFKC", s or "").lower())


def name_forms(name: str) -> tuple[list[str], list[str]]:
    """(限定的な形, 一般的な形) を返す。限定＝商品名そのもの、一般＝先頭の語や先頭の仮名漢字列も含む。"""
    no_br = _BRACKETS.sub("", name or "")
    head = re.split(r"[\s　／/]", no_br.strip())[0]
    full, core = squash(name), squash(no_br)
    lead = _LEAD.match(core)
    specific = [s for s in dict.fromkeys([full, core]) if len(s) >= 2]
    general = [s for s in dict.fromkeys(specific + [squash(head), lead.group(0) if lead else ""]) if len(s) >= 2]
    return specific, general


def bigrams(s: str) -> set[str]:
    return {s[i:i + 2] for i in range(len(s) - 1)}


def name_score(name: str, text: str, specific: bool = False) -> float:
    """商品名と文章の一致度（0〜1）。含まれていれば1、そうでなければ2文字連の重なりで0.9まで。"""
    t = squash(text)
    if not t:
        return 0.0
    spec, gen = name_forms(name)
    best = 0.0
    for v in (spec if specific else gen):
        if v in t:
            return 1.0
        bg = bigrams(v)
        if bg:
            best = max(best, 0.9 * len(bg & bigrams(t)) / len(bg))
    return best


# ---------------------------------------------------------------- HTML の読み取り ----------------------------------
def to_int(v) -> int | None:
    m = re.match(r"\s*(\d+)", v or "")
    return int(m.group(1)) if m else None


def best_from_srcset(s: str) -> str | None:
    best, best_w = None, -1.0
    for part in re.split(r",\s+", (s or "").strip()):
        bits = part.split()
        if not bits:
            continue
        w = 0.0
        if len(bits) > 1:
            m = re.match(r"([\d.]+)[wx]", bits[-1])
            w = float(m.group(1)) if m else 0.0
        if w >= best_w:
            best, best_w = bits[0], w
    return best


class PageParser(HTMLParser):
    """meta・link・題名・h1・JSON-LD・img を集める（木は作らない。壊れたHTMLでも止まらない）。"""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.metas: list[dict] = []
        self.links: list[dict] = []
        self.imgs: list[dict] = []
        self.jsonld: list[str] = []
        self.title = ""
        self.h1s: list[str] = []
        self.base: str | None = None
        self._stack: list[dict] = []
        self._cap: str | None = None
        self._buf: list[str] = []

    def handle_startendtag(self, tag, attrs):
        if tag in _VOID:
            self.handle_starttag(tag, attrs)

    def handle_starttag(self, tag, attrs):
        a = {k.lower(): (v or "") for k, v in attrs}
        if tag == "meta":
            self.metas.append(a)
        elif tag == "link":
            self.links.append(a)
        elif tag == "base":
            self.base = a.get("href") or self.base
        elif tag == "img":
            self._add_img(a)
        elif tag not in _VOID:
            self._stack.append({"tag": tag, "cls": f"{a.get('class', '')} {a.get('id', '')}".lower(), "attrs": a, "parts": []})
            if tag in ("title", "h1"):
                self._cap, self._buf = tag, []
            elif tag == "script" and "ld+json" in a.get("type", "").lower():
                self._cap, self._buf = "jsonld", []

    def handle_endtag(self, tag):
        if tag in _VOID or not any(n["tag"] == tag for n in self._stack):
            return
        while self._stack:
            node = self._stack.pop()
            text = " ".join(node["parts"])[:300]
            if self._stack:
                self._stack[-1]["parts"].append(text)
            if node["tag"] == tag:
                break
        if tag == "script" and self._cap == "jsonld":
            self.jsonld.append("".join(self._buf))
            self._cap = None
        elif self._cap == tag:
            text = " ".join("".join(self._buf).split())
            if tag == "title":
                self.title = self.title or text
            else:
                self.h1s.append(text)
            self._cap = None

    def handle_data(self, data):
        if self._cap:
            self._buf.append(data)
        if self._stack and self._stack[-1]["tag"] not in ("script", "style"):
            d = data.strip()
            if d:
                self._stack[-1]["parts"].append(d[:200])

    def _add_img(self, a: dict):
        srcs = [a[k] for k in ("src", "data-src", "data-original", "data-lazy-src", "data-lazy", "data-image") if a.get(k)]
        for k in ("srcset", "data-srcset"):
            b = best_from_srcset(a.get(k, ""))
            if b:
                srcs.insert(0, b)
        if not srcs:
            return
        chrome = any(n["tag"] in _CHROME_TAGS or _CHROME_CLS.search(n["cls"]) for n in self._stack)
        cls = " ".join(n["cls"] for n in self._stack[-8:]) + " " + f"{a.get('class', '')} {a.get('id', '')}".lower()
        a_title = next((n["attrs"].get("title") or n["attrs"].get("aria-label") or "" for n in reversed(self._stack)
                        if n["tag"] == "a" and (n["attrs"].get("title") or n["attrs"].get("aria-label"))), "")
        ctx = next((n for n in reversed(self._stack) if n["tag"] in _CONTAINER_TAGS or _CONTAINER_CLS.search(n["cls"])), None)
        self.imgs.append({"srcs": srcs, "alt": a.get("alt", ""), "title": a.get("title", "") or a_title,
                          "w": to_int(a.get("width")), "h": to_int(a.get("height")), "cls": cls, "chrome": chrome, "ctx": ctx})


def decode_html(content: bytes, header_enc: str | None) -> str:
    head = content[:4096].decode("ascii", errors="ignore")
    m = re.search(r"charset\s*=\s*[\"']?([\w\-]+)", head, re.I)
    for enc in (m.group(1) if m else None, header_enc if header_enc and header_enc.lower() != "iso-8859-1" else None, "utf-8", "cp932", "euc-jp"):
        if not enc:
            continue
        try:
            return content.decode(enc)
        except (LookupError, UnicodeDecodeError):
            continue
    return content.decode("utf-8", errors="replace")


def parse_page(html: str) -> PageParser:
    p = PageParser()
    try:
        p.feed(html)
        p.close()
    except Exception:  # 壊れたHTMLでも、そこまでに集めた分は使う
        pass
    return p


def meta_content(page: PageParser, *keys: str) -> str:
    for k in keys:
        for m in page.metas:
            if k in (m.get("property", "").lower(), m.get("name", "").lower(), m.get("itemprop", "").lower()) and m.get("content", "").strip():
                return m["content"].strip()
    return ""


def jsonld_product_images(page: PageParser) -> list[str]:
    out: list[str] = []

    def walk(node):
        if isinstance(node, list):
            for x in node:
                walk(x)
        elif isinstance(node, dict):
            t = node.get("@type")
            if any(isinstance(x, str) and x.lower() == "product" for x in (t if isinstance(t, list) else [t])):
                img = node.get("image")
                for x in (img if isinstance(img, list) else [img]):
                    if isinstance(x, str):
                        out.append(x)
                    elif isinstance(x, dict) and isinstance(x.get("url"), str):
                        out.append(x["url"])
            for v in node.values():
                if isinstance(v, (list, dict)):
                    walk(v)

    for raw in page.jsonld:
        try:
            walk(json.loads(raw))
        except ValueError:
            continue
    return out


# ---------------------------------------------------------------- 候補の選定 ---------------------------------------
def usable_url(u: str) -> bool:
    if not u or u.startswith(("data:", "javascript:", "#")):
        return False
    if HARD_BAD_URL.search(u):
        return False
    stem = pathlib.PurePosixPath(urllib.parse.urlparse(u).path).stem
    return not GENERIC_STEM.match(stem)


def plain_url(u: str) -> str:
    """クエリ・フラグメントを除いたURL（同じ画像かどうかの比較用）。"""
    pu = urllib.parse.urlparse(u)
    return f"{pu.scheme}://{pu.netloc}{pu.path}"


def ctx_text(img: dict) -> str:
    n = img.get("ctx")
    return " ".join(n["parts"])[:300] if n else ""


def first_usable(img: dict, absolutize) -> str | None:
    """img の src 候補から、遅延読み込みの仮画像（data:）やロゴ類を除いた最初のURL。"""
    for s in img["srcs"]:
        u = absolutize(s)
        if usable_url(u):
            return u
    return None


def listing_candidates(prod: dict, page: PageParser, group: list[dict], absolutize) -> list[dict]:
    """同じページを指す商品が複数ある（一覧ページ）とき、画像の alt・題名・周辺の文字で商品名と照合する。"""
    out = []
    for img in page.imgs:
        if img["chrome"]:
            continue
        text = f"{img['alt']} {img['title']} {ctx_text(img)}"
        mine = name_score(prod["name"], text, specific=True)
        others = max((name_score(g["name"], text, specific=True) for g in group if g["id"] != prod["id"]), default=0.0)
        u = first_usable(img, absolutize)
        if u and mine >= 0.8 and mine - others >= 0.1:
            out.append({"url": u, "method": "listing", "conf": 0.7, "score": mine})
    out.sort(key=lambda c: -c["score"])
    return out[:2]


def gallery_candidates(prod: dict, page: PageParser, absolutize) -> list[dict]:
    """og:image が使えないページで、本文中の画像から商品写真らしいものを選ぶ（確からしさは低め）。"""
    scored = []
    for img in page.imgs:
        if img["chrome"]:
            continue
        u = first_usable(img, absolutize)
        if not u:
            continue
        s = 0.0
        if name_score(prod["name"], f"{img['alt']} {img['title']}") >= 0.7:
            s += 0.4
        if (img["w"] or 0) >= 200 or (img["h"] or 0) >= 200:
            s += 0.2
        if _GALLERY_CLS.search(img["cls"]):
            s += 0.2
        if re.search(r"(item|product|goods|upload|/img/|/images?/)", u, re.I):
            s += 0.1
        if s >= 0.4:
            scored.append((s, u))
    scored.sort(key=lambda t: -t[0])
    return [{"url": u, "method": "gallery", "conf": 0.5, "score": s} for s, u in scored[:3]]


def select_candidates(prod: dict, page: PageParser, page_url: str, group: list[dict]) -> tuple[list[dict], dict]:
    base = page.base or page_url

    def absolutize(u: str) -> str:
        return urllib.parse.urljoin(base, u.strip())

    name = prod["name"]
    titles = [meta_content(page, "og:title"), page.title, *page.h1s]
    title_score = max((name_score(name, t) for t in titles if t), default=0.0)
    ld = jsonld_product_images(page)
    og_type = meta_content(page, "og:type").lower()
    strong = title_score >= 0.7
    listy = bool(LIST_URL.search(page_url))
    product_like = strong or (bool(DETAIL_URL.search(page_url)) and not listy) or og_type.startswith("product")
    info = {"title_score": round(title_score, 2), "product_like": product_like, "page_title": (page.title or "")[:60]}

    cands: list[dict] = []
    if len(group) > 1:
        cands += listing_candidates(prod, page, group, absolutize)
    elif product_like:
        k = 1.0 if (strong and not listy) else 0.8
        for u in ld:
            cands.append({"url": absolutize(u), "method": "jsonld", "conf": 0.95 * k})
        for key, method, conf in (("og:image", "og", 0.9), ("og:image:secure_url", "og", 0.9), ("twitter:image", "twitter", 0.85),
                                  ("image", "meta", 0.75)):
            v = meta_content(page, key)
            if v:
                cands.append({"url": absolutize(v), "method": method, "conf": conf * k})
        for ln in page.links:
            if ln.get("rel", "").lower() == "image_src" and ln.get("href"):
                cands.append({"url": absolutize(ln["href"]), "method": "link", "conf": 0.8 * k})
        cands += gallery_candidates(prod, page, absolutize)
    else:
        cands += listing_candidates(prod, page, [prod], absolutize)
    seen, out = set(), []
    for c in cands:
        if c["url"] not in seen and usable_url(c["url"]):
            seen.add(c["url"])
            out.append(c)
    return out, info


# ---------------------------------------------------------------- 通信 ---------------------------------------------
class Gate:
    """同じホストへの通信の間隔をあける（礼儀）。429（アクセス過多）が返ったホストは間隔を広げる。"""

    def __init__(self, delay: float):
        self.delay = delay
        self._guard = threading.Lock()
        self._locks: dict[str, threading.Lock] = defaultdict(threading.Lock)
        self._last: dict[str, float] = {}
        self._delay_for: dict[str, float] = {}
        self.slow_base = 1.0

    def wait(self, host: str):
        with self._guard:
            lock = self._locks[host]
        with lock:
            gap = self._last.get(host, 0.0) + self._delay_for.get(host, self.delay) - time.monotonic()
            if gap > 0:
                time.sleep(gap)
            self._last[host] = time.monotonic()

    def slow_down(self, host: str):
        with self._guard:
            self._delay_for[host] = min(max(self._delay_for.get(host, self.delay), self.slow_base) * 2, 10.0)


class Net:
    def __init__(self, delay: float):
        self.gate = Gate(delay)
        self.backoff = 5.0        # 429 のときの待ち秒（Retry-After があればそれ）
        self.blocked: set[str] = set()
        self._tl = threading.local()
        self._robots: dict[str, urllib.robotparser.RobotFileParser | None] = {}
        self._rlock = threading.Lock()
        self._site: dict[str, set[str]] = {}

    def session(self) -> requests.Session:
        if not hasattr(self._tl, "s"):
            s = requests.Session()
            s.headers.update({"User-Agent": UA, "Accept-Language": "ja,en;q=0.7"})
            self._tl.s = s
        return self._tl.s

    def get(self, url: str, referer: str | None, accept: str, max_bytes: int) -> tuple[bytes, requests.Response]:
        host = urllib.parse.urlparse(url).netloc
        last: FetchError | None = None
        for attempt in range(3):
            self.gate.wait(host)
            pause = 0.0
            try:
                headers = {"Accept": accept}
                if referer:
                    headers["Referer"] = referer
                with self.session().get(url, headers=headers, timeout=(10, 30), stream=True, allow_redirects=True) as r:
                    if r.status_code == 429 and attempt < 2:
                        last = FetchError("http", "HTTP 429")
                        self.gate.slow_down(host)
                        ra = r.headers.get("Retry-After", "")
                        pause = min(float(ra), 20.0) if ra.isdigit() else self.backoff
                    elif r.status_code in (500, 502, 503, 504) and attempt == 0:
                        last = FetchError("http", f"HTTP {r.status_code}")
                        pause = 2.0
                    elif r.status_code != 200:
                        raise FetchError("http", f"HTTP {r.status_code}")
                    else:
                        buf = bytearray()
                        for chunk in r.iter_content(65536):
                            buf += chunk
                            if len(buf) > max_bytes:
                                raise FetchError("size", "大きすぎる")
                        return bytes(buf), r
                time.sleep(pause)   # 一時的なエラー・アクセス過多は待ってやり直す
            except requests.exceptions.ProxyError:
                self.blocked.add(host)
                raise FetchError("egress", f"ネットワーク設定で接続を拒否された: {host}")
            except requests.exceptions.SSLError:
                raise FetchError("ssl", f"証明書エラー: {host}")
            except requests.exceptions.Timeout:
                last = FetchError("timeout", f"応答なし: {host}")
            except requests.exceptions.RequestException as e:
                last = FetchError("net", f"{type(e).__name__}: {str(e)[:80]}")
        raise last or FetchError("net", "通信に失敗")

    def site_images(self, page_url: str) -> set[str]:
        """そのサイトのトップページの og:image / twitter:image（＝店共通の画像）。商品ページの og:image がこれと同じなら商品写真ではない。"""
        pu = urllib.parse.urlparse(page_url)
        origin = f"{pu.scheme}://{pu.netloc}"
        with self._rlock:
            if origin in self._site:
                return self._site[origin]
        imgs: set[str] = set()
        try:
            body, r = self.get(origin + "/", None, "text/html,application/xhtml+xml;q=0.9,*/*;q=0.5", HTML_MAX_BYTES)
            page = parse_page(decode_html(body, r.encoding))
            for key in ("og:image", "og:image:secure_url", "twitter:image"):
                v = meta_content(page, key)
                if v:
                    imgs.add(plain_url(urllib.parse.urljoin(page.base or r.url or origin, v)))
        except FetchError as e:
            if e.kind == "egress":
                raise
        with self._rlock:
            self._site[origin] = imgs
        return imgs

    def robots_ok(self, url: str) -> bool:
        pu = urllib.parse.urlparse(url)
        origin = f"{pu.scheme}://{pu.netloc}"
        with self._rlock:
            known = origin in self._robots
        if not known:
            rp: urllib.robotparser.RobotFileParser | None = None
            try:
                body, r = self.get(origin + "/robots.txt", None, "text/plain,*/*;q=0.5", 512 * 1024)
                rp = urllib.robotparser.RobotFileParser()
                rp.parse(decode_html(body, r.encoding).splitlines())
            except FetchError as e:
                if e.kind == "egress":
                    raise
                rp = None  # robots.txt がない・読めない店は制限なしとみなす
            with self._rlock:
                self._robots[origin] = rp
        rp = self._robots[origin]
        return rp is None or rp.can_fetch(ROBOTS_TOKEN, url)


def looks_like_icon(im: Image.Image) -> bool:
    """線画のアイコン（ハート・握手・買い物かごなど）は白地がほとんどで、色数が少ないか、白黒（彩度がない）だけでできている。
    商品写真は陰影・JPEGの階調で色数が多く、どこかに色（彩度）がある。"""
    small = im.convert("RGB").resize((64, 64), Image.LANCZOS)
    n_colors = len(small.getcolors(maxcolors=5000) or range(5000))
    data = small.tobytes()
    white = colorful = 0
    for i in range(0, len(data), 3):
        r, g, b = data[i], data[i + 1], data[i + 2]
        white += r > 240 and g > 240 and b > 240
        colorful += max(r, g, b) - min(r, g, b) > 40
    total = len(data) // 3
    return white / total >= 0.8 and (n_colors <= 250 or colorful / total < 0.005)


def to_thumb(raw: bytes, relax: bool = False) -> tuple[bytes, tuple[int, int]]:
    """画像を OUT_PX 四方・白背景のJPEGにする。小さすぎる・縦横比が極端・アイコンらしい画像は FetchError。
    relax=True（手動で選んだ画像）のときは、これらの判定を省く。"""
    try:
        im = Image.open(io.BytesIO(raw))
        im.load()
    except Exception:
        raise FetchError("image", "画像として読めない")
    im = ImageOps.exif_transpose(im)
    w, h = im.size
    if not relax:
        if max(w, h) < MIN_LONG or min(w, h) < MIN_SHORT:
            raise FetchError("image", f"小さすぎる（{w}x{h}）")
        if max(w, h) / min(w, h) > MAX_ASPECT:
            raise FetchError("image", f"縦横比が極端（{w}x{h}）")
    if im.mode in ("RGBA", "LA", "P"):
        im = im.convert("RGBA")
        bg = Image.new("RGB", im.size, "white")
        bg.paste(im, mask=im.split()[-1])
        im = bg
    else:
        im = im.convert("RGB")
    if not relax and looks_like_icon(im):
        raise FetchError("image", "アイコンのような画像（色数が少なく白地）")
    im = ImageOps.contain(im, (OUT_PX, OUT_PX), Image.LANCZOS)
    canvas = Image.new("RGB", (OUT_PX, OUT_PX), "white")
    canvas.paste(im, ((OUT_PX - im.width) // 2, (OUT_PX - im.height) // 2))
    out = io.BytesIO()
    canvas.save(out, "JPEG", quality=88, optimize=True)
    return out.getvalue(), (w, h)


# ---------------------------------------------------------------- 商品ごとの処理 ----------------------------------
def page_urls_of(prod: dict) -> list[str]:
    urls = [prod.get("page_url")] + [o.get("source_url") for o in prod["options"]]
    out = []
    for u in urls:
        if u and u.startswith("http") and u not in out:
            out.append(u)
    return out[:3]


def process(prod: dict, ctx: dict) -> dict:
    net: Net = ctx["net"]
    img_dir: pathlib.Path = ctx["img_dir"]
    rec = {"id": prod["id"], "shop": prod["shop"], "name": prod["name"], "status": "fail", "reason": "", "page_url": None,
           "image_url": None, "method": None, "conf": 0.0, "review": False, "title_score": None, "size": None}
    ov = ctx["overrides"].get(prod["id"], {})
    if ov.get("approx"):
        rec["approx"] = ov["approx"]       # 同じ商品ではなく近い商品・シリーズの写真であることのメモ（build_xlsx.py が備考に出す）
    if ov.get("skip"):
        rec.update(status="skip", reason=ov.get("reason", "手動でスキップ"))
        return rec
    pages = [ov["page_url"]] if ov.get("page_url") else page_urls_of(prod)
    if not pages and not ov.get("image_url") and not ov.get("file"):
        rec["reason"] = "商品ページのURLがない"
        return rec

    def save(raw: bytes, url: str, method: str, conf: float, page_url: str | None, info: dict | None = None, relax: bool = False) -> dict:
        data, size = to_thumb(raw, relax)
        (img_dir / f"{prod['id']}.jpg").write_bytes(data)
        rec.update(status="ok", reason="", image_url=url, method=method, conf=round(conf, 2), page_url=page_url, size=list(size),
                   review=conf < REVIEW_BELOW, title_score=(info or {}).get("title_score"))
        return rec

    try:
        if ov.get("file"):
            return save(pathlib.Path(ov["file"]).expanduser().read_bytes(), ov["file"], "manual", 1.0, None, relax=True)
        if ov.get("image_url"):
            raw, _ = net.get(ov["image_url"], pages[0] if pages else None, "image/jpeg,image/png,image/webp,image/*;q=0.8", IMG_MAX_BYTES)
            return save(raw, ov["image_url"], "manual", 1.0, pages[0] if pages else None, relax=True)
    except FetchError as e:
        rec["reason"] = f"手動指定の画像が使えない: {e.msg}"
        return rec

    reasons: list[str] = []
    for page_url in pages:
        try:
            if not net.robots_ok(page_url):
                reasons.append("robots.txt で自動取得が禁止されている")
                continue
            body, r = net.get(page_url, None, "text/html,application/xhtml+xml;q=0.9,*/*;q=0.5", HTML_MAX_BYTES)
        except FetchError as e:
            reasons.append(e.msg)
            continue
        page = parse_page(decode_html(body, r.encoding))
        group = ctx["by_page"].get(page_url) or [prod]
        cands, info = select_candidates(prod, page, r.url or page_url, group)
        if urllib.parse.urlparse(page_url).path.strip("/") or urllib.parse.urlparse(page_url).query:   # トップページ自体は比較しない
            try:
                common = net.site_images(page_url)
            except FetchError:
                common = set()
            dropped = [c for c in cands if c["method"] in ("og", "twitter", "meta", "link") and plain_url(c["url"]) in common]
            if dropped:
                reasons.append("og:image が店のトップページと同じ共通画像のため不採用")
            cands = [c for c in cands if c not in dropped]
        if not cands:
            reasons.append(f"写真の候補がない（題名「{info['page_title']}」、商品名との一致 {info['title_score']}）")
            continue
        for c in cands[:4]:
            try:
                raw, _ = net.get(c["url"], page_url, "image/jpeg,image/png,image/webp,image/*;q=0.8", IMG_MAX_BYTES)
                return save(raw, c["url"], c["method"], c["conf"], page_url, info)
            except FetchError as e:
                reasons.append(f"{c['method']}の画像が使えない: {e.msg}")
    rec["reason"] = " / ".join(dict.fromkeys(reasons))[:300] or "取得できなかった"
    if any("拒否された" in x for x in reasons):
        rec["reason"] = next(x for x in reasons if "拒否された" in x)
    return rec


# ---------------------------------------------------------------- 入出力 -------------------------------------------
def load_products(data_dir: pathlib.Path) -> list[dict]:
    return json.loads((data_dir / "products.json").read_text(encoding="utf-8"))["products"]


def nz(s: str) -> str:
    return unicodedata.normalize("NFKC", s or "").replace(" ", "").replace("　", "").lower()


def pick_ids(products: list[dict], picks: dict | None) -> list[str]:
    """picks.json のおすすめ案・差し替え候補に当たる商品IDを、案の順に返す（build_xlsx.py の照合と同じ考え方）。"""
    if not picks:
        return []
    ids: list[str] = []
    for item in list(picks.get("picks", [])) + list(picks.get("alternatives", [])):
        m = item["match"]
        for p in products:
            if not ((nz(m["shop"]) in nz(p["shop"]) or nz(p["shop"]) in nz(m["shop"])) and nz(m["product"]) in nz(p["name"])):
                continue
            if m.get("label") and not any(nz(m["label"]) in nz(o["label"]) for o in p["options"]):
                continue
            if p["id"] not in ids:
                ids.append(p["id"])
            break
    return ids


def has_photo(p: dict, img_dir: pathlib.Path) -> bool:
    if p.get("image_key") and (img_dir / f"{p['image_key']}.jpg").exists():
        return True
    return (img_dir / f"{p['id']}.jpg").exists()


def list_hosts(products: list[dict]) -> list[str]:
    hosts = {urllib.parse.urlparse(u).netloc for p in products for u in page_urls_of(p)}
    return sorted(h for h in hosts if h)


def find_cjk_font(size: int):
    for f in ("/System/Library/Fonts/ヒラギノ角ゴシック W3.ttc", "/System/Library/Fonts/Hiragino Sans GB.ttc",
              "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc", "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
              "/usr/share/fonts/opentype/unifont/unifont_jp.otf"):
        if pathlib.Path(f).exists():
            try:
                return ImageFont.truetype(f, size)
            except OSError:
                continue
    return ImageFont.load_default()


def make_sheets(products: list[dict], img_dir: pathlib.Path, out_dir: pathlib.Path, per_sheet: int = 20, cols: int = 5) -> list[pathlib.Path]:
    """取得した写真を、商品ID・店名・商品名つきで並べた画像にする（商品名と写真が合っているかを目で確かめる用）。"""
    out_dir.mkdir(parents=True, exist_ok=True)
    items = [p for p in products if (img_dir / f"{p['id']}.jpg").exists()]
    font, small = find_cjk_font(13), find_cjk_font(11)
    cell_w, cell_h = OUT_PX, OUT_PX + 44
    paths = []
    for n in range(0, len(items), per_sheet):
        chunk = items[n:n + per_sheet]
        rows = (len(chunk) + cols - 1) // cols
        sheet = Image.new("RGB", (cols * cell_w, rows * cell_h), "white")
        d = ImageDraw.Draw(sheet)
        for i, p in enumerate(chunk):
            x, y = (i % cols) * cell_w, (i // cols) * cell_h
            sheet.paste(Image.open(img_dir / f"{p['id']}.jpg"), (x, y))
            d.rectangle([x, y, x + cell_w - 1, y + cell_h - 1], outline=(200, 200, 200))
            d.text((x + 4, y + OUT_PX + 2), f"{p['id']}  {p['shop']}"[:24], fill=(0, 0, 0), font=font)
            d.text((x + 4, y + OUT_PX + 22), p["name"][:20], fill=(60, 60, 60), font=small)
        path = out_dir / f"sheet_{n // per_sheet + 1:02d}.png"
        sheet.save(path)
        paths.append(path)
    return paths


def load_json(path: pathlib.Path, default):
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default


def main() -> int:
    ap = argparse.ArgumentParser(description="商品ページから商品写真を取得して private/images/<商品ID>.jpg に保存する")
    ap.add_argument("--data", default=str(ROOT / "data"))
    ap.add_argument("--private", default=str(ROOT / "private"))
    ap.add_argument("--ids", help="商品IDをカンマ区切りで指定（例: P001,P045）")
    ap.add_argument("--only-picks", action="store_true", help="private/picks.json のおすすめ案・差し替え候補だけ")
    ap.add_argument("--force", action="store_true", help="すでに写真がある商品も取り直す（手動の写真 image_key は除く）")
    ap.add_argument("--limit", type=int, help="処理する商品数の上限")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--delay", type=float, default=1.0, help="同じホストへの通信の最小間隔（秒）")
    ap.add_argument("--list-hosts", action="store_true", help="通信が必要なドメインを1行ずつ表示して終わる")
    ap.add_argument("--status", action="store_true", help="写真の有無を一覧して終わる")
    ap.add_argument("--sheet", action="store_true", help="取得済みの写真の一覧画像を作って終わる")
    ap.add_argument("--probe", metavar="URL", help="1ページだけ読んで、写真の候補を表示する（保存しない）")
    ap.add_argument("--name", help="--probe で照合する商品名")
    a = ap.parse_args()

    data_dir, priv = pathlib.Path(a.data), pathlib.Path(a.private)
    img_dir = priv / "images"
    products = load_products(data_dir)
    picks = load_json(priv / "picks.json", None)

    if a.list_hosts:
        print("\n".join(list_hosts(products)))
        return 0

    if a.probe:
        net = Net(a.delay)
        try:
            body, r = net.get(a.probe, None, "text/html,application/xhtml+xml;q=0.9,*/*;q=0.5", HTML_MAX_BYTES)
        except FetchError as e:
            print(f"ページを取得できない: {e.msg}")
            return 2
        page = parse_page(decode_html(body, r.encoding))
        prod = {"id": "PROBE", "name": a.name or "", "shop": "", "options": []}
        cands, info = select_candidates(prod, page, r.url or a.probe, [prod])
        print(json.dumps({"info": info, "og:image": meta_content(page, "og:image"), "jsonld": jsonld_product_images(page)[:3],
                          "imgs": len(page.imgs), "candidates": cands}, ensure_ascii=False, indent=1))
        return 0

    if a.status:
        missing = [p for p in products if not has_photo(p, img_dir)]
        print(f"写真あり {len(products) - len(missing)} / 全 {len(products)} 商品")
        for p in missing:
            print(f"  {p['id']}  {p['shop']} | {p['name']} | {p.get('page_url') or '(商品ページなし)'}")
        return 0

    if a.sheet:
        paths = make_sheets(products, img_dir, priv / "photo_sheets")
        print(f"{len(paths)} 枚: {priv / 'photo_sheets'}")
        return 0

    img_dir.mkdir(parents=True, exist_ok=True)
    overrides = load_json(priv / "photo_overrides.json", {})
    wanted = [x.strip() for x in a.ids.split(",")] if a.ids else None
    order = pick_ids(products, picks)
    if a.only_picks:
        wanted = order
    todo = []
    for p in products:
        if wanted is not None and p["id"] not in wanted:
            continue
        if p.get("image_key") and (img_dir / f"{p['image_key']}.jpg").exists():
            continue                      # 前任が公式ページで確認した手動の写真は取り直さない
        if (img_dir / f"{p['id']}.jpg").exists() and not a.force:
            continue
        todo.append(p)
    todo.sort(key=lambda p: (order.index(p["id"]) if p["id"] in order else len(order), p["id"]))
    if a.limit:
        todo = todo[:a.limit]
    if not todo:
        print("取得する商品がありません（--status で状況、--force で取り直し）")
        return 0

    by_page: dict[str, list[dict]] = defaultdict(list)
    for p in products:
        if p.get("page_url"):
            by_page[p["page_url"]].append(p)
    net = Net(a.delay)
    ctx = {"net": net, "img_dir": img_dir, "overrides": overrides, "by_page": by_page}
    log_path = priv / "photo_log.json"
    log = load_json(log_path, {"items": {}})
    print(f"{len(todo)} 商品の写真を取得します（同時 {a.workers}、同じホストへは {a.delay} 秒おき）")
    done = 0
    with cf.ThreadPoolExecutor(max_workers=a.workers) as ex:
        futs = {ex.submit(process, p, ctx): p for p in todo}
        for fut in cf.as_completed(futs):
            p = futs[fut]
            try:
                rec = fut.result()
            except Exception as e:  # 1商品の失敗で全体を止めない
                rec = {"id": p["id"], "shop": p["shop"], "name": p["name"], "status": "fail", "reason": f"想定外のエラー: {type(e).__name__}: {e}"}
            rec["ts"] = time.strftime("%Y-%m-%d %H:%M:%S")
            log["items"][rec["id"]] = rec
            done += 1
            mark = {"ok": "OK  ", "skip": "SKIP", "fail": "NG  "}[rec["status"]]
            extra = f"{rec.get('method')} {rec.get('conf')}{' 要確認' if rec.get('review') else ''}" if rec["status"] == "ok" else rec["reason"]
            print(f"[{done:3d}/{len(todo)}] {mark} {rec['id']} {rec['shop']} | {rec['name'][:22]} | {extra}", flush=True)
            if done % 20 == 0:
                log_path.write_text(json.dumps(log, ensure_ascii=False, indent=1), encoding="utf-8")
    log_path.write_text(json.dumps(log, ensure_ascii=False, indent=1), encoding="utf-8")

    # 同じ写真が別のページの商品に使われていたら、店の共通画像の疑い
    by_hash: dict[str, list[str]] = defaultdict(list)
    for p in products:
        f = img_dir / f"{p['id']}.jpg"
        if f.exists() and not p.get("image_key") and log["items"].get(p["id"], {}).get("method") != "manual":
            by_hash[hashlib.sha1(f.read_bytes()).hexdigest()].append(p["id"])
    for ids in by_hash.values():
        pages = {next(q["page_url"] for q in products if q["id"] == i) for i in ids}
        if len(ids) >= 3 and len(pages) >= 3:
            for i in ids:
                (img_dir / f"{i}.jpg").unlink(missing_ok=True)
                log["items"].setdefault(i, {}).update(status="fail", reason="複数の商品ページで同じ画像（店の共通画像の疑い）のため不採用")
            print(f"共通画像の疑いで不採用: {', '.join(ids)}")
        elif len(ids) >= 2 and len(pages) >= 2:
            print(f"同じ写真が別ページの商品に使われています（要確認）: {', '.join(ids)}")
    log_path.write_text(json.dumps(log, ensure_ascii=False, indent=1), encoding="utf-8")

    recs = [log["items"][p["id"]] for p in todo if p["id"] in log["items"]]
    ok = [r for r in recs if r.get("status") == "ok"]
    ng = [r for r in recs if r.get("status") == "fail"]
    print(f"\n結果: 取得 {len(ok)} / 失敗 {len(ng)} / スキップ {len(recs) - len(ok) - len(ng)}（全 {len(recs)}）")
    meth: dict[str, int] = defaultdict(int)
    for r in ok:
        meth[r["method"]] += 1
    print("方法別:", "、".join(f"{k} {v}" for k, v in sorted(meth.items())))
    rev = [r["id"] for r in ok if r.get("review")]
    if rev:
        print(f"要確認（一覧画像で目視）: {', '.join(rev)}")
    if net.blocked:
        print("\nネットワーク設定で接続を拒否されたホスト（許可が必要）:\n  " + "\n  ".join(sorted(net.blocked)))
    why: dict[str, list[str]] = defaultdict(list)
    for r in ng:
        why[r["reason"][:60]].append(r["id"])
    for k, v in sorted(why.items(), key=lambda kv: -len(kv[1])):
        print(f"  失敗 {len(v):3d}件: {k}  [{', '.join(v[:6])}{'…' if len(v) > 6 else ''}]")
    return 0 if ok or not ng else 2


if __name__ == "__main__":
    try:
        sys.exit(main())
    except BrokenPipeError:      # `| head` などで出力が途中で閉じられた
        sys.exit(0)
