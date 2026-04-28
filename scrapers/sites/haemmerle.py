"""
Scraper HÄMMERLE (DE) - v1
===========================

Cabinet allemand traditionnel (30+ ans) specialise en Industrie- et
Insolvenzversteigerungen. Bureaux dans toute l'Allemagne (Eching/Bavière,
Berlin, Baden-Württemberg, Rhein-Pfalz, Niedersachsen, NRW, Ostdeutschland).

Plateforme : Bidpath (comme AssetOrb).
Specialise dans les FAILLITES (Insolvenzversteigerungen) - PARFAIT pour Faillink !
References notables : Grundig, Bench, Junghans, Meteor Gummiwerke, ENKA...

Strategie : HTTP statique + httpx (HTML classique, pas de JS).
1. Fetch /de/Auktionen/Aktuelle -> liste les auctions actives
   Pattern auction: /de/objekte/au-{auction_id}/{slug}?lstatus=0
2. Pour chaque auction, paginer ?pagesize=96&pagenumber=N
   Le HTML contient TOUS les lots de la page directement
3. Pour chaque lot, fetch + parse:
   Pattern lot: /de/l/{lot_id}/{slug}?aid={auction_id}&aname={slug}&...
   - Titre (h1)
   - Image (CDN haemmerle.de/Cms/Files/...)
   - Description
   - Prix (Mindestpreis)
   - Localisation (toujours DE)

1 ligne Airtable = 1 lot individuel

Volume estimé : ~3-5 auctions × ~50-200 lots = ~300-500 lots actifs

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


BASE_URL = "https://www.haemmerle.de"
LISTING_URL = f"{BASE_URL}/de/Auktionen/Aktuelle"

# Pattern pour URLs auction
# /de/objekte/au-3530/lageraufloesung_stapler_verpackungsmaschinen_foerdertechnik_lagerregale_bueroaus
AUCTION_URL_PATTERN = re.compile(
    r'(/de/objekte/au-\d+/[^"\'<>\s?#]+)',
    re.IGNORECASE,
)

# Pattern pour URLs lot
# /de/l/129063/yale_ms12_elektro-hubameise?aid=3530&aname=...
LOT_URL_PATTERN = re.compile(
    r'(/de/l/\d+/[^"\'<>\s?]+)',
    re.IGNORECASE,
)

# Pattern pour images CDN HÄMMERLE
# https://www.haemmerle.de/Cms/Files/UUID
# https://www.haemmerle.de/Content/images/...
IMAGE_PATTERN = re.compile(
    r'(https?://www\.haemmerle\.de/(?:Cms/Files/[a-f0-9-]+|Content/[^"\'<>\s]+\.(?:jpg|jpeg|png|webp)))',
    re.IGNORECASE,
)

# Pattern pour prix - "Mindestpreis 800 EUR" ou "1.200 EUR" ou "800 EUR"
PRIX_PATTERNS = [
    re.compile(
        r"Mindestpreis\s*([\d.,]+)\s*EUR",
        re.IGNORECASE,
    ),
    # Format simple: "800 EUR" ou "1.200 EUR"
    re.compile(r"\b([\d.,]+)\s*EUR\b", re.IGNORECASE),
]

# Pattern pour code postal allemand "97215 Uffenheim" ou "Raum München"
LOCATION_PATTERN = re.compile(
    r'(?:D[-–]?\s*)?(\d{5})\s+([A-Z][a-zA-ZÀ-ÿ\s\-\.\']+?)(?:[,\.]|$)',
    re.IGNORECASE,
)

# Limites de sécurité
MAX_PAGES_PER_AUCTION = 30   # Pagination interne (96 lots/page = ~3000 max par auction)
MAX_AUCTIONS = 50            # Nb max auctions
PAGE_SIZE = 96               # Demande 96 lots/page (max possible)


class HaemmerleScraper(BaseScraper):
    source_nom = "Hämmerle"
    source_pays = "DE"
    base_url = BASE_URL
    requires_javascript = False  # HTML statique
    rate_limit_seconds = 1.0
    default_category = "Autre / non classe"

    def __init__(self, fetcher=None, llm_extractor=None):
        super().__init__(fetcher=fetcher, llm_extractor=llm_extractor)
        self._existing_urls = None
        # Cache: URLs des auctions decouvertes
        self._auction_urls: Optional[list] = None

    def list_listing_urls(self) -> Iterator[str]:
        """Genere les URLs des pages liste.

        Strategie en 2 phases :
        1. D'abord la page /de/Auktionen/Aktuelle (decouvre les auctions actives)
        2. Puis pour chaque auction, ses pages paginees
        """
        # Phase 1 : page d'index principale
        yield LISTING_URL

        # Phase 2 : pour chaque auction decouverte, ses pages de lots
        if not self._auction_urls:
            logger.warning(
                "[%s] Aucune auction decouverte sur l'index",
                self.source_nom,
            )
            return

        for auction_url in self._auction_urls[:MAX_AUCTIONS]:
            # Pagination par 96 : pagenumber=1, 2, 3...
            for page_num in range(1, MAX_PAGES_PER_AUCTION + 1):
                separator = "&" if "?" in auction_url else "?"
                url = (
                    f"{auction_url}{separator}"
                    f"pagesize={PAGE_SIZE}&pagenumber={page_num}"
                )
                yield url

    def parse_listing(
        self, html: str, listing_url: str
    ) -> Iterator[str]:
        """Parse une page liste et yield les URLs des lots.

        Si on est sur /de/Auktionen/Aktuelle, on extrait les auctions.
        Si on est sur /de/objekte/au-..., on extrait les URLs des lots.
        """
        # Detection du type de page
        is_index = "/Auktionen/" in listing_url
        is_auction_page = "/de/objekte/au-" in listing_url

        if is_index:
            # Extraire les URLs des auctions depuis la page d'index
            matches = AUCTION_URL_PATTERN.findall(html)
            unique_paths = list(dict.fromkeys(matches))

            # Construire les URLs complètes avec ?lstatus=0 (actif)
            full_urls = []
            for path in unique_paths:
                # Ajouter ?lstatus=0 si pas déjà present
                if "?" not in path:
                    full_url = BASE_URL + path + "?lstatus=0"
                else:
                    full_url = BASE_URL + path
                if full_url not in full_urls:
                    full_urls.append(full_url)

            self._auction_urls = full_urls
            logger.info(
                "[%s] Index : %d auctions decouvertes",
                self.source_nom, len(full_urls),
            )
            # La page d'index ne contient pas de lots directement
            return

        if is_auction_page:
            # On est sur la page d'une auction
            # Extraire les URLs des lots
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
        (via BaseScraper.normalize) faire le travail apres avoir vu
        le titre + description complets.
        """
        soup = BeautifulSoup(html, "html.parser")

        # === Titre : h1 ou h4 (selon la page) ===
        titre = ""
        # HÄMMERLE utilise souvent h4 pour les titres de lots
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
                # Nettoyer "Titre | HÄMMERLE"
                if " | " in titre:
                    titre = titre.split(" | ")[0]

        # === Image : depuis le CDN HÄMMERLE ===
        image_url = ""
        all_images = IMAGE_PATTERN.findall(html)
        # Filtrer les images de logo / loading
        for img in all_images:
            img_lower = img.lower()
            if ("loading" not in img_lower
                and "logo" not in img_lower
                and "default" not in img_lower):
                image_url = img
                break

        # === Description : meta description + body text ===
        description = ""
        meta_desc = soup.find("meta", attrs={"name": "description"})
        if meta_desc:
            description = (meta_desc.get("content", "") or "").strip()

        # Enrichir avec body si description courte
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
                    # Format allemand classique avec virgule decimale
                    amount = amount_raw.replace(".", "").replace(",", ".")
                elif "," in amount_raw:
                    parts = amount_raw.split(",")
                    if len(parts[-1]) == 2:
                        # Decimal: "1200,50"
                        amount = amount_raw.replace(",", ".")
                    else:
                        amount = amount_raw.replace(",", "")
                else:
                    # Pas de virgule -> "." est forcément séparateur de milliers
                    amount = amount_raw.replace(".", "")
                # Validation
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
            type_vente = "Insolvenzversteigerung"
        elif "betriebsauflösung" in text_lower or "betriebsaufloesung" in text_lower:
            type_vente = "Betriebsauflösung"
        elif "lagerauflösung" in text_lower or "lageraufloesung" in text_lower:
            type_vente = "Lagerauflösung"
        elif "werksschließung" in text_lower or "werksschliessung" in text_lower:
            type_vente = "Werksschliessung"

        # === source_id : extrait de l'URL (ex: lot=129063 + aid=3530) ===
        source_id = ""
        # Pattern URL: /de/l/{lot_id}/...?aid={auction_id}
        lot_id_match = re.search(r'/de/l/(\d+)/', url)
        aid_match = re.search(r'aid=(\d+)', url)
        if lot_id_match and aid_match:
            source_id = f"{aid_match.group(1)}-{lot_id_match.group(1)}"
        elif lot_id_match:
            source_id = lot_id_match.group(1)

        # === categorie_native : laissé vide pour Haiku ===
        categorie_native = None

        return {
            "titre": titre,
            "description": description,
            "prix": prix,
            "image_url": image_url,
            "type_vente": type_vente,
            "categorie_native": categorie_native,
            "source_id": source_id,
            # pays par défaut = DE (source_pays de la classe)
        }

    def map_category(self, native_category: Optional[str]) -> str:
        """Pas de mapping local : on retourne toujours 'Autre / non classe'.

        Le BaseScraper.normalize() detectera 'Autre' et appellera
        automatiquement Claude Haiku 4.5 (LLMExtractor) pour categoriser
        proprement le lot dans une des 8 categories Faillink officielles.
        """
        return self.default_category

    @staticmethod
    def clean_text(text: str) -> str:
        """Normalise les espaces / retours a la ligne."""
        if not text:
            return ""
        return re.sub(r"\s+", " ", text).strip()