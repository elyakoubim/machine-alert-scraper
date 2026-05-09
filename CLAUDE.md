# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Big picture

The repo runs two unrelated processes against the same Airtable base (`appQrNOm3Q7D9uEed`):

1. **Worker (`main.py`)** — auction/liquidation site scraper. Discovers v2 scrapers in `scrapers/sites/*.py`, runs each, pushes new annonces into the Airtable `Annonces` table, updates `Sources` row status. Designed to be run nightly via Railway cron.
2. **Web (`webhook_server.py`)** — FastAPI app that receives the Tally form webhook (free-trial funnel). It parses the lead, queries Airtable for matching annonces, sends a teaser email through Brevo, and creates a row in `Leads_Essai`. Runs as the `web` process on Railway.

Both are deployed from the same Railway service; the `Procfile` defines `web` and `worker` separately.

## Common commands

```bash
pip install -r requirements.txt
playwright install chromium      # required: most sites need JS

# Worker — auction scraper
python main.py                          # all v2 scrapers
python main.py --scraper auctelia       # one scraper (matches BaseScraper.source_nom, case-insensitive)
python main.py --dry-run                # collect + log, don't write to Airtable
python main.py --no-llm                 # skip Haiku categorization
python main.py --max-annonces 5         # cap per scraper (smoke test)

# Webhook server — local dev
uvicorn webhook_server:app --reload --port 8000

# One-shot: backfill Airtable rows missing the `categorie` field
python scripts/backfill_categories.py --dry-run
python scripts/backfill_categories.py --limit 50
```

There is **no test suite**, no linter config, and no build step.

## Scraper architecture (v2 — the only one in use)

Each site lives in `scrapers/sites/<slug>.py` and defines a subclass of `BaseScraper` (in `scrapers/base.py`). `main.py:discover_scrapers()` walks the `scrapers.sites` package with `pkgutil` and runs every `BaseScraper` subclass it finds — **adding a new scraper means dropping a new file in that directory; nothing else needs to be touched.**

Each subclass must set four class attributes (`source_nom`, `source_pays`, `base_url`, `requires_javascript`) and implement four abstract methods:

- `list_listing_urls()` — yield listing/index URLs (or a sitemap URL, like `auctelia.py`)
- `parse_listing(html, listing_url)` — yield detail page URLs from a listing page
- `parse_detail(html, url)` — return a `dict` of raw fields for one annonce
- `map_category(native_category)` — map a site's native category to one of the 8 Faillink categories, or return `default_category` to let the LLM decide

`BaseScraper.run()` orchestrates fetch → parse_listing → fetch detail → parse_detail → `normalize()` → yield `Annonce`. `normalize()` is where the LLM fallback kicks in: if `map_category` returned the default, it calls `LLMExtractor.categorize()` (Claude Haiku).

### Fetchers

`scrapers/fetchers.py` provides three interchangeable fetchers exposing `fetch(url) -> str`:

- `HttpxFetcher` — plain HTTP, used when `requires_javascript = False`
- `PlaywrightFetcher` — used when `requires_javascript = True`. **Runs Playwright in a dedicated thread with its own asyncio loop.** This is intentional — running sync Playwright inline conflicts with the asyncio loop the Anthropic SDK initializes, so do not "simplify" it back to `sync_playwright()`.
- `ScraperApiFetcher` — proxy fallback for Cloudflare/captcha sites

`BaseScraper.fetch()` lazily picks one based on `requires_javascript`. Individual scrapers can override `fetch()` to mix fetchers (e.g. `auctelia.py` uses httpx for the sitemap and Playwright for detail pages).

### Legacy code — do not use

`scrapers/static.py` (`StaticScraper`), `scrapers/dynamic.py` (`DynamicScraper`), and the `SOURCES` list in `config.py` are leftovers from a config-driven v1 architecture. They are **not imported by `main.py` or any v2 scraper**, their `__init__` signatures don't even match the current `BaseScraper`, and the README still references them. Treat them as dead code; don't extend them, and prefer deleting over patching if you touch that area. The same goes for the "Add a new site" section of the README — it describes the v1 flow.

## The 8 canonical Faillink categories (load-bearing invariant)

`scrapers/base.py:CATEGORIES_FAILLINK` is the single source of truth: 8 strings **with French accents** that must match the Airtable single-select field exactly. The defensive layer:

- `normalize_categorie()` maps known accent-stripped legacy values to the canonical form and falls back to `"Autre / non classé"` for anything else.
- `Annonce.to_airtable()` calls `normalize_categorie()` on every push. This is critical: `airtable.py` POSTs with `typecast: true`, which would otherwise silently create new option values (and split alerts across duplicates) the moment any code path emits a string outside the canonical set.
- `LLMExtractor` returns the same 8 strings; it has its own `_normalize_accents` for robustness.

When adding a category, update `CATEGORIES_FAILLINK`, the `LLMExtractor.SYSTEM_PROMPT`, and the Airtable single-select options together — and run `scripts/backfill_categories.py` if existing rows need re-classifying.

## Airtable safety rules

- **Never write `alerte_envoyee` from Python.** This field is owned by a Make automation; writing it can re-fire alerts or cancel pending ones. `airtable.py:update_annonce_fields()` and `batch_update_annonces()` strip it defensively — don't bypass them.
- **Don't use `git add -A` for commits that touch `.env`.** `.env.example` is the only env file that should ever be tracked.
- Push dedup: `airtable.py:push_annonces()` calls `get_existing_urls(source_nom)` first and filters out URLs already present, so re-running a scraper is safe and idempotent.
- Table IDs in code (`tbljV9HICBsPyqjQk` for Annonces, `tblPaOrQekEMdaW5x` for Sources, `tblXhuS0M1TtRuSel` for Profils, `tbl5LfC0pUmt4rUJl` for Leads_Essai) can be overridden via env vars — the defaults in `airtable.py` and `webhook_server.py` are the production ones.

## Required environment

See `.env.example` for the full list. Production secrets live in Railway → Variables, never in the repo. Minimum to run anything useful:

- `AIRTABLE_TOKEN` — required by both `main.py` and `webhook_server.py`
- `ANTHROPIC_API_KEY` — required for LLM categorization (or pass `--no-llm`)
- `BREVO_API_KEY` — required by the webhook server to actually send the trial email
