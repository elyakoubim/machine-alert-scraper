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

CATEGORISATION :
On retourne toujours "Autre / non classe" comme map_category().
Le BaseScraper.normalize() detecte "Autre" et appelle automatiquement
Claude Haiku 4.5 (LLMExtractor) pour categoriser proprement le lot
dans une des 8 categories Faillink officielles :
- Immobilier
- Machines industrielles
- Materiel informatique
- Mobilier
- Stocks & liquidations
- Vehicules
- Outillage & equipement chantier
- Autre / non classe
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

# Early-stop pagination : si N pages consecutives produisent 0 nouveau lot
# (toutes URLs deja connues en Airtable), on saute le reste de la veiling.
# Trade-off : si Appelboom mettait des nouveaux lots a la FIN de la pagination
# (peu probable pour un site auction, generalement tri par date desc), on les
# raterait. Compromis 2 = tolere 1 page vide isolee, mais stop apres confirmation.
# Audit 12 mai : 551 pages fetched pour 2 nouvelles annonces (98% gaspillage).
ZERO_NEW_PAGE_THRESHOLD = 2


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
        # Early-stop : pages consecutives sans nouveau lot, par veiling_id
        self._zero_new_streak: dict = {}
        # Veilingen "epuisees" (early-stop active) : on saute le reste
        self._exhausted_veilingen: set = set()

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
                # Early-stop : si parse_listing a marque cette veiling comme
                # epuisee (N pages consecutives sans nouveau lot), on skip
                # le reste de sa pagination.
                if veiling_id in self._exhausted_veilingen:
                    logger.info(
                        "[%s] veiling %s : early-stop apres page %d",
                        self.source_nom, veiling_id, page - 1,
                    )
                    break
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

        # Yield seulement les nouvelles URLs, et tient le compteur early-stop.
        new_count = 0
        for path in unique_paths:
            full_url = BASE_URL + path
            if full_url not in self._existing_urls:
                yield full_url
                new_count += 1

        # Update early-stop : extrait le veiling_id de l'URL de listing
        # (pattern /Alle-kavels/{veiling_id}/Veiling).
        m = VEILING_URL_PATTERN.search(listing_url)
        if m:
            veiling_id = m.group(1)
            if new_count == 0:
                streak = self._zero_new_streak.get(veiling_id, 0) + 1
                self._zero_new_streak[veiling_id] = streak
                if streak >= ZERO_NEW_PAGE_THRESHOLD:
                    self._exhausted_veilingen.add(veiling_id)
                    logger.info(
                        "[%s] veiling %s : %d pages consecutives sans nouveau lot, "
                        "early-stop active (reste de la pagination skippe)",
                        self.source_nom, veiling_id, streak,
                    )
            else:
                # Reset le streak si on a trouve du nouveau
                if self._zero_new_streak.get(veiling_id):
                    self._zero_new_streak[veiling_id] = 0

    def parse_detail(self, html: str, url: str) -> dict:
        """Parse une page kavel et retourne un dict avec les champs raw.

        Note: on ne categorise PAS ici. On laisse Claude Haiku 4.5
        (via BaseScraper.normalize) faire le travail apres avoir vu
        le titre + description complets.
        """
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

        # categorie_native : on n'a PAS de categorie native sur Appelboom.
        # On laisse vide -> map_category() retournera "Autre / non classe"
        # -> BaseScraper.normalize() declenchera Claude Haiku automatiquement
        # avec le titre + description complets pour categoriser correctement.
        categorie_native = None

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
        """Pas de mapping local : on retourne toujours 'Autre / non classe'.

        Le BaseScraper.normalize() detectera 'Autre' et appellera
        automatiquement Claude Haiku 4.5 (LLMExtractor) pour categoriser
        proprement le lot dans une des 8 categories Faillink officielles.

        C'est plus precis qu'un dictionnaire de mots-cles statique car
        Haiku comprend le contexte (ex: "Showroomkeuken" -> Mobilier
        et pas Materiaux construction).
        """
        return self.default_category

    @staticmethod
    def clean_text(text: str) -> str:
        """Normalise les espaces / retours a la ligne."""
        if not text:
            return ""
        return re.sub(r"\s+", " ", text).strip()