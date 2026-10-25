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
| `--delay` | 0 | Politeness floor (s) per fetch — adaptive backoff can raise the effective delay above it, never below (see below) |
| `--time-budget SECONDS` | 0 (off) | Stop starting new fetches after this long; the report states how many URLs were skipped |
| `--check-links` | off | Also verify internal link targets that are **not** in the sitemap (capped at 50 extra fetches, most-linked first) |
| `--explain-scores` | off | Show the score arithmetic and internal issue codes in the report |
| `--markdown [FILE]` | — | Also write the report as markdown; without FILE, auto-names `seo-audit-<host>-<timestamp>.md` (put the flag after the url) |
| `--json [FILE]` | — | Dump verdict, coverage, ranking, page data + issues as JSON; auto-names like `--markdown` when FILE is omitted |
| `--quiet` | — | Suppress progress output |

### Adaptive politeness

The crawl detects how polite to be, so `--workers 2 --delay 1` is rarely needed:

- **robots.txt `Crawl-delay`** for the audit UA is honoured as a delay floor (capped
  at 10s), noted in the report.
- **Platform fingerprint**: a `Powered-By: Shopify` header at discovery starts the
  crawl at 2 workers with a 1s floor — Shopify's limiter trips on the initial burst
  faster than reactive backoff can respond (measured: 8 workers cost 68% of a sample
  to rate-limiting, and the block outlives the crawl).
- **Any 429/503 anywhere doubles a shared delay** for every worker (capped 15s) — one
  throttle signal slows the whole crawl, not just the URL that saw it. Twenty
  consecutive clean responses halve it back toward the floor. Canonical-target and
  `--check-links` fetches obey the same throttle.
- **Slow start**: with no explicit `--delay` on crawls >20 pages, the first 10 fetches
  run at 0.5s until the origin proves fast.

Explicit `--workers`/`--delay` pin behaviour exactly — adaptive behaviour only ever
makes the crawl *more* polite, never faster than your flags. The coverage block states
what happened ("backed off to 4s/fetch after 3 throttle responses") so a rerun is
explainable.

Individual 429/503 responses are also retried with backoff (honouring `Retry-After`).
Pages still throttled after retries are reported as **rate-limited (NOT audited)** — a
coverage gap, never as broken pages.

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

- **Indexation blockers** — pages cannot be crawled, indexed, or reached: sitemap
  4xx/5xx, fetch failures, noindex-in-sitemap, redirect loops, sitemap URLs blocked
  from Googlebot by robots.txt, a robots.txt that blocks Googlebot from effectively
  the whole site. Reserved strictly for indexation — a flawed-but-indexable page never
  lands here.
- **Material issues** — likely to affect discovery, consolidation, or presentation:
  missing `<title>`, mixed content, redirected sitemap URLs, duplicate titles,
  canonical problems (including broken/redirecting/noindexed canonical targets),
  internal links to 4xx/5xx pages, rate-limited coverage gaps, no http→https redirect.
- **Opportunities** — worth reviewing, phrased as review-not-fix: missing or shared
  meta descriptions, missing H1 / self-canonical, title & description length, thin
  visible text, slow-fetch outliers, internal links to redirects or noindexed pages,
  orphan pages.
- **Observations** — context only: multiple H1s, no structured data, missing viewport,
  large HTML, non-HTML sitemap entries, images without alt, cross-domain canonicals,
  many-to-one canonical consolidation, robots.txt blocking the audit UA (a coverage
  caveat, never an SEO finding).

### robots.txt handling

Assessment uses a small built-in **Google-REP parser**. Python's `urllib.robotparser`
follows RFC 9309, the 2022 robots.txt standard, only from 3.13.14, 3.14.5 and 3.15. On
the earlier releases this tool still supports, it applies the first matching rule
instead of the longest, has no `*`/`$` wildcards, and judges `Allow: /public/` under
`Disallow: /` as blocked. The parser implements the most-specific user-agent group,
longest-path-match with `*` and `$`, Allow winning ties, consistently on every
Python ≥3.9. What matters for indexation is
**Googlebot**: sitemap URLs Googlebot can't fetch are blockers, and "site blocked" is
derived only when Googlebot is blocked from the homepage **and** ≥95% of sitemap URLs.
The audit UA being blocked is reported separately as an observation ("audit coverage
may be limited").

Indexability directives are read per Google's spec: every `<meta name="robots">` **and**
`<meta name="googlebot">` tag, every `X-Robots-Tag` header (including repeated ones and
agent-prefixed rules — another crawler's `noindex` doesn't count), `none` treated as
`noindex`, and the most restrictive applicable rule winning.

Sitemap discovery reads at most 50 sitemap files; if an index has more, the report
carries an explicit warning that totals are incomplete.

### Priority scoring (transparent by design)

Every issue code has a static `(impact 1–5, confidence 1–5)` entry in the `IMPACT`
table in the source. **score = impact × confidence × affected_fraction**, where
affected_fraction = **affected crawled pages** / crawled pages (capped at 1). Scope is
what matters: a site-wide issue (robots block, missing https redirect) counts as every
crawled page, and a single broken canonical target or nav link counts as all the pages
that use it — one technical URL never dilutes a widespread problem down to 1/N.

The default report shows plain language ("high impact, high confidence — affects 37 of
100 crawled pages"); `--explain-scores` (and the JSON output) exposes the full
arithmetic and issue codes so any ranking can be recomputed by hand. No opaque logic.

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
counts across the crawl with **zero extra requests** by default: internal links to
4xx/5xx pages, to redirects, to noindexed pages, and to canonicalised-away pages are
all checked against pages already crawled. Off-sitemap link targets — where broken
internal links usually live — are not judged by default; `--check-links` verifies up
to 50 of them concurrently, most-linked first.

**Orphan rule:** orphan detection (sitemap URLs with zero inlinks) is only
statistically valid when the crawl saw enough of the site. It is reported **only at
≥80% sitemap coverage** and suppressed entirely below that — a sampled run never leads
with an orphan claim.

### Canonical target validation

Canonicals pointing outside the crawl set are deduped and at most 20 are fetched
(concurrently, most-used targets first, honouring `--time-budget`): targets that
don't return 200, redirect, or are noindexed are material issues; cross-domain
canonicals and many-pages-to-one-destination are observations. A rate-limited target
is left **unverified** — never reported as broken.

### Duplicate detection

Duplicate titles/descriptions are compared only across indexable pages: 200, HTML,
not redirected, not noindexed, and self-canonical. A redirected sitemap URL parsing
its destination's metadata, or pages deliberately consolidating via canonicals, never
manufacture duplicates.

## How it works

1. **Discover** — reads `robots.txt` `Sitemap:` lines, falls back to `/sitemap.xml` and
   `/sitemap_index.xml`. Sitemap indexes recurse; gzipped sitemaps handled. Each URL's
   source sitemap is kept for sampling and clustering.
2. **Sample** — stratified: proportional per child sitemap (minimum 3 per sitemap),
   homepage and top-level pages always included, evenly spaced within each stratum.
3. **Crawl** — concurrent fetches with manual redirect following (chains and loops are
   recorded), UA `performify-seo-audit/1.0`.
4. **Check** — ~30 per-page and site-level checks, the link graph, canonical targets,
   and per-URL robots.txt validation (Googlebot + audit UA via the built-in parser).
5. **Report** — verdict first, impact-ranked, clustered.

## Testing

`python3 test_seo_audit.py` — a local fixture suite (throwaway HTTP server with
deliberate faults) covering all four classes, the orphan-coverage gate, canonical
target checks, robots blocking, timing outliers, and stratified sampling.

## Limitations (by design)

- **Raw HTML only, no JS rendering** — client-side-rendered SPAs will look emptier than
  they are. If the raw HTML has no content, that's a finding in itself (raw HTML is
  what Googlebot fetches before it queues the page for rendering), but verify with a
  rendered crawl before calling it broken.
- **Sitemap-scoped discovery** — pages not in the sitemap are not crawled (internal
  links are analysed but not followed). Crawl-depth and full link-equity analysis
  remain Screaming Frog's job; this tool tells you *whether* you need that deeper pass.
- **No Core Web Vitals** — TTFB and fetch time are measured; LCP/CLS/INP need a browser.
- WAFs may challenge the UA and produce false "unreachable" results — retest with
  `--workers 2 --delay 1` before believing them.

Be polite on production origins: defaults are ~8 req/s.

## Licence

MIT. See [LICENSE](LICENSE).
