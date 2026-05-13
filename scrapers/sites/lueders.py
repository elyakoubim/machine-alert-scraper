"""
Scraper Lueders & Partner (DE) - v1
====================================

Cabinet allemand d'enchères industrielles basé à Hambourg, fondé en 1954.
Plus de 2000 auctions et 400.000 positions vendues.
Specialisé dans les machines, equipements industriels et immobilier.

Site web : https://lueders-partner.com (presentation WordPress)
Plateforme auctions : https://auktionen.lueders-partner.com (PHP/Joomla)

Strategie : HTTP statique + httpx (HTML classique, pas de JS).
1. Fetch /index.php?vm=g -> liste les auctions actives
   Pattern: /index.php/de/{auction_id}/ (ex: 2618 = COHLINE)
2. Pour chaque auction, paginer via ?pps=N&ppe=N+19&poszae=20
   (pps=1, 21, 41, 61... step de 20 lots par page)
3. Sur chaque page, extraire les URLs des lots
   Pattern lot: /index.php/de/{auction_id}/{lot_id}/{slug}?vm=g
4. Pour chaque lot, fetch + parse:
   - Titre
   - Image (CDN auktionsdaten/0{auction_id}/0{lot_id}.jpg)
   - Description
   - Prix (Mindestpreis ou Aktuelles Gebot)
   - Localisation (toujours DE)

1 ligne Airtable = 1 lot individuel (Position)

Volume estimé : ~3 auctions × ~150 lots = ~450 lots actifs.

CATEGORISATION :
On retourne toujours "Autre / non classe" comme map_category().
Le BaseScraper.normalize() detecte "Autre" et appelle automatiquement
Claude Haiku 4.5 (LLMExtractor) pour categoriser proprement le lot
dans une des 8 categories Faillink officielles.
"""

from __future__ import annotations

from datetime import datetime, timezone
import logging
import re
from typing import Iterator, Optional

from bs4 import BeautifulSoup

from scrapers.base import BaseScraper, Annonce
import airtable as airtable_client

logger = logging.getLogger(__name__)


BASE_URL = "https://auktionen.lueders-partner.com"
INDEX_URL = f"{BASE_URL}/index.php?vm=g"

# Pattern pour URLs auction (page d'une vente complete)
# /index.php/de/2618/cohline-gmbh-rohrleitungssysteme
# /de/2618/cohline-gmbh-rohrleitungssysteme
# Capturer juste l'auction_id (4 chiffres typiquement)
AUCTION_URL_PATTERN = re.compile(
    r'(?:/index\.php)?/de/(\d+)/(?!\d)([a-z0-9\-]+)?',
    re.IGNORECASE,
)

# Pattern pour URLs lot
# /index.php/de/2618/1/wiegesystem
# /de/2618/366/rohrenden-bearbeitungszelle
# Format: /de/{auction_id}/{lot_id}/{slug}
LOT_URL_PATTERN = re.compile(
    r'(?:/index\.php)?/de/(\d+)/(\d+)/([a-z0-9\-äöüÄÖÜß%]+)',
    re.IGNORECASE,
)

# Pattern pour images CDN Lueders.
# Le HTML expose des URLs RELATIVES dans les <img src=...> :
#   /auktionsdaten/02430/00027-no.jpg     <- full size original
#   /auktionsdaten/02430/00027-th.jpg     <- thumbnail
#   /auktionsdaten/02430/00027-1-th.jpg   <- variante numerotee thumbnail
#   /auktionsdaten/02430/00027.jpg        <- defensif (pas observe en pratique)
# Le prefix https://auktionen.lueders-partner.com est OPTIONNEL et NON capture ;
# le capture group 1 contient toujours le path (qui commence par /). Le code
# prefixe avec BASE_URL pour obtenir l'URL absolue.
IMAGE_PATTERN = re.compile(
    r'(?:https?://auktionen\.lueders-partner\.com)?'
    r'(/auktionsdaten/\d+/\d+[^\s"\'<>]*\.jpg)',
    re.IGNORECASE,
)

# Pattern pour prix - "Mindestpreis: 350.000,00 EUR" ou "200 EUR" ou "1.000 EUR"
PRIX_PATTERNS = [
    re.compile(
        r"(?:Mindestpreis|Aktuelles?\s*Gebot|Startgebot|Current\s*bid|Starting\s*bid)"
        r"[\s:]*([\d.,]+)\s*(?:EUR|€)",
        re.IGNORECASE,
    ),
    # Format simple: "200 EUR" ou "1.000 EUR" (suit immédiatement le titre)
    re.compile(r"\b([\d.,]+)\s*EUR\b", re.IGNORECASE),
]

# Pattern pour code postal allemand "D-{5 chiffres} {Ville}"
# Ex: "D-56410 Montabaur" ou "20148 Hamburg"
LOCATION_PATTERN = re.compile(
    r'(?:D[-–]?\s*)?(\d{5})\s+([A-Z][a-zA-ZÀ-ÿ\s\-\.\']+?)(?:[,\.]|$)',
    re.IGNORECASE,
)

# Limites de sécurité
MAX_PAGES_PER_AUCTION = 25  # Pagination interne (20 lots/page = 500 max par auction)
MAX_AUCTIONS = 50           # Nb max auctions en parallele (largement assez pour ~3 actives)


class LuedersScraper(BaseScraper):
    source_nom = "Lueders & Partner"
    source_pays = "DE"
    base_url = BASE_URL
    requires_javascript = False  # HTML statique PHP/Joomla
    rate_limit_seconds = 1.0
    default_category = "Autre / non classe"

    def __init__(self, fetcher=None, llm_extractor=None):
        super().__init__(fetcher=fetcher, llm_extractor=llm_extractor)
        self._existing_urls = None
        # Cache : auction_ids decouverts depuis l'index
        self._auction_ids: Optional[list] = None

    def list_listing_urls(self) -> Iterator[str]:
        """Genere les URLs des pages liste.

        Strategie en 2 phases :
        1. D'abord la page d'index /index.php?vm=g (decouvre les auctions)
        2. Puis pour chaque auction, ses pages paginees (?pps=1, 21, 41...)
        """
        # Phase 1 : page d'index principale
        yield INDEX_URL

        # Phase 2 : pour chaque auction decouverte, ses pages de lots
        if not self._auction_ids:
            logger.warning(
                "[%s] Aucune auction decouverte sur l'index",
                self.source_nom,
            )
            return

        for auction_id in self._auction_ids[:MAX_AUCTIONS]:
            # Pagination par 20 : pps=1, 21, 41, 61...
            for page_num in range(MAX_PAGES_PER_AUCTION):
                pps = page_num * 20 + 1
                ppe = pps + 19
                # On utilise une URL simple qui inclut l'auction_id
                # Le slug n'est pas obligatoire pour avoir le HTML
                url = (
                    f"{BASE_URL}/index.php/de/{auction_id}/"
                    f"?vm=g&pps={pps}&ppe={ppe}&poszae=20"
                )
                yield url

    def parse_listing(
        self, html: str, listing_url: str
    ) -> Iterator[str]:
        """Parse une page liste et yield les URLs des lots.

        Si on est sur la page d'index, on extrait les auction_ids.
        Sinon (page d'une auction paginée), on extrait les URLs des lots.
        """
        # Detection : page d'index ou page d'auction ?
        # La page d'index est /index.php?vm=g (sans /de/{id} dedans)
        is_index = (
            "/index.php" in listing_url
            and "/de/" not in listing_url.split("?")[0]
        )

        if is_index:
            # Extraire les auction_ids depuis la page d'index
            # Format: /de/{auction_id}/{slug}
            matches = AUCTION_URL_PATTERN.findall(html)
            # matches est liste de tuples (auction_id, slug)
            ids = []
            for m in matches:
                auction_id = m[0] if isinstance(m, tuple) else m
                if auction_id and auction_id not in ids:
                    ids.append(auction_id)

            self._auction_ids = ids
            logger.info(
                "[%s] Index : %d auctions decouvertes (IDs: %s)",
                self.source_nom, len(ids), ids[:10],
            )
            # La page d'index ne contient pas de lots directement
            return

        # Sinon, on est sur une page paginée d'une auction
        # Extraire les URLs des lots : /de/{auction_id}/{lot_id}/{slug}
        lot_matches = LOT_URL_PATTERN.findall(html)
        # lot_matches est liste de tuples (auction_id, lot_id, slug)
        unique_lot_paths = []
        seen = set()
        for m in lot_matches:
            auction_id, lot_id, slug = m
            path = f"/index.php/de/{auction_id}/{lot_id}/{slug}"
            if path not in seen:
                seen.add(path)
                unique_lot_paths.append(path)

        if not unique_lot_paths:
            logger.debug(
                "[%s] page vide : %s",
                self.source_nom, listing_url,
            )
            return

        logger.info(
            "[%s] %s : %d lots trouves",
            self.source_nom, listing_url, len(unique_lot_paths),
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
        for path in unique_lot_paths:
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

        # === Titre : h2 (Lueders met le titre du lot dans h2, pas h1) ===
        # Aucun <h1> sur les pages lot ; <title> retourne "Alle Auktionen |
        # Lueders & Partner GmbH" (titre du template Joomla), ce qui faisait
        # historiquement que 100% des records avaient titre="Alle Auktionen".
        h2 = soup.find("h2")
        titre = ""
        if h2:
            titre = self.clean_text(h2.get_text())
        if not titre:
            title_tag = soup.find("title")
            if title_tag:
                titre = self.clean_text(title_tag.get_text())
                # Nettoyer "Titre | Lueders & Partner GmbH"
                if " | " in titre:
                    titre = titre.split(" | ")[0]

        # === Image : path CDN extrait via regex + prefix BASE_URL ===
        # Le regex ramene toujours un path relatif (capture group 1).
        # Priorite : -no.jpg (full size original) > pas de suffixe > -th.jpg
        # (thumbnail) > variantes numerotees -N-th.jpg.
        # Avant : regex exigeait URL absolue + ne couvrait pas -no, 100% des
        # 5 327 records Lueders avaient image_url vide.
        image_url = ""
        all_image_paths = IMAGE_PATTERN.findall(html)
        if all_image_paths:
            def _img_rank(path: str) -> int:
                p = path.lower()
                if "-no.jpg" in p:
                    return 0  # full size original
                if re.search(r"/\d+\.jpg$", p):
                    return 1  # sans suffixe (defensif, rarement vu)
                if "-th.jpg" in p:
                    return 2  # thumbnail simple ou numerote (-N-th.jpg)
                return 3
            chosen_path = sorted(all_image_paths, key=_img_rank)[0]
            image_url = BASE_URL + chosen_path

        # === Description : conteneur .ak-posinfo (lot-specific) ===
        # .ak-posinfo isole la description du lot (titre, prix, fabrikat,
        # baujahr, beschreibung) sans inclure le cookie banner, la nav,
        # ou le footer. Confirme sur 4 samples (3 fermes + 1 actif).
        # Avant : body.get_text() captait le cookie banner allemand
        # ("Hinweis schliessen / verwendet Cookies"), polluant 100% des records.
        description = ""
        posinfo = soup.select_one(".ak-posinfo")
        if posinfo:
            description = self.clean_text(posinfo.get_text(" "))[:2000]
        if not description:
            # Fallback defensif : meta description (~"Auktionen" en general,
            # rarement informative mais non polluante).
            meta_desc = soup.find("meta", attrs={"name": "description"})
            if meta_desc:
                description = (meta_desc.get("content", "") or "").strip()

        # === Prix : essayer plusieurs patterns ===
        prix = ""
        text_for_prix = soup.get_text() if soup else html
        for pattern in PRIX_PATTERNS:
            prix_match = pattern.search(text_for_prix)
            if prix_match:
                amount_raw = prix_match.group(1)
                # Format allemand: "350.000,00" -> "350000.00"
                # Si on a des "." comme separateur de milliers et "," comme decimal
                if "," in amount_raw and "." in amount_raw:
                    # Format allemand classique
                    amount = amount_raw.replace(".", "").replace(",", ".")
                else:
                    amount = amount_raw.replace(",", ".")
                # Validation
                try:
                    val = float(amount)
                    if 0 < val < 10_000_000:
                        prix = f"EUR {amount_raw}"
                        break
                except ValueError:
                    continue

        # === Type de vente : Lueders fait des Auktion + Freiverkauf ===
        # On detecte via le HTML
        type_vente = "Auktion"
        if "freiverkauf" in html.lower():
            type_vente = "Freiverkauf"
        elif "auktion" in html.lower():
            type_vente = "Auktion"

        # === source_id : extrait de l'URL (ex: 2618-366) ===
        source_id = ""
        url_match = LOT_URL_PATTERN.search(url)
        if url_match:
            auction_id, lot_id, _ = url_match.groups()
            source_id = f"{auction_id}-{lot_id}"

        # === Auktionsstatus : detection pour lifecycle-worker ===
        # Si "Auktionsstatus: abgeschlossen" est present, l'auction est
        # terminee. On set date_fin = now() pour que update_annonce_statuts.py
        # marque l'annonce en "expiree" au prochain run quotidien (03h30 UTC).
        # Pour les autres statuts (aktiv, etc.) on laisse date_fin a None
        # (peut etre raffine plus tard en parsant "Gebote bis ..." pour les actifs).
        date_fin = None
        status_match = re.search(
            r"Auktionsstatus[:\s]+([a-zA-ZäöüÄÖÜß]+)", html
        )
        if status_match and status_match.group(1).lower() == "abgeschlossen":
            date_fin = datetime.now(timezone.utc).isoformat()

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
            "date_fin": date_fin,
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