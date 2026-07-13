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
    # indexation blockers
    "http-error": BLOCKER,
    "fetch-failed": BLOCKER,
    "noindex-in-sitemap": BLOCKER,
    "redirect-problem": BLOCKER,
    "robots-blocks-all": BLOCKER,
    "robots-blocked-sitemap-url": BLOCKER,
    "missing-title": BLOCKER,
    "mixed-content": BLOCKER,
    # material issues
    "redirected-sitemap-url": MATERIAL,
    "duplicate-title": MATERIAL,
    "duplicate-description": MATERIAL,
    "canonical-mismatch": MATERIAL,
    "canonical-target-broken": MATERIAL,
    "canonical-target-redirects": MATERIAL,
    "canonical-target-noindex": MATERIAL,
    "internal-link-broken": MATERIAL,
    "rate-limited": MATERIAL,
    "no-https-redirect": MATERIAL,
    # opportunities
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
}

FIX_HINTS = {
    # blockers — direct fix language
    "http-error": "Remove dead URLs from the sitemap or restore the pages (a sitemap must only list 200s).",
    "fetch-failed": "Check DNS/TLS/WAF — the crawler could not fetch these at all; Googlebot may be failing too.",
    "noindex-in-sitemap": "A noindexed URL in the sitemap sends Google contradictory signals — drop it from the sitemap or remove the noindex.",
    "missing-title": "Every indexable page needs a unique <title>; it is the single strongest on-page relevance signal.",
    "mixed-content": "Load all subresources over https — browsers block or warn, and it erodes trust signals.",
    "redirect-problem": "Redirect loops or chains beyond 5 hops mean the page cannot be reached or indexed.",
    "robots-blocks-all": "robots.txt disallows everything — the site cannot be crawled.",
    "robots-blocked-sitemap-url": "These URLs are listed in the sitemap but blocked by robots.txt — contradictory signals; unblock them or drop them from the sitemap.",
    # material — direct but measured
    "redirected-sitemap-url": "Sitemaps should list final URLs, not redirects — update the sitemap generator.",
    "duplicate-title": "Duplicate titles make pages compete with each other; differentiate or canonicalise.",
    "duplicate-description": "Duplicate meta descriptions blur which page should surface; write per-page or omit and let Google generate.",
    "canonical-mismatch": "Page canonicalises elsewhere but sits in the sitemap — pick one: fix the canonical or remove from sitemap.",
    "canonical-target-broken": "Canonical points at a URL that does not return 200 — the consolidation signal is being thrown away.",
    "canonical-target-redirects": "Canonical points at a redirecting URL — point it at the final destination instead.",
    "canonical-target-noindex": "Canonical points at a noindexed URL — contradictory signals; the target cannot consolidate anything.",
    "internal-link-broken": "Internal links point at pages returning 4xx/5xx — fix the links or restore the targets.",
    "rate-limited": "The origin throttled the crawl (429/503) even after backoff — these pages were NOT audited. Rerun with --workers 2 --delay 1.",
    "no-https-redirect": "http:// version doesn't redirect to https — duplicate-protocol indexing risk.",
    # opportunities — review-not-fix language
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
}


def issue(code, detail=None):
    return (CLASS_OF[code], code, detail)

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
    """Resolve a sitemap URL or site root into a flat list of page URLs."""
    parsed = urllib.parse.urlparse(start_url)
    candidates, fallbacks = [], []
    looks_like_sitemap = parsed.path.rstrip("/").endswith((".xml", ".xml.gz")) or "sitemap" in parsed.path.lower()
    if looks_like_sitemap:
        candidates.append(start_url)
    else:
        root = f"{parsed.scheme}://{parsed.netloc}"
        robots = fetch(root + "/robots.txt")
        if robots["status"] == 200:
            for line in decode_body(robots).splitlines():
                if line.lower().startswith("sitemap:"):
                    candidates.append(line.split(":", 1)[1].strip())
        fallbacks = [root + "/sitemap.xml", root + "/sitemap_index.xml"]

    seen, pages = set(), []
    queue = list(dict.fromkeys(candidates + fallbacks))
    while queue and len(seen) < 50:
        sm = queue.pop(0)
        if sm in seen:
            continue
        seen.add(sm)
        r = fetch_with_retry(sm)
        if r["status"] != 200 or not r["body"]:
            continue
        p, children = parse_sitemap_xml(decode_body(r))
        if p or children:
            log(f"  sitemap: {sm} — {len(p)} pages, {len(children)} child sitemaps")
        pages += p
        queue += children
        if pages:
            # a working sitemap was found — no need to probe the guessed fallbacks
            queue = [q for q in queue if q not in fallbacks]
    return list(dict.fromkeys(pages)), sorted(seen)


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
    """Normalise for canonical comparison: scheme+host lowered, no fragment, no trailing slash."""
    p = urllib.parse.urlsplit(u.strip())
    path = p.path.rstrip("/") or "/"
    return urllib.parse.urlunsplit((p.scheme.lower(), p.netloc.lower(), path, p.query, ""))


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


def site_checks(base_url, pages, log):
    """Site-level checks: robots.txt, https enforcement, duplicates."""
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
        if re.search(r"^user-agent:\s*\*\s*$", body, re.I | re.M) and \
           re.search(r"^disallow:\s*/\s*$", body, re.I | re.M):
            issues["robots-blocks-all"].append((BLOCKER, root + "/robots.txt", "Disallow: /"))
        if "sitemap:" not in body.lower():
            issues["robots-no-sitemap"].append((OBSERVATION, root + "/robots.txt", None))

    if parsed.scheme == "https":
        h = fetch(f"http://{parsed.netloc}/", follow=False)
        if h["status"] not in (301, 308) and h["error"] is None:
            issues["no-https-redirect"].append((CLASS_OF["no-https-redirect"], f"http://{parsed.netloc}/",
                                                f"HTTP {h['status']} (expected 301)"))

    for field, code in (("title", "duplicate-title"), ("description", "duplicate-description")):
        groups = defaultdict(list)
        for pg in pages:
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


def build_report(base_url, page_urls, sampled, results, grouped, elapsed, tty=False, md=False, skipped=0):
    cls_colour = {BLOCKER: "31;1", MATERIAL: "33", OPPORTUNITY: "36", OBSERVATION: "2"}
    lines = []
    w = lines.append

    statuses = Counter()
    fetch_times, ttfbs = [], []
    for r, _ in results:
        fetch_times.append(r["elapsed"])
        if r.get("ttfb") is not None:
            ttfbs.append(r["ttfb"])
        if r["error"]:
            statuses["error"] += 1
        elif r["chain"]:
            statuses["redirect"] += 1
        elif r["status"] in RATE_LIMIT_STATUSES:
            statuses["rate-limited"] += 1
        elif r["status"] >= 400:
            statuses[f"{r['status'] // 100}xx"] += 1
        else:
            statuses["200"] += 1

    counts = Counter()
    for code, items in grouped.items():
        for cls, _, _ in items:
            counts[cls] += 1

    host = urllib.parse.urlparse(base_url).netloc
    h = (lambda s: f"## {s}") if md else (lambda s: c("1", s.upper(), tty))
    w(f"# SEO audit — {host}" if md else c("1;4", f"SEO AUDIT — {host}", tty))
    w("")
    sample_note = f" (sampled from {len(page_urls)} in sitemap)" if sampled else ""
    budget_note = f" — time budget hit, {skipped} URLs not crawled" if skipped else ""
    w(f"Crawled {len(results)} pages{sample_note} in {elapsed:.0f}s{budget_note}.")
    ok = statuses.get("200", 0)
    rate_limited = statuses.get("rate-limited", 0)
    rl_note = f" · {rate_limited} rate-limited (NOT audited)" if rate_limited else ""
    if fetch_times:
        timing = f"TTFB median {statistics.median(ttfbs):.2f}s · " if ttfbs else ""
        timing += (f"fetch time median {statistics.median(fetch_times):.2f}s, "
                   f"p90 {sorted(fetch_times)[int(len(fetch_times) * 0.9) - 1]:.2f}s")
        w(f"Status: {ok} OK · {statuses.get('redirect', 0)} redirected · "
          f"{statuses.get('4xx', 0) + statuses.get('5xx', 0)} broken · "
          f"{statuses.get('error', 0)} unreachable{rl_note}. {timing}.")
    else:
        w("No pages fetched.")
    w("")
    verdict = (f"{counts[BLOCKER]} indexation blockers, {counts[MATERIAL]} material issues, "
               f"{counts[OPPORTUNITY]} opportunities, {counts[OBSERVATION]} observations")
    w(("**" + verdict + "**") if md else c("1", verdict, tty))
    w("")

    for cls in CLASS_ORDER:
        codes = [(code, items) for code, items in grouped.items() if items and items[0][0] == cls]
        if not codes:
            continue
        w(h(f"{CLASS_LABELS[cls].title()} ({sum(len(i) for _, i in codes)})"))
        for code, items in sorted(codes, key=lambda x: -len(x[1])):
            label = c(cls_colour[cls], f"[{code}]", tty) if not md else f"`{code}`"
            w(f"  {label} ×{len(items)}" if not md else f"- {label} ×{len(items)}")
            for _, url, detail in items[:3]:
                d = f"  — {detail}" if detail else ""
                w(f"      {url}{d}" if not md else f"    - {url}{d}")
            if len(items) > 3:
                w(f"      … and {len(items) - 3} more" if not md else f"    - … and {len(items) - 3} more")
        w("")

    # top actions: blockers first, then material, by page count
    actionable = [(code, items) for code, items in grouped.items()
                  if items and items[0][0] in (BLOCKER, MATERIAL)]
    actionable.sort(key=lambda x: (x[1][0][0] != BLOCKER, -len(x[1])))
    if actionable:
        w(h("Top actions"))
        for i, (code, items) in enumerate(actionable[:3], 1):
            n = f"{len(items)} page{'s' if len(items) != 1 else ''}"
            w(f"  {i}. {code} ({n}) — {FIX_HINTS.get(code, code)}"
              if not md else f"{i}. **{code}** ({n}) — {FIX_HINTS.get(code, code)}")
        w("")
    if not counts:
        if results:
            w("No issues found — clean crawl." if not md else "_No issues found — clean crawl._")
        else:
            w("Nothing was crawled — no verdict." if not md else "_Nothing was crawled — no verdict._")
    return "\n".join(lines)


# ---------------------------------------------------------------- pipeline
def run_audit(base, max_pages=100, workers=8, delay=0.0, time_budget=0, log=lambda *a: None):
    """Full pipeline: discover -> sample -> crawl -> check. Returns a model dict.

    Returns None if no sitemap URLs could be discovered.
    """
    start = time.monotonic()
    log(f"Discovering sitemap(s) for {base} ...")
    page_urls, sitemaps = discover_and_load_sitemaps(base, log)
    if not page_urls:
        return {"base": base, "sitemaps": sitemaps, "page_urls": []}

    sampled = max_pages > 0 and len(page_urls) > max_pages
    targets = page_urls
    if sampled:
        step = len(page_urls) / max_pages
        targets = [page_urls[int(i * step)] for i in range(max_pages)]
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
    for code, items in site_checks(base, pagedata, log).items():
        grouped[code].extend(items)
    for cls, url, detail in timing_checks(results):
        grouped["slow-fetch-outlier"].append((cls, url, detail))

    return {
        "base": base, "sitemaps": sitemaps, "page_urls": page_urls,
        "sampled": sampled, "targets": targets, "results": results,
        "pagedata": pagedata, "grouped": grouped, "skipped": skipped,
        "elapsed": time.monotonic() - start,
    }


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
    ap.add_argument("--markdown", metavar="FILE", help="Also write the report as markdown")
    ap.add_argument("--json", metavar="FILE", help="Also dump raw results as JSON")
    ap.add_argument("--quiet", action="store_true", help="Suppress progress output")
    args = ap.parse_args()

    tty = sys.stdout.isatty()
    log = (lambda *a: None) if args.quiet else (lambda *a: print(*a, file=sys.stderr))

    base = args.url if args.url.startswith("http") else "https://" + args.url
    model = run_audit(base, max_pages=args.max_pages, workers=args.workers,
                      delay=args.delay, time_budget=args.time_budget, log=log)
    if not model["page_urls"]:
        print(f"error: no sitemap URLs found for {base} (tried: {', '.join(model['sitemaps'])})",
              file=sys.stderr)
        sys.exit(1)

    grouped = model["grouped"]
    fetch_results = [(raw, None) for raw, _ in model["results"]]
    print(build_report(base, model["page_urls"], model["sampled"], fetch_results, grouped,
                       model["elapsed"], tty=tty, skipped=model["skipped"]))

    if args.markdown:
        with open(args.markdown, "w") as f:
            f.write(build_report(base, model["page_urls"], model["sampled"], fetch_results, grouped,
                                 model["elapsed"], md=True, skipped=model["skipped"]) + "\n")
        log(f"markdown report -> {args.markdown}")
    if args.json:
        with open(args.json, "w") as f:
            json.dump({"base": base, "sitemaps": model["sitemaps"], "pages": model["pagedata"],
                       "issues": {k: [{"class": s, "url": u, "detail": d} for s, u, d in v]
                                  for k, v in grouped.items()}}, f, indent=2, default=str)
        log(f"json dump -> {args.json}")

    # exit 2 only when indexation blockers exist
    sys.exit(2 if has_blockers(grouped) else 0)


if __name__ == "__main__":
    main()
