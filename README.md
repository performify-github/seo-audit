# seo-audit

Streamlined technical SEO audit from the terminal. Give it a sitemap URL (or just the
site root) and it crawls up to N pages, runs ~20 checks, and prints a 1–2 page summary
with top actions. The answer to "is this site technically sound?" in about a minute —
without firing up Screaming Frog.

**Stdlib-only Python 3.9+.** No installs, no dependencies, runs anywhere.

## Usage

```sh
python3 seo_audit.py https://example.com                      # discovers the sitemap itself
python3 seo_audit.py https://example.com/sitemap.xml --max-pages 150
python3 seo_audit.py https://example.com --markdown report.md --json raw.json
```

| Flag | Default | |
|---|---|---|
| `--max-pages` | 100 | Evenly spaced sample across the sitemap if it's larger |
| `--workers` | 8 | Concurrent fetches |
| `--delay` | 0 | Per-worker politeness delay (s) — use `--workers 2 --delay 0.5` on fragile origins |
| `--markdown FILE` | — | Also write the report as markdown |
| `--json FILE` | — | Dump raw page data + issues as JSON |
| `--quiet` | — | Suppress progress output |

Exit code `0` = clean or warnings only, `2` = critical issues found — usable in CI or a
cron health check.

## What it checks

- **Critical** — 4xx/5xx or unreachable URLs in the sitemap · noindex (meta or
  `X-Robots-Tag`) in the sitemap · missing `<title>` · mixed content on https pages ·
  redirect loops / >5 hops · robots.txt blanket `Disallow: /`
- **Warning** — sitemap URLs that redirect · duplicate titles / meta descriptions ·
  missing description / H1 / canonical · multiple H1s · canonical pointing elsewhere ·
  title outside 30–60 chars · thin content (<200 words) · TTFB >1.5s · no http→https
  redirect · robots.txt missing
- **Notice** — description outside 70–160 chars · images missing alt · no JSON-LD ·
  missing viewport meta · HTML >300KB · non-HTML content types in the sitemap ·
  robots.txt not referencing the sitemap

## How it works

1. **Discover** — reads `robots.txt` `Sitemap:` lines, falls back to `/sitemap.xml` and
   `/sitemap_index.xml`. Sitemap indexes recurse; gzipped sitemaps handled.
2. **Sample** — evenly spaced across the sitemap so every site section is represented.
3. **Crawl** — concurrent fetches with manual redirect following (chains and loops are
   recorded), UA `performify-seo-audit/1.0`.
4. **Report** — findings grouped by issue code with example URLs, ranked critical-first,
   ending with a plain-English "Top actions" list.

## Limitations (by design)

- **Raw HTML only, no JS rendering** — client-side-rendered SPAs will look emptier than
  they are. If the raw HTML has no content, that's a finding in itself (crawler-first
  indexing sees the same), but verify with a rendered crawl before calling it broken.
- **Sitemap-scoped, not link-graph-scoped** — no orphan-page discovery, crawl depth, or
  internal-link equity. This tool tells you *whether* you need that deeper pass; Screaming
  Frog remains the tool for it.
- **No Core Web Vitals** — TTFB is measured; LCP/CLS/INP need a browser.
- WAFs may challenge the UA and produce false "unreachable" results — retest with
  `--workers 2 --delay 1` before believing them.

Be polite on production origins: defaults are ~8 req/s.
