"""
Scraper AssetOrb (DE) - v1
===========================

Cabinet allemand specialise en Industrieauktionen et Insolvenzversteigerungen.
Plateforme : Bidpath (white-label).
Concentre sur l'Allemagne (Berlin, Falkensee, Rangsdorf, Lübeck...).

PARFAIT pour Faillink : la majorite des lots viennent de faillites ("Insolvenz").

Strategie : HTTP statique + httpx (HTML classique, pas de JS).
1. Fetch /upcoming-auctions -> liste les auctions actives
   Pattern: /auction/details/{auction_id}-{slug}?au={au_internal_id}
2. Pour chaque auction, fetch la page auction
   Les lots sont visibles directement dans la page auction
   Pattern lot: /auction/lot/{numero}-{slug}/?lot={lot_id}&au={auction_id}
3. Pour chaque lot, fetch + parse:
   - Titre
   - Image (CDN storagegoassetorb.bidpath.cloud)
   - Description
   - Prix (Hochstgebot ou Mindestpreis)
   - Localisation (toujours DE, codes postaux Berlin/Brandenburg)

1 ligne Airtable = 1 lot individuel

Volume estimé : ~13 auctions actives × ~50-500 lots = ~1500-2000 lots
(Note: Mounting Systems insolvency = 1577 lots à elle seule)

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


BASE_URL = "https://www.assetorb.com"
LISTING_URL = f"{BASE_URL}/upcoming-auctions"

# Pattern pour URLs auction
# /auction/details/60575-werk-2-montageanlagen?au=715
AUCTION_URL_PATTERN = re.compile(
    r'(/auction/details/[^"\'<>\s?]+\?au=\d+)',
    re.IGNORECASE,
)

# Pattern pour URLs lot
# Format reel observe (diag 12 mai 20h40) :
# /auction/lot/{slug}/?lot={id}&so=0&st=&sto=0&au={au_id}&ef=&et=&ic=False&sd=0&pp=96&pn=1&g=1
# On capture jusqu'a `&au=Y` inclus (= contexte auction probablement requis pour
# que la page de detail renvoie du contenu utile). Tolere `&amp;` HTML-escape
# (qu'on convertira en `&` avant yield).
LOT_URL_PATTERN = re.compile(
    r'(/auction/lot/[^"\'<>\s?]+/\?lot=\d+(?:&(?:amp;)?[^"&]*)*&(?:amp;)?au=\d+)',
    re.IGNORECASE,
)

# Pattern pour images CDN AssetOrb (Bidpath)
# https://storagegoassetorb.bidpath.cloud/stock/26363-12.jpg
# https://storagegoassetorb.bidpath.cloud/main/...
# https://storagegoassetorb.blob.core.windows.net/...
IMAGE_PATTERN = re.compile(
    r'(https?://storagegoassetorb\.(?:bidpath\.cloud|blob\.core\.windows\.net)/'
    r'[^"\'<>\s]+\.(?:jpg|jpeg|png|webp))',
    re.IGNORECASE,
)

# Pattern pour prix
# "Höchstgebot: €750" / "Mindestgebot: €500" / "Schätzpreis: €1,200"
PRIX_PATTERNS = [
    re.compile(
        r"(?:Höchstgebot|Mindestgebot|Schätzpreis|Aktuelles?\s*Gebot|Startgebot)"
        r"[\s:]*€\s*([\d.,]+)",
        re.IGNORECASE,
    ),
    # Format simple: "€750" ou "€1,200"
    re.compile(r"€\s*([\d.,]+)", re.IGNORECASE),
]

# Pattern pour code postal allemand "Standort: 10829 Berlin"
# Ex: "10829 Berlin" / "14612 Falkensee" / "23560 Lübeck"
LOCATION_PATTERN = re.compile(
    r'(?:Standort[\s:]*)?(\d{5})\s+([A-Z][a-zA-ZÀ-ÿ\s\-\.\']+?)(?:[,\.]|$)',
    re.IGNORECASE,
)

# Limites de sécurité
MAX_AUCTIONS = 50           # Nb max auctions decouvertes
# Pas de pagination interne sur les pages auction d'AssetOrb (tous lots affiches)


class AssetOrbScraper(BaseScraper):
    source_nom = "AssetOrb"
    source_pays = "DE"
    base_url = BASE_URL
    requires_javascript = False  # HTML statique
    rate_limit_seconds = 1.0
    default_category = "Autre / non classe"

    def __init__(self, fetcher=None, llm_extractor=None):
        # AssetOrb bloque les IPs datacenter (audit 12 mai : HTML vide en prod
        # alors que fetch local Mohamed retourne le contenu). On route via
        # ScraperAPI sans render : le HTML statique d'AssetOrb contient deja
        # les lots (~643KB par page auction, confirme en diag 12 mai 20:36).
        # Render JS n'est PAS necessaire (et causait des HTTP 500 timeout
        # cote ScraperAPI sur le rendu lourd). Cout : 1 credit/req.
        if fetcher is None:
            try:
                from scrapers.fetchers import ScraperApiFetcher
                fetcher = ScraperApiFetcher()
            except ValueError as e:
                logger.warning(
                    "[AssetOrb] ScraperApiFetcher indisponible (%s), "
                    "fallback HttpxFetcher (sera probablement bloque en prod)",
                    e,
                )
        super().__init__(fetcher=fetcher, llm_extractor=llm_extractor)
        self._existing_urls = None
        # Cache : URLs des auctions decouvertes
        self._auction_urls: Optional[list] = None

    def list_listing_urls(self) -> Iterator[str]:
        """Genere les URLs des pages liste.

        Strategie en 2 phases :
        1. D'abord la page /upcoming-auctions (decouvre les auctions)
        2. Puis pour chaque auction, sa page detail (qui contient les lots)
        """
        # Phase 1 : page d'index principale
        yield LISTING_URL

        # Phase 2 : pour chaque auction, sa page detail
        if not self._auction_urls:
            logger.warning(
                "[%s] Aucune auction decouverte sur l'index",
                self.source_nom,
            )
            return

        for auction_url in self._auction_urls[:MAX_AUCTIONS]:
            yield auction_url

    def parse_listing(
        self, html: str, listing_url: str
    ) -> Iterator[str]:
        """Parse une page liste et yield les URLs des lots.

        Si on est sur /upcoming-auctions, on extrait les URLs des auctions.
        Si on est sur /auction/details/, on extrait les URLs des lots.
        """
        # Detection : page index ou page auction ?
        is_index = "/upcoming-auctions" in listing_url
        is_auction_page = "/auction/details/" in listing_url

        if is_index:
            # Extraire les URLs des auctions depuis /upcoming-auctions
            matches = AUCTION_URL_PATTERN.findall(html)
            unique_paths = list(dict.fromkeys(matches))

            full_urls = [BASE_URL + p for p in unique_paths]
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

            # Yield seulement les nouvelles URLs. Le HTML contient `&amp;`
            # (HTML entity), il faut le convertir en `&` pour que httpx/AssetOrb
            # interprete correctement les query params.
            for path in unique_paths:
                full_url = BASE_URL + path.replace("&amp;", "&")
                if full_url not in self._existing_urls:
                    yield full_url

    def parse_detail(self, html: str, url: str) -> dict:
        """Parse une page lot et retourne un dict avec les champs raw.

        Note: on ne categorise PAS ici. On laisse Claude Haiku 4.5
        (via BaseScraper.normalize) faire le travail apres avoir vu
        le titre + description complets.
        """
        soup = BeautifulSoup(html, "html.parser")

        # === Titre : h1 prioritaire, sinon title ===
        h1 = soup.find("h1")
        titre = ""
        if h1:
            titre = self.clean_text(h1.get_text())
        if not titre:
            title_tag = soup.find("title")
            if title_tag:
                titre = self.clean_text(title_tag.get_text())
                # Nettoyer "Titre - AssetOrb"
                if " - AssetOrb" in titre:
                    titre = titre.split(" - AssetOrb")[0]

        # === Image : depuis le CDN Bidpath ===
        image_url = ""
        all_images = IMAGE_PATTERN.findall(html)
        # Filtrer les images de logo / icones (qui contiennent "main/" ou "logo")
        for img in all_images:
            if "/stock/" in img or "/auctions/" in img:
                image_url = img
                break
        # Fallback : premiere image trouvee
        if not image_url and all_images:
            # Eviter les logos
            non_logos = [img for img in all_images
                         if "logo" not in img.lower()
                         and "wortbildmarke" not in img.lower()]
            if non_logos:
                image_url = non_logos[0]

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
                # Format europeen: "1,200" (US) ou "1.200" (DE)
                # On normalise
                if "," in amount_raw and "." in amount_raw:
                    # Format mixte: "1,200.00" -> "1200.00"
                    amount = amount_raw.replace(",", "")
                elif "," in amount_raw:
                    # Format US (1,200) ou DE (1,50)
                    parts = amount_raw.split(",")
                    if len(parts[-1]) == 2:
                        # Decimal: "1.200,50"
                        amount = amount_raw.replace(".", "").replace(",", ".")
                    else:
                        # Milliers US: "1,200"
                        amount = amount_raw.replace(",", "")
                else:
                    amount = amount_raw
                # Validation
                try:
                    val = float(amount)
                    if 0 < val < 10_000_000:
                        prix = f"EUR {amount_raw}"
                        break
                except ValueError:
                    continue

        # === Type de vente : AssetOrb fait Industrieauktion ou Insolvenzversteigerung ===
        type_vente = "Industrieauktion"
        text_lower = html.lower()
        if "insolvenz" in text_lower:
            type_vente = "Insolvenzversteigerung"
        elif "werksschließung" in text_lower or "werksschlieung" in text_lower:
            type_vente = "Werksschliessung"
        elif "betriebsauflösung" in text_lower or "betriebsauflsung" in text_lower:
            type_vente = "Betriebsauflösung"

        # === source_id : extrait de l'URL (ex: lot=42675&au=715) ===
        source_id = ""
        lot_match = re.search(r'lot=(\d+).*?au=(\d+)', url)
        if lot_match:
            lot_id, au_id = lot_match.groups()
            source_id = f"{au_id}-{lot_id}"

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