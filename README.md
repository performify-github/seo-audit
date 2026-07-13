# seo-audit

Streamlined technical SEO audit from the terminal. Give it a sitemap URL (or just the
site root) and it crawls the sitemap (stratified sample if large), runs ~30 checks
including an internal-link graph, and prints an impact-ranked 1–2 page report that
leads with a verdict. The answer to "is this site technically sound, and what are the
top 3 things to fix?" in about a minute — without firing up Screaming Frog.

**Stdlib-only Python 3.9+.** No installs, no dependencies, runs anywhere.

## Usage

```sh
python3 seo_audit.py https://example.com                      # discovers the sitemap itself
python3 seo_audit.py https://example.com/sitemap.xml --max-pages 150
python3 seo_audit.py https://example.com --markdown report.md --json raw.json
```

| Flag | Default | |
|---|---|---|
| `--max-pages` | 100 | Stratified sample if the sitemap is larger (see below); `0` = crawl everything |
| `--workers` | 8 | Concurrent fetches |
| `--delay` | 0 | Per-worker politeness delay (s) — use `--workers 2 --delay 0.5` on fragile origins |
| `--time-budget SECONDS` | 0 (off) | Stop starting new fetches after this long; the report states how many URLs were skipped |
| `--markdown FILE` | — | Also write the report as markdown |
| `--json FILE` | — | Dump verdict, coverage, ranking, page data + issues as JSON |
| `--quiet` | — | Suppress progress output |

Rate limiting: 429/503 responses are retried with backoff (honouring `Retry-After`).
Pages still throttled after retries are reported as **rate-limited (NOT audited)** — a
coverage gap, never as broken pages. Shopify storefronts throttle aggressively; use
`--workers 2 --delay 1` there.

Exit code `0` = no indexation blockers, `2` = indexation blockers found — usable in CI
or a cron health check.

## Report structure

```
VERDICT                    1–2 generated sentences: indexation health, then the top issue
COVERAGE                   sampled/total, indexable HTML, redirected, broken, noindexed,
                           rate-limited, unreachable · TTFB and fetch-time medians
PRIORITY ACTIONS           top 3 blocker/material issues by score, inputs shown
SECONDARY OPPORTUNITIES    review-not-fix items, clustered
OBSERVATIONS               context only, one line each
```

### The four issue classes

- **Indexation blockers** — pages cannot or should not be indexed: sitemap 4xx/5xx,
  fetch failures, noindex-in-sitemap, redirect loops, robots.txt blanket disallow,
  sitemap URLs blocked by robots.txt, missing `<title>`, mixed content.
- **Material issues** — likely to affect discovery, consolidation, or presentation:
  redirected sitemap URLs, duplicate titles/descriptions, canonical problems (including
  broken/redirecting/noindexed canonical targets), internal links to 4xx/5xx pages,
  rate-limited coverage gaps, no http→https redirect.
- **Opportunities** — worth reviewing, phrased as review-not-fix: missing meta
  description / H1 / self-canonical, title & description length, thin visible text,
  slow-fetch outliers, internal links to redirects or noindexed pages, orphan pages.
- **Observations** — context only: multiple H1s, no structured data, missing viewport,
  large HTML, non-HTML sitemap entries, images without alt, cross-domain canonicals,
  many-to-one canonical consolidation.

### Priority scoring (transparent by design)

Every issue code has a static `(impact 1–5, confidence 1–5)` entry in the `IMPACT`
table in the source. **score = impact × confidence × affected_fraction**, where
affected_fraction = affected URLs / crawled pages (capped at 1). The report prints the
inputs with every priority action — e.g. `score 4.0 = impact 5 × confidence 5 × 8/50
affected` — so any ranking can be recomputed by hand. No opaque logic.

### Pattern clustering

Issues are clustered by (source sitemap, first path segment) with crawled-pages
denominators: `42/48 crawled pages under /products/ (sitemap products.xml) — likely
template-level` instead of bare counts, with example URLs.

### Timing

TTFB (headers received) and total fetch time are recorded separately and never
conflated. There is no absolute "slow page" threshold: a page is flagged only when its
fetch time is **both** >2.5s **and** >3× the site's own median (minimum 5 successful
fetches) — slow-but-uniform sites aren't nagged page by page, and fast sites aren't
flattered.

### Internal-link graph

Every `<a href>` is kept, resolved, and filtered to internal links, giving inlink
counts across the crawl with **zero extra requests**: internal links to 4xx/5xx pages,
to redirects, to noindexed pages, and to canonicalised-away pages are all checked
against pages already crawled.

**Orphan rule:** orphan detection (sitemap URLs with zero inlinks) is only
statistically valid when the crawl saw enough of the site. It is reported **only at
≥80% sitemap coverage** and suppressed entirely below that — a sampled run never leads
with an orphan claim.

### Canonical target validation

Canonicals pointing outside the crawl set are deduped and at most 50 are fetched:
targets that don't return 200, redirect, or are noindexed are material issues;
cross-domain canonicals and many-pages-to-one-destination are observations.

## How it works

1. **Discover** — reads `robots.txt` `Sitemap:` lines, falls back to `/sitemap.xml` and
   `/sitemap_index.xml`. Sitemap indexes recurse; gzipped sitemaps handled. Each URL's
   source sitemap is kept for sampling and clustering.
2. **Sample** — stratified: proportional per child sitemap (minimum 3 per sitemap),
   homepage and top-level pages always included, evenly spaced within each stratum.
3. **Crawl** — concurrent fetches with manual redirect following (chains and loops are
   recorded), UA `performify-seo-audit/1.0`.
4. **Check** — ~30 per-page and site-level checks, the link graph, canonical targets,
   and per-URL robots.txt validation (Googlebot + audit UA via `urllib.robotparser`).
5. **Report** — verdict first, impact-ranked, clustered.

## Testing

`python3 test_seo_audit.py` — a local fixture suite (throwaway HTTP server with
deliberate faults) covering all four classes, the orphan-coverage gate, canonical
target checks, robots blocking, timing outliers, and stratified sampling.

## Limitations (by design)

- **Raw HTML only, no JS rendering** — client-side-rendered SPAs will look emptier than
  they are. If the raw HTML has no content, that's a finding in itself (crawler-first
  indexing sees the same), but verify with a rendered crawl before calling it broken.
- **Sitemap-scoped discovery** — pages not in the sitemap are not crawled (internal
  links are analysed but not followed). Crawl-depth and full link-equity analysis
  remain Screaming Frog's job; this tool tells you *whether* you need that deeper pass.
- **No Core Web Vitals** — TTFB and fetch time are measured; LCP/CLS/INP need a browser.
- WAFs may challenge the UA and produce false "unreachable" results — retest with
  `--workers 2 --delay 1` before believing them.

Be polite on production origins: defaults are ~8 req/s.
