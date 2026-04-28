"""
Scraper Rockmann Industrieauktionen (DE) - v1
==============================================

Cabinet allemand specialise en Industrie- et Insolvenzauktionen.
Plateforme : Bidpath (meme que HÄMMERLE et AssetOrb).

Site presentation : rockmann-industrieauktionen.de (WordPress)
Plateforme auctions : auktionen.rockmann-industrieauktionen.de (Bidpath)

Specialise dans les FAILLITES (Insolvenzauktionen) - PARFAIT pour Faillink !
References notables : Abstrakt UG, Ackermann CNC Technik...

Strategie : HTTP statique + httpx (HTML classique, pas de JS).
1. Fetch /de/Auktionen/Alle -> liste les auctions actives
   Pattern auction: /de/{auction_id}_{slug}/a/{auction_id}
   OU         /de/objekte?aid={auction_id}&Lstatus=1 (catalog direct)
2. Pour chaque auction, paginer ?pagesize=96&pagenumber=N
3. Pour chaque lot, fetch + parse:
   Pattern lot: /de/l/{lot_id}/{slug}?aid={auction_id}&...
   - Titre (h1/h4)
   - Image
   - Description
   - Prix (Mindestpreis)

1 ligne Airtable = 1 lot individuel

Volume estimé : ~3-5 auctions × ~50-300 lots = ~300-500 lots actifs

CATEGORISATION :
On retourne toujours "Autre / non classe" comme map_category().
Le BaseScraper.normalize() detecte "Autre" et appelle automatiquement
Claude Haiku 4.5 (LLMExtractor) pour categoriser proprement le lot
dans une des 8 categories Faillink officielles.
"""

from __future__ import annotations

import logging
import re
from typing import Iterator, Optional

from bs4 import BeautifulSoup

from scrapers.base import BaseScraper, Annonce
import airtable as airtable_client

logger = logging.getLogger(__name__)


# La VRAIE plateforme auction (Bidpath) est sur le sous-domaine
BASE_URL = "https://auktionen.rockmann-industrieauktionen.de"
LISTING_URL = f"{BASE_URL}/de/Auktionen/Alle"

# Pattern pour URLs auction (catalog direct via aid)
# /de/objekte?aid=1656&Lstatus=1
# /de/00677_finalauktion_-_ackermann_cnc_technik_gmbh.../a/1656
AUCTION_CATALOG_PATTERN = re.compile(
    r'/de/objekte\?aid=(\d+)(?:&Lstatus=\d+)?',
    re.IGNORECASE,
)
# Pattern alternatif: /de/{nom}/a/{auction_id}
AUCTION_INFO_PATTERN = re.compile(
    r'/de/[^"\'<>\s?]+/a/(\d+)',
    re.IGNORECASE,
)

# Pattern pour URLs lot
# /de/l/{lot_id}/{slug}?aid={auction_id}&...
LOT_URL_PATTERN = re.compile(
    r'(/de/l/\d+/[^"\'<>\s?]+)',
    re.IGNORECASE,
)

# Pattern pour images CDN Rockmann
# https://auktionen.rockmann-industrieauktionen.de/Cms/Files/UUID
# https://rockmann-industrieauktionen.de/wp-content/uploads/.../image.jpeg
IMAGE_PATTERN = re.compile(
    r'(https?://(?:auktionen\.)?rockmann-industrieauktionen\.de/'
    r'(?:Cms/Files/[a-f0-9-]+|wp-content/uploads/[^"\'<>\s]+\.(?:jpg|jpeg|png|webp)))',
    re.IGNORECASE,
)

# Pattern pour prix - "Mindestpreis 800 EUR" ou "1.200 EUR" ou "3 EUR"
PRIX_PATTERNS = [
    re.compile(
        r"Mindestpreis\s*([\d.,]+)\s*EUR",
        re.IGNORECASE,
    ),
    # Format simple: "800 EUR" ou "1.200 EUR"
    re.compile(r"\b([\d.,]+)\s*EUR\b", re.IGNORECASE),
]

# Limites de sécurité
MAX_PAGES_PER_AUCTION = 30   # Pagination interne (96 lots/page = ~3000 max)
MAX_AUCTIONS = 50
PAGE_SIZE = 96


class RockmannScraper(BaseScraper):
    source_nom = "Rockmann Industrieauktionen"
    source_pays = "DE"
    base_url = BASE_URL
    requires_javascript = False  # HTML statique
    rate_limit_seconds = 1.0
    default_category = "Autre / non classe"

    def __init__(self, fetcher=None, llm_extractor=None):
        super().__init__(fetcher=fetcher, llm_extractor=llm_extractor)
        self._existing_urls = None
        # Cache: auction IDs decouverts
        self._auction_ids: Optional[list] = None

    def list_listing_urls(self) -> Iterator[str]:
        """Genere les URLs des pages liste.

        Strategie en 2 phases :
        1. D'abord la page /de/Auktionen/Alle (decouvre les auction_ids)
        2. Puis pour chaque auction, ses pages paginees via /de/objekte?aid=X
        """
        # Phase 1 : page d'index principale
        yield LISTING_URL

        # Phase 2 : pour chaque auction decouverte, ses pages de lots
        if not self._auction_ids:
            logger.warning(
                "[%s] Aucune auction decouverte sur l'index",
                self.source_nom,
            )
            return

        for auction_id in self._auction_ids[:MAX_AUCTIONS]:
            # Pagination par 96 : pagenumber=1, 2, 3...
            # Lstatus=1 = en cours, Lstatus=3 = nachverkauf (post-vente)
            for lstatus in [1, 3]:  # On essaie les 2 statuts
                for page_num in range(1, MAX_PAGES_PER_AUCTION + 1):
                    url = (
                        f"{BASE_URL}/de/objekte?aid={auction_id}"
                        f"&Lstatus={lstatus}"
                        f"&pagesize={PAGE_SIZE}&pagenumber={page_num}"
                    )
                    yield url

    def parse_listing(
        self, html: str, listing_url: str
    ) -> Iterator[str]:
        """Parse une page liste et yield les URLs des lots."""

        # Detection : page index ou page catalog ?
        is_index = "/Auktionen/" in listing_url
        is_catalog_page = "/de/objekte" in listing_url

        if is_index:
            # Extraire les auction_ids depuis les liens "Katalog" et "Informations"
            # Pattern 1: /de/objekte?aid=1656
            ids_from_catalog = AUCTION_CATALOG_PATTERN.findall(html)
            # Pattern 2: /de/{slug}/a/1656
            ids_from_info = AUCTION_INFO_PATTERN.findall(html)

            all_ids = list(dict.fromkeys(ids_from_catalog + ids_from_info))
            self._auction_ids = all_ids

            logger.info(
                "[%s] Index : %d auction_ids decouverts (%s)",
                self.source_nom, len(all_ids), all_ids[:10],
            )
            # La page d'index ne contient pas de lots directement
            return

        if is_catalog_page:
            # On est sur la page catalog d'une auction
            lot_matches = LOT_URL_PATTERN.findall(html)
            unique_paths = list(dict.fromkeys(lot_matches))

            if not unique_paths:
                logger.debug(
                    "[%s] aucun lot sur %s",
                    self.source_nom, listing_url,
                )
                return

            logger.info(
                "[%s] %s : %d lots trouves",
                self.source_nom, listing_url, len(unique_paths),
            )

            # Charger la liste des URLs deja en Airtable (1 seule fois)
            if self._existing_urls is None:
                try:
                    self._existing_urls = airtable_client.get_existing_urls(
                        self.source_nom
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

            # Yield seulement les nouvelles URLs
            for path in unique_paths:
                full_url = BASE_URL + path
                if full_url not in self._existing_urls:
                    yield full_url

    def parse_detail(self, html: str, url: str) -> dict:
        """Parse une page lot et retourne un dict avec les champs raw.

        Note: on ne categorise PAS ici. On laisse Claude Haiku 4.5
        (via BaseScraper.normalize) faire le travail.
        """
        soup = BeautifulSoup(html, "html.parser")

        # === Titre : h1, h4, h2, h3 ===
        titre = ""
        for tag_name in ["h1", "h4", "h2", "h3"]:
            tag = soup.find(tag_name)
            if tag:
                txt = self.clean_text(tag.get_text())
                if txt and len(txt) > 5:
                    titre = txt
                    break

        # Fallback : title HTML
        if not titre:
            title_tag = soup.find("title")
            if title_tag:
                titre = self.clean_text(title_tag.get_text())
                if " | " in titre:
                    titre = titre.split(" | ")[0]

        # === Image : depuis le CDN Rockmann ===
        image_url = ""
        all_images = IMAGE_PATTERN.findall(html)
        for img in all_images:
            img_lower = img.lower()
            if ("loading" not in img_lower
                and "logo" not in img_lower
                and "default" not in img_lower
                and "gruppe" not in img_lower
                and "siegel" not in img_lower):
                image_url = img
                break

        # === Description : meta description + body text ===
        description = ""
        meta_desc = soup.find("meta", attrs={"name": "description"})
        if meta_desc:
            description = (meta_desc.get("content", "") or "").strip()

        if len(description) < 100 and soup.body:
            for tag in soup.body.find_all(
                ["script", "style", "nav", "footer", "header"]
            ):
                tag.decompose()
            body_text = self.clean_text(soup.body.get_text())[:1500]
            if body_text:
                description = (description + " " + body_text).strip()[:2000]

        # === Prix : essayer plusieurs patterns ===
        prix = ""
        text_for_prix = soup.get_text() if soup else html
        for pattern in PRIX_PATTERNS:
            prix_match = pattern.search(text_for_prix)
            if prix_match:
                amount_raw = prix_match.group(1)
                # Format allemand: "1.200" (milliers) ou "1.200,50" (decimal)
                if "," in amount_raw and "." in amount_raw:
                    amount = amount_raw.replace(".", "").replace(",", ".")
                elif "," in amount_raw:
                    parts = amount_raw.split(",")
                    if len(parts[-1]) == 2:
                        amount = amount_raw.replace(",", ".")
                    else:
                        amount = amount_raw.replace(",", "")
                else:
                    amount = amount_raw.replace(".", "")
                try:
                    val = float(amount)
                    if 0 < val < 10_000_000:
                        prix = f"EUR {amount_raw}"
                        break
                except ValueError:
                    continue

        # === Type de vente : detection auto ===
        type_vente = "Industrieauktion"
        text_lower = html.lower()
        if "insolvenz" in text_lower:
            type_vente = "Insolvenzauktion"
        elif "nachverkauf" in text_lower:
            type_vente = "Nachverkauf"
        elif "finalauktion" in text_lower:
            type_vente = "Finalauktion"
        elif "sammelauktion" in text_lower or "sammelversteigerung" in text_lower:
            type_vente = "Sammelauktion"
        elif "betriebsauflösung" in text_lower or "betriebsaufloesung" in text_lower:
            type_vente = "Betriebsauflösung"

        # === source_id : extrait de l'URL ===
        source_id = ""
        lot_id_match = re.search(r'/de/l/(\d+)/', url)
        aid_match = re.search(r'aid=(\d+)', url)
        if lot_id_match and aid_match:
            source_id = f"{aid_match.group(1)}-{lot_id_match.group(1)}"
        elif lot_id_match:
            source_id = lot_id_match.group(1)

        return {
            "titre": titre,
            "description": description,
            "prix": prix,
            "image_url": image_url,
            "type_vente": type_vente,
            "categorie_native": None,
            "source_id": source_id,
            # pays par défaut = DE (source_pays de la classe)
        }

    def map_category(self, native_category: Optional[str]) -> str:
        """Pas de mapping local : on retourne toujours 'Autre / non classe'.

        Le BaseScraper.normalize() detectera 'Autre' et appellera
        automatiquement Claude Haiku 4.5 pour categoriser.
        """
        return self.default_category

    @staticmethod
    def clean_text(text: str) -> str:
        """Normalise les espaces / retours a la ligne."""
        if not text:
            return ""
        return re.sub(r"\s+", " ", text).strip()