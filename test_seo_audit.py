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

    def test_blocker_class_reserved_for_indexation(self):
        # crawlable-but-flawed pages are material, not blockers
        self.assertEqual(sa.CLASS_OF["missing-title"], sa.MATERIAL)
        self.assertEqual(sa.CLASS_OF["mixed-content"], sa.MATERIAL)
        self.assertEqual(sa.CLASS_OF["duplicate-description"], sa.OPPORTUNITY)
        self.assertFalse(sa.has_blockers({"missing-title": [(sa.MATERIAL, "u", None)]}))

    def test_opportunities(self):
        cm = classes_of(self.grouped)
        self.assertEqual(cm.get("missing-description"), sa.OPPORTUNITY)

    def test_observations(self):
        cm = classes_of(self.grouped)
        self.assertEqual(cm.get("multiple-h1"), sa.OBSERVATION)

    def test_duplicates_only_across_indexable_pages(self):
        # /redirected parses /about's title but must not create a duplicate;
        # noindexed pages are excluded too
        urls = {u for _, u, _ in self.grouped.get("duplicate-title", [])}
        base = f"http://127.0.0.1:{self.srv.server_address[1]}"
        self.assertEqual(urls, {f"{base}/dup-a", f"{base}/dup-b"})

    def test_coverage_categories_exclusive(self):
        cov = self.model["coverage"]
        self.assertEqual(cov["crawled"], 9)
        self.assertEqual(cov["broken"], 1)
        self.assertEqual(cov["noindexed"], 1)
        self.assertEqual(cov["redirected"], 1)
        self.assertEqual(cov["indexable_html"], 6)   # redirected page NOT double-counted
        total = (cov["broken"] + cov["noindexed"] + cov["redirected"]
                 + cov["indexable_html"] + cov["rate_limited"] + cov["unreachable"])
        self.assertEqual(total, cov["crawled"])

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


# ---------------------------------------------------------------- ranking, verdict, clustering
class TestRankingAndVerdict(unittest.TestCase):
    """Impact ranking is hand-recomputable; verdict and clusters read the data."""

    @classmethod
    def setUpClass(cls):
        routes = {}
        cls.srv = make_server(routes)
        base = f"http://127.0.0.1:{cls.srv.server_address[1]}"
        cls.base = base
        pages_a = [f"{base}/", f"{base}/about", f"{base}/contact"]
        products = [f"{base}/products/p{i}" for i in range(5)]
        routes.update({
            "/robots.txt": (200, TXT, f"User-agent: *\nAllow: /\nSitemap: {base}/sitemap.xml\n".encode()),
            "/sitemap.xml": (200, XML, sitemap_index([f"{base}/pages.xml", f"{base}/products.xml"])),
            "/pages.xml": (200, XML, sitemap_xml(pages_a)),
            "/products.xml": (200, XML, sitemap_xml(products)),
            "/": (200, HTML, html_page(title="Homepage title of the fixture site ok",
                                       canonical=f"{base}/")),
            "/about": (200, HTML, html_page(title="About page title of the fixture site",
                                            canonical=f"{base}/about")),
            "/contact": (200, HTML, html_page(title="Contact page title of the fixture ok",
                                              canonical=f"{base}/contact")),
            # 4 of 5 product pages 404 — a template-shaped failure
            "/products/p4": (200, HTML, html_page(title="Product four page title fixture ok",
                                                  canonical=f"{base}/products/p4")),
        })
        cls.model = sa.run_audit(f"{base}/sitemap.xml", max_pages=0, workers=4)

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def test_scores_recomputable_by_hand(self):
        crawled = len(self.model["results"])
        for e in self.model["ranked"]:
            imp, conf = sa.IMPACT.get(e["code"], (1, 1))
            expected = round(imp * conf * min(1.0, e["affected"] / crawled), 2)
            self.assertEqual(e["score"], expected, e["code"])

    def test_top_priority_is_http_error(self):
        priority = [e for e in self.model["ranked"] if e["class"] in (sa.BLOCKER, sa.MATERIAL)]
        self.assertEqual(priority[0]["code"], "http-error")

    def test_verdict_names_blockers_and_concentration(self):
        v = self.model["verdict"]
        self.assertIn("Indexation problems affect 4 of 8 crawled URLs", v)
        self.assertIn("4 sitemap URLs returning 4xx/5xx", v)
        self.assertIn("concentrated under /products/", v)

    def test_cluster_denominator_is_crawled_pages(self):
        urls = [u for _, u, _ in self.model["grouped"]["http-error"]]
        crawled = [r["url"] for r, _ in self.model["results"]]
        clusters = sa.cluster_issue(urls, crawled, self.model["source_of"])
        top = clusters[0]
        self.assertEqual((top["n"], top["of"], top["segment"]), (4, 5, "/products/"))
        line = sa.cluster_line(top)
        self.assertIn("4/5 crawled pages under /products/", line)
        self.assertIn("products.xml", line)
        self.assertIn("likely template-level", line)

    def test_source_sitemap_tracked(self):
        self.assertTrue(self.model["source_of"][f"{self.base}/products/p0"].endswith("/products.xml"))
        self.assertTrue(self.model["source_of"][f"{self.base}/about"].endswith("/pages.xml"))

    def test_report_sections(self):
        report = sa.build_report(self.model, md=True)
        for section in ("## Verdict", "## Coverage", "## Priority actions"):
            self.assertIn(section, report)
        # default report: human labels and plain-language scoring, no mechanics
        self.assertIn("Broken sitemap URLs (4xx/5xx)", report)
        self.assertIn("high impact, high confidence", report)
        self.assertIn("affects 4 of 8 crawled pages", report)
        self.assertNotIn("impact 5 x confidence 5", report)
        self.assertNotIn("[http-error]", report)

    def test_explain_scores_mode(self):
        report = sa.build_report(self.model, md=True, explain=True)
        self.assertIn("impact 5 x confidence 5", report)
        self.assertIn("[http-error]", report)

    def test_coverage_stats(self):
        cov = self.model["coverage"]
        self.assertEqual(cov["sitemap_total"], 8)
        self.assertEqual(cov["crawled"], 8)
        self.assertEqual(cov["broken"], 4)
        self.assertEqual(cov["indexable_html"], 4)


class TestCleanVerdict(unittest.TestCase):
    def test_clean_site_verdict(self):
        routes = {}
        srv = make_server(routes)
        try:
            base = f"http://127.0.0.1:{srv.server_address[1]}"
            routes.update({
                "/robots.txt": (200, TXT, f"User-agent: *\nAllow: /\nSitemap: {base}/sitemap.xml\n".encode()),
                "/sitemap.xml": (200, XML, sitemap_xml([f"{base}/", f"{base}/about"])),
                "/": (200, HTML, html_page(title="Homepage title of the fixture site ok",
                                           canonical=f"{base}/")),
                "/about": (200, HTML, html_page(title="About page title of the fixture site",
                                                canonical=f"{base}/about")),
            })
            model = sa.run_audit(f"{base}/sitemap.xml", max_pages=0, workers=4)
            self.assertTrue(model["verdict"].startswith("Indexation is broadly healthy."))
        finally:
            srv.shutdown()


# ---------------------------------------------------------------- link graph
class TestLinkGraph(unittest.TestCase):
    """Internal links to broken/redirected/noindexed/canonicalised targets."""

    @classmethod
    def setUpClass(cls):
        routes = {}
        cls.srv = make_server(routes)
        base = f"http://127.0.0.1:{cls.srv.server_address[1]}"
        cls.base = base
        pages = [f"{base}/", f"{base}/a", f"{base}/b", f"{base}/missing",
                 f"{base}/redirected", f"{base}/noindexed", f"{base}/canonical-away"]
        routes.update({
            "/robots.txt": (200, TXT, f"User-agent: *\nAllow: /\nSitemap: {base}/sitemap.xml\n".encode()),
            "/sitemap.xml": (200, XML, sitemap_xml(pages)),
            "/": (200, HTML, html_page(title="Homepage title of the fixture site ok", canonical=f"{base}/",
                                       links=[f"{base}/a", f"{base}/missing", f"{base}/redirected",
                                              f"{base}/noindexed", f"{base}/canonical-away"])),
            "/a": (200, HTML, html_page(title="Page A title of the fixture site okay",
                                        canonical=f"{base}/a", links=[f"{base}/", f"{base}/b"])),
            "/b": (200, HTML, html_page(title="Page B title of the fixture site okay",
                                        canonical=f"{base}/b", links=[f"{base}/a"])),
            "/missing": (404, HTML, b"<html><body>gone</body></html>"),
            "/redirected": (301, {"Location": "/a", **HTML}, b""),
            "/noindexed": (200, HTML, html_page(title="Noindexed page title of fixture site",
                                                noindex=True, canonical=f"{base}/noindexed")),
            "/canonical-away": (200, HTML, html_page(title="Canonicalised page title fixture ok",
                                                     canonical=f"{base}/a")),
        })
        cls.model = sa.run_audit(f"{base}/sitemap.xml", max_pages=0, workers=4)
        cls.grouped = cls.model["grouped"]

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def _urls(self, code):
        return [u for _, u, _ in self.grouped.get(code, [])]

    def test_link_to_broken(self):
        self.assertEqual(self._urls("internal-link-broken"), [f"{self.base}/missing"])
        self.assertEqual(self.grouped["internal-link-broken"][0][0], sa.MATERIAL)

    def test_link_to_redirect(self):
        self.assertEqual(self._urls("internal-link-redirect"), [f"{self.base}/redirected"])

    def test_link_to_noindexed(self):
        self.assertEqual(self._urls("internal-link-noindex"), [f"{self.base}/noindexed"])

    def test_link_to_canonicalised_away(self):
        self.assertEqual(self._urls("internal-link-canonicalised"), [f"{self.base}/canonical-away"])
        self.assertEqual(self.grouped["internal-link-canonicalised"][0][0], sa.OBSERVATION)

    def test_linked_pages_not_orphans(self):
        self.assertNotIn(f"{self.base}/a", self._urls("orphan-page"))
        self.assertNotIn(f"{self.base}/b", self._urls("orphan-page"))


class TestOrphanGate(unittest.TestCase):
    """Orphans reported at full coverage, suppressed entirely below 80%."""

    @classmethod
    def setUpClass(cls):
        cls.routes = {}
        cls.srv = make_server(cls.routes)
        base = f"http://127.0.0.1:{cls.srv.server_address[1]}"
        cls.base = base
        pages = [f"{base}/", f"{base}/linked", f"{base}/orphan"]
        cls.routes.update({
            "/robots.txt": (200, TXT, f"User-agent: *\nAllow: /\nSitemap: {base}/sitemap.xml\n".encode()),
            "/sitemap.xml": (200, XML, sitemap_xml(pages)),
            "/": (200, HTML, html_page(title="Homepage title of the fixture site ok",
                                       canonical=f"{base}/", links=[f"{base}/linked"])),
            "/linked": (200, HTML, html_page(title="Linked page title of the fixture ok",
                                             canonical=f"{base}/linked", links=[f"{base}/"])),
            "/orphan": (200, HTML, html_page(title="Orphan page title of the fixture ok",
                                             canonical=f"{base}/orphan")),
        })

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def test_reported_at_full_coverage(self):
        model = sa.run_audit(f"{self.base}/sitemap.xml", max_pages=0, workers=4)
        urls = [u for _, u, _ in model["grouped"].get("orphan-page", [])]
        self.assertEqual(urls, [f"{self.base}/orphan"])

    def test_suppressed_at_low_coverage(self):
        model = sa.run_audit(f"{self.base}/sitemap.xml", max_pages=2, workers=4)
        self.assertLess(len(model["results"]) / len(model["page_urls"]), 0.8)
        self.assertNotIn("orphan-page", model["grouped"])


class TestCanonicalTargets(unittest.TestCase):
    """Canonical destinations outside the crawl set get validated (max 50 fetches)."""

    @classmethod
    def setUpClass(cls):
        routes = {}
        cls.srv = make_server(routes)
        base = f"http://127.0.0.1:{cls.srv.server_address[1]}"
        cls.base = base
        cpages = [f"{base}/c1", f"{base}/c2", f"{base}/c3", f"{base}/c4", f"{base}/c5",
                  f"{base}/c6"]
        kpages = [f"{base}/k{i}" for i in range(5)]
        routes.update({
            "/robots.txt": (200, TXT, f"User-agent: *\nAllow: /\nSitemap: {base}/sitemap.xml\n".encode()),
            "/sitemap.xml": (200, XML, sitemap_xml(cpages + kpages)),
            "/c1": (200, HTML, html_page(title="C1 page title of the fixture site ok",
                                         canonical=f"{base}/good-target")),
            "/c2": (200, HTML, html_page(title="C2 page title of the fixture site ok",
                                         canonical=f"{base}/dead-target")),
            "/c3": (200, HTML, html_page(title="C3 page title of the fixture site ok",
                                         canonical=f"{base}/redir-target")),
            "/c4": (200, HTML, html_page(title="C4 page title of the fixture site ok",
                                         canonical=f"{base}/noindex-target")),
            "/c5": (200, HTML, html_page(title="C5 page title of the fixture site ok",
                                         canonical="https://syndication.example/article")),
            "/c6": (200, HTML, html_page(title="C6 page title of the fixture site ok",
                                         canonical=f"{base}/throttled-target")),
            "/good-target": (200, HTML, html_page(title="Good canonical target title fixture",
                                                  canonical=f"{base}/good-target")),
            # /dead-target is unrouted -> 404
            "/redir-target": (301, {"Location": "/good-target", **HTML}, b""),
            "/noindex-target": (200, HTML, html_page(title="Noindex canonical target title ok",
                                                     noindex=True,
                                                     canonical=f"{base}/noindex-target")),
            "/throttled-target": (429, {"Retry-After": "0", **HTML}, b""),
        })
        # five more pages all canonicalising to one destination -> consolidation
        for i in range(5):
            routes[f"/k{i}"] = (200, HTML, html_page(
                title=f"K{i} page title of the fixture site ok",
                canonical=f"{base}/good-target"))
        cls.model = sa.run_audit(f"{base}/sitemap.xml", max_pages=0, workers=4)
        cls.grouped = cls.model["grouped"]

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def _urls(self, code):
        return [u for _, u, _ in self.grouped.get(code, [])]

    def test_broken_target(self):
        self.assertEqual(self._urls("canonical-target-broken"), [f"{self.base}/dead-target"])
        self.assertEqual(self.grouped["canonical-target-broken"][0][0], sa.MATERIAL)

    def test_redirecting_target(self):
        self.assertEqual(self._urls("canonical-target-redirects"), [f"{self.base}/redir-target"])

    def test_noindexed_target(self):
        self.assertEqual(self._urls("canonical-target-noindex"), [f"{self.base}/noindex-target"])

    def test_healthy_target_not_flagged(self):
        for code in ("canonical-target-broken", "canonical-target-redirects",
                     "canonical-target-noindex"):
            self.assertNotIn(f"{self.base}/good-target", self._urls(code))

    def test_cross_domain_is_observation(self):
        self.assertEqual(self._urls("cross-domain-canonical"), [f"{self.base}/c5"])
        self.assertEqual(self.grouped["cross-domain-canonical"][0][0], sa.OBSERVATION)

    def test_consolidation_observation(self):
        items = self.grouped.get("canonical-consolidation", [])
        self.assertEqual([u for _, u, _ in items], [f"{self.base}/good-target"])
        self.assertIn("6 crawled pages", items[0][2])

    def test_rate_limited_target_is_unverified_not_broken(self):
        self.assertNotIn(f"{self.base}/throttled-target", self._urls("canonical-target-broken"))

    def test_broken_target_scope_is_source_pages(self):
        entry = next(e for e in self.model["ranked"] if e["code"] == "canonical-target-broken")
        self.assertEqual((entry["n"], entry["affected"]), (1, 1))


class TestCheckLinks(unittest.TestCase):
    """--check-links verifies off-sitemap internal destinations; default doesn't."""

    @classmethod
    def setUpClass(cls):
        routes = {}
        cls.srv = make_server(routes)
        base = f"http://127.0.0.1:{cls.srv.server_address[1]}"
        cls.base = base
        pages = [f"{base}/", f"{base}/present"]
        routes.update({
            "/robots.txt": (200, TXT, f"User-agent: *\nAllow: /\nSitemap: {base}/sitemap.xml\n".encode()),
            "/sitemap.xml": (200, XML, sitemap_xml(pages)),
            "/": (200, HTML, html_page(title="Homepage title of the fixture site ok",
                                       canonical=f"{base}/",
                                       links=[f"{base}/present", f"{base}/ghost"])),
            "/present": (200, HTML, html_page(title="Present page title of the fixture ok",
                                              canonical=f"{base}/present", links=[f"{base}/"])),
            # /ghost is unrouted -> 404, and not in the sitemap
        })

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def test_default_skips_off_sitemap_targets(self):
        model = sa.run_audit(f"{self.base}/sitemap.xml", max_pages=0, workers=4)
        self.assertNotIn("internal-link-broken", model["grouped"])

    def test_check_links_finds_off_sitemap_404(self):
        model = sa.run_audit(f"{self.base}/sitemap.xml", max_pages=0, workers=4,
                             check_links=True)
        urls = [u for _, u, _ in model["grouped"].get("internal-link-broken", [])]
        self.assertEqual(urls, [f"{self.base}/ghost"])
        self.assertIn("not in sitemap", model["grouped"]["internal-link-broken"][0][2])


class TestRobotsValidation(unittest.TestCase):
    """Sitemap-listed-but-robots-blocked URLs are indexation blockers."""

    def _run(self, robots_body, pages_extra=()):
        routes = {}
        srv = make_server(routes)
        try:
            base = f"http://127.0.0.1:{srv.server_address[1]}"
            pages = [f"{base}/", f"{base}/blocked/secret"] + [f"{base}{p}" for p in pages_extra]
            routes.update({
                "/robots.txt": (200, TXT, robots_body.encode()),
                "/sitemap.xml": (200, XML, sitemap_xml(pages)),
                "/": (200, HTML, html_page(title="Homepage title of the fixture site ok",
                                           canonical=f"{base}/")),
                "/blocked/secret": (200, HTML, html_page(
                    title="Blocked page title of the fixture ok",
                    canonical=f"{base}/blocked/secret")),
            })
            return base, sa.run_audit(f"{base}/sitemap.xml", max_pages=0, workers=4)
        finally:
            srv.shutdown()

    def test_blocked_sitemap_url_is_blocker(self):
        base, model = self._run("User-agent: *\nDisallow: /blocked/\n")
        items = model["grouped"].get("robots-blocked-sitemap-url", [])
        self.assertEqual([u for _, u, _ in items], [f"{base}/blocked/secret"])
        self.assertEqual(items[0][0], sa.BLOCKER)
        self.assertTrue(sa.has_blockers(model["grouped"]))

    def test_blanket_disallow_reported_once(self):
        base, model = self._run("User-agent: *\nDisallow: /\n")
        self.assertIn("robots-blocks-all", model["grouped"])
        # blanket case must not additionally list every sitemap URL
        self.assertNotIn("robots-blocked-sitemap-url", model["grouped"])

    def test_unblocked_site_clean(self):
        base, model = self._run("User-agent: *\nAllow: /\n")
        self.assertNotIn("robots-blocked-sitemap-url", model["grouped"])

    def test_other_bot_blanket_is_not_a_site_block(self):
        # Disallow: / in ANOTHER bot's group must not read as blocking the site
        base, model = self._run(
            "User-agent: some-other-bot\nDisallow: /\n\nUser-agent: *\nAllow: /\n")
        self.assertNotIn("robots-blocks-all", model["grouped"])
        self.assertNotIn("robots-blocked-sitemap-url", model["grouped"])
        self.assertFalse(sa.has_blockers(model["grouped"]))

    def test_audit_ua_block_is_coverage_note_not_blocker(self):
        base, model = self._run("User-agent: performify-seo-audit\nDisallow: /\n"
                                "\nUser-agent: *\nAllow: /\n")
        self.assertNotIn("robots-blocks-all", model["grouped"])
        self.assertNotIn("robots-blocked-sitemap-url", model["grouped"])
        items = model["grouped"].get("robots-blocks-audit-ua", [])
        self.assertTrue(items)
        self.assertEqual(items[0][0], sa.OBSERVATION)
        self.assertFalse(sa.has_blockers(model["grouped"]))

    def test_site_block_derived_and_ranked_sitewide(self):
        base, model = self._run("User-agent: *\nDisallow: /\n")
        top = [e for e in model["ranked"] if e["class"] in (sa.BLOCKER, sa.MATERIAL)][0]
        self.assertEqual(top["code"], "robots-blocks-all")
        # one technical URL, but scope = every crawled page
        self.assertEqual(top["affected"], len(model["results"]))
        self.assertEqual(top["score"], 25.0)


class TestRankingScope(unittest.TestCase):
    def test_sitewide_outranks_page_level(self):
        grouped = {
            "robots-blocks-all": [(sa.BLOCKER, "https://x/robots.txt", None)],
            "missing-title": [(sa.MATERIAL, f"https://x/p{i}", None) for i in range(10)],
        }
        ranked = sa.rank_issues(grouped, 100, {"robots-blocks-all": 100})
        self.assertEqual(ranked[0]["code"], "robots-blocks-all")
        self.assertEqual(ranked[0]["score"], 25.0)     # 5 x 5 x 100/100
        self.assertEqual(ranked[1]["score"], 2.0)      # 4 x 5 x 10/100

    def test_shared_target_scope_uses_source_pages(self):
        grouped = {"canonical-target-broken": [(sa.MATERIAL, "https://x/dead", None)]}
        ranked = sa.rank_issues(grouped, 100, {"canonical-target-broken": 37})
        self.assertEqual(ranked[0]["n"], 1)
        self.assertEqual(ranked[0]["affected"], 37)
        self.assertEqual(ranked[0]["score"], round(4 * 4 * 0.37, 2))


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


# ---------------------------------------------------------------- stratified sampling
class TestStratifiedSampling(unittest.TestCase):
    BASE = "https://example.com"

    def _site(self):
        home = [f"{self.BASE}/", f"{self.BASE}/about", f"{self.BASE}/contact"]
        products = [f"{self.BASE}/products/p{i}" for i in range(90)]
        blog = [f"{self.BASE}/blog/post-{i}" for i in range(4)]
        source_of = {u: f"{self.BASE}/pages.xml" for u in home}
        source_of.update({u: f"{self.BASE}/products.xml" for u in products})
        source_of.update({u: f"{self.BASE}/blog.xml" for u in blog})
        return home + products + blog, source_of

    def test_respects_max_pages(self):
        urls, source_of = self._site()
        sample = sa.stratified_sample(urls, source_of, 20)
        self.assertEqual(len(sample), 20)
        self.assertEqual(len(set(sample)), 20)

    def test_homepage_and_top_level_always_included(self):
        urls, source_of = self._site()
        sample = sa.stratified_sample(urls, source_of, 20)
        for u in (f"{self.BASE}/", f"{self.BASE}/about", f"{self.BASE}/contact"):
            self.assertIn(u, sample)

    def test_minimum_three_per_sitemap(self):
        urls, source_of = self._site()
        sample = sa.stratified_sample(urls, source_of, 20)
        blog_sampled = [u for u in sample if "/blog/" in u]
        self.assertGreaterEqual(len(blog_sampled), 3)

    def test_proportional_to_stratum_size(self):
        urls, source_of = self._site()
        sample = sa.stratified_sample(urls, source_of, 20)
        products_sampled = [u for u in sample if "/products/" in u]
        blog_sampled = [u for u in sample if "/blog/" in u]
        self.assertGreater(len(products_sampled), len(blog_sampled))

    def test_zero_means_crawl_all(self):
        urls, source_of = self._site()
        self.assertEqual(sa.stratified_sample(urls, source_of, 0), urls)

    def test_small_sitemap_untouched(self):
        urls, source_of = self._site()
        self.assertEqual(sa.stratified_sample(urls, source_of, 500), urls)

    def test_more_top_level_than_budget(self):
        urls = [f"{self.BASE}/page-{i}" for i in range(50)]
        source_of = {u: f"{self.BASE}/sitemap.xml" for u in urls}
        sample = sa.stratified_sample(urls, source_of, 10)
        self.assertEqual(len(sample), 10)

    def test_many_sitemaps_tight_budget(self):
        urls, source_of = [], {}
        for s in range(10):
            for i in range(5):
                u = f"{self.BASE}/s{s}/p{i}"
                urls.append(u)
                source_of[u] = f"{self.BASE}/s{s}.xml"
        sample = sa.stratified_sample(urls, source_of, 12)
        self.assertEqual(len(sample), 12)


if __name__ == "__main__":
    unittest.main(verbosity=2)
