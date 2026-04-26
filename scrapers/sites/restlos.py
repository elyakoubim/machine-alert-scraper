"""
Scraper Restlos (DE) - v2
==========================

Strategie HTTP statique :
1. Liste les URLs depuis la page /auktionen?astatus=current
2. Filtre les URLs deja en Airtable (skip)
3. Pour les nouvelles : fetch via Playwright + parse meta og:

Restlos a un anti-bot strict (Cloudflare).
ScraperAPI Free ne supporte pas ce domaine (host_not_allowed).
On utilise donc Playwright (gratuit) qui simule un vrai navigateur
et passe l'anti-bot.

1 ligne Airtable = 1 vente entiere (auction)
   -> Voir ses lots individuels sur le site externe
"""

from __future__ import annotations

import logging
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Iterator, Optional

import httpx

from scrapers.base import BaseScraper, Annonce
import airtable as airtable_client

logger = logging.getLogger(__name__)


BASE_URL = "https://auktionen.restlos.com"
LIST_URL = f"{BASE_URL}/auktionen?astatus=current"

# Pattern URL des auctions: /auktionen/-/{id}/{slug}/lose
AUCTION_URL_PATTERN = re.compile(
    r'href="(/auktionen/-/(\d+)/[^"/]+/lose)"',
    re.IGNORECASE
)


class RestlosScraper(BaseScraper):
    source_nom = "Restlos"
    source_pays = "DE"
    base_url = BASE_URL
    requires_javascript = True  # Restlos a un anti-bot, Playwright passe ou
    rate_limit_seconds = 1.5
    default_category = "Autre / non classe"

    def __init__(self, fetcher=None, llm_extractor=None):
        # Restlos a un anti-bot strict (Cloudflare).
        # Playwright (gratuit) simule un vrai navigateur et passe l'anti-bot.
        # ScraperAPI Free ne supporte pas ce domaine (host_not_allowed).
        super().__init__(fetcher=fetcher, llm_extractor=llm_extractor)
        self._existing_urls = None

    def list_listing_urls(self) -> Iterator[str]:
        """Une seule URL de listing : la page des auctions actives."""
        yield LIST_URL

    def parse_listing(self, html: str, listing_url: str) -> Iterator[str]:
        """Parse la liste des auctions et yield les URLs nouvelles."""
        matches = AUCTION_URL_PATTERN.findall(html)
        # Dedup par auction_id
        unique_paths = list({m[1]: m[0] for m in matches}.values())
        logger.info(
            "[%s] page listing : %d auctions trouvees",
            self.source_nom, len(unique_paths),
        )

        if not unique_paths:
            return

        # Charger les URLs deja en Airtable
        if self._existing_urls is None:
            try:
                self._existing_urls = airtable_client.get_existing_urls(
                    self.source_nom
                )
                logger.info(
                    "[%s] %d URLs deja en Airtable, skip celles-la",
                    self.source_nom, len(self._existing_urls),
                )
            except Exception as e:
                logger.warning(
                    "[%s] Impossible de charger URLs existantes : %s",
                    self.source_nom, e,
                )
                self._existing_urls = set()

        for path in unique_paths:
            full_url = f"{BASE_URL}{path}"
            if full_url in self._existing_urls:
                continue
            yield full_url

    def parse_detail(self, html: str, url: str) -> dict:
        """
        Parse une page de detail Restlos.
        Extrait : titre, image, description depuis les meta og:
        + date_fin depuis le HTML.
        """
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html, "lxml")

        # Titre depuis og:title (rapide et fiable)
        og_title = soup.find("meta", property="og:title")
        titre = (og_title.get("content", "") if og_title else "").strip()

        # Fallback : <title>
        if not titre:
            title_tag = soup.find("title")
            if title_tag:
                titre = self.clean_text(title_tag.get_text())
                # Nettoyer le suffixe "| RESTLOS"
                titre = re.sub(r"\s*\|\s*RESTLOS\s*$", "", titre)

        # Image depuis og:image
        og_image = soup.find("meta", property="og:image")
        image_url = (og_image.get("content", "") if og_image else "").strip()

        # Description depuis og:description (peut etre court)
        og_desc = soup.find("meta", property="og:description")
        description = (og_desc.get("content", "") if og_desc else "").strip()

        # Enrichir la description avec un peu du body
        if soup.body:
            body_text = self.clean_text(soup.body.get_text())[:1500]
            if description and description != "Sehen Sie alle Lose dieser Auktion":
                description = description + " | " + body_text[:800]
            else:
                description = body_text[:1500]

        # Type de vente : Insolvenz si dans le titre, sinon Auktion
        type_vente = (
            "Insolvenz"
            if "insolvenz" in titre.lower() or "insolvenz" in description.lower()
            else "Auktion"
        )

        # Date_fin : parsing patterns relatifs ("Endet Morgen", "Endet in X Tagen")
        date_fin = self._parse_date_fin(html)

        # Categorie native : on essaie de la deduire du titre/description
        categorie_native = self._guess_category(titre + " " + description)

        return {
            "titre": titre,
            "description": description,
            "image_url": image_url,
            "prix": "",  # Restlos n'expose pas de prix au niveau auction
            "type_vente": type_vente,
            "date_fin_brut": date_fin,
            "date_publication_brut": datetime.now(timezone.utc).isoformat(),
            "categorie_native": categorie_native,
        }

    def map_category(self, native_category: Optional[str]) -> str:
        """
        Mapping basique des keywords allemands vers categories Faillink.
        Si rien ne match, retourne default → LLM Haiku decidera.
        """
        if not native_category:
            return self.default_category

        cat = native_category.lower()

        # Mapping keywords DE -> Faillink
        if any(kw in cat for kw in [
            "fahrzeug", "auto", "lkw", "pkw", "kfz", "motorrad",
            "transporter", "stapler", "gabelstapler"
        ]):
            return "Vehicules"

        if any(kw in cat for kw in [
            "immobil", "grundstuck", "haus", "wohnung", "buro", "halle"
        ]):
            return "Immobilier"

        if any(kw in cat for kw in [
            "computer", "edv", "it ", " it,", "server", "drucker",
            "laptop", "monitor", "elektronik"
        ]):
            return "Materiel informatique"

        if any(kw in cat for kw in [
            "mobel", "moebel", "stuhl", "tisch", "schrank",
            "einrichtung", "buromobel"
        ]):
            return "Mobilier"

        if any(kw in cat for kw in [
            "maschine", "werkzeug", "metall", "industrie", "produktion",
            "fertigung", "fraese", "drehmaschine"
        ]):
            return "Machines industrielles"

        if any(kw in cat for kw in [
            "bau", "werkstatt", "handwerker", "sanitaer", "sanitar"
        ]):
            return "Outillage & equipement chantier"

        if any(kw in cat for kw in [
            "lager", "warenbestand", "stocks", "restposten",
            "verpackung", "gastro"
        ]):
            return "Stocks & liquidations"

        return self.default_category

    def _parse_date_fin(self, html: str) -> Optional[str]:
        """
        Parse les patterns date_fin depuis le HTML allemand de Restlos.
        Patterns possibles :
            - "Endet heute"          -> aujourd'hui
            - "Endet Morgen"         -> demain
            - "Endet in X Tagen"     -> +X jours
            - "Mo. 28.04."           -> date courte allemande
            - "DD.MM.YYYY"           -> date longue
        """
        today = datetime.now(timezone.utc).date()

        # Cherche d'abord une date complete DD.MM.YYYY
        full_date = re.search(r"(\d{2})\.(\d{2})\.(\d{4})", html)
        if full_date:
            try:
                day, month, year = (
                    int(full_date.group(1)),
                    int(full_date.group(2)),
                    int(full_date.group(3)),
                )
                return datetime(year, month, day).date().isoformat()
            except ValueError:
                pass

        # Cherche les patterns relatifs
        if re.search(r"Endet\s+heute", html, re.I):
            return today.isoformat()

        if re.search(r"Endet\s+Morgen", html, re.I):
            return (today + timedelta(days=1)).isoformat()

        in_days = re.search(r"Endet\s+in\s+(\d+)\s+Tagen", html, re.I)
        if in_days:
            try:
                days = int(in_days.group(1))
                return (today + timedelta(days=days)).isoformat()
            except ValueError:
                pass

        return None

    def _guess_category(self, text: str) -> str:
        """Devine la categorie native a partir du texte de l'auction."""
        if not text:
            return ""
        return text.lower()[:500]