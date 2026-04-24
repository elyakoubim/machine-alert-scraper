"""
Scraper Auctelia (BE) - v2
============================

Strategie hybride :
1. Liste les URLs depuis le sitemap upcoming-items-fr.xml (httpx, rapide)
2. Filtre les URLs deja en Airtable (skip)
3. Pour les nouvelles uniquement : Playwright pour avoir titre/image/prix/description

Auctelia est un site Nuxt.js client-side : impossible de scraper en HTTP brut.
Le sitemap est notre seul moyen de lister TOUTES les annonces.
"""

from __future__ import annotations

import logging
import re
import time
from datetime import datetime, timezone
from typing import Iterator, Optional

import httpx

from scrapers.base import BaseScraper, Annonce
import airtable as airtable_client

logger = logging.getLogger(__name__)


SITEMAP_URL = "https://www.auctelia.com/sitemaps/upcoming-items-fr.xml"


class AucteliaScraper(BaseScraper):
    source_nom = "Auctelia"
    source_pays = "BE"
    base_url = "https://www.auctelia.com"
    requires_javascript = True  # Sites pages de detail en client-side rendering
    rate_limit_seconds = 1.5
    default_category = "Autre / non classe"

    def __init__(self, fetcher=None, llm_extractor=None):
        super().__init__(fetcher=fetcher, llm_extractor=llm_extractor)
        self._existing_urls = None  # cache des URLs deja en Airtable

    def list_listing_urls(self) -> Iterator[str]:
        """
        On ne fait pas de listing classique :
        on retourne juste l'URL du sitemap pour declencher run().
        """
        yield SITEMAP_URL

    def parse_listing(self, html: str, listing_url: str) -> Iterator[str]:
        """
        Parse le sitemap XML et yield les URLs d'annonces NON DEJA presentes en Airtable.
        Le tri est fait par lastmod desc (les plus recentes en premier).
        """
        # Parse XML simple (sans BeautifulSoup pour ce cas, regex suffit pour sitemap)
        url_pattern = re.compile(
            r"<url>\s*<loc>([^<]+)</loc>\s*<lastmod>([^<]+)</lastmod>",
            re.MULTILINE
        )
        matches = url_pattern.findall(html)
        logger.info("[%s] sitemap : %d URLs trouvees", self.source_nom, len(matches))

        if not matches:
            return

        # Charger les URLs existantes en Airtable (une seule fois)
        if self._existing_urls is None:
            try:
                self._existing_urls = airtable_client.get_existing_urls(self.source_nom)
                logger.info(
                    "[%s] %d URLs deja en Airtable, on skip celles-la",
                    self.source_nom, len(self._existing_urls),
                )
            except Exception as e:
                logger.warning(
                    "[%s] Impossible de charger URLs existantes : %s. On scrape tout.",
                    self.source_nom, e,
                )
                self._existing_urls = set()

        # Filtrer + trier par lastmod desc (plus recentes en premier)
        nouvelles = [
            (url, lastmod) for url, lastmod in matches
            if url not in self._existing_urls
        ]
        nouvelles.sort(key=lambda x: x[1], reverse=True)

        logger.info(
            "[%s] %d nouvelles annonces a scraper (sur %d total)",
            self.source_nom, len(nouvelles), len(matches),
        )

        for url, lastmod in nouvelles:
            # On stocke lastmod dans un dict global temporaire pour le passer a parse_detail
            self._lastmods = getattr(self, "_lastmods", {})
            self._lastmods[url] = lastmod
            yield url

    def fetch(self, url: str) -> str:
        """
        Override fetch :
        - Si c'est le sitemap : httpx (XML rapide)
        - Sinon : Playwright (page de detail JS)
        """
        if url == SITEMAP_URL or url.endswith(".xml"):
            self._respect_rate_limit()
            try:
                resp = httpx.get(url, timeout=15.0, follow_redirects=True)
                resp.raise_for_status()
                self._last_request_at = time.time()
                return resp.text
            except Exception as e:
                logger.error("[%s] Erreur fetch sitemap : %s", self.source_nom, e)
                raise
        # Sinon : utilise le fetcher Playwright via BaseScraper
        return super().fetch(url)

    def parse_detail(self, html: str, url: str) -> dict:
        """
        Parse une page de detail Auctelia.
        Extrait : titre (h1), image (og:image), description, prix, date_fin.
        """
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html, "lxml")

        # Titre depuis h1
        h1 = soup.find("h1")
        titre = self.clean_text(h1.get_text() if h1 else "")

        # Si pas de h1, fallback sur le slug de l'URL
        if not titre:
            slug_match = re.search(r"/materiel-occasion/([^/]+)/", url)
            if slug_match:
                titre = slug_match.group(1).replace("-", " ").capitalize()

        # Image depuis og:image
        og_image = soup.find("meta", property="og:image")
        image_url = og_image.get("content", "") if og_image else ""

        # Description : tout le texte sous "Informations complementaires" ou le texte principal
        description = ""
        info_section = soup.find(string=re.compile(r"Informations compl", re.IGNORECASE))
        if info_section and info_section.parent:
            # Prendre le parent et extraire son texte
            section = info_section.find_parent()
            if section:
                description = self.clean_text(section.get_text())[:2000]

        # Si pas de description riche, prendre le texte du body
        if not description and soup.body:
            description = self.clean_text(soup.body.get_text())[:1500]

        # Prix : chercher le pattern "Enchere actuelle ... XX EUR"
        prix = ""
        body_text = soup.body.get_text() if soup.body else ""
        prix_match = re.search(
            r"(?:Enchere actuelle|Prix(?: de depart)?)[\s:]*(\d+(?:[\.,]\d+)?)\s*(?:EUR|EUR)",
            body_text, re.IGNORECASE
        )
        if prix_match:
            prix = prix_match.group(1).replace(",", ".") + " EUR"
        else:
            # Fallback : chercher juste un montant en EUR
            prix_simple = re.search(r"(\d+(?:[\.,]\d{1,2})?)\s*(?:EUR|EUR)", body_text)
            if prix_simple:
                prix = prix_simple.group(1).replace(",", ".") + " EUR"

        # Date fin : "Fin de vente : DD/MM/YYYY a HH:MM"
        date_fin = None
        date_match = re.search(
            r"Fin de vente[\s:]*(\d{1,2}[/-]\d{1,2}[/-]\d{2,4})",
            body_text, re.IGNORECASE
        )
        if date_match:
            date_fin = date_match.group(1)

        # Lastmod du sitemap comme date_publication
        date_pub = getattr(self, "_lastmods", {}).get(url)

        return {
            "titre": titre,
            "description": description,
            "image_url": image_url,
            "prix": prix,
            "type_vente": "Enchere",
            "date_fin_brut": date_fin,
            "date_publication_brut": date_pub,
            "categorie_native": "",  # Auctelia ne fournit pas de categorie native exploitable
        }

    def map_category(self, native_category: Optional[str]) -> str:
        """
        Pas de mapping de categorie native pour Auctelia :
        on laisse Claude Haiku decider via le LLM extractor du BaseScraper.
        """
        return self.default_category