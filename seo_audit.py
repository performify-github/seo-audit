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

# ---------------------------------------------------------------- severities
CRITICAL, WARNING, NOTICE = "CRITICAL", "WARNING", "NOTICE"

FIX_HINTS = {
    "http-error": "Remove dead URLs from the sitemap or restore the pages (a sitemap must only list 200s).",
    "fetch-failed": "Check DNS/TLS/WAF — the crawler could not fetch these at all; Googlebot may be failing too.",
    "noindex-in-sitemap": "A noindexed URL in the sitemap sends Google contradictory signals — drop it from the sitemap or remove the noindex.",
    "missing-title": "Every indexable page needs a unique <title>; it is the single strongest on-page relevance signal.",
    "mixed-content": "Load all subresources over https — browsers block or warn, and it erodes trust signals.",
    "redirect-problem": "Redirect loops / chains >3 hops waste crawl budget and can drop the page from the index.",
    "robots-blocks-all": "robots.txt disallows everything — the site cannot be crawled.",
    "redirected-sitemap-url": "Sitemaps should list final URLs, not redirects — update the sitemap generator.",
    "duplicate-title": "Duplicate titles make pages compete with each other; differentiate or canonicalise.",
    "duplicate-description": "Duplicate meta descriptions depress CTR; write per-page or omit and let Google generate.",
    "missing-description": "Missing meta descriptions cede your SERP snippet to Google's extraction.",
    "missing-h1": "Each page should have exactly one <h1> stating the page topic.",
    "multiple-h1": "Multiple <h1>s dilute the topical signal — demote extras to <h2>.",
    "title-length": "Aim for 30-60 chars; longer truncates in the SERP, shorter wastes the slot.",
    "canonical-mismatch": "Page canonicalises elsewhere but sits in the sitemap — pick one: fix the canonical or remove from sitemap.",
    "missing-canonical": "Add self-referencing canonicals to defend against parameter/duplicate-URL indexing.",
    "thin-content": "Pages under ~200 words rarely rank; expand, merge, or noindex them.",
    "slow-response": "TTFB over 1.5s hurts crawl rate and Core Web Vitals; check caching/CDN/origin.",
    "no-https-redirect": "http:// version doesn't redirect to https — duplicate-protocol indexing risk.",
    "robots-missing": "Add a robots.txt referencing the sitemap.",
    "robots-no-sitemap": "Reference the sitemap in robots.txt (Sitemap: <url>) so all crawlers find it.",
    "images-missing-alt": "Images with no alt attribute at all — add alt text, or alt=\"\" if purely decorative.",
    "no-structured-data": "No JSON-LD found — structured data unlocks rich results (org, product, article, FAQ...).",
    "missing-viewport": "No viewport meta = fails mobile-friendly checks; mobile-first indexing applies.",
    "description-length": "Aim for 70-160 chars so the snippet neither truncates nor looks empty.",
    "large-html": "HTML documents over 300KB slow first paint and waste crawl budget.",
    "non-html": "Sitemap URL returned a non-HTML content type; verify it belongs in the page sitemap.",
}


# ---------------------------------------------------------------- fetching
class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


_opener = urllib.request.build_opener(_NoRedirect)


def fetch(url, timeout=TIMEOUT, follow=True):
    """Fetch a URL following redirects manually. Returns a dict."""
    chain = []
    current = url
    start = time.monotonic()
    for _ in range(MAX_REDIRECTS + 1):
        req = urllib.request.Request(current, headers={"User-Agent": UA, "Accept-Encoding": "gzip"})
        try:
            resp = _opener.open(req, timeout=timeout)
            status, headers, body = resp.status, resp.headers, resp.read()
        except urllib.error.HTTPError as e:
            status, headers = e.code, e.headers
            try:
                body = e.read()
            except Exception:
                body = b""
        except Exception as e:
            return {"url": url, "final_url": current, "status": None, "chain": chain,
                    "error": f"{type(e).__name__}: {e}", "elapsed": time.monotonic() - start,
                    "headers": {}, "body": b""}
        if status in (301, 302, 303, 307, 308) and follow:
            loc = headers.get("Location")
            if not loc:
                break
            nxt = urllib.parse.urljoin(current, loc)
            chain.append((status, current))
            if any(nxt == c[1] for c in chain) or nxt == current:
                return {"url": url, "final_url": nxt, "status": status, "chain": chain,
                        "error": "redirect loop", "elapsed": time.monotonic() - start,
                        "headers": dict(headers), "body": b""}
            current = nxt
            continue
        if (headers.get("Content-Encoding") == "gzip" or body[:2] == b"\x1f\x8b") and body:
            try:
                body = gzip.decompress(body)
            except OSError:
                pass
        return {"url": url, "final_url": current, "status": status, "chain": chain,
                "error": None, "elapsed": time.monotonic() - start,
                "headers": dict(headers), "body": body}
    return {"url": url, "final_url": current, "status": None, "chain": chain,
            "error": "too many redirects", "elapsed": time.monotonic() - start,
            "headers": {}, "body": b""}


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
        r = fetch(sm)
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
    issues = []  # (severity, code, detail-or-None)
    url = result["url"]
    data = {"url": url, "status": result["status"], "elapsed": round(result["elapsed"], 2)}

    if result["error"]:
        if "redirect" in result["error"]:
            issues.append((CRITICAL, "redirect-problem", result["error"]))
        else:
            issues.append((CRITICAL, "fetch-failed", result["error"]))
        return issues, data
    if result["status"] >= 400:
        issues.append((CRITICAL, "http-error", f"HTTP {result['status']}"))
        return issues, data
    if result["chain"]:
        issues.append((WARNING, "redirected-sitemap-url",
                       f"{len(result['chain'])} hop(s) -> {result['final_url']}"))

    ctype = result["headers"].get("Content-Type", "")
    if "html" not in ctype:
        issues.append((NOTICE, "non-html", ctype.split(";")[0] or "no content-type"))
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
    if "noindex" in robots:
        issues.append((CRITICAL, "noindex-in-sitemap", None))

    # title
    if not p.title:
        issues.append((CRITICAL, "missing-title", None))
    elif not 30 <= len(p.title) <= 60:
        issues.append((WARNING, "title-length", f"{len(p.title)} chars"))

    # description
    if not p.description:
        issues.append((WARNING, "missing-description", None))
    elif not 70 <= len(p.description) <= 160:
        issues.append((NOTICE, "description-length", f"{len(p.description)} chars"))

    # headings
    if not p.h1s:
        issues.append((WARNING, "missing-h1", None))
    elif len(p.h1s) > 1:
        issues.append((WARNING, "multiple-h1", f"{len(p.h1s)} h1s"))

    # canonical
    if not p.canonical:
        issues.append((WARNING, "missing-canonical", None))
    else:
        canon_abs = urllib.parse.urljoin(result["final_url"], p.canonical)
        if norm_url(canon_abs) != norm_url(result["final_url"]):
            issues.append((WARNING, "canonical-mismatch", f"-> {canon_abs}"))

    # content
    if p.words < 200:
        issues.append((WARNING, "thin-content", f"~{p.words} words"))
    if p.img_missing_alt:
        issues.append((NOTICE, "images-missing-alt", f"{p.img_missing_alt}/{p.img_total} images"))
    if not p.jsonld:
        issues.append((NOTICE, "no-structured-data", None))
    if not p.viewport:
        issues.append((NOTICE, "missing-viewport", None))

    # protocol / performance
    if result["final_url"].startswith("https://") and p.mixed:
        issues.append((CRITICAL, "mixed-content", p.mixed[0]))
    if result["elapsed"] > 1.5:
        issues.append((WARNING, "slow-response", f"{result['elapsed']:.1f}s"))
    if len(result["body"]) > 300_000:
        issues.append((NOTICE, "large-html", f"{len(result['body']) // 1024}KB"))

    data.update({
        "title": p.title, "description": p.description,
        "h1": p.h1s[0] if p.h1s else None, "words": p.words,
        "size_kb": len(result["body"]) // 1024,
        "jsonld_types": sorted({d.get("@type") for d in p.jsonld
                                if isinstance(d, dict) and isinstance(d.get("@type"), str)}),
    })
    return issues, data


def site_checks(base_url, pages, log):
    """Site-level checks: robots.txt, https enforcement, duplicates."""
    issues = defaultdict(list)   # code -> [(severity, url, detail)]
    parsed = urllib.parse.urlparse(base_url)
    root = f"{parsed.scheme}://{parsed.netloc}"

    r = fetch(root + "/robots.txt")
    if r["status"] != 200:
        issues["robots-missing"].append((WARNING, root + "/robots.txt", f"HTTP {r['status']}"))
    else:
        body = decode_body(r)
        if re.search(r"^user-agent:\s*\*\s*$", body, re.I | re.M) and \
           re.search(r"^disallow:\s*/\s*$", body, re.I | re.M):
            issues["robots-blocks-all"].append((CRITICAL, root + "/robots.txt", "Disallow: /"))
        if "sitemap:" not in body.lower():
            issues["robots-no-sitemap"].append((NOTICE, root + "/robots.txt", None))

    if parsed.scheme == "https":
        h = fetch(f"http://{parsed.netloc}/", follow=False)
        if h["status"] not in (301, 308) and h["error"] is None:
            issues["no-https-redirect"].append((WARNING, f"http://{parsed.netloc}/",
                                                f"HTTP {h['status']} (expected 301)"))

    for field, code in (("title", "duplicate-title"), ("description", "duplicate-description")):
        groups = defaultdict(list)
        for pg in pages:
            if pg.get(field):
                groups[pg[field]].append(pg["url"])
        for val, urls in groups.items():
            if len(urls) > 1:
                for u in urls:
                    issues[code].append((WARNING, u, f'"{val[:60]}" shared by {len(urls)} pages'))
    return issues


# ---------------------------------------------------------------- report
def c(code, s, tty):
    return f"\033[{code}m{s}\033[0m" if tty else s


def build_report(base_url, page_urls, sampled, results, grouped, elapsed, tty=False, md=False):
    sev_colour = {CRITICAL: "31;1", WARNING: "33", NOTICE: "36"}
    lines = []
    w = lines.append

    statuses = Counter()
    times = []
    for r, _ in results:
        times.append(r["elapsed"])
        if r["error"]:
            statuses["error"] += 1
        elif r["chain"]:
            statuses["redirect"] += 1
        elif r["status"] >= 400:
            statuses[f"{r['status'] // 100}xx"] += 1
        else:
            statuses["200"] += 1

    counts = Counter()
    for code, items in grouped.items():
        for sev, _, _ in items:
            counts[sev] += 1

    host = urllib.parse.urlparse(base_url).netloc
    h = (lambda s: f"## {s}") if md else (lambda s: c("1", s.upper(), tty))
    w(f"# SEO audit — {host}" if md else c("1;4", f"SEO AUDIT — {host}", tty))
    w("")
    sample_note = f" (sampled from {len(page_urls)} in sitemap)" if sampled else ""
    w(f"Crawled {len(results)} pages{sample_note} in {elapsed:.0f}s.")
    ok = statuses.get("200", 0)
    w(f"Status: {ok} OK · {statuses.get('redirect', 0)} redirected · "
      f"{statuses.get('4xx', 0) + statuses.get('5xx', 0)} broken · {statuses.get('error', 0)} unreachable. "
      f"Response time median {statistics.median(times):.2f}s, p90 {sorted(times)[int(len(times) * 0.9) - 1]:.2f}s."
      if times else "No pages fetched.")
    w("")
    verdict = (f"{counts[CRITICAL]} critical, {counts[WARNING]} warnings, {counts[NOTICE]} notices")
    w(("**" + verdict + "**") if md else c("1", verdict, tty))
    w("")

    for sev in (CRITICAL, WARNING, NOTICE):
        codes = [(code, items) for code, items in grouped.items() if items and items[0][0] == sev]
        if not codes:
            continue
        w(h(f"{sev.title()} ({sum(len(i) for _, i in codes)})"))
        for code, items in sorted(codes, key=lambda x: -len(x[1])):
            label = c(sev_colour[sev], f"[{code}]", tty) if not md else f"`{code}`"
            w(f"  {label} ×{len(items)}" if not md else f"- {label} ×{len(items)}")
            for _, url, detail in items[:3]:
                d = f"  — {detail}" if detail else ""
                w(f"      {url}{d}" if not md else f"    - {url}{d}")
            if len(items) > 3:
                w(f"      … and {len(items) - 3} more" if not md else f"    - … and {len(items) - 3} more")
        w("")

    # top actions
    actionable = [(code, items) for code, items in grouped.items()
                  if items and items[0][0] in (CRITICAL, WARNING)]
    actionable.sort(key=lambda x: (x[1][0][0] != CRITICAL, -len(x[1])))
    if actionable:
        w(h("Top actions"))
        for i, (code, items) in enumerate(actionable[:3], 1):
            n = f"{len(items)} page{'s' if len(items) != 1 else ''}"
            w(f"  {i}. {code} ({n}) — {FIX_HINTS.get(code, code)}"
              if not md else f"{i}. **{code}** ({n}) — {FIX_HINTS.get(code, code)}")
        w("")
    if not counts:
        w("No issues found — clean crawl." if not md else "_No issues found — clean crawl._")
    return "\n".join(lines)


# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(description="Streamlined technical SEO audit from a sitemap.")
    ap.add_argument("url", help="Sitemap URL or site root (https://example.com)")
    ap.add_argument("--max-pages", type=int, default=100, help="Max pages to crawl (default 100)")
    ap.add_argument("--workers", type=int, default=8, help="Concurrent fetches (default 8)")
    ap.add_argument("--delay", type=float, default=0.0, help="Per-worker delay between fetches (s)")
    ap.add_argument("--markdown", metavar="FILE", help="Also write the report as markdown")
    ap.add_argument("--json", metavar="FILE", help="Also dump raw results as JSON")
    ap.add_argument("--quiet", action="store_true", help="Suppress progress output")
    args = ap.parse_args()

    tty = sys.stdout.isatty()
    log = (lambda *a: None) if args.quiet else (lambda *a: print(*a, file=sys.stderr))
    start = time.monotonic()

    base = args.url if args.url.startswith("http") else "https://" + args.url
    log(f"Discovering sitemap(s) for {base} ...")
    page_urls, sitemaps = discover_and_load_sitemaps(base, log)
    if not page_urls:
        print(f"error: no sitemap URLs found for {base} (tried: {', '.join(sitemaps)})", file=sys.stderr)
        sys.exit(1)

    sampled = len(page_urls) > args.max_pages
    targets = page_urls
    if sampled:
        step = len(page_urls) / args.max_pages
        targets = [page_urls[int(i * step)] for i in range(args.max_pages)]
    log(f"Crawling {len(targets)} of {len(page_urls)} URLs with {args.workers} workers ...")

    results = []

    def worker(u):
        if args.delay:
            time.sleep(args.delay)
        r = fetch(u)
        return audit_page(r), r

    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as ex:
        futures = {ex.submit(worker, u): u for u in targets}
        done = 0
        for fut in concurrent.futures.as_completed(futures):
            done += 1
            if done % 25 == 0:
                log(f"  {done}/{len(targets)}")
            try:
                (issues, data), raw = fut.result()
                results.append((raw, (issues, data)))
            except Exception as e:
                u = futures[fut]
                results.append(({"url": u, "status": None, "error": str(e), "elapsed": 0,
                                 "chain": [], "headers": {}, "body": b"", "final_url": u},
                                ([(CRITICAL, "fetch-failed", str(e))], {"url": u})))

    # group issues by code
    grouped = defaultdict(list)   # code -> [(sev, url, detail)]
    pagedata = []
    for raw, (issues, data) in results:
        pagedata.append(data)
        for sev, code, detail in issues:
            grouped[code].append((sev, raw["url"], detail))
    for code, items in site_checks(base, pagedata, log).items():
        grouped[code].extend(items)

    elapsed = time.monotonic() - start
    fetch_results = [(raw, None) for raw, _ in results]
    print(build_report(base, page_urls, sampled, fetch_results, grouped, elapsed, tty=tty))

    if args.markdown:
        with open(args.markdown, "w") as f:
            f.write(build_report(base, page_urls, sampled, fetch_results, grouped, elapsed, md=True) + "\n")
        log(f"markdown report -> {args.markdown}")
    if args.json:
        with open(args.json, "w") as f:
            json.dump({"base": base, "sitemaps": sitemaps, "pages": pagedata,
                       "issues": {k: [{"severity": s, "url": u, "detail": d} for s, u, d in v]
                                  for k, v in grouped.items()}}, f, indent=2)
        log(f"json dump -> {args.json}")

    sys.exit(2 if any(i[0][0] == CRITICAL for i in grouped.values() if i) else 0)


if __name__ == "__main__":
    main()
