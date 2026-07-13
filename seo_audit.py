#!/usr/bin/env python3
"""seo_audit.py — streamlined technical SEO audit from a sitemap URL.

Give it a sitemap (or just the site root) and it crawls up to --max-pages URLs,
runs ~20 checks, and prints a 1-2 page summary to the terminal. Stdlib only —
no installs, runs anywhere Python 3.9+ exists.

    python3 seo_audit.py https://example.com/sitemap.xml
    python3 seo_audit.py https://example.com --max-pages 150 --markdown report.md

Knowledge/thresholds are documented in the technical-seo-audit method note that
lives alongside this file.
"""

import argparse
import concurrent.futures
import gzip
import html
import json
import re
import statistics
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import urllib.robotparser
from collections import Counter, defaultdict
from html.parser import HTMLParser

UA = "Mozilla/5.0 (compatible; performify-seo-audit/1.0; +https://performify.co.uk)"
TIMEOUT = 15
MAX_REDIRECTS = 5

# ---------------------------------------------------------------- issue classes
# Four classes, replacing the old CRITICAL/WARNING/NOTICE severities:
#   BLOCKER     — pages cannot or should not be indexed
#   MATERIAL    — likely to affect discovery, consolidation, or presentation
#   OPPORTUNITY — worth reviewing; phrased as review-not-fix
#   OBSERVATION — context only, no action implied
BLOCKER, MATERIAL, OPPORTUNITY, OBSERVATION = "BLOCKER", "MATERIAL", "OPPORTUNITY", "OBSERVATION"
CLASS_ORDER = (BLOCKER, MATERIAL, OPPORTUNITY, OBSERVATION)
CLASS_LABELS = {
    BLOCKER: "INDEXATION BLOCKERS",
    MATERIAL: "MATERIAL ISSUES",
    OPPORTUNITY: "OPPORTUNITIES",
    OBSERVATION: "OBSERVATIONS",
}

CLASS_OF = {
    # indexation blockers — reserved for "cannot be crawled/indexed/consolidated"
    "http-error": BLOCKER,
    "fetch-failed": BLOCKER,
    "noindex-in-sitemap": BLOCKER,
    "redirect-problem": BLOCKER,
    "robots-blocks-all": BLOCKER,
    "robots-blocked-sitemap-url": BLOCKER,
    # material issues (a page without a title or with mixed content still
    # gets crawled and indexed — serious, but not a blocker)
    "missing-title": MATERIAL,
    "mixed-content": MATERIAL,
    "redirected-sitemap-url": MATERIAL,
    "duplicate-title": MATERIAL,
    "canonical-mismatch": MATERIAL,
    "canonical-target-broken": MATERIAL,
    "canonical-target-redirects": MATERIAL,
    "canonical-target-noindex": MATERIAL,
    "internal-link-broken": MATERIAL,
    "rate-limited": MATERIAL,
    "no-https-redirect": MATERIAL,
    # opportunities
    "duplicate-description": OPPORTUNITY,
    "missing-description": OPPORTUNITY,
    "missing-h1": OPPORTUNITY,
    "title-length": OPPORTUNITY,
    "description-length": OPPORTUNITY,
    "thin-content": OPPORTUNITY,
    "missing-canonical": OPPORTUNITY,
    "slow-fetch-outlier": OPPORTUNITY,
    "robots-missing": OPPORTUNITY,
    "internal-link-redirect": OPPORTUNITY,
    "internal-link-noindex": OPPORTUNITY,
    "orphan-page": OPPORTUNITY,
    # observations
    "multiple-h1": OBSERVATION,
    "no-structured-data": OBSERVATION,
    "missing-viewport": OBSERVATION,
    "large-html": OBSERVATION,
    "non-html": OBSERVATION,
    "images-missing-alt": OBSERVATION,
    "robots-no-sitemap": OBSERVATION,
    "internal-link-canonicalised": OBSERVATION,
    "cross-domain-canonical": OBSERVATION,
    "canonical-consolidation": OBSERVATION,
    "robots-blocks-audit-ua": OBSERVATION,
}

# issues whose subject is one technical URL but whose scope is the whole site —
# ranked with affected = every crawled page, not 1/crawled
SITE_WIDE = {"robots-blocks-all", "no-https-redirect", "robots-missing",
             "robots-no-sitemap", "robots-blocks-audit-ua"}

FIX_HINTS = {
    # blockers — direct fix language
    "http-error": "Remove dead URLs from the sitemap or restore the pages (a sitemap must only list 200s).",
    "fetch-failed": "Check DNS/TLS/WAF — the crawler could not fetch these at all; Googlebot may be failing too.",
    "noindex-in-sitemap": "A noindexed URL in the sitemap sends Google contradictory signals — drop it from the sitemap or remove the noindex.",
    "redirect-problem": "Redirect loops or chains beyond 5 hops mean the page cannot be reached or indexed.",
    "robots-blocks-all": "robots.txt blocks Googlebot from the homepage and essentially every sitemap URL — the site cannot be crawled.",
    "robots-blocked-sitemap-url": "These URLs are listed in the sitemap but robots.txt blocks Googlebot — contradictory signals; unblock them or drop them from the sitemap.",
    # material — direct but measured
    "missing-title": "Every indexable page needs a unique <title>; it is the single strongest on-page relevance signal.",
    "mixed-content": "Load all subresources over https — browsers block or warn, and it erodes trust signals.",
    "redirected-sitemap-url": "Sitemaps should list final URLs, not redirects — update the sitemap generator.",
    "duplicate-title": "Duplicate titles make pages compete with each other; differentiate or canonicalise.",
    "canonical-mismatch": "Page canonicalises elsewhere but sits in the sitemap — pick one: fix the canonical or remove from sitemap.",
    "canonical-target-broken": "Canonical points at a URL that does not return 200 — the consolidation signal is being thrown away.",
    "canonical-target-redirects": "Canonical points at a redirecting URL — point it at the final destination instead.",
    "canonical-target-noindex": "Canonical points at a noindexed URL — contradictory signals; the target cannot consolidate anything.",
    "internal-link-broken": "Internal links point at pages returning 4xx/5xx — fix the links or restore the targets.",
    "rate-limited": "The origin throttled the crawl (429/503) even after backoff — these pages were NOT audited. Rerun with --workers 2 --delay 1.",
    "no-https-redirect": "http:// version doesn't redirect to https — duplicate-protocol indexing risk.",
    # opportunities — review-not-fix language
    "duplicate-description": "Several pages share one description — review whether per-page copy is worth writing on key templates; Google generates its own snippets anyway.",
    "missing-description": "No meta description — Google generates the snippet. Review whether hand-written descriptions would present key pages better; not a ranking factor.",
    "missing-h1": "No <h1> found in the raw HTML — review whether the main heading is marked up as one; worth tidying, not urgent.",
    "title-length": "Title length is outside the typical 30-60 char display band — review whether it still describes the page; length alone is not a ranking issue.",
    "description-length": "Description length is outside the typical 70-160 char display band — review whether the snippet reads well; length alone is not a ranking issue.",
    "thin-content": "Little visible text in the raw HTML — review whether these pages carry enough content (JS-rendered content is not counted here).",
    "missing-canonical": "No self-referencing canonical — review whether parameter or duplicate URLs exist; harmless on clean URL structures.",
    "slow-fetch-outlier": "These pages fetch markedly slower than the site's own median — review caching/origin for this template.",
    "robots-missing": "No robots.txt — review adding one that references the sitemap so all crawlers find it.",
    "internal-link-redirect": "Internal links resolve through redirects — review updating them to the final URLs.",
    "internal-link-noindex": "Internal links point at noindexed pages — review whether those targets should be linked or indexed.",
    "orphan-page": "Sitemap URLs with no internal links found in the crawl — review whether they should be linked from somewhere.",
    # observations — context only
    "multiple-h1": "More than one <h1> — common with HTML5 sectioning and rarely a problem; context only.",
    "no-structured-data": "No JSON-LD detected — an enhancement opportunity (rich results), not a defect.",
    "missing-viewport": "No viewport meta found — context: check how these pages render on mobile.",
    "large-html": "HTML over 300KB — context for anyone diagnosing page speed.",
    "non-html": "Sitemap URL serves a non-HTML content type — fine if intentional (PDFs etc.).",
    "images-missing-alt": "Images without any alt attribute — context for accessibility/image search; alt=\"\" (decorative) is not counted.",
    "robots-no-sitemap": "robots.txt does not reference the sitemap — harmless if the sitemap is submitted in Search Console.",
    "internal-link-canonicalised": "Internal links point at pages that canonicalise elsewhere — context; often deliberate (variants, tracking pages).",
    "cross-domain-canonical": "Canonical points at another domain — often deliberate (syndication); verify it is intended.",
    "canonical-consolidation": "Many pages canonicalise to one URL — context; verify the consolidation is intended.",
    "robots-blocks-audit-ua": "robots.txt blocks this audit's crawler but not Googlebot — no SEO impact; the origin may treat unknown bots differently, so audit coverage could be limited.",
}

# human labels for the default report (codes stay in JSON / --explain-scores)
ISSUE_LABELS = {
    "http-error": "Broken sitemap URLs (4xx/5xx)",
    "fetch-failed": "Unreachable sitemap URLs",
    "noindex-in-sitemap": "Noindexed pages in the sitemap",
    "redirect-problem": "Redirect loops / over-long chains",
    "robots-blocks-all": "robots.txt blocks the whole site",
    "robots-blocked-sitemap-url": "Sitemap URLs blocked by robots.txt",
    "missing-title": "Pages with no <title>",
    "mixed-content": "Mixed content on https pages",
    "redirected-sitemap-url": "Sitemap URLs that redirect",
    "duplicate-title": "Duplicate titles",
    "duplicate-description": "Shared meta descriptions",
    "canonical-mismatch": "Pages canonicalising elsewhere",
    "canonical-target-broken": "Broken canonical targets",
    "canonical-target-redirects": "Canonical targets that redirect",
    "canonical-target-noindex": "Noindexed canonical targets",
    "internal-link-broken": "Internal links to broken pages",
    "rate-limited": "Rate-limited (not audited)",
    "no-https-redirect": "No http-to-https redirect",
    "missing-description": "Missing meta descriptions",
    "missing-h1": "Pages without an <h1>",
    "title-length": "Titles outside the display band",
    "description-length": "Descriptions outside the display band",
    "thin-content": "Little visible text",
    "missing-canonical": "No self-canonical",
    "slow-fetch-outlier": "Slow-fetch outliers",
    "robots-missing": "No robots.txt",
    "internal-link-redirect": "Internal links via redirects",
    "internal-link-noindex": "Internal links to noindexed pages",
    "orphan-page": "Sitemap URLs with no internal links",
    "multiple-h1": "Multiple <h1>s",
    "no-structured-data": "No JSON-LD structured data",
    "missing-viewport": "No viewport meta",
    "large-html": "HTML over 300KB",
    "non-html": "Non-HTML sitemap entries",
    "images-missing-alt": "Images without alt attributes",
    "robots-no-sitemap": "robots.txt without a Sitemap line",
    "internal-link-canonicalised": "Internal links to canonicalised-away pages",
    "cross-domain-canonical": "Cross-domain canonicals",
    "canonical-consolidation": "Many pages canonicalising to one URL",
    "robots-blocks-audit-ua": "robots.txt blocks the audit crawler",
}


def label(code):
    return ISSUE_LABELS.get(code, code)


def level_word(x):
    return "high" if x >= 4 else "medium" if x == 3 else "low"


def issue(code, detail=None):
    return (CLASS_OF[code], code, detail)


# ---------------------------------------------------------------- impact ranking
# Transparent, static scoring: score = impact x confidence x affected_fraction,
# where affected_fraction = distinct affected URLs / crawled pages (capped at 1).
# No opaque logic — any ranking in the report can be recomputed by hand from
# this table. impact = how much it matters when real; confidence = how sure the
# check is that it matters (1-5 each).
IMPACT = {
    "http-error": (5, 5),
    "fetch-failed": (5, 4),
    "noindex-in-sitemap": (5, 5),
    "redirect-problem": (5, 4),
    "robots-blocks-all": (5, 5),
    "robots-blocked-sitemap-url": (5, 5),
    "missing-title": (4, 5),
    "mixed-content": (4, 4),
    "redirected-sitemap-url": (3, 4),
    "duplicate-title": (3, 4),
    "duplicate-description": (2, 3),
    "canonical-mismatch": (3, 4),
    "canonical-target-broken": (4, 4),
    "canonical-target-redirects": (3, 4),
    "canonical-target-noindex": (4, 4),
    "internal-link-broken": (3, 4),
    "rate-limited": (3, 3),
    "no-https-redirect": (3, 4),
    "missing-description": (2, 3),
    "missing-h1": (2, 2),
    "title-length": (1, 2),
    "description-length": (1, 2),
    "thin-content": (2, 2),
    "missing-canonical": (2, 2),
    "slow-fetch-outlier": (2, 2),
    "robots-missing": (1, 2),   # site-wide scope (fraction 1.0) already weights it
    "internal-link-redirect": (2, 3),
    "internal-link-noindex": (2, 3),
    "orphan-page": (3, 2),
    # observations all score at the floor
}

# short human phrases for the generated verdict ({n}/{s} filled in)
ISSUE_PHRASES = {
    "http-error": "{n} sitemap URL{s} returning 4xx/5xx",
    "fetch-failed": "{n} sitemap URL{s} that could not be fetched at all",
    "noindex-in-sitemap": "{n} noindexed page{s} listed in the sitemap",
    "redirect-problem": "{n} URL{s} with redirect loops or over-long chains",
    "robots-blocks-all": "a robots.txt that blocks the whole site",
    "robots-blocked-sitemap-url": "{n} sitemap URL{s} blocked by robots.txt",
    "missing-title": "{n} page{s} with no <title>",
    "mixed-content": "{n} https page{s} loading http subresources",
    "redirected-sitemap-url": "{n} sitemap URL{s} that redirect",
    "duplicate-title": "{n} page{s} sharing duplicate titles",
    "duplicate-description": "{n} page{s} sharing duplicate meta descriptions",
    "canonical-mismatch": "{n} sitemap page{s} canonicalising elsewhere",
    "canonical-target-broken": "{n} canonical target{s} not returning 200",
    "canonical-target-redirects": "{n} canonical target{s} that redirect",
    "canonical-target-noindex": "{n} noindexed canonical target{s}",
    "internal-link-broken": "{n} internally linked URL{s} returning 4xx/5xx",
    "rate-limited": "{n} URL{s} the origin rate-limited (a coverage gap, not a fault)",
    "no-https-redirect": "an http version that does not redirect to https",
    "missing-description": "{n} page{s} without a meta description",
    "missing-h1": "{n} page{s} without an <h1>",
    "title-length": "{n} page{s} with titles outside the typical display length",
    "description-length": "{n} page{s} with descriptions outside the typical display length",
    "multiple-h1": "{n} page{s} with more than one <h1>",
    "thin-content": "{n} page{s} with little visible text",
    "missing-canonical": "{n} page{s} without a self-canonical",
    "slow-fetch-outlier": "{n} page{s} fetching far slower than the site median",
    "orphan-page": "{n} sitemap URL{s} with no internal links pointing at them",
    "robots-missing": "a missing robots.txt",
}


def phrase(code, n):
    s = "s" if n != 1 else ""
    tpl = ISSUE_PHRASES.get(code)
    return tpl.format(n=n, s=s) if tpl else f"{n} URL{s} with {code}"


def rank_issues(grouped, n_crawled, affected_pages=None):
    """Score every issue group. Returns dicts sorted by score desc.

    affected_pages overrides the scope per code: for issues whose subject is
    one technical URL (a sitewide robots block, a canonical target shared by
    37 pages) the scope is the crawled pages affected, not the issue-URL count.
    """
    affected_pages = affected_pages or {}
    ranked = []
    for code, items in grouped.items():
        if not items:
            continue
        cls = items[0][0]
        urls = list(dict.fromkeys(u for _, u, _ in items))
        affected = affected_pages.get(code, len(urls))
        imp, conf = IMPACT.get(code, (1, 1))
        frac = min(1.0, affected / n_crawled) if n_crawled else 0.0
        ranked.append({"code": code, "class": cls, "n": len(urls), "affected": affected,
                       "impact": imp, "confidence": conf, "fraction": round(frac, 3),
                       "score": round(imp * conf * frac, 2)})
    ranked.sort(key=lambda e: (-e["score"], CLASS_ORDER.index(e["class"]), e["code"]))
    return ranked


# ---------------------------------------------------------------- clustering
def first_segment(url):
    seg = urllib.parse.urlparse(url).path.strip("/").split("/")[0]
    return f"/{seg}/" if seg else "/"


def cluster_issue(urls, crawled_urls, source_of):
    """Cluster issue URLs by (source sitemap, first path segment).

    Denominator = crawled pages in the same cluster, so "42/48 product pages"
    is always a claim about pages actually looked at. Sorted by size desc.
    """
    denom = Counter((source_of.get(u), first_segment(u)) for u in crawled_urls)
    groups = defaultdict(list)
    for u in urls:
        groups[(source_of.get(u), first_segment(u))].append(u)
    out = []
    for (sm, seg), us in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        out.append({"n": len(us), "of": denom.get((sm, seg), 0), "segment": seg,
                    "sitemap": sm, "examples": us[:3]})
    return out


def cluster_line(cl):
    """One human line for a cluster, or None if too weak to claim a pattern."""
    if cl["n"] < 2 or not cl["of"]:
        return None
    where = f"under {cl['segment']}" if cl["segment"] != "/" else "at the top level"
    sm = ""
    if cl["sitemap"]:
        sm_name = urllib.parse.urlparse(cl["sitemap"]).path.rsplit("/", 1)[-1] or cl["sitemap"]
        sm = f" (sitemap {sm_name})"
    template = " — likely template-level" if cl["n"] >= 3 and cl["n"] / cl["of"] >= 0.8 else ""
    return f"{cl['n']}/{cl['of']} crawled pages {where}{sm}{template}"


def build_verdict(grouped, ranked, n_crawled, crawled_urls, source_of):
    """1-2 sentences: indexation health first, then the highest-scored issue."""
    blockers = [e for e in ranked if e["class"] == BLOCKER]
    if blockers:
        n_urls = len({u for e in blockers for _, u, _ in grouped[e["code"]]})
        s1 = f"Indexation problems affect {n_urls} of {n_crawled} crawled URLs."
    else:
        s1 = "Indexation is broadly healthy."
    actionable = [e for e in ranked if e["class"] in (BLOCKER, MATERIAL)]
    pool = actionable or [e for e in ranked if e["class"] == OPPORTUNITY]
    if not pool:
        return s1 + " No issues of note were found."
    top = pool[0]
    conc = ""
    urls = list(dict.fromkeys(u for _, u, _ in grouped[top["code"]]))
    clusters = cluster_issue(urls, crawled_urls, source_of)
    if clusters and clusters[0]["of"] and clusters[0]["n"] >= 2 \
            and clusters[0]["n"] / top["n"] >= 0.6 and clusters[0]["segment"] != "/":
        conc = f", concentrated under {clusters[0]['segment']}"
    lead = ("The main issue is " if actionable
            else "Nothing material was found; the top review item is ")
    return f"{s1} {lead}{phrase(top['code'], top['n'])}{conc}."

RATE_LIMIT_STATUSES = (429, 503)


# ---------------------------------------------------------------- fetching
class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


_opener = urllib.request.build_opener(_NoRedirect)


def fetch(url, timeout=TIMEOUT, follow=True):
    """Fetch a URL following redirects manually. Returns a dict.

    Timing: "ttfb" = start until the final response's headers arrive (opener
    returns); "elapsed" = start until the body is fully read. Both cumulative
    across any redirect chain. Never conflate the two in reporting.
    """
    chain = []
    current = url
    start = time.monotonic()
    ttfb = None
    for _ in range(MAX_REDIRECTS + 1):
        req = urllib.request.Request(current, headers={"User-Agent": UA, "Accept-Encoding": "gzip"})
        try:
            resp = _opener.open(req, timeout=timeout)
            ttfb = time.monotonic() - start
            status, headers, body = resp.status, resp.headers, resp.read()
        except urllib.error.HTTPError as e:
            ttfb = time.monotonic() - start
            status, headers = e.code, e.headers
            try:
                body = e.read()
            except Exception:
                body = b""
        except Exception as e:
            return {"url": url, "final_url": current, "status": None, "chain": chain,
                    "error": f"{type(e).__name__}: {e}", "elapsed": time.monotonic() - start,
                    "ttfb": None, "headers": {}, "body": b""}
        if status in (301, 302, 303, 307, 308) and follow:
            loc = headers.get("Location")
            if not loc:
                break
            nxt = urllib.parse.urljoin(current, loc)
            chain.append((status, current))
            if any(nxt == c[1] for c in chain) or nxt == current:
                return {"url": url, "final_url": nxt, "status": status, "chain": chain,
                        "error": "redirect loop", "elapsed": time.monotonic() - start,
                        "ttfb": ttfb, "headers": dict(headers), "body": b""}
            current = nxt
            continue
        if (headers.get("Content-Encoding") == "gzip" or body[:2] == b"\x1f\x8b") and body:
            try:
                body = gzip.decompress(body)
            except OSError:
                pass
        return {"url": url, "final_url": current, "status": status, "chain": chain,
                "error": None, "elapsed": time.monotonic() - start,
                "ttfb": ttfb, "headers": dict(headers), "body": body}
    return {"url": url, "final_url": current, "status": None, "chain": chain,
            "error": "too many redirects", "elapsed": time.monotonic() - start,
            "ttfb": ttfb, "headers": {}, "body": b""}


def fetch_with_retry(url, timeout=TIMEOUT, retries=2):
    """fetch(), backing off and retrying on 429/503 (honours Retry-After, capped 15s)."""
    r = fetch(url, timeout)
    for attempt in range(retries):
        if r["status"] not in RATE_LIMIT_STATUSES:
            break
        ra = r["headers"].get("Retry-After")
        try:
            wait = min(float(ra), 15.0) if ra else 2.0 * (attempt + 1)
        except ValueError:
            wait = 2.0 * (attempt + 1)
        time.sleep(wait)
        r = fetch(url, timeout)
    return r


def decode_body(result):
    ctype = result["headers"].get("Content-Type", "")
    m = re.search(r"charset=([\w-]+)", ctype)
    enc = m.group(1) if m else "utf-8"
    try:
        return result["body"].decode(enc, errors="replace")
    except LookupError:
        return result["body"].decode("utf-8", errors="replace")


# ---------------------------------------------------------------- sitemap
def parse_sitemap_xml(text):
    """Return (page_urls, child_sitemap_urls) from a sitemap or sitemap index."""
    pages, children = [], []
    is_index = "<sitemapindex" in text[:2000]
    for m in re.finditer(r"<loc>\s*(.*?)\s*</loc>", text, re.S):
        loc = html.unescape(m.group(1)).strip()
        (children if is_index else pages).append(loc)
    return pages, children


def discover_and_load_sitemaps(start_url, log):
    """Resolve a sitemap URL or site root into a flat list of page URLs.

    Returns (page_urls, sitemaps_probed, source_of) where source_of maps each
    page URL to the sitemap it was listed in (first occurrence wins) — kept
    for stratified sampling and pattern clustering.
    """
    parsed = urllib.parse.urlparse(start_url)
    candidates, fallbacks = [], []
    looks_like_sitemap = parsed.path.rstrip("/").endswith((".xml", ".xml.gz")) or "sitemap" in parsed.path.lower()
    attempts = {}  # url -> outcome, for diagnosable discovery failures

    def outcome(r):
        if r["error"]:
            return r["error"]
        s = f"HTTP {r['status']}"
        if r["status"] in RATE_LIMIT_STATUSES:
            s += " — origin is throttling/blocking this client; wait a few minutes, then retry with --workers 2 --delay 1"
        return s

    if looks_like_sitemap:
        candidates.append(start_url)
    else:
        root = f"{parsed.scheme}://{parsed.netloc}"
        robots = fetch_with_retry(root + "/robots.txt")
        if robots["status"] == 200:
            for line in decode_body(robots).splitlines():
                if line.lower().startswith("sitemap:"):
                    candidates.append(line.split(":", 1)[1].strip())
        else:
            attempts[root + "/robots.txt"] = outcome(robots)
        fallbacks = [root + "/sitemap.xml", root + "/sitemap_index.xml"]

    seen, pages, source_of = set(), [], {}
    queue = list(dict.fromkeys(candidates + fallbacks))
    while queue and len(seen) < 50:
        sm = queue.pop(0)
        if sm in seen:
            continue
        seen.add(sm)
        r = fetch_with_retry(sm)
        if r["status"] != 200 or not r["body"]:
            attempts[sm] = outcome(r)
            continue
        p, children = parse_sitemap_xml(decode_body(r))
        if p or children:
            log(f"  sitemap: {sm} — {len(p)} pages, {len(children)} child sitemaps")
            attempts[sm] = f"ok — {len(p)} pages, {len(children)} child sitemaps"
        else:
            attempts[sm] = "HTTP 200 but no <loc> entries"
        pages += p
        for u in p:
            source_of.setdefault(u, sm)
        queue += children
        if pages:
            # a working sitemap was found — no need to probe the guessed fallbacks
            queue = [q for q in queue if q not in fallbacks]
    return list(dict.fromkeys(pages)), attempts, source_of


# ---------------------------------------------------------------- HTML parsing
class PageParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.title = None
        self.description = None
        self.robots_meta = None
        self.viewport = None
        self.canonical = None
        self.h1s = []
        self.img_total = 0
        self.img_missing_alt = 0
        self.jsonld = []
        self.links = []
        self.mixed = []
        self.words = 0
        self._stack = []          # element capture: title / h1 / script(ld+json)
        self._buf = []
        self._skip_depth = 0      # inside script/style/noscript/template

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag in ("script", "style", "noscript", "template"):
            self._skip_depth += 1
            if tag == "script" and a.get("type", "").strip().lower() == "application/ld+json":
                self._stack.append("jsonld")
                self._buf = []
            return
        if tag == "meta":
            name = (a.get("name") or a.get("property") or "").lower()
            content = a.get("content", "")
            if name == "description" and self.description is None:
                self.description = content.strip()
            elif name == "robots":
                self.robots_meta = content.lower()
            elif name == "viewport":
                self.viewport = content
        elif tag == "link" and (a.get("rel") or "").lower() == "canonical" and self.canonical is None:
            self.canonical = (a.get("href") or "").strip()
        elif tag == "title" and self.title is None:
            self._stack.append("title")
            self._buf = []
        elif tag == "h1":
            self._stack.append("h1")
            self._buf = []
        elif tag == "img":
            self.img_total += 1
            # alt="" is deliberate (decorative image, screen-reader skip) — only an
            # absent alt attribute is a defect
            if a.get("alt") is None:
                self.img_missing_alt += 1
            src = a.get("src", "")
            if src.startswith("http://"):
                self.mixed.append(src)
        elif tag == "a" and a.get("href"):
            self.links.append(a["href"])
        elif tag in ("iframe", "source", "video", "audio", "embed"):
            src = a.get("src", "")
            if src.startswith("http://"):
                self.mixed.append(src)

    def handle_endtag(self, tag):
        if tag in ("script", "style", "noscript", "template"):
            if self._stack and self._stack[-1] == "jsonld":
                self._stack.pop()
                raw = "".join(self._buf)
                try:
                    self.jsonld.append(json.loads(raw))
                except (ValueError, TypeError):
                    self.jsonld.append(None)   # present but invalid
            self._skip_depth = max(0, self._skip_depth - 1)
            return
        if self._stack and self._stack[-1] in ("title", "h1") and tag == self._stack[-1]:
            text = " ".join("".join(self._buf).split())
            if self._stack[-1] == "title":
                self.title = text
            else:
                self.h1s.append(text)
            self._stack.pop()

    def handle_data(self, data):
        if self._stack:
            self._buf.append(data)
        if not self._skip_depth:
            self.words += len(data.split())


def norm_url(u):
    """Normalise for canonical comparison: scheme+host lowered, no fragment, no trailing
    slash, percent-escapes upper-cased (RFC 3986: %e2 == %E2)."""
    p = urllib.parse.urlsplit(u.strip())
    path = p.path.rstrip("/") or "/"
    upper_esc = lambda s: re.sub(r"%[0-9a-fA-F]{2}", lambda m: m.group(0).upper(), s)
    return urllib.parse.urlunsplit(
        (p.scheme.lower(), p.netloc.lower(), upper_esc(path), upper_esc(p.query), ""))


# ---------------------------------------------------------------- audit
def audit_page(result):
    """Run per-page checks. Returns (issues, pagedata)."""
    issues = []  # (class, code, detail-or-None)
    url = result["url"]
    data = {"url": url, "status": result["status"],
            "fetch_time": round(result["elapsed"], 3),
            "ttfb": round(result["ttfb"], 3) if result.get("ttfb") is not None else None}

    if result["error"]:
        if "redirect" in result["error"]:
            issues.append(issue("redirect-problem", result["error"]))
        else:
            issues.append(issue("fetch-failed", result["error"]))
        return issues, data
    if result["status"] in RATE_LIMIT_STATUSES:
        issues.append(issue("rate-limited", f"HTTP {result['status']} after retries"))
        return issues, data
    if result["status"] >= 400:
        issues.append(issue("http-error", f"HTTP {result['status']}"))
        return issues, data
    if result["chain"]:
        issues.append(issue("redirected-sitemap-url",
                            f"{len(result['chain'])} hop(s) -> {result['final_url']}"))

    ctype = result["headers"].get("Content-Type", "")
    if "html" not in ctype:
        issues.append(issue("non-html", ctype.split(";")[0] or "no content-type"))
        return issues, data

    xrobots = (result["headers"].get("X-Robots-Tag") or "").lower()
    text = decode_body(result)
    p = PageParser()
    try:
        p.feed(text)
        p.close()
    except Exception:
        pass

    # indexability
    robots = f"{p.robots_meta or ''} {xrobots}"
    noindex = "noindex" in robots
    if noindex:
        issues.append(issue("noindex-in-sitemap", None))

    # title
    if not p.title:
        issues.append(issue("missing-title", None))
    elif not 30 <= len(p.title) <= 60:
        issues.append(issue("title-length", f"{len(p.title)} chars"))

    # description
    if not p.description:
        issues.append(issue("missing-description", None))
    elif not 70 <= len(p.description) <= 160:
        issues.append(issue("description-length", f"{len(p.description)} chars"))

    # headings
    if not p.h1s:
        issues.append(issue("missing-h1", None))
    elif len(p.h1s) > 1:
        issues.append(issue("multiple-h1", f"{len(p.h1s)} h1s"))

    # canonical
    canon_abs = None
    if not p.canonical:
        issues.append(issue("missing-canonical", None))
    else:
        canon_abs = urllib.parse.urljoin(result["final_url"], p.canonical)
        if norm_url(canon_abs) != norm_url(result["final_url"]):
            issues.append(issue("canonical-mismatch", f"-> {canon_abs}"))

    # content
    if p.words < 200:
        issues.append(issue("thin-content", f"~{p.words} words"))
    if p.img_missing_alt:
        issues.append(issue("images-missing-alt", f"{p.img_missing_alt}/{p.img_total} images"))
    if not p.jsonld:
        issues.append(issue("no-structured-data", None))
    if not p.viewport:
        issues.append(issue("missing-viewport", None))

    # protocol / size (slow pages are flagged site-level, relative to the median)
    if result["final_url"].startswith("https://") and p.mixed:
        issues.append(issue("mixed-content", p.mixed[0]))
    if len(result["body"]) > 300_000:
        issues.append(issue("large-html", f"{len(result['body']) // 1024}KB"))

    data.update({
        "title": p.title, "description": p.description,
        "h1": p.h1s[0] if p.h1s else None, "words": p.words,
        "size_kb": len(result["body"]) // 1024,
        "noindex": noindex,
        "canonical_abs": canon_abs,
        "links": p.links,
        "final_url": result["final_url"],
        "redirected": bool(result["chain"]),
        "html": True,
        "jsonld_types": sorted({d.get("@type") for d in p.jsonld
                                if isinstance(d, dict) and isinstance(d.get("@type"), str)}),
    })
    return issues, data


def timing_checks(results):
    """Relative slow-page outliers: fetch time >2.5s AND >3x the site median.

    Absolute thresholds punish slow-but-uniform sites and flatter fast ones;
    a page is only notable when it is slow relative to its own site. Needs at
    least 5 successful fetches for the median to mean anything.
    """
    ok = [r for r, _ in results if not r["error"] and r["status"] and r["status"] < 400]
    if len(ok) < 5:
        return []
    med = statistics.median(r["elapsed"] for r in ok)
    out = []
    for r in ok:
        if r["elapsed"] > 2.5 and r["elapsed"] > 3 * med:
            out.append((CLASS_OF["slow-fetch-outlier"], r["url"],
                        f"fetch {r['elapsed']:.1f}s vs site median {med:.2f}s"))
    return out


# ---------------------------------------------------------------- link graph
def _strip_www(netloc):
    n = netloc.lower()
    return n[4:] if n.startswith("www.") else n


def build_link_graph(results, base_netloc):
    """Index crawled pages by normalised URL and collect internal inlink
    sources: target -> set of linking page URLs. Self-links are ignored.
    Built entirely from the crawl — no new requests.
    """
    index = {}
    # a page's own crawl record wins over being some other URL's redirect target
    for r, (_, data) in results:
        index.setdefault(norm_url(r["url"]), (r, data))
    for r, (_, data) in results:
        index.setdefault(norm_url(r.get("final_url") or r["url"]), (r, data))
    inlink_sources = defaultdict(set)
    base_host = _strip_www(base_netloc)
    for r, (_, data) in results:
        src_final = r.get("final_url") or r["url"]
        for href in data.get("links") or []:
            absu = urllib.parse.urljoin(src_final, href)
            sp = urllib.parse.urlsplit(absu)
            if sp.scheme not in ("http", "https") or _strip_www(sp.netloc) != base_host:
                continue
            t = norm_url(absu)
            if t in (norm_url(src_final), norm_url(r["url"])):
                continue
            inlink_sources[t].add(r["url"])
    return index, inlink_sources


def link_checks(index, inlink_sources):
    """Internal links -> broken / redirected / noindexed / canonicalised-away
    targets. Only judges targets that were crawled — zero extra requests.

    Returns (issues, sources_by_code) — the latter is the set of linking pages
    per code, which is the real scope of the problem for ranking.
    """
    issues = defaultdict(list)
    sources_by_code = defaultdict(set)

    def add(code, url, detail, sources):
        issues[code].append((CLASS_OF[code], url, detail))
        sources_by_code[code].update(sources)

    for target, sources in inlink_sources.items():
        entry = index.get(target)
        if entry is None:
            continue
        r, data = entry
        n = len(sources)
        linked = f"linked from {n} crawled page{'s' if n != 1 else ''}"
        if r["error"]:
            add("internal-link-broken", r["url"], f"{linked} — {r['error']}", sources)
        elif r["status"] in RATE_LIMIT_STATUSES:
            continue  # coverage gap, already reported; can't judge the target
        elif r["status"] >= 400:
            add("internal-link-broken", r["url"], f"{linked} — HTTP {r['status']}", sources)
        elif r["chain"] and target == norm_url(r["url"]):
            # the link hits the redirecting URL itself, not its destination
            add("internal-link-redirect", r["url"], f"{linked} — -> {r['final_url']}", sources)
        elif data.get("noindex"):
            add("internal-link-noindex", r["url"], linked, sources)
        elif data.get("canonical_abs") and \
                norm_url(data["canonical_abs"]) != norm_url(r.get("final_url") or r["url"]):
            add("internal-link-canonicalised", r["url"],
                f"{linked} — canonicalises to {data['canonical_abs']}", sources)
    return issues, sources_by_code


def orphan_checks(page_urls, results, inlink_sources):
    """Sitemap URLs with zero internal inlinks across the crawl.

    Only statistically valid at high coverage: with a sampled crawl, "no
    inlinks found" mostly means "the linking pages weren't crawled". Suppressed
    entirely below 80% sitemap coverage — a sampled run must never lead with an
    orphan claim.
    """
    if not page_urls or len(results) / len(page_urls) < 0.8:
        return {}
    issues = defaultdict(list)
    for r, (_, data) in results:
        if r["error"] or not data.get("html") or r["status"] >= 400 \
                or r["chain"] or data.get("noindex"):
            continue
        if urllib.parse.urlparse(r["url"]).path in ("", "/"):
            continue  # the homepage needs no inlinks
        norms = {norm_url(r["url"]), norm_url(r.get("final_url") or r["url"])}
        if not any(inlink_sources.get(nu) for nu in norms):
            issues["orphan-page"].append((CLASS_OF["orphan-page"], r["url"],
                                          "no internal links found in the crawl"))
    return issues


def _judge_canonical_target(r, noindex, sources):
    """Classify one canonical target from its fetch result. Returns (code, detail) or None.

    A rate-limited target is UNVERIFIED, never broken — the origin said "not
    now", not "gone"."""
    n = len(sources)
    for_pages = f"canonical for {n} crawled page{'s' if n != 1 else ''}"
    if r["status"] in RATE_LIMIT_STATUSES:
        return None
    if r["error"]:
        return "canonical-target-broken", f"{r['error']} — {for_pages}"
    if r["status"] >= 400:
        return "canonical-target-broken", f"HTTP {r['status']} — {for_pages}"
    if r["chain"]:
        return "canonical-target-redirects", f"-> {r['final_url']} — {for_pages}"
    if noindex:
        return "canonical-target-noindex", for_pages
    return None


def canonical_target_checks(results, index, base_netloc, log, max_fetch=20,
                            workers=4, deadline=None):
    """Validate where canonicals point. Crawled targets are judged from crawl
    data; at most max_fetch deduped out-of-crawl targets get fetched
    concurrently, most-used targets first, honouring any time budget.

    Returns (issues, sources_by_code) — the linking scope per code."""
    issues = defaultdict(list)
    sources_by_code = defaultdict(set)
    base_host = _strip_www(base_netloc)
    targets = {}   # norm target -> {"url": abs url, "sources": [page urls]}
    for r, (_, data) in results:
        canon = data.get("canonical_abs")
        if not canon or norm_url(canon) == norm_url(r.get("final_url") or r["url"]):
            continue
        t = targets.setdefault(norm_url(canon), {"url": canon, "sources": []})
        t["sources"].append(r["url"])

    def add(code, url, detail, sources):
        issues[code].append((CLASS_OF[code], url, detail))
        sources_by_code[code].update(sources)

    to_fetch = []
    for nt, t in targets.items():
        if len(t["sources"]) >= 5:
            add("canonical-consolidation", t["url"],
                f"{len(t['sources'])} crawled pages canonicalise here", t["sources"])
        if _strip_www(urllib.parse.urlsplit(t["url"]).netloc) != base_host:
            for src in t["sources"]:
                add("cross-domain-canonical", src, f"-> {t['url']}", [src])
            continue
        if nt in index:
            r, data = index[nt]
            verdict = _judge_canonical_target(r, data.get("noindex"), t["sources"])
            if verdict:
                add(verdict[0], t["url"], verdict[1], t["sources"])
        else:
            to_fetch.append(t)

    # most-used targets first; the cap and the time budget bound the extra work
    to_fetch.sort(key=lambda t: -len(t["sources"]))
    if len(to_fetch) > max_fetch:
        log(f"  canonical targets: verifying {max_fetch} of {len(to_fetch)} "
            f"out-of-crawl targets (most-used first)")
    unverified = max(0, len(to_fetch) - max_fetch)

    def verify(t):
        if deadline and time.monotonic() > deadline:
            return t, None, None
        r = fetch_with_retry(t["url"])
        noindex = "noindex" in (r["headers"].get("X-Robots-Tag") or "").lower()
        if not noindex and not r["error"] and r["status"] and r["status"] < 400 \
                and "html" in r["headers"].get("Content-Type", ""):
            p = PageParser()
            try:
                p.feed(decode_body(r))
                p.close()
            except Exception:
                pass
            noindex = "noindex" in (p.robots_meta or "")
        return t, r, noindex

    if to_fetch:
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
            for t, r, noindex in ex.map(verify, to_fetch[:max_fetch]):
                if r is None or r["status"] in RATE_LIMIT_STATUSES:
                    unverified += 1
                    continue
                verdict = _judge_canonical_target(r, noindex, t["sources"])
                if verdict:
                    add(verdict[0], t["url"], verdict[1], t["sources"])
    if unverified:
        log(f"  canonical targets: {unverified} left unverified (cap, rate limit, or time budget)")
    return issues, sources_by_code


def offsite_link_checks(inlink_sources, index, log, max_fetch=50, workers=4, deadline=None):
    """--check-links: verify internal link targets that are NOT in the crawl
    set (off-sitemap destinations — where broken internal links usually live).
    Capped, concurrent, most-linked first. Returns (issues, sources_by_code)."""
    issues = defaultdict(list)
    sources_by_code = defaultdict(set)
    unknown = sorted(((t, srcs) for t, srcs in inlink_sources.items() if t not in index),
                     key=lambda kv: -len(kv[1]))
    if len(unknown) > max_fetch:
        log(f"  link check: verifying {max_fetch} of {len(unknown)} off-sitemap "
            f"targets (most-linked first)")

    def verify(item):
        t, srcs = item
        if deadline and time.monotonic() > deadline:
            return t, srcs, None
        return t, srcs, fetch_with_retry(t)

    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
        for t, srcs, r in ex.map(verify, unknown[:max_fetch]):
            if r is None or r["status"] in RATE_LIMIT_STATUSES:
                continue  # unverified — never report a throttled target as broken
            n = len(srcs)
            linked = f"linked from {n} crawled page{'s' if n != 1 else ''}, not in sitemap"
            if r["error"]:
                issues["internal-link-broken"].append(
                    (CLASS_OF["internal-link-broken"], t, f"{linked} — {r['error']}"))
                sources_by_code["internal-link-broken"].update(srcs)
            elif r["status"] >= 400:
                issues["internal-link-broken"].append(
                    (CLASS_OF["internal-link-broken"], t, f"{linked} — HTTP {r['status']}"))
                sources_by_code["internal-link-broken"].update(srcs)
    return issues, sources_by_code


def site_checks(base_url, pages, sitemap_urls, log):
    """Site-level checks: robots.txt (including per-URL robotparser validation
    of every sitemap URL), https enforcement, duplicates."""
    issues = defaultdict(list)   # code -> [(class, url, detail)]
    parsed = urllib.parse.urlparse(base_url)
    root = f"{parsed.scheme}://{parsed.netloc}"

    r = fetch_with_retry(root + "/robots.txt")
    if r["status"] in RATE_LIMIT_STATUSES:
        issues["rate-limited"].append((CLASS_OF["rate-limited"], root + "/robots.txt",
                                       f"HTTP {r['status']} after retries"))
    elif r["status"] != 200:
        issues["robots-missing"].append((CLASS_OF["robots-missing"], root + "/robots.txt",
                                         f"HTTP {r['status']}"))
    else:
        body = decode_body(r)
        if "sitemap:" not in body.lower():
            issues["robots-no-sitemap"].append((OBSERVATION, root + "/robots.txt", None))
        # what matters for indexation is what GOOGLEBOT may fetch — judged by the
        # real parser, per group, never by grepping for Disallow lines
        rp = urllib.robotparser.RobotFileParser()
        rp.parse(body.splitlines())
        g_blocked = [u for u in sitemap_urls if not rp.can_fetch("Googlebot", u)]
        home_blocked = not rp.can_fetch("Googlebot", root + "/")
        if home_blocked and sitemap_urls and len(g_blocked) >= 0.95 * len(sitemap_urls):
            issues["robots-blocks-all"].append(
                (BLOCKER, root + "/robots.txt",
                 f"Googlebot blocked from the homepage and "
                 f"{len(g_blocked)}/{len(sitemap_urls)} sitemap URLs"))
        else:
            for u in g_blocked:
                issues["robots-blocked-sitemap-url"].append((BLOCKER, u, "blocked for Googlebot"))
        # the audit UA being blocked is a coverage caveat, never an SEO finding
        audit_only = [u for u in sitemap_urls
                      if not rp.can_fetch("performify-seo-audit", u)]
        audit_only = [u for u in audit_only if u not in set(g_blocked)]
        if audit_only:
            issues["robots-blocks-audit-ua"].append(
                (OBSERVATION, root + "/robots.txt",
                 f"{len(audit_only)} sitemap URLs blocked for the audit UA but not for "
                 f"Googlebot — audit coverage may be limited"))

    if parsed.scheme == "https":
        h = fetch(f"http://{parsed.netloc}/", follow=False)
        if h["status"] not in (301, 308) and h["error"] is None:
            issues["no-https-redirect"].append((CLASS_OF["no-https-redirect"], f"http://{parsed.netloc}/",
                                                f"HTTP {h['status']} (expected 301)"))

    # duplicates only across indexable, non-redirected, self-canonical pages —
    # a redirected sitemap URL parses its destination's metadata, and pages
    # deliberately canonicalising to one destination are consolidation, not
    # duplication
    def dup_eligible(pg):
        if not pg.get("html") or pg.get("redirected") or pg.get("noindex"):
            return False
        canon = pg.get("canonical_abs")
        if canon and norm_url(canon) != norm_url(pg.get("final_url") or pg["url"]):
            return False
        return True

    eligible = [pg for pg in pages if dup_eligible(pg)]
    for field, code in (("title", "duplicate-title"), ("description", "duplicate-description")):
        groups = defaultdict(list)
        for pg in eligible:
            if pg.get(field):
                groups[pg[field]].append(pg["url"])
        for val, urls in groups.items():
            if len(urls) > 1:
                for u in urls:
                    issues[code].append((CLASS_OF[code], u, f'"{val[:60]}" shared by {len(urls)} pages'))
    return issues


# ---------------------------------------------------------------- report
def c(code, s, tty):
    return f"\033[{code}m{s}\033[0m" if tty else s


def coverage_stats(model):
    """The honest numbers behind the crawl: what was and wasn't looked at."""
    stats = {"sitemap_total": len(model["page_urls"]), "crawled": len(model["results"]),
             "sampled": model["sampled"], "skipped": model["skipped"],
             "indexable_html": 0, "redirected": 0, "broken": 0,
             "noindexed": 0, "rate_limited": 0, "unreachable": 0}
    fetch_times, ttfbs = [], []
    for r, (_, data) in model["results"]:
        fetch_times.append(r["elapsed"])
        if r.get("ttfb") is not None:
            ttfbs.append(r["ttfb"])
        if r["error"]:
            stats["unreachable"] += 1
            continue
        if r["status"] in RATE_LIMIT_STATUSES:
            stats["rate_limited"] += 1
            continue
        if r["status"] >= 400:
            stats["broken"] += 1
            continue
        # categories are exclusive: a redirected sitemap URL is "redirected",
        # not also an indexable sitemap entry — the sitemap listed the wrong URL
        if r["chain"]:
            stats["redirected"] += 1
        elif data.get("noindex"):
            stats["noindexed"] += 1
        elif data.get("html"):
            stats["indexable_html"] += 1
    stats["ttfb_median"] = round(statistics.median(ttfbs), 2) if ttfbs else None
    stats["fetch_median"] = round(statistics.median(fetch_times), 2) if fetch_times else None
    stats["fetch_p90"] = (round(sorted(fetch_times)[max(0, int(len(fetch_times) * 0.9) - 1)], 2)
                          if fetch_times else None)
    return stats


def build_report(model, tty=False, md=False, explain=False):
    cls_colour = {BLOCKER: "31;1", MATERIAL: "33", OPPORTUNITY: "36", OBSERVATION: "2"}
    grouped, ranked, cov = model["grouped"], model["ranked"], model["coverage"]
    crawled_urls = [r["url"] for r, _ in model["results"]]
    source_of = model["source_of"]
    lines = []
    w = lines.append
    h = (lambda s: f"## {s}") if md else (lambda s: c("1", s.upper(), tty))
    bullet = "- " if md else "  "

    def head(e):
        text = label(e["code"])
        return f"**{text}**" if md else c(cls_colour[e["class"]], text, tty)

    def maths(e):
        return (f" (score {e['score']} = impact {e['impact']} x confidence {e['confidence']} "
                f"x {e['affected']}/{cov['crawled']} affected) [{e['code']}]") if explain else ""

    def clusters_for(code, max_lines=2):
        urls = list(dict.fromkeys(u for _, u, _ in grouped[code]))
        out = []
        for cl in cluster_issue(urls, crawled_urls, source_of)[:max_lines]:
            line = cluster_line(cl)
            if line:
                out.append((line, cl["examples"]))
        if not out:
            out.append((None, urls[:3]))
        return out

    host = urllib.parse.urlparse(model["base"]).netloc
    w(f"# SEO audit — {host}" if md else c("1;4", f"SEO AUDIT — {host}", tty))
    w("")

    # VERDICT
    w(h("Verdict"))
    w(f"{bullet}{model['verdict']}")
    w("")

    # COVERAGE
    w(h("Coverage"))
    sample_note = " (sampled)" if cov["sampled"] else ""
    budget_note = f" — time budget hit, {cov['skipped']} URLs not crawled" if cov["skipped"] else ""
    w(f"{bullet}Crawled {cov['crawled']} of {cov['sitemap_total']} sitemap URLs{sample_note} "
      f"in {model['elapsed']:.0f}s{budget_note}.")
    w(f"{bullet}Indexable HTML {cov['indexable_html']} · redirected {cov['redirected']} · "
      f"broken {cov['broken']} · noindexed {cov['noindexed']} · "
      f"rate-limited {cov['rate_limited']} · unreachable {cov['unreachable']}.")
    if cov["fetch_median"] is not None:
        ttfb_note = f"TTFB median {cov['ttfb_median']:.2f}s · " if cov["ttfb_median"] is not None else ""
        w(f"{bullet}{ttfb_note}fetch time median {cov['fetch_median']:.2f}s, p90 {cov['fetch_p90']:.2f}s.")
    w("")

    # PRIORITY ACTIONS — top 3 blocker/material by score (see --explain-scores)
    priority = [e for e in ranked if e["class"] in (BLOCKER, MATERIAL)]
    w(h("Priority actions"))
    if not priority:
        w(f"{bullet}None — no indexation blockers or material issues found.")
    for i, e in enumerate(priority[:3], 1):
        code = e["code"]
        scope = (f"affects {e['affected']} of {cov['crawled']} crawled pages"
                 if e["affected"] != e["n"] or e["n"] > 1
                 else f"{e['n']} URL")
        w(f"{i}. {head(e)} — {level_word(e['impact'])} impact, "
          f"{level_word(e['confidence'])} confidence — {scope}{maths(e)}")
        for line, examples in clusters_for(code):
            if line:
                w(f"{bullet}   {line}")
            for u in examples[:2]:
                w(f"{bullet}     {u}")
        w(f"{bullet}   Fix: {FIX_HINTS.get(code, code)}")
    if len(priority) > 3:
        rest = " · ".join(f"{label(e['code'])} ×{e['n']}{maths(e)}" for e in priority[3:])
        w(f"{bullet}Also: {rest}")
    w("")

    # SECONDARY OPPORTUNITIES — review-not-fix items
    opps = [e for e in ranked if e["class"] == OPPORTUNITY]
    if opps:
        w(h("Secondary opportunities"))
        for e in opps:
            code = e["code"]
            w(f"{bullet}{head(e)} ×{e['n']}{maths(e)} — {FIX_HINTS.get(code, code)}")
            for line, examples in clusters_for(code, max_lines=1):
                if line:
                    w(f"{bullet}    {line} — e.g. {examples[0]}")
                elif examples:
                    w(f"{bullet}    e.g. {' · '.join(examples[:2])}")
        w("")

    # OBSERVATIONS — one line each, context only
    obs = [e for e in ranked if e["class"] == OBSERVATION]
    if obs:
        w(h("Observations"))
        for e in obs:
            w(f"{bullet}{head(e)} ×{e['n']} — {FIX_HINTS.get(e['code'], e['code'])}")
        w("")

    if not ranked:
        if model["results"]:
            w("No issues found — clean crawl." if not md else "_No issues found — clean crawl._")
        else:
            w("Nothing was crawled — no verdict." if not md else "_Nothing was crawled — no verdict._")
    return "\n".join(lines)


# ---------------------------------------------------------------- sampling
def _evenly_spaced(items, k):
    if k >= len(items):
        return list(items)
    step = len(items) / k
    return [items[int(i * step)] for i in range(k)]


def stratified_sample(page_urls, source_of, max_pages):
    """Sample proportionally per child sitemap (minimum 3 per sitemap), always
    including the homepage and top-level pages, evenly spaced within each
    stratum — so every site section is represented, weighted by its size."""
    if max_pages <= 0 or len(page_urls) <= max_pages:
        return page_urls

    def is_top_level(u):
        return "/" not in urllib.parse.urlparse(u).path.strip("/")

    must = [u for u in page_urls if is_top_level(u)]
    if len(must) >= max_pages:
        return _evenly_spaced(must, max_pages)

    rest = [u for u in page_urls if u not in set(must)]
    strata = {}
    for u in rest:
        strata.setdefault(source_of.get(u, ""), []).append(u)
    budget = max_pages - len(must)

    quotas = {sm: min(3, len(us)) for sm, us in strata.items()}
    spent = sum(quotas.values())
    if spent > budget:
        # more sitemaps than the budget can give 3 each: round-robin one at a
        # time, biggest strata first (len(rest) > budget, so this terminates)
        quotas = {sm: 0 for sm in strata}
        order = sorted(strata, key=lambda sm: -len(strata[sm]))
        i = 0
        while budget > 0:
            sm = order[i % len(order)]
            if quotas[sm] < len(strata[sm]):
                quotas[sm] += 1
                budget -= 1
            i += 1
    else:
        # spread what's left proportionally to remaining stratum capacity
        left = budget - spent
        capacity = {sm: len(us) - quotas[sm] for sm, us in strata.items()}
        total_cap = sum(capacity.values())
        if total_cap and left:
            shares = {sm: left * capacity[sm] / total_cap for sm in strata}
            for sm in strata:
                quotas[sm] += int(shares[sm])
            leftover = left - sum(int(s) for s in shares.values())
            for sm in sorted(strata, key=lambda s: -(shares[s] - int(shares[s]))):
                if leftover <= 0:
                    break
                if quotas[sm] < len(strata[sm]):
                    quotas[sm] += 1
                    leftover -= 1

    sample = list(must)
    for sm, us in strata.items():
        sample.extend(_evenly_spaced(us, quotas[sm]))
    return sample


# ---------------------------------------------------------------- pipeline
def run_audit(base, max_pages=100, workers=8, delay=0.0, time_budget=0,
              check_links=False, log=lambda *a: None):
    """Full pipeline: discover -> sample -> crawl -> check. Returns a model dict.

    Returns None if no sitemap URLs could be discovered.
    """
    start = time.monotonic()
    log(f"Discovering sitemap(s) for {base} ...")
    page_urls, sitemaps, source_of = discover_and_load_sitemaps(base, log)
    if not page_urls:
        return {"base": base, "sitemaps": sitemaps, "page_urls": []}

    sampled = max_pages > 0 and len(page_urls) > max_pages
    targets = stratified_sample(page_urls, source_of, max_pages)
    log(f"Crawling {len(targets)} of {len(page_urls)} URLs with {workers} workers ...")

    results = []
    deadline = start + time_budget if time_budget else None

    def worker(u):
        if deadline and time.monotonic() > deadline:
            return None
        if delay:
            time.sleep(delay)
        r = fetch_with_retry(u)
        return audit_page(r), r

    skipped = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
        futures = {ex.submit(worker, u): u for u in targets}
        done = 0
        for fut in concurrent.futures.as_completed(futures):
            done += 1
            if done % 25 == 0:
                log(f"  {done}/{len(targets)}")
            try:
                res = fut.result()
                if res is None:
                    skipped += 1
                    continue
                (issues, data), raw = res
                results.append((raw, (issues, data)))
            except Exception as e:
                u = futures[fut]
                results.append(({"url": u, "status": None, "error": str(e), "elapsed": 0,
                                 "ttfb": None, "chain": [], "headers": {}, "body": b"",
                                 "final_url": u},
                                ([issue("fetch-failed", str(e))], {"url": u})))
    if skipped:
        log(f"  time budget reached — {skipped} of {len(targets)} URLs not crawled")

    # group issues by code
    grouped = defaultdict(list)   # code -> [(class, url, detail)]
    pagedata = []
    for raw, (issues_, data) in results:
        pagedata.append(data)
        for cls, code, detail in issues_:
            grouped[code].append((cls, raw["url"], detail))
    for code, items in site_checks(base, pagedata, page_urls, log).items():
        grouped[code].extend(items)
    for cls, url, detail in timing_checks(results):
        grouped["slow-fetch-outlier"].append((cls, url, detail))

    # internal-link graph, orphans, canonical targets — sources_by_code feeds
    # the ranking with the real scope (linking pages, not issue-target URLs)
    scope_sources = defaultdict(set)
    base_netloc = urllib.parse.urlparse(base).netloc
    graph_index, inlink_sources = build_link_graph(results, base_netloc)
    link_issues, link_scope = link_checks(graph_index, inlink_sources)
    canon_issues, canon_scope = canonical_target_checks(
        results, graph_index, base_netloc, log, deadline=deadline)
    checks = [link_issues, orphan_checks(page_urls, results, inlink_sources), canon_issues]
    scopes = [link_scope, canon_scope]
    if check_links:
        off_issues, off_scope = offsite_link_checks(inlink_sources, graph_index, log,
                                                    deadline=deadline)
        checks.append(off_issues)
        scopes.append(off_scope)
    for ch in checks:
        for code, items in ch.items():
            grouped[code].extend(items)
    for sc in scopes:
        for code, srcs in sc.items():
            scope_sources[code].update(srcs)

    affected_pages = {code: len(srcs) for code, srcs in scope_sources.items()}
    for code in SITE_WIDE:
        if code in grouped:
            affected_pages[code] = len(results)

    model = {
        "base": base, "sitemaps": sitemaps, "page_urls": page_urls,
        "source_of": source_of, "sampled": sampled, "targets": targets,
        "results": results, "pagedata": pagedata, "grouped": grouped,
        "skipped": skipped, "elapsed": time.monotonic() - start,
    }
    model["ranked"] = rank_issues(grouped, len(results), affected_pages)
    model["coverage"] = coverage_stats(model)
    model["verdict"] = build_verdict(grouped, model["ranked"], len(results),
                                     [r["url"] for r, _ in results], source_of)
    return model


def has_blockers(grouped):
    return any(items and items[0][0] == BLOCKER for items in grouped.values())


# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(description="Streamlined technical SEO audit from a sitemap.")
    ap.add_argument("url", help="Sitemap URL or site root (https://example.com)")
    ap.add_argument("--max-pages", type=int, default=100,
                    help="Max pages to crawl (default 100; 0 = crawl the whole sitemap)")
    ap.add_argument("--workers", type=int, default=8, help="Concurrent fetches (default 8)")
    ap.add_argument("--delay", type=float, default=0.0, help="Per-worker delay between fetches (s)")
    ap.add_argument("--time-budget", type=float, default=0, metavar="SECONDS",
                    help="Stop starting new fetches after this many seconds (0 = no limit)")
    ap.add_argument("--check-links", action="store_true",
                    help="Also verify internal link targets that are not in the sitemap "
                         "(capped at 50 extra fetches, most-linked first)")
    ap.add_argument("--explain-scores", action="store_true",
                    help="Show the score arithmetic and issue codes in the report")
    ap.add_argument("--markdown", metavar="FILE", help="Also write the report as markdown")
    ap.add_argument("--json", metavar="FILE", help="Also dump raw results as JSON")
    ap.add_argument("--quiet", action="store_true", help="Suppress progress output")
    args = ap.parse_args()

    tty = sys.stdout.isatty()
    log = (lambda *a: None) if args.quiet else (lambda *a: print(*a, file=sys.stderr))

    base = args.url if args.url.startswith("http") else "https://" + args.url
    model = run_audit(base, max_pages=args.max_pages, workers=args.workers,
                      delay=args.delay, time_budget=args.time_budget,
                      check_links=args.check_links, log=log)
    if not model["page_urls"]:
        print(f"error: no sitemap URLs found for {base}", file=sys.stderr)
        for u, result in model["sitemaps"].items():
            print(f"  {u} -> {result}", file=sys.stderr)
        sys.exit(1)

    grouped = model["grouped"]
    print(build_report(model, tty=tty, explain=args.explain_scores))

    if args.markdown:
        with open(args.markdown, "w") as f:
            f.write(build_report(model, md=True, explain=args.explain_scores) + "\n")
        log(f"markdown report -> {args.markdown}")
    if args.json:
        pages_out = [{k: v for k, v in pg.items() if k != "links"} for pg in model["pagedata"]]
        with open(args.json, "w") as f:
            json.dump({"base": base, "sitemaps": model["sitemaps"],
                       "verdict": model["verdict"], "coverage": model["coverage"],
                       "ranking": model["ranked"],
                       "impact_table": {k: {"impact": i, "confidence": cf}
                                        for k, (i, cf) in IMPACT.items()},
                       "pages": pages_out,
                       "issues": {k: [{"class": s, "url": u, "detail": d} for s, u, d in v]
                                  for k, v in grouped.items()}}, f, indent=2, default=str)
        log(f"json dump -> {args.json}")

    # exit 2 only when indexation blockers exist
    sys.exit(2 if has_blockers(grouped) else 0)


if __name__ == "__main__":
    main()
