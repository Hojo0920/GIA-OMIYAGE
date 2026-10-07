"""scripts/fetch_photos.py の試験。外部には出ず、手元のHTTPサーバに置いた疑似ページで確かめる。

  python3 -m unittest discover -s tests -v
"""
import http.server
import io
import json
import pathlib
import sys
import tempfile
import threading
import unittest
from unittest import mock

import requests
from PIL import Image

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "scripts"))
import fetch_photos as fp  # noqa: E402


def jpeg(w=400, h=400, color=(200, 80, 60)) -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (w, h), color).save(out, "JPEG")
    return out.getvalue()


def icon_png(w=400, h=400) -> bytes:
    """白地に黒い図形だけのアイコン風の画像。"""
    out = io.BytesIO()
    im = Image.new("RGB", (w, h), "white")
    im.paste((0, 0, 0), (150, 150, 250, 250))
    im.save(out, "PNG")
    return out.getvalue()


def icon_antialiased_png() -> bytes:
    """アンチエイリアスのかかった白黒の線画（色数は多いが彩度がない）。"""
    from PIL import ImageDraw
    big = Image.new("RGB", (1600, 1600), "white")
    d = ImageDraw.Draw(big)
    d.ellipse((300, 300, 1300, 1300), outline="black", width=60)
    d.line((300, 800, 1300, 800), fill="black", width=50)
    out = io.BytesIO()
    big.resize((400, 400), Image.LANCZOS).save(out, "PNG")
    return out.getvalue()


def png_rgba(w=400, h=400) -> bytes:
    out = io.BytesIO()
    im = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    im.paste((10, 120, 200, 255), (100, 100, 300, 300))
    im.save(out, "PNG")
    return out.getvalue()


def page(head: str = "", body: str = "", title: str = "") -> bytes:
    return f"<!doctype html><html><head><meta charset='utf-8'><title>{title}</title>{head}</head><body>{body}</body></html>".encode()


class Handler(http.server.BaseHTTPRequestHandler):
    routes: dict = {}

    def do_GET(self):
        r = self.routes.get(self.path.split("#")[0])
        if callable(r):
            r = r()
        if r is None:
            self.send_response(404)
            self.end_headers()
            return
        status, ctype, body = r
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


def serve(routes: dict):
    """試験ごとに別のサーバを立てる（トップページの og:image など、他の試験に影響する経路を分けたいとき）。"""
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), type("H", (Handler,), {"routes": routes}))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


class FetchPhotosTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        h, html = "text/html; charset=utf-8", "text/html"
        R = Handler.routes
        R["/img/p1.jpg"] = (200, "image/jpeg", jpeg())
        R["/img/p2.jpg"] = (200, "image/jpeg", jpeg(color=(10, 200, 10)))
        R["/img/p3.jpg"] = (200, "image/jpeg", jpeg(500, 300))
        R["/img/a.jpg"] = (200, "image/jpeg", jpeg(color=(255, 0, 0)))
        R["/img/b.jpg"] = (200, "image/jpeg", jpeg(color=(0, 0, 255)))
        R["/img/tiny.jpg"] = (200, "image/jpeg", jpeg(60, 60))
        R["/img/wide.jpg"] = (200, "image/jpeg", jpeg(1200, 200))
        R["/img/logo.png"] = (200, "image/png", png_rgba())
        R["/img/heart.png"] = (200, "image/png", icon_png())
        R["/img/hands.png"] = (200, "image/png", icon_antialiased_png())
        R["/img/ogp.png"] = (200, "image/png", png_rgba())
        R["/img/alpha.png"] = (200, "image/png", png_rgba())
        R["/robots.txt"] = (200, "text/plain", b"User-agent: *\nDisallow: /secret/\n")

        # og:image が商品写真のページ
        R["/items/1"] = (200, h, page(f'<meta property="og:image" content="{cls.base}/img/p1.jpg">', "<h1>加賀八幡 起上もなか</h1>", "加賀八幡 起上もなか | うら田"))
        # og:image がロゴ、JSON-LD に商品写真
        ld = json.dumps({"@context": "https://schema.org", "@type": "Product", "name": "かいちん", "image": ["/img/p2.jpg"]})
        R["/items/2"] = (200, h, page(f'<meta property="og:image" content="/img/logo.png"><script type="application/ld+json">{ld}</script>', "", "かいちん いろいろ"))
        # 商品ページらしくなく、og:image は共通画像 → 写真なし
        R["/about"] = (200, h, page('<meta property="og:image" content="/img/ogp.png">', "<p>会社案内</p>", "会社案内 | 森八"))
        # og なし。本文の画像（ヘッダのロゴ・フッタのバナーは除く）
        R["/items/3"] = (200, h, page("", '<header><img src="/img/logo.png"></header><div class="product-main"><img src="/img/p3.jpg" width="600" alt="四季の羊羹"></div>'
                                            '<footer><img src="/img/b.jpg" alt="バナー"></footer>', "四季の羊羹 | 森八"))
        # og:image が小さすぎるので twitter:image を使う
        R["/items/4"] = (200, h, page('<meta property="og:image" content="/img/tiny.jpg"><meta name="twitter:image" content="/img/p2.jpg">', "", "長生殿小墨"))
        # og:image がバナー（縦横比が極端）
        R["/items/5"] = (200, h, page('<meta property="og:image" content="/img/wide.jpg">', "", "俵っ子"))
        # 一覧ページ（2商品が同じページを指す）
        R["/list"] = (200, h, page("", '<ul><li class="item"><a href="/x"><img src="data:image/gif;base64,R0lGOD" data-src="/img/a.jpg" alt=""></a><p>クルミのおやつ シナモン</p><p>1,080円</p></li>'
                                       '<li class="item"><a href="/y"><img src="/img/b.jpg" alt="クルミのおやつ 黒糖カラメル"></a><p>黒糖カラメル 1,080円</p></li></ul>', "クルミのおやつ一覧"))
        R["/secret/items/9"] = (200, h, page('<meta property="og:image" content="/img/p1.jpg">', "", "秘密の商品"))
        R["/items/6"] = (200, h, page('<meta property="og:image" content="/img/missing.jpg">', "", "存在しない写真"))
        sjis = "<!doctype html><html><head><meta charset='Shift_JIS'><title>金沢羽二重餅 | みのや</title><meta property='og:image' content='/img/p1.jpg'></head><body></body></html>".encode("cp932")
        R["/items/7"] = (200, "text/html", sjis)
        R["/items/8"] = (200, h, page('<meta property="og:image" content="/img/alpha.png">', "", "透過PNGの商品"))
        # カテゴリの一覧ページ（題名が商品名と合わない）→ og:image は使わない
        R["/category/sweets/"] = (200, h, page('<meta property="og:image" content="/img/p1.jpg">', "", "和菓子一覧 | 森八"))
        # itemprop="image" の meta
        R["/items/10"] = (200, h, page('<meta itemprop="image" content="/img/p2.jpg">', "", "能登大納言甘納豆 | まめや"))

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self.net = fp.Net(0.0)
        self.net.session().trust_env = False       # 手元のサーバへはプロキシを通さない
        self.ctx = {"net": self.net, "img_dir": self.tmp, "overrides": {}, "by_page": {}}

    def run_one(self, pid, name, path, group=None, **extra):
        prod = {"id": pid, "shop": "試験店", "name": name, "page_url": self.base + path, "options": [], **extra}
        self.ctx["by_page"] = {prod["page_url"]: group or [prod]}
        return fp.process(prod, self.ctx), prod

    def thumb(self, pid):
        with Image.open(self.tmp / f"{pid}.jpg") as im:
            return im.copy()

    # --- 商品ページ
    def test_og_image_on_product_page(self):
        rec, _ = self.run_one("P1", "加賀八幡 起上もなか", "/items/1")
        self.assertEqual(rec["status"], "ok", rec)
        self.assertEqual(rec["method"], "og")
        self.assertFalse(rec["review"])
        self.assertEqual(self.thumb("P1").size, (fp.OUT_PX, fp.OUT_PX))

    def test_jsonld_beats_logo_og(self):
        rec, _ = self.run_one("P2", "かいちん いろいろ", "/items/2")
        self.assertEqual((rec["status"], rec["method"]), ("ok", "jsonld"), rec)

    def test_generic_og_on_non_product_page_is_not_used(self):
        rec, _ = self.run_one("P3", "四季の羊羹", "/about")
        self.assertEqual(rec["status"], "fail", rec)
        self.assertFalse((self.tmp / "P3.jpg").exists())

    def test_gallery_fallback_skips_header_and_footer(self):
        rec, _ = self.run_one("P4", "四季の羊羹", "/items/3")
        self.assertEqual((rec["status"], rec["method"]), ("ok", "gallery"), rec)
        self.assertTrue(rec["review"])               # 確からしさが低い方法は「要確認」
        self.assertTrue(rec["image_url"].endswith("/img/p3.jpg"))

    def test_small_og_falls_through_to_twitter(self):
        rec, _ = self.run_one("P5", "長生殿小墨", "/items/4")
        self.assertEqual((rec["status"], rec["method"]), ("ok", "twitter"), rec)

    def test_icon_like_image_is_rejected(self):
        R = Handler.routes
        R["/items/20"] = (200, "text/html; charset=utf-8", page('<meta property="og:image" content="/img/heart.png">', "", "ありがとうギフト"))
        rec, _ = self.run_one("P17", "ありがとうギフト", "/items/20")
        self.assertEqual(rec["status"], "fail", rec)
        self.assertIn("アイコン", rec["reason"])

    def test_antialiased_monochrome_line_art_is_rejected(self):
        Handler.routes["/items/21"] = (200, "text/html; charset=utf-8", page('<meta property="og:image" content="/img/hands.png">', "", "握手のギフト"))
        rec, _ = self.run_one("P18", "握手のギフト", "/items/21")
        self.assertEqual(rec["status"], "fail", rec)
        self.assertIn("アイコン", rec["reason"])

    def test_banner_aspect_is_rejected(self):
        rec, _ = self.run_one("P6", "俵っ子", "/items/5")
        self.assertEqual(rec["status"], "fail", rec)
        self.assertIn("縦横比", rec["reason"])

    def test_missing_image_is_reported(self):
        rec, _ = self.run_one("P7", "存在しない写真", "/items/6")
        self.assertEqual(rec["status"], "fail")
        self.assertIn("404", rec["reason"])

    def test_robots_txt_is_respected(self):
        rec, _ = self.run_one("P8", "秘密の商品", "/secret/items/9")
        self.assertEqual(rec["status"], "fail")
        self.assertIn("robots", rec["reason"])

    def test_shift_jis_page(self):
        rec, _ = self.run_one("P9", "金沢羽二重餅", "/items/7")
        self.assertEqual(rec["status"], "ok", rec)
        self.assertEqual(rec["title_score"], 1.0)

    def test_transparent_png_gets_white_background(self):
        rec, _ = self.run_one("P10", "透過PNGの商品", "/items/8")
        self.assertEqual(rec["status"], "ok", rec)
        im = self.thumb("P10")
        self.assertEqual(im.getpixel((2, 2)), (255, 255, 255))      # 透明部分は白

    def test_category_url_with_unrelated_title_is_not_a_product_page(self):
        rec, _ = self.run_one("P14", "小型羊羹", "/category/sweets/")
        self.assertEqual(rec["status"], "fail", rec)

    def test_itemprop_image_meta(self):
        rec, _ = self.run_one("P15", "能登大納言甘納豆", "/items/10")
        self.assertEqual((rec["status"], rec["method"]), ("ok", "meta"), rec)

    def test_og_same_as_site_top_page_is_rejected(self):
        routes = {"/img/shop.jpg": (200, "image/jpeg", jpeg()),
                  "/": (200, "text/html", page('<meta property="og:image" content="/img/shop.jpg">', "", "店のトップ")),
                  "/items/1": (200, "text/html", page('<meta property="og:image" content="/img/shop.jpg">', "", "加賀八幡 起上もなか"))}
        srv, base = serve(routes)
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        prod = {"id": "P16", "shop": "試験店", "name": "加賀八幡 起上もなか", "page_url": base + "/items/1", "options": []}
        self.ctx["by_page"] = {prod["page_url"]: [prod]}
        rec = fp.process(prod, self.ctx)
        self.assertEqual(rec["status"], "fail", rec)
        self.assertIn("共通画像", rec["reason"])

    def test_429_is_retried_after_backing_off(self):
        calls = {"n": 0}

        def flaky():
            calls["n"] += 1
            return (429, "text/plain", b"slow down") if calls["n"] == 1 else (200, "text/plain", b"ok")

        srv, base = serve({"/x": flaky})
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        self.net.backoff, self.net.gate.slow_base = 0.05, 0.01
        body, r = self.net.get(base + "/x", None, "*/*", 1024)
        self.assertEqual((body, calls["n"]), (b"ok", 2))

    # --- 一覧ページ
    def test_listing_page_assigns_each_product_its_own_image(self):
        p1 = {"id": "L1", "shop": "大畑食品", "name": "クルミのおやつ シナモン", "page_url": self.base + "/list", "options": []}
        p2 = {"id": "L2", "shop": "大畑食品", "name": "クルミのおやつ 黒糖カラメル", "page_url": self.base + "/list", "options": []}
        self.ctx["by_page"] = {p1["page_url"]: [p1, p2]}
        r1, r2 = fp.process(p1, self.ctx), fp.process(p2, self.ctx)
        self.assertEqual((r1["status"], r2["status"]), ("ok", "ok"), (r1, r2))
        self.assertTrue(r1["image_url"].endswith("/img/a.jpg"), r1)       # data: の仮画像ではなく data-src を使う
        self.assertTrue(r2["image_url"].endswith("/img/b.jpg"), r2)
        self.assertEqual(r1["method"], "listing")

    def test_listing_page_without_match_gives_no_photo(self):
        p1 = {"id": "L3", "shop": "大畑食品", "name": "クルミのおやつ 抹茶", "page_url": self.base + "/list", "options": []}
        p2 = {"id": "L4", "shop": "大畑食品", "name": "クルミのおやつ シナモン", "page_url": self.base + "/list", "options": []}
        self.ctx["by_page"] = {p1["page_url"]: [p1, p2]}
        self.assertEqual(fp.process(p1, self.ctx)["status"], "fail")

    # --- 手動指定・通信の分類
    def test_override_skip_and_image_url(self):
        self.ctx["overrides"] = {"P11": {"skip": True, "reason": "掲載終了"}, "P12": {"image_url": self.base + "/img/p2.jpg"}}
        rec, _ = self.run_one("P11", "x", "/items/1")
        self.assertEqual((rec["status"], rec["reason"]), ("skip", "掲載終了"))
        rec, _ = self.run_one("P12", "y", "/about")
        self.assertEqual((rec["status"], rec["method"]), ("ok", "manual"), rec)

    def test_egress_block_is_classified(self):
        with mock.patch.object(requests.Session, "get", side_effect=requests.exceptions.ProxyError("Tunnel connection failed: 403 Forbidden")):
            rec, _ = self.run_one("P13", "加賀八幡 起上もなか", "/items/1")
        self.assertEqual(rec["status"], "fail")
        self.assertIn("拒否", rec["reason"])
        self.assertIn("127.0.0.1", " ".join(self.net.blocked))

    # --- 部品
    def test_name_score(self):
        self.assertEqual(fp.name_score("四季の羊羹 粋（能登栗ほか季節の味）", "四季の羊羹「粋」 | 森八"), 1.0)
        self.assertGreaterEqual(fp.name_score("わり氷【キューブ】25g×4袋入", "わり氷 | 村上"), 0.9)
        self.assertLess(fp.name_score("金城巻", "おすすめの和菓子 | 金沢の老舗"), 0.5)
        self.assertEqual(fp.name_score("クルミのおやつ シナモン", "クルミのおやつ 黒糖カラメル", specific=True) < 1.0, True)

    def test_pick_ids_matches_label(self):
        prods = [{"id": "A", "shop": "森八", "name": "四季の羊羹", "options": [{"label": "3本入"}]},
                 {"id": "B", "shop": "森八", "name": "四季の羊羹 粋", "options": [{"label": "5本入"}]}]
        picks = {"picks": [{"match": {"shop": "森八", "product": "四季の羊羹", "label": "5本"}}], "alternatives": []}
        self.assertEqual(fp.pick_ids(prods, picks), ["B"])

    def test_srcset_picks_largest(self):
        self.assertEqual(fp.best_from_srcset("/a.jpg 320w, /b.jpg 960w, /c.jpg 640w"), "/b.jpg")

    def test_list_hosts_and_sheet(self):
        prods = [{"id": "P1", "shop": "店", "name": "品", "page_url": "https://a.example/x", "options": [{"source_url": "https://b.example/y"}]}]
        self.assertEqual(fp.list_hosts(prods), ["a.example", "b.example"])
        Image.new("RGB", (240, 240), "red").save(self.tmp / "P1.jpg")
        paths = fp.make_sheets(prods, self.tmp, self.tmp / "sheets")
        self.assertEqual(len(paths), 1)


if __name__ == "__main__":
    unittest.main()
