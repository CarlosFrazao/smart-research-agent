# Errors, Integration & Quality Checklist

---

## 1. ERROR HANDLING — FULL PROTOCOL

### HTTP 403 — Forbidden (site blocks the scraper)

```
Symptom: "HTTP 403" error or empty content returned

Step 1: add --wait 3000 and try again
  python3 scripts/scrape.py <url> --wait 3000

Step 2: if it persists → --wait 5000
  python3 scripts/scrape.py <url> --wait 5000

Step 3: try crawl.py with --limit 1 (uses a different user-agent)
  python3 scripts/crawl.py <url> --limit 1

Step 4: escalate to Level 4 (web-scraping-resilience)
```

### HTTP 429 — Too Many Requests (rate limit)

```
Symptom: "HTTP 429" error

Step 1: wait 30 seconds
Step 2: try again with --wait 3000
Step 3: if it persists → wait 2 minutes and try again
Step 4: if it persists → process other sites in the batch first
         and return to this one in the next cycle
```

### Empty page or incomplete content (JS did not render)

```
Symptom: returned markdown is too short, missing the expected content,
         or returns only the HTML shell with no data

Step 1: add --wait 2000
  python3 scripts/scrape.py <url> --wait 2000

Step 2: if still incomplete → --wait 4000

Step 3: if still incomplete → try crawl.py (sometimes extracts more)
  python3 scripts/crawl.py <url> --limit 1

Step 4: escalate to browser_subagent (Level 2) to inspect
         what the JS is rendering
```

### Timeout

```
Symptom: operation does not respond or returns a timeout

Step 1: simplify the URL (remove tracking params ?utm_=...)
  python3 scripts/scrape.py https://exemplo.com/page  (no ?utm_source=...)

Step 2: try the cached version via search_web
  "cache:https://exemplo.com/page"

Step 3: try the Wayback Machine
  python3 scripts/scrape.py https://web.archive.org/web/*/https://exemplo.com/page

Step 4: for a crawl with timeout → reduce --limit
  python3 scripts/crawl.py <url> --limit 5
```

### Batch does not finish (polling timeout)

```
Symptom: "Timeout — batch did not finish in 80s"

Cause: too many URLs or slow sites

Solution 1: split the batch into smaller groups
  Before: 10 URLs in 1 batch
  After: 2 batches of 5 URLs each

Solution 2: process individually with scrape.py
  to identify which URL is hanging

Solution 3: for design mode, reduce to 3-4 URLs per batch
  (design analysis is heavier than markdown extraction)
```

### Partial content (scrape returns half the page)

```
Symptom: content clearly cut off, missing sections visible on the site

Step 1: check if the page uses infinite scroll or lazy loading
  → add --wait 4000 to allow load time

Step 2: try crawl.py with --limit 1
  (crawl sometimes extracts content that scrape misses)

Step 3: for paginated pages, extract each page separately
  python3 scripts/scrape.py https://exemplo.com/blog?page=1
  python3 scripts/scrape.py https://exemplo.com/blog?page=2
```

### API Key not configured

```
Symptom: "FIRECRAWL_API_KEY not configured"

Solution:
  1. Get a free key at: https://firecrawl.dev
  2. Configure in the current session:
     export FIRECRAWL_API_KEY=fc-...
  3. Configure permanently (add to .bashrc or .zshrc):
     echo 'export FIRECRAWL_API_KEY=fc-...' >> ~/.bashrc

Self-hosted (no public API key):
  export FIRECRAWL_BASE_URL=http://localhost:3002
  (crawl.py and scrape.py detect it automatically and don't require API_KEY)
```

### HTTP 404 — URL Not Found (guessed URL does not exist)

```
Symptom: "404 Not Found" or empty content on a URL that looked valid

Root cause: URL guessed from memory/link text rather than discovered.
Common with integration pages (e.g., /integrations/whatsapp.html that was moved
or renamed) and deep product docs.

Protocol (NEW — applies BEFORE scraping):
  Step 1: do NOT scrape blind. Use firecrawl_map first to discover real URLs:
    firecrawl map <domain> --search "<integration keyword>"
    e.g.  firecrawl map https://www.zoho.com --search "whatsapp integration"

  Step 2: review the map output for the real integration URL, then scrape:
    firecrawl scrape "<url-from-map-output>"

  Step 3: only if map returns nothing relevant → fall back to web search:
    firecrawl search "site:<domain> whatsapp integration"

Why map first: integration pages are frequently at non-obvious paths
(/apps/whatsapp, /integrations, /products/crm/whatsapp, etc.).
Guessing paths (e.g., /whatsapp.html) fails ~30% of the time on real sites.
```

---

## 2. WEB-RESEARCHER INTEGRATION FLOW

```
WEB RESEARCHER runs the search
        ↓
LEVEL 1: read_url_content
  → Failed (403) or incomplete content (JS did not render)?
        ↓
LEVEL 2: browser_subagent
  → Still insufficient (very dynamic site, needs more)?
        ↓
LEVEL 3: FIRECRAWL SCRAPER ← this skill
        ↓
  Select operation:
  ├── 1 page + text → scrape.py <url>
  ├── 1 page + heavy JS → scrape.py <url> --wait 2000
  ├── 1 page + design → scrape.py <url> --mode design
  ├── 1 page + screenshot → scrape.py <url> --mode screenshot
  ├── 1 page + PDF → scrape.py <url> --mode pdf
  ├── Discover URLs → map.py <url>
  ├── Entire site → crawl.py <url>
  └── Several sites → batch.py "url1,url2,url3"
        ↓
  Error during execution?
  ├── 403/429 → apply error protocol (Section 1)
  ├── Empty content → larger --wait or crawl.py
  └── Timeout → simplify URL or reduce --limit
        ↓
  Still no result after protocol?
        ↓
LEVEL 4: web-scraping-resilience (persistent blocks)
LEVEL 5: indirect search (Google cache, Wayback Machine, snippets)
```

---

## 3. DECISION: SCRAPE VS. CRAWL VS. BATCH

| Scenario | Correct script |
|---|---|
| I need the content of 1 specific URL | `scrape.py` |
| I need the full documentation of a technology | `crawl.py` |
| I need to compare pricing of 5 competitors | `batch.py` |
| I need the design of 1 site for reference | `scrape.py --mode design` |
| I need design references from 6+ sites | `batch.py --mode design` |
| I don't know which site pages hold the content | `map.py` → then `crawl.py` |
| Site blocks normal reading | `scrape.py --wait 3000` |
| Need visual evidence of a site | `scrape.py --mode screenshot` |
| PDF report at a public URL | `scrape.py --mode pdf` |
| I want a competitor's entire blog | `crawl.py --include /blog --limit 50` |

---

## 4. SELF-HOSTED — LOCAL CONFIGURATION

For those running Firecrawl on their own server (without relying on the public API):

```bash
# Point to the local instance
export FIRECRAWL_BASE_URL=http://localhost:3002

# Or to a remote server
export FIRECRAWL_BASE_URL=https://firecrawl.meuservidor.com

# Optional API key in self-hosted (depends on config)
export FIRECRAWL_API_KEY=fc-...  # omit if not configured
```

`crawl.py` and `scrape.py` automatically detect when `BASE_URL` does not
contain `firecrawl.dev` and don't require a public API key. `batch.py` and
`map.py` always use the configured base URL.

---

## 5. QUALITY CHECKLIST

Run before delivering any scraping result:

```
[ ] FIRECRAWL_API_KEY is configured (or FIRECRAWL_BASE_URL for self-hosted)
[ ] Correct script selected for the goal (see Section 3)
[ ] For JS/SPA sites: --wait added (minimum 2000ms)
[ ] Extracted content is complete — not cut off mid-way
[ ] For design mode: all main fields filled
    (colors, typography, components, exceptional_elements)
[ ] For batch: all URLs in the batch processed successfully
[ ] For crawl: irrelevant sections excluded with --exclude
[ ] Errors handled per protocol before escalating to Level 4
[ ] Output formatted and readable for the final consumer of the research
```
