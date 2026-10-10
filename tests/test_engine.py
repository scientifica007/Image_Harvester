"""stdlib integration tests use an isolated local HTTP server; no internet needed."""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from image_harvester.engine import (Engine, PageParser, Settings, StopRequested, _detect_image_mime,
                                    css_urls, is_site_page, srcset_urls, url_normalize)

# Small valid image headers (engine checks recognized signatures, not full decoding).
PNG = b"\x89PNG\r\n\x1a\n" + b"A" * 50
GIF = b"GIF89a" + b"B" * 50
JPEG = b"\xff\xd8\xff" + b"C" * 50
WEBP = b"RIFF\x18\x00\x00\x00WEBPVP8 " + b"D" * 50
SVG = b'<svg xmlns="http://www.w3.org/2000/svg"><rect width="2"/></svg>'


class FixtureHandler(BaseHTTPRequestHandler):
    counts = {}

    def do_GET(self):
        p = urlsplit(self.path).path
        self.counts[p] = self.counts.get(p, 0) + 1
        responses = {
            "/robots.txt": ("text/plain", b"User-agent: *\nDisallow: /hidden/\n"),
            "/": ("text/html", b'''<html><head><link rel="stylesheet" href="/style.css">
                <meta property="og:image" content="/images/meta.jpg"></head><body>
                <img src="/images/a.png"><img data-src="/images/lazy.gif">
                <picture><source srcset="/images/small.webp 1x, /images/large.webp 2x"></picture>
                <div style="background-image: url('/images/bg.svg')"></div>
                <img src="/hidden/block.png"><img src="/images/fake.jpg">
                <a href="/sub.html">Next page</a><a href="/images/direct.png">Open image</a>
                </body></html>'''),
            "/sub.html": ("text/html", b'''<h1>Second</h1><img src="/images/clone.png">
                         <a href="/deep.html">Deep</a>'''),
            "/deep.html": ("text/html", b'<img src="/images/deep.png">'),
            "/style.css": ("text/css", b'@import "/nested.css";body{background:url("/images/css.png")}'),
            "/nested.css": ("text/css", b'p { background: url("/images/nested.gif") }'),
            "/images/a.png": ("image/png", PNG),
            "/images/clone.png": ("image/png", PNG),
            "/images/direct.png": ("image/png", PNG + b"direct"),
            "/images/css.png": ("image/png", PNG + b"css"),
            "/images/deep.png": ("image/png", PNG + b"deep"),
            "/images/lazy.gif": ("image/gif", GIF),
            "/images/nested.gif": ("image/gif", GIF + b"nested"),
            "/images/meta.jpg": ("image/jpeg", JPEG),
            "/images/small.webp": ("image/webp", WEBP),
            "/images/large.webp": ("image/webp", WEBP + b"large"),
            "/images/bg.svg": ("image/svg+xml", SVG),
            "/hidden/block.png": ("image/png", PNG + b"blocked"),
            "/images/fake.jpg": ("text/html", b"<html>not image</html>"),
        }
        if p not in responses:
            self.send_response(404)
            self.end_headers()
            return
        mime, content = responses[p]
        self.send_response(200)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def log_message(self, *args):
        pass


class EngineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        FixtureHandler.counts = {}
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), FixtureHandler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.url = f"http://127.0.0.1:{cls.server.server_port}/"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        FixtureHandler.counts.clear()

    def tearDown(self):
        self.temp.cleanup()

    def settings(self, **kwargs):
        config = dict(url=self.url, output_dir=self.temp.name, mode="page", delay_seconds=0, timeout_seconds=5)
        config.update(kwargs)
        return Settings(**config)

    def test_page_only_images_css_lazy_robots_and_reject_nonimage(self):
        result = Engine(self.settings()).run()
        self.assertEqual(result.stats.pages, 1)
        self.assertEqual(result.stats.downloaded, 9)  # a, lazy, small, large, svg, meta, direct, css, nested
        self.assertEqual(result.stats.robots_skipped, 1)
        self.assertEqual(result.stats.failed, 1)
        self.assertNotIn("/sub.html", FixtureHandler.counts)
        self.assertNotIn("/hidden/block.png", FixtureHandler.counts)
        self.assertEqual(len(list(Path(result.output_dir, "images").glob("*"))), 9)
        self.assertTrue(Path(result.output_dir, "manifest.json").is_file())
        self.assertTrue(Path(result.output_dir, "images.csv").is_file())
        self.assertEqual(json.loads(Path(result.output_dir, "manifest.json").read_text())["statistics"]["downloaded"], 9)
        self.assertFalse(any(p.suffix == ".html" for p in Path(result.output_dir).rglob("*")))

    def test_site_depth_one_download_and_sha_dedup(self):
        result = Engine(self.settings(mode="site", max_depth=1)).run()
        self.assertEqual(result.stats.pages, 2)
        self.assertEqual(result.stats.duplicates, 1)
        self.assertEqual(result.stats.downloaded, 9)
        self.assertEqual(FixtureHandler.counts.get("/deep.html", 0), 0)

    def test_site_depth_two(self):
        result = Engine(self.settings(mode="site", max_depth=2)).run()
        self.assertEqual(result.stats.pages, 3)
        self.assertEqual(result.stats.downloaded, 10)

    def test_total_page_limit(self):
        result = Engine(self.settings(mode="site", max_depth=6, max_pages=1)).run()
        self.assertEqual(result.stats.pages, 1)

    def test_max_image_candidates(self):
        result = Engine(self.settings(max_images=2)).run()
        self.assertEqual(result.stats.discovered, 2)
        self.assertEqual(result.stats.downloaded, 2)

    def test_image_size_limit(self):
        result = Engine(self.settings(max_total_mb=1, max_image_mb=1)).run()
        self.assertEqual(result.stats.downloaded, 9)

    def test_stop_initially_requested_keeps_reports(self):
        stop = threading.Event()
        stop.set()
        result = Engine(self.settings(), stop_event=stop).run()
        self.assertTrue(result.stopped)
        self.assertEqual(result.stats.pages, 0)
        self.assertTrue(Path(result.output_dir, "manifest.json").is_file())

    def test_stop_after_first_download(self):
        stop = threading.Event()
        def stop_on_download(event):
            if event.get("kind") == "stats" and event["stats"]["downloaded"] >= 1:
                stop.set()
        result = Engine(self.settings(), on_event=stop_on_download, stop_event=stop).run()
        self.assertTrue(result.stopped)
        self.assertEqual(result.stats.downloaded, 1)
        self.assertFalse(list(Path(result.output_dir).rglob("*.part")))
        self.assertTrue(Path(result.output_dir, "manifest.json").exists())

    def test_page_parser_direct_og_and_srcset(self):
        p = PageParser("https://example.com/test")
        p.feed('<img src="a.png" data-srcset="b.png 1x,c.jpg 2x">'
               '<style>p{background:url("/d.svg")}</style>'
               '<a href="/else">Next</a>')
        p.finish()
        self.assertEqual(len(p.images), 4)
        self.assertEqual(len(p.links), 1)

    def test_url_normalization(self):
        self.assertIsNone(url_normalize("https://example.com", "javascript:alert(1)"))
        self.assertIsNone(url_normalize("https://example.com", "data:image/png;base64,xyz"))
        self.assertEqual(url_normalize("https://example.com/a", "/b.png#x"), "https://example.com/b.png")
        self.assertTrue(is_site_page("https://www.example.com/a", "https://example.com/"))
        self.assertFalse(is_site_page("https://fakeexample.com", "https://example.com"))
        self.assertTrue(is_site_page("https://example.com/final", "http://example.com/"))
        self.assertFalse(is_site_page("http://127.0.0.1:9002/", "http://127.0.0.1:9001/"))

    def test_css_import(self):
        images, imports = css_urls('@import "other.css";p{background:url(\'../x.png\')}', "https://example.com/css/a.css")
        self.assertEqual(images, ["https://example.com/x.png"])
        self.assertEqual(imports, ["https://example.com/css/other.css"])

    def test_image_mime_detector(self):
        self.assertEqual(_detect_image_mime(PNG), "image/png")
        self.assertEqual(_detect_image_mime(SVG), "image/svg+xml")
        self.assertIsNone(_detect_image_mime(b"not a picture"))

    def test_invalid_settings(self):
        with self.assertRaises(ValueError):
            self.settings(url="file:///etc/passwd").validate()
        with self.assertRaises(ValueError):
            self.settings(max_pages=100000).validate()
        with self.assertRaises(ValueError):
            self.settings(url="https://user:pw@example.com/").validate()


if __name__ == "__main__":
    unittest.main()
