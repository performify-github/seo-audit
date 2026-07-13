#!/usr/bin/env python3
"""Fixture tests for seo_audit.py — a throwaway local HTTP server with deliberate faults.

Run:  python3 test_seo_audit.py
Stdlib only, like the tool itself.
"""

import http.server
import os
import threading
import unittest
import urllib.parse

import seo_audit as sa

# keep any egress proxy out of the loop — fixtures live on localhost
os.environ["no_proxy"] = "127.0.0.1,localhost"
for var in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY", "all_proxy", "ALL_PROXY"):
    os.environ.pop(var, None)


# ---------------------------------------------------------------- fixtures
def make_server(routes):
    """Serve a dict path -> (status, headers, body-bytes-or-callable)."""
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            entry = routes.get(self.path)
            if entry is None:
                entry = (404, {"Content-Type": "text/html"}, b"<html><body>gone</body></html>")
            status, headers, body = entry
            if callable(body):
                body = body()
            self.send_response(status)
            for k, v in headers.items():
                self.send_header(k, v)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def html_page(title="A perfectly reasonable page title here", desc="D" * 100,
              h1s=("Main heading",), canonical=None, words=250, noindex=False,
              viewport=True, jsonld=True, links=(), body_extra=""):
    parts = ["<html><head>"]
    if title is not None:
        parts.append(f"<title>{title}</title>")
    if desc is not None:
        parts.append(f'<meta name="description" content="{desc}">')
    if noindex:
        parts.append('<meta name="robots" content="noindex">')
    if viewport:
        parts.append('<meta name="viewport" content="width=device-width">')
    if canonical:
        parts.append(f'<link rel="canonical" href="{canonical}">')
    if jsonld:
        parts.append('<script type="application/ld+json">{"@type": "WebPage"}</script>')
    parts.append("</head><body>")
    for h in h1s:
        parts.append(f"<h1>{h}</h1>")
    for href in links:
        parts.append(f'<a href="{href}">link</a>')
    parts.append("<p>" + " ".join(f"word{i}" for i in range(words)) + "</p>")
    parts.append(body_extra)
    parts.append("</body></html>")
    return "".join(parts).encode()


def sitemap_xml(urls):
    locs = "".join(f"<url><loc>{u}</loc></url>" for u in urls)
    return (f'<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
            f"{locs}</urlset>").encode()


def sitemap_index(sitemaps):
    locs = "".join(f"<sitemap><loc>{u}</loc></sitemap>" for u in sitemaps)
    return (f'<?xml version="1.0"?><sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
            f"{locs}</sitemapindex>").encode()


HTML = {"Content-Type": "text/html; charset=utf-8"}
XML = {"Content-Type": "application/xml"}
TXT = {"Content-Type": "text/plain"}


def classes_of(grouped):
    return {code: items[0][0] for code, items in grouped.items() if items}


# ---------------------------------------------------------------- class mapping
class TestClasses(unittest.TestCase):
    """Each of the four classes is emitted, with the right membership."""

    @classmethod
    def setUpClass(cls):
        routes = {}
        cls.srv = make_server(routes)
        base = f"http://127.0.0.1:{cls.srv.server_address[1]}"
        pages = [f"{base}/", f"{base}/about", f"{base}/missing", f"{base}/noindexed",
                 f"{base}/redirected", f"{base}/dup-a", f"{base}/dup-b",
                 f"{base}/multi-h1", f"{base}/no-desc"]
        routes.update({
            "/robots.txt": (200, TXT, f"User-agent: *\nAllow: /\nSitemap: {base}/sitemap.xml\n".encode()),
            "/sitemap.xml": (200, XML, sitemap_xml(pages)),
            "/": (200, HTML, html_page(title="Homepage title of the fixture site ok",
                                       canonical=f"{base}/")),
            "/about": (200, HTML, html_page(title="About page title of the fixture site",
                                            canonical=f"{base}/about")),
            "/missing": (404, HTML, b"<html><body>gone</body></html>"),
            "/noindexed": (200, HTML, html_page(title="Noindexed page title of fixture site",
                                                noindex=True, canonical=f"{base}/noindexed")),
            "/redirected": (301, {"Location": "/about", **HTML}, b""),
            "/dup-a": (200, HTML, html_page(title="Shared duplicate title across two pages",
                                            canonical=f"{base}/dup-a")),
            "/dup-b": (200, HTML, html_page(title="Shared duplicate title across two pages",
                                            canonical=f"{base}/dup-b")),
            "/multi-h1": (200, HTML, html_page(title="Multi H1 page title of fixture site",
                                               h1s=("One", "Two"), canonical=f"{base}/multi-h1")),
            "/no-desc": (200, HTML, html_page(title="No description page title fixture ok",
                                              desc=None, canonical=f"{base}/no-desc")),
        })
        cls.model = sa.run_audit(f"{base}/sitemap.xml", max_pages=0, workers=4)
        cls.grouped = cls.model["grouped"]

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def test_blockers(self):
        cm = classes_of(self.grouped)
        self.assertEqual(cm.get("http-error"), sa.BLOCKER)
        self.assertEqual(cm.get("noindex-in-sitemap"), sa.BLOCKER)

    def test_material(self):
        cm = classes_of(self.grouped)
        self.assertEqual(cm.get("redirected-sitemap-url"), sa.MATERIAL)
        self.assertEqual(cm.get("duplicate-title"), sa.MATERIAL)

    def test_opportunities(self):
        cm = classes_of(self.grouped)
        self.assertEqual(cm.get("missing-description"), sa.OPPORTUNITY)

    def test_observations(self):
        cm = classes_of(self.grouped)
        self.assertEqual(cm.get("multiple-h1"), sa.OBSERVATION)

    def test_no_fixed_slow_threshold(self):
        self.assertNotIn("slow-response", self.grouped)

    def test_exit_code_rule(self):
        self.assertTrue(sa.has_blockers(self.grouped))
        no_blockers = {k: v for k, v in self.grouped.items() if v and v[0][0] != sa.BLOCKER}
        self.assertFalse(sa.has_blockers(no_blockers))

    def test_both_timings_recorded(self):
        for raw, (issues, data) in self.model["results"]:
            if raw["error"]:
                continue
            self.assertIsNotNone(data.get("ttfb"), data["url"])
            self.assertLessEqual(data["ttfb"], data["fetch_time"] + 1e-9, data["url"])

    def test_every_code_has_class_and_hint(self):
        for code in self.grouped:
            self.assertIn(code, sa.CLASS_OF, code)
            self.assertIn(code, sa.FIX_HINTS, code)


class TestCleanSite(unittest.TestCase):
    """A clean site produces no blockers (exit 0 path)."""

    @classmethod
    def setUpClass(cls):
        routes = {}
        cls.srv = make_server(routes)
        base = f"http://127.0.0.1:{cls.srv.server_address[1]}"
        pages = [f"{base}/", f"{base}/about"]
        routes.update({
            "/robots.txt": (200, TXT, f"User-agent: *\nAllow: /\nSitemap: {base}/sitemap.xml\n".encode()),
            "/sitemap.xml": (200, XML, sitemap_xml(pages)),
            "/": (200, HTML, html_page(title="Homepage title of the fixture site ok",
                                       canonical=f"{base}/", links=[f"{base}/about"])),
            "/about": (200, HTML, html_page(title="About page title of the fixture site",
                                            canonical=f"{base}/about", links=["/"])),
        })
        cls.model = sa.run_audit(f"{base}/sitemap.xml", max_pages=0, workers=4)

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def test_no_blockers(self):
        self.assertFalse(sa.has_blockers(self.model["grouped"]))


# ---------------------------------------------------------------- timing outliers
def _timing_result(url, elapsed, status=200, error=None):
    return ({"url": url, "elapsed": elapsed, "status": status, "error": error}, None)


class TestTimingOutliers(unittest.TestCase):
    def test_outlier_flagged(self):
        results = [_timing_result(f"u{i}", 0.2) for i in range(4)]
        results.append(_timing_result("slow", 2.6))
        flagged = sa.timing_checks(results)
        self.assertEqual([u for _, u, _ in flagged], ["slow"])
        self.assertEqual(flagged[0][0], sa.OPPORTUNITY)

    def test_slow_but_uniform_site_not_flagged(self):
        # 3.0s exceeds the absolute floor but not 3x the 1.2s median
        results = [_timing_result(f"u{i}", 1.2) for i in range(4)]
        results.append(_timing_result("slowish", 3.0))
        self.assertEqual(sa.timing_checks(results), [])

    def test_fast_outlier_needs_absolute_floor(self):
        # 1.0s is >3x the 0.1s median but under the 2.5s floor
        results = [_timing_result(f"u{i}", 0.1) for i in range(4)]
        results.append(_timing_result("relative", 1.0))
        self.assertEqual(sa.timing_checks(results), [])

    def test_small_sample_suppressed(self):
        results = [_timing_result(f"u{i}", 0.2) for i in range(3)]
        results.append(_timing_result("slow", 5.0))
        self.assertEqual(sa.timing_checks(results), [])

    def test_failed_fetches_excluded(self):
        results = [_timing_result(f"u{i}", 0.2) for i in range(5)]
        results.append(_timing_result("err", 9.0, status=None, error="timeout"))
        results.append(_timing_result("gone", 9.0, status=404))
        self.assertEqual(sa.timing_checks(results), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
