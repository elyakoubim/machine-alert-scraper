"""
Scraper NetBid (DE/AT/EU) - v1
==============================

Site allemand specialise dans les encheres industrielles et faillites
(insolvency auctions). Couvre principalement Allemagne et Autriche,
plus quelques pays europeens.

Strategie : JSON-LD (Schema.org SaleEvent) + Playwright (Cloudflare bloque httpx).
1. Page liste : /en/auctions (toutes les encheres en 1 page, pas de pagination)
2. Extrait les URLs /en/auctions/{auction_id}-{lot_id}-{slug}
3. Pour chaque enchere, parse JSON-LD :
   - SaleEvent.name = titre
   - SaleEvent.description = description
   - SaleEvent.endDate = date_fin
   - SaleEvent.startDate = date_publication
   - SaleEvent.offers[0].price = prix
   - SaleEvent.offers[0].itemOffered.image = image_url
   - Pays detecte via drapeau "/img/flags/flaglib-XX.svg" (priorite)
     ou code postal de l'adresse en fallback.

1 ligne Airtable = 1 enchere (pas 1 lot - sinon trop de lignes)
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from typing import Iterator, Optional

from scrapers.base import BaseScraper, Annonce
import airtable as airtable_client

logger = logging.getLogger(__name__)


BASE_URL = "https://www.netbid.com"
LIST_URL = "https://www.netbid.com/en/auctions"

# Pattern URL detail : /en/auctions/{auction_id}-{lot_id}-{slug}
ITEM_URL_PATTERN = re.compile(
    r'href="(/en/auctions/\d+-\d+-[a-z0-9-]+)"'
)

# Pattern drapeau pays NetBid : /img/flags/flaglib-de.svg
FLAG_PATTERN = re.compile(r'/flags/flaglib-([a-z]{2})\.svg')

# Pattern adresse allemande (5 digits + ville)
DE_POSTAL_PATTERN = re.compile(r"\b\d{5}\s+[A-ZÄÖÜ][a-zäöüß]+")
# Pattern adresse autrichienne explicite
AT_PATTERN = re.compile(r"\bAT[\s-]\d{4}")
# Pattern adresse belge (4 digits + ville)
BE_POSTAL_PATTERN = re.compile(r"\b[1-9]\d{3}\s+[A-Z][a-z]+")


def detect_country_from_html(html: str) -> Optional[str]:
    """
    Detecte le pays depuis le HTML.
    Priorite 1 : drapeau /flags/flaglib-XX.svg
    Priorite 2 : code postal dans le texte
    Priorite 3 : nom pays explicite
    """
    # 1. Drapeau (le plus fiable)
    flag_matches = FLAG_PATTERN.findall(html)
    if flag_matches:
        # Filtre les drapeaux genericues (souvent meta/footer)
        flags = [f.upper() for f in flag_matches if f != "en"]
        if flags:
            # Le plus frequent (souvent affiche sur chaque lot)
            from collections import Counter
            most_common = Counter(flags).most_common(1)[0][0]
            return most_common

    # 2. Pattern code postal allemand
    if DE_POSTAL_PATTERN.search(html):
        return "DE"

    # 3. Patterns autres pays
    if AT_PATTERN.search(html):
        return "AT"
    if BE_POSTAL_PATTERN.search(html[:5000]):
        return "BE"

    # 4. Nom pays explicite
    html_lower = html[:5000].lower()
    if "germany" in html_lower or "deutschland" in html_lower:
        return "DE"
    if "austria" in html_lower or "österreich" in html_lower:
        return "AT"
    if "netherlands" in html_lower or "nederland" in html_lower:
        return "NL"
    if "belgium" in html_lower or "belgique" in html_lower:
        return "BE"

    return None


def parse_iso_date(date_str: str) -> Optional[str]:
    """Normalise une date ISO. Retourne None si invalide."""
    if not date_str:
        return None
    try:
        if "T" not in date_str:
            return date_str + "T00:00:00+00:00"
        # Nettoie les nanoseconds non standards (ex: ".0000000Z")
        cleaned = re.sub(r"\.\d+Z$", "+00:00", date_str)
        cleaned = cleaned.replace("Z", "+00:00")
        dt = datetime.fromisoformat(cleaned)
        return dt.isoformat()
    except (ValueError, TypeError):
        return None


class NetBidScraper(BaseScraper):
    source_nom = "NetBid"
    source_pays = "DE"  # Defaut, override par enchere selon localisation
    base_url = BASE_URL
    requires_javascript = True  # Cloudflare bloque httpx
    rate_limit_seconds = 1.5
    default_category = "Autre / non classe"

    # Mapping mots-cles EN/DE -> categorie Faillink
    CATEGORY_KEYWORDS = [
        # Machines industrielles (priorite haute)
        ("cnc", "Machines industrielles"),
        ("lathe", "Machines industrielles"),
        ("milling", "Machines industrielles"),
        ("press", "Machines industrielles"),
        ("injection molding", "Machines industrielles"),
        ("die casting", "Machines industrielles"),
        ("robot", "Machines industrielles"),
        ("welding", "Machines industrielles"),
        ("paper industry", "Machines industrielles"),
        ("production equipment", "Machines industrielles"),
        ("test system", "Machines industrielles"),
        ("packaging", "Machines industrielles"),
        ("machinery", "Machines industrielles"),
        ("machine", "Machines industrielles"),
        # Vehicules
        ("vehicle", "Vehicules"),
        ("truck", "Vehicules"),
        ("forklift", "Vehicules"),
        # Outillage
        ("shredder", "Outillage & equipement chantier"),
        ("extractor", "Outillage & equipement chantier"),
        ("dust extractor", "Outillage & equipement chantier"),
        ("tools", "Outillage & equipement chantier"),
        # Mobilier
        ("office", "Mobilier"),
        ("furniture", "Mobilier"),
        # Stocks
        ("warehouse", "Stocks & liquidations"),
        ("inventory", "Stocks & liquidations"),
    ]

    def __init__(self, fetcher=None, llm_extractor=None):
        super().__init__(fetcher=fetcher, llm_extractor=llm_extractor)
        self._existing_urls = None

    def list_listing_urls(self) -> Iterator[str]:
        """Une seule page liste (pas de pagination chez NetBid)."""
        yield LIST_URL

    def parse_listing(
        self, html: str, listing_url: str
    ) -> Iterator[str]:
        """Parse la liste et yield les URLs des encheres non deja en Airtable."""
        matches = ITEM_URL_PATTERN.findall(html)
        unique_paths = list(dict.fromkeys(matches))
        logger.info(
            "[%s] page %s : %d encheres trouvees",
            self.source_nom, listing_url, len(unique_paths),
        )

        if not unique_paths:
            return

        # Charge les URLs deja en Airtable (1 fois pour eviter rescrape)
        if self._existing_urls is None:
            try:
                self._existing_urls = airtable_client.get_existing_urls(
                    self.source_nom
                )
                logger.info(
                    "[%s] %d URLs deja en Airtable",
                    self.source_nom, len(self._existing_urls),
                )
            except Exception as e:
                logger.warning(
                    "[%s] echec get_existing_urls : %s",
                    self.source_nom, e,
                )
                self._existing_urls = set()

        new_count = 0
        for path in unique_paths:
            full_url = BASE_URL + path
            if full_url in self._existing_urls:
                continue
            new_count += 1
            yield full_url

        logger.info(
            "[%s] %d nouvelles encheres a scraper",
            self.source_nom, new_count,
        )

    def parse_detail(self, html: str, url: str) -> dict:
        """
        Parse une page detail NetBid via JSON-LD.
        Extrait : titre, description, prix, image_url, pays, type_vente,
                  date_fin_brut, date_publication_brut, categorie_native.
        """
        # 1. Trouve les blocs JSON-LD
        jsonld_pattern = re.compile(
            r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>',
            re.DOTALL,
        )
        scripts = jsonld_pattern.findall(html)

        sale_event = None
        for script_content in scripts:
            try:
                data = json.loads(script_content)
            except (json.JSONDecodeError, ValueError):
                continue

            graph = []
            if isinstance(data, dict):
                graph = data.get("@graph", [])
                if not graph and data.get("@type"):
                    graph = [data]
            elif isinstance(data, list):
                graph = data

            for item in graph:
                if not isinstance(item, dict):
                    continue
                t = item.get("@type", "")
                if isinstance(t, list):
                    t = "+".join(t)
                if "SaleEvent" in t or "Event" in t:
                    sale_event = item
                    break
            if sale_event:
                break

        if not sale_event:
            logger.warning(
                "[%s] aucun SaleEvent JSON-LD trouve : %s",
                self.source_nom, url,
            )
            return {}

        # 2. Extraction des champs
        titre = (sale_event.get("name") or "").strip()
        description = (sale_event.get("description") or "")[:2000]

        # Dates
        date_fin = parse_iso_date(sale_event.get("endDate"))
        date_pub = parse_iso_date(sale_event.get("startDate"))

        # Prix : prend le 1er offer
        prix = ""
        offers = sale_event.get("offers", [])
        if isinstance(offers, list) and offers:
            first_offer = offers[0]
            if isinstance(first_offer, dict):
                p = first_offer.get("price", "")
                if p:
                    prix = f"{p} EUR"

        # Image principale (depuis 1er offer.itemOffered.image)
        image_url = ""
        if isinstance(offers, list):
            for offer in offers:
                if not isinstance(offer, dict):
                    continue
                item_off = offer.get("itemOffered", {})
                if not isinstance(item_off, dict):
                    continue
                img = item_off.get("image")
                if isinstance(img, str) and img.startswith("http"):
                    image_url = img
                    break
                if isinstance(img, dict):
                    img_url = img.get("url") or img.get("@id", "")
                    if img_url and img_url.startswith("http"):
                        image_url = img_url
                        break

        # Pays : detection multi-source (drapeau > postal > nom)
        pays = detect_country_from_html(html) or self.source_pays

        # Type de vente
        type_vente = "Enchère"
        title_lower = titre.lower()
        desc_lower = description.lower()
        if any(
            x in title_lower or x in desc_lower
            for x in ["insolvency", "insolvenz", "bankruptcy", "faillite"]
        ):
            type_vente = "Faillite"

        # Categorie native = on prend le titre (sera mappe ensuite)
        categorie_native = titre[:150]

        return {
            "titre": titre,
            "description": description,
            "prix": prix,
            "image_url": image_url,
            "pays": pays,
            "type_vente": type_vente,
            "date_publication_brut": date_pub or "",
            "date_fin_brut": date_fin or "",
            "categorie_native": categorie_native,
        }

    def map_category(self, native_category: Optional[str]) -> str:
        """Mappe le titre vers une categorie Faillink standard."""
        if not native_category:
            return self.default_category

        text = native_category.lower()

        for keyword, category in self.CATEGORY_KEYWORDS:
            if keyword in text:
                return category

        return self.default_category