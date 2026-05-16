"""
Scraper IVG.auction (DE) — German judicial insolvency auctions
================================================================

Plateforme : IVG.auction (Industrie-Verwertung-Gesellschaft) — enchères
d'insolvabilité judiciaire en Allemagne. Petit volume (~80 auctions
actives) mais data riche : numéro de procédure judiciaire (`procedure_number`)
+ curateur identifié (`contact`).

Stack technique :
   - Frontend : SPA Nuxt.js 3 (Vue.js)
   - Backend : API JSON publique sur api.ivg.auction
   - Endpoint principal : /auction-api/auctions (paginated)
   - Pas d'auth, pas de captcha, pas d'IP block

Strategie : API JSON direct, pattern similaire a Biddit (override run()).
1 page API = 25 auctions par defaut. ~4 pages = ~80 auctions actives.

Haiku ON : multi-categories (machines industrielles, vehicules, mobilier,
agro, outillage, etc.). On laisse Haiku categoriser depuis titre+description.

Specificite : 1 "auction" = 1 dossier judiciaire. Peut contenir N items
(item_count > 1) qu'on n'expose pas individuellement (l'API ne sert pas
les sub-items). Pour les bundles, on signale "Bundle de N items" dans
la description.
"""

from __future__ import annotations

import logging
import time
from typing import Iterator, Optional

import httpx

from scrapers.base import BaseScraper, Annonce
import airtable as airtable_client

logger = logging.getLogger(__name__)


BASE_URL = "https://ivg.auction"
API_URL = "https://api.ivg.auction/auction-api/auctions"
PAGE_SIZE = 25  # Defaut serveur, on ne le force pas
# Garde-fou anti-runaway. Pagination reelle ~4 pages, on stoppe tot
# via `data.pages` ou page vide.
MAX_PAGES = 20


class _IvgFetcher:
    """Fetcher httpx pour l'API JSON IVG (sans brotli, Accept JSON)."""

    def __init__(self, timeout: int = 30):
        self.timeout = timeout
        self.headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "de-DE,de;q=0.9,en;q=0.7",
            "Accept-Encoding": "gzip, deflate",  # PAS de brotli
        }

    def fetch(self, url: str) -> str:
        """Compat BaseScraper. Pour IVG on n'utilise que get_json."""
        with httpx.Client(
            timeout=self.timeout, follow_redirects=True, headers=self.headers,
        ) as client:
            r = client.get(url)
            r.raise_for_status()
            return r.text

    def get_json(self, url: str, params: Optional[dict] = None) -> dict:
        with httpx.Client(
            timeout=self.timeout, follow_redirects=True, headers=self.headers,
        ) as client:
            r = client.get(url, params=params)
            r.raise_for_status()
            return r.json()


class IvgScraper(BaseScraper):
    source_nom = "IVG"
    source_pays = "DE"
    base_url = BASE_URL
    requires_javascript = False
    rate_limit_seconds = 1.0
    # Sentinelle pour trigger le fallback Haiku dans BaseScraper.normalize().
    # IVG est multi-categories : Haiku categorise depuis titre + description.
    default_category = "Autre / non classe"

    def __init__(self, fetcher=None, llm_extractor=None):
        if fetcher is None:
            fetcher = _IvgFetcher()
        super().__init__(fetcher=fetcher, llm_extractor=llm_extractor)
        self._existing_urls: Optional[set] = None
        self._fetcher_typed = fetcher  # acces direct au get_json

    # =========================================================
    # Methodes abstract BaseScraper : stubs (on override run())
    # =========================================================

    def list_listing_urls(self) -> Iterator[str]:
        """Stub : non utilise (run() override fait son propre loop API)."""
        return
        yield  # type: ignore[unreachable]

    def parse_listing(self, html: str, listing_url: str) -> Iterator[str]:
        """Stub : non utilise."""
        return
        yield  # type: ignore[unreachable]

    def parse_detail(self, html: str, url: str) -> dict:
        """Stub : non utilise."""
        return {}

    def map_category(self, native_category: Optional[str]) -> str:
        """Retourne default_category pour declencher Haiku categorisation."""
        return self.default_category

    # =========================================================
    # Mapping JSON API -> raw dict
    # =========================================================

    @staticmethod
    def _build_description(a: dict) -> str:
        """Description structuree : description courte + procedure + contact +
        viewing/pickup + adresse + bundle marker.
        """
        parts: list = []
        desc = (a.get("description") or "").strip()
        if desc:
            parts.append(desc)
        pn = a.get("procedure_number")
        if pn:
            parts.append(f"Procédure : N°{pn}")
        contact = (a.get("contact") or "").strip()
        if contact:
            parts.append(f"Contact : {contact}")
        vt = (a.get("viewing_time") or "").strip()
        if vt:
            parts.append(f"Visite : {vt}")
        pt = (a.get("pickup_time") or "").strip()
        if pt:
            parts.append(f"Enlèvement : {pt}")
        addr = a.get("address") or {}
        pcity = f"{(addr.get('postal_code') or '').strip()} {(addr.get('city') or '').strip()}".strip()
        if pcity:
            parts.append(f"Adresse : {pcity}")
        item_count = a.get("item_count") or 0
        if isinstance(item_count, int) and item_count > 1:
            parts.append(f"Bundle de {item_count} items")
        return "\n\n".join(parts)[:2000]

    def _map_auction(self, auction: dict) -> Optional[dict]:
        """Convertit une auction JSON IVG en raw dict pour normalize().

        Retourne None si l'auction doit etre skippee (status offline,
        champs critiques manquants).
        """
        aid = (auction.get("id") or "").strip()
        title = (auction.get("title") or "").strip()
        if not aid or not title:
            return None
        if auction.get("status") != "online":
            return None

        # Image : signed URL CDN api.ivg.auction/general-api/file/...
        image_url = ""
        cover = auction.get("cover_image")
        if isinstance(cover, dict):
            image_url = (cover.get("url") or "").strip()

        return {
            "titre": title,
            "description": self._build_description(auction),
            "prix": "Sur offre",
            "image_url": image_url,
            "type_vente": "Insolvenzversteigerung",
            "categorie_native": None,  # laisse Haiku decider
            "source_id": aid,
            # date_fin : format "YYYY-MM-DD HH:MM:SS" (Berlin TZ implicite).
            # BaseScraper.normalize -> _iso_or_none() parse via dateutil.
            "date_fin": (auction.get("end_date_time") or "").strip() or None,
        }

    # =========================================================
    # Run override : loop API direct
    # =========================================================

    def run(self) -> Iterator[Annonce]:
        """Loop API : GET /auction-api/auctions?page=N.

        Yield des Annonces deja normalisees. Pas de fetch detail.
        Stop early sur page vide ou page >= data.pages.
        """
        seen_urls: set = set()
        n_pages = 0
        n_yielded = 0
        n_skipped = 0
        total_in_api: Optional[int] = None
        api_total_pages: Optional[int] = None

        # Lazy-load dedup une fois
        if self._existing_urls is None:
            try:
                self._existing_urls = airtable_client.get_existing_urls(
                    self.source_nom,
                )
                logger.info(
                    "[%s] %d URLs deja en Airtable (skip)",
                    self.source_nom, len(self._existing_urls),
                )
            except Exception as e:
                logger.warning(
                    "[%s] echec get_existing_urls : %s",
                    self.source_nom, e,
                )
                self._existing_urls = set()

        try:
            for page in range(1, MAX_PAGES + 1):
                self._respect_rate_limit()
                try:
                    data = self._fetcher_typed.get_json(
                        API_URL, params={"page": page},
                    )
                except Exception as e:
                    logger.warning(
                        "[%s] page=%d : echec fetch API : %s",
                        self.source_nom, page, e,
                    )
                    continue
                self._last_request_at = time.time()
                n_pages += 1

                inner = data.get("data") or {}
                items = inner.get("items") or []
                if total_in_api is None:
                    total_in_api = inner.get("count")
                    api_total_pages = inner.get("pages")
                    logger.info(
                        "[%s] API count=%s pages=%s page_size=%s",
                        self.source_nom, total_in_api, api_total_pages,
                        inner.get("page_size"),
                    )

                if not items:
                    logger.info(
                        "[%s] page=%d : 0 items, stop pagination",
                        self.source_nom, page,
                    )
                    break

                logger.info(
                    "[%s] page=%d/%s : %d items",
                    self.source_nom, page, api_total_pages or "?", len(items),
                )

                for auction in items:
                    aid = (auction.get("id") or "").strip()
                    if not aid:
                        n_skipped += 1
                        continue
                    url = f"{BASE_URL}/de/auction/{aid}"
                    if url in seen_urls:
                        continue
                    seen_urls.add(url)
                    if url in self._existing_urls:
                        n_skipped += 1
                        continue

                    raw = self._map_auction(auction)
                    if raw is None:
                        # status offline ou champs critiques manquants
                        n_skipped += 1
                        continue

                    try:
                        yield self.normalize(raw, url)
                        n_yielded += 1
                    except Exception as e:
                        logger.warning(
                            "[%s] %s : echec normalize : %s",
                            self.source_nom, url, e,
                        )
                        n_skipped += 1

                # Early stop si on a atteint la derniere page annoncee par l'API
                if api_total_pages and page >= api_total_pages:
                    logger.info(
                        "[%s] page=%d == api_total_pages, fin pagination",
                        self.source_nom, page,
                    )
                    break
        finally:
            logger.info(
                "[%s] run() recap : pages=%d | yielded=%d | skipped=%d | total_in_api=%s",
                self.source_nom, n_pages, n_yielded, n_skipped, total_in_api,
            )
