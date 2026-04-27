"""
Scraper Appelboom (BE) - v2
============================

Maison de ventes aux enchères belge (Olen) spécialisée dans les faillites
(executie veiling). Ventes de matériel professionnel, véhicules, IT, machines.

Strategie : HTTP statique + httpx (HTML classique ASP.NET, pas de JS).
1. Fetch homepage -> extraire IDs des veilingen (actives + binnenkort)
   Pattern: /Alle-kavels/{veiling_id}/Veiling
2. Pour chaque veiling, paginer ?Page=1, 2, 3...
   Extraire les URLs des kavels (lots individuels)
   Pattern: /{slug}/{kavel_id}/detail
3. Pour chaque kavel, fetch + parse :
   - <h1> = titre
   - <img src="img.appelboomonline.be/..."> = image
   - "Huidig bod" = prix actuel
   - "Locatie" = ville (info, on garde pays=BE)
   - meta description = description

1 ligne Airtable = 1 lot individuel (kavel)
"""

from __future__ import annotations

import logging
import re
from typing import Iterator, Optional

from bs4 import BeautifulSoup

from scrapers.base import BaseScraper, Annonce
import airtable as airtable_client

logger = logging.getLogger(__name__)


BASE_URL = "https://www.appelboomonline.be"

# Pattern pour extraire les IDs des veilingen depuis la homepage
# /Alle-kavels/1157/Veiling -> 1157
VEILING_URL_PATTERN = re.compile(r'/Alle-kavels/(\d+)/Veiling', re.IGNORECASE)

# Pattern pour extraire les URLs complètes des kavels depuis une page de veiling
# /Cirkelzaag---Makita---5903R/74728/detail
KAVEL_URL_PATTERN = re.compile(r'(/[^/"\'<>\s]+/\d+/detail)', re.IGNORECASE)

# Pattern pour extraire le source_id (= kavel ID) depuis une URL kavel
KAVEL_ID_PATTERN = re.compile(r'/(\d+)/detail$')

# Pattern pour images du CDN Appelboom
IMAGE_PATTERN = re.compile(
    r'(https?://img\.appelboomonline\.be/Image\.aspx\?[^"\'<>\s]+)',
    re.IGNORECASE,
)

# Pattern pour le prix actuel ("Huidig bod: € 25,00")
PRIX_PATTERN = re.compile(
    r"Huidig\s*bod[:\s]*€\s*([\d.,]+)",
    re.IGNORECASE,
)

# Pattern pour le startbod (fallback si pas de huidig bod)
STARTBOD_PATTERN = re.compile(
    r"Startbod[:\s]*€\s*([\d.,]+)",
    re.IGNORECASE,
)

# Limite de sécurité : nb max de pages par veiling
MAX_PAGES_PER_VEILING = 50

# Limite de sécurité : nb max de veilingen
MAX_VEILINGEN = 30


# Mapping des catégories Appelboom (mots-clés dans le titre/description)
# vers les catégories standards du projet
CATEGORY_KEYWORDS = {
    "Vehicules": [
        "voertuig", "auto", "wagen", "camion", "camionnette", "transit",
        "ford", "volkswagen", "audi", "peugeot", "fiat", "dodge", "iveco",
        "nissan", "jeep", "renault", "citroen", "leaf", "vrachtwagen",
        "stationwagen", "vuilniswagen", "veegwagen", "polo", "caddy",
    ],
    "Outillage": [
        "boormachine", "haakse slijper", "cirkelzaag", "decoupeerzaag",
        "schroevendraaier", "boorhamer", "schaafmachine", "bandschuur",
        "reciprozaag", "gereedschap", "makita", "dewalt", "bosch", "metabo",
        "hilti", "festool", "multitool", "bovenfrees", "freesmachine",
        "gereedschappen", "outillage",
    ],
    "Machines industrielles": [
        "reachtruck", "heftruck", "machine", "houtbewerking", "frees",
        "draaibank", "compressor", "generator", "stroomverdeel", "lasapparaat",
        "industriel", "ingeneurs",
    ],
    "Energie": [
        "zonnepaneel", "zonnepanelen", "batterij", "batterijen", "omvormer",
        "solar", "panneau solaire", "panneaux solaires", "goodwe",
    ],
    "IT / Informatique": [
        "laptop", "informatique", "ordinateur", "computer", "tft",
        "scherm", "schermen", "e-reader", "audio apparatuur", "it materiaal",
        "imprimante", "printer",
    ],
    "Mobilier": [
        "kantoormeubilair", "showroomkeuken", "keuken", "mobilier",
        "meubelen", "badkamermeubel", "bureau", "magazijnrek", "rekken",
        "kantoor",
    ],
    "Materiaux construction": [
        "container", "containers", "bureelcontainer", "materiaalcontainer",
        "ladders", "ladder", "elektrakabel", "kabels", "aannemersmateriaal",
    ],
    "Stock / Marchandises": [
        "wijnhandel", "inboedel", "stock", "marchandise", "showroom",
        "verlichting",
    ],
}


class AppelboomScraper(BaseScraper):
    source_nom = "Appelboom"
    source_pays = "BE"
    base_url = BASE_URL
    requires_javascript = False  # HTML statique ASP.NET, pas de JS
    rate_limit_seconds = 1.5
    default_category = "Autre / non classe"

    def __init__(self, fetcher=None, llm_extractor=None):
        super().__init__(fetcher=fetcher, llm_extractor=llm_extractor)
        self._existing_urls = None
        # Cache: liste des veiling_ids decouverts depuis la homepage
        self._veiling_ids: Optional[list] = None

    def list_listing_urls(self) -> Iterator[str]:
        """Genere les URLs des pages liste (homepage + pages kavels paginees).

        Strategie :
        1. Yield d'abord la homepage (qui sert juste a decouvrir les veiling_ids)
        2. Puis pour chaque veiling, yield ses pages paginees
        """
        # Etape 1 : la homepage elle-meme (pour decouvrir les veilingen)
        yield BASE_URL

        # Etape 2 : pour chaque veiling decouverte, yield les pages paginees
        # On suppose que parse_listing a deja popule self._veiling_ids
        if not self._veiling_ids:
            logger.warning(
                "[%s] Aucune veiling decouverte sur la homepage",
                self.source_nom,
            )
            return

        for veiling_id in self._veiling_ids[:MAX_VEILINGEN]:
            for page in range(1, MAX_PAGES_PER_VEILING + 1):
                if page == 1:
                    yield f"{BASE_URL}/Alle-kavels/{veiling_id}/Veiling"
                else:
                    yield f"{BASE_URL}/Alle-kavels/{veiling_id}/Veiling?Page={page}"

    def parse_listing(
        self, html: str, listing_url: str
    ) -> Iterator[str]:
        """Parse une page liste et yield les URLs des kavels non deja en Airtable.

        Si on est sur la homepage, on en profite pour extraire les veiling_ids
        et les memoriser dans self._veiling_ids.
        """
        # Si on est sur la homepage, extraire les veiling_ids
        if listing_url.rstrip("/") == BASE_URL.rstrip("/"):
            ids = VEILING_URL_PATTERN.findall(html)
            unique_ids = list(dict.fromkeys(ids))
            self._veiling_ids = unique_ids
            logger.info(
                "[%s] Homepage : %d veilingen decouvertes (IDs: %s)",
                self.source_nom, len(unique_ids), unique_ids[:10],
            )
            # La homepage ne contient pas directement de kavels
            return

        # Sinon, on est sur une page de kavels d'une veiling
        kavel_paths = KAVEL_URL_PATTERN.findall(html)
        unique_paths = list(dict.fromkeys(kavel_paths))

        if not unique_paths:
            # Page vide = on a depasse la derniere page
            logger.debug(
                "[%s] page vide : %s", self.source_nom, listing_url,
            )
            return

        logger.info(
            "[%s] %s : %d kavels trouves",
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
        """Parse une page kavel et retourne un dict avec les champs raw."""
        soup = BeautifulSoup(html, "html.parser")

        # Titre : h1 prioritaire
        h1 = soup.find("h1")
        titre = ""
        if h1:
            titre = self.clean_text(h1.get_text())
        if not titre:
            title_tag = soup.find("title")
            titre = self.clean_text(title_tag.get_text()) if title_tag else ""

        # Image : depuis le CDN img.appelboomonline.be
        image_url = ""
        img_match = IMAGE_PATTERN.search(html)
        if img_match:
            image_url = img_match.group(1)

        # Description : meta description + body (limit 1500 chars)
        description = ""
        meta_desc = soup.find("meta", attrs={"name": "description"})
        if meta_desc:
            description = (meta_desc.get("content", "") or "").strip()

        # Enrichir avec body text si description courte
        if len(description) < 100 and soup.body:
            for tag in soup.body.find_all(["script", "style", "nav", "footer"]):
                tag.decompose()
            body_text = self.clean_text(soup.body.get_text())[:1500]
            if body_text:
                description = (description + " " + body_text).strip()[:2000]

        # Prix : Huidig bod prioritaire, sinon Startbod
        prix = ""
        text_for_prix = soup.get_text() if soup else html
        huidig_match = PRIX_PATTERN.search(text_for_prix)
        if huidig_match:
            prix = "EUR " + huidig_match.group(1)
        else:
            start_match = STARTBOD_PATTERN.search(text_for_prix)
            if start_match:
                prix = "EUR " + start_match.group(1)

        # Type de vente : toujours "Executie veiling" (= vente sur faillite)
        # car Appelboom ne fait que ca
        type_vente = "Executie veiling"

        # source_id : numerique extrait de l'URL
        source_id = ""
        id_match = KAVEL_ID_PATTERN.search(url)
        if id_match:
            source_id = id_match.group(1)

        # Categorie native : on n'a pas de breadcrumb categorie sur Appelboom,
        # on laisse vide et on utilise map_category() basee sur le titre
        categorie_native = titre

        return {
            "titre": titre,
            "description": description,
            "prix": prix,
            "image_url": image_url,
            "type_vente": type_vente,
            "categorie_native": categorie_native,
            "source_id": source_id,
        }

    def map_category(self, native_category: Optional[str]) -> str:
        """Mappe le titre/description vers une categorie standard.

        Appelboom n'a pas de categorie native exposee dans le HTML, donc
        on utilise des mots-cles dans le titre.
        """
        if not native_category:
            return self.default_category

        text = native_category.lower()
        for category, keywords in CATEGORY_KEYWORDS.items():
            for kw in keywords:
                if kw.lower() in text:
                    return category

        return self.default_category

    @staticmethod
    def clean_text(text: str) -> str:
        """Normalise les espaces / retours a la ligne."""
        if not text:
            return ""
        return re.sub(r"\s+", " ", text).strip()