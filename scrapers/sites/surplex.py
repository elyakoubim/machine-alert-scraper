"""
Scraper Surplex (Europe) - v1
==============================

Surplex est un gros marketplace d'enchères industrielles européennes
avec ~156 auctions actives et 5 000-15 000 lots individuels.
Couvre 14 pays : DE, BE, NL, FR, IT, CH, AT, ES, CZ, HU, PT, RO, PL, HR.

Strategie : HTTP statique + httpx (HTML classique, pas de JS).
1. Fetch /auctions?page=1..N -> extraire URLs des auctions actives
   Pattern: /a/{slug}-A1-44567 ou /a/{slug}-A3-43501 (A1, A3, A7 = type)
2. Pour chaque auction, paginer ?page=1..N pour lister tous ses lots
   Pattern lot: /l/{slug}-A3-43501-191 (lot_id = auction-numero)
3. Pour chaque lot, fetch + parse :
   - <h1> = titre du lot
   - Image du CDN tbauctions.com
   - Localisation (ville, pays sur 2 lettres)
   - Description meta + body
   - Prix actuel (Aktuelles Gebot / Current bid)

1 ligne Airtable = 1 lot individuel

CATEGORISATION :
On retourne toujours "Autre / non classe" comme map_category().
Le BaseScraper.normalize() detecte "Autre" et appelle automatiquement
Claude Haiku 4.5 (LLMExtractor) pour categoriser proprement le lot
dans une des 8 categories Faillink officielles.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from typing import Iterator, Optional

from bs4 import BeautifulSoup

from scrapers.base import BaseScraper, Annonce
import airtable as airtable_client

logger = logging.getLogger(__name__)


BASE_URL = "https://www.surplex.com"

# Pattern pour URLs auctions: /a/{slug}-A1-44567 ou /a/{slug}-A3-43501
# Les preffixes A1, A3, A7 indiquent le type (regular, online-only, direct sale)
AUCTION_URL_PATTERN = re.compile(
    r'(/a/[^/"\'<>\s?#]+-A\d+-\d+)',
    re.IGNORECASE,
)

# Pattern pour URLs lots: /l/{slug}-A3-43501-191
LOT_URL_PATTERN = re.compile(
    r'(/l/[^/"\'<>\s?#]+-A\d+-\d+-\d+)',
    re.IGNORECASE,
)

# Pattern pour extraire l'auction_id depuis une URL auction
# /a/industrie-und-maschinen-A3-43501 -> A3-43501
AUCTION_ID_PATTERN = re.compile(r'-(A\d+-\d+)$', re.IGNORECASE)

# Pattern pour extraire le lot_id depuis une URL lot
# /l/2019-linde-h30t-01-gabelstapler-A3-43501-191 -> A3-43501-191
LOT_ID_PATTERN = re.compile(r'-(A\d+-\d+-\d+)$', re.IGNORECASE)

# Pattern pour images CDN Surplex
# https://media.tbauctions.com/image-media/UUID/file?imageSize=512x384
IMAGE_PATTERN = re.compile(
    r'(https?://media\.tbauctions\.com/image-media/[^"\'<>\s]+/file[^"\'<>\s]*)',
    re.IGNORECASE,
)

# Pattern pour extraire localisation et code pays de fin de page
# Ex: "Ardooie, BE" ou "Mehrere Standorte (79)" -> on extrait BE
LOCATION_PATTERN = re.compile(
    r'\b([A-Z][a-zA-ZÀ-ÿ\s\-\.\']{1,40}),\s*([A-Z]{2})\b',
)

# Pattern pour prix (formats multiples selon langue)
# "Aktuelles Gebot: € 250" / "Current bid: €1,500" / "Startgebot: 100 €"
PRIX_PATTERNS = [
    re.compile(r"(?:aktuelle?s?\s*gebot|current\s*bid|enchère?\s*actuelle?|huidig\s*bod)"
               r"[\s:]*€?\s*([\d.,]+)\s*€?",
               re.IGNORECASE),
    re.compile(r"(?:startgebot|starting\s*bid|mise?\s*de?\s*départ|start\s*bod|starbod)"
               r"[\s:]*€?\s*([\d.,]+)\s*€?",
               re.IGNORECASE),
    re.compile(r"€\s*([\d.]+(?:[.,]\d{2})?)", re.IGNORECASE),
]

# Mapping codes pays Surplex -> codes Faillink
# (les codes sont déjà ISO-2, on les garde tels quels)
SUPPORTED_COUNTRIES = {
    "BE", "NL", "FR", "DE", "IT", "AT", "CH", "ES",
    "CZ", "HU", "PT", "RO", "PL", "HR", "BG", "GR",
    "DK", "SE", "NO", "FI", "LU", "IE", "GB", "UK",
}

# Limites de sécurité
MAX_AUCTION_LIST_PAGES = 15  # Pagination /auctions (réel ~7)
# Pagination interne d'une auction. 100 pages * 48 lots/page = 4800 lots max,
# marge confortable au-dela des plus grosses auctions Surplex observees
# (~1500-1600 lots type Mounting Systems). En mode hybride niveau 2, on
# stoppe tot via __NEXT_DATA__.lots.hasNext=False, cette limite est juste
# un garde-fou anti-runaway.
MAX_PAGES_PER_AUCTION = 100
MAX_AUCTIONS = 200           # Garde-fou nb total auctions


class SurplexScraper(BaseScraper):
    source_nom = "Surplex"
    source_pays = "EU"  # Multi-pays, on extrait par lot
    base_url = BASE_URL
    requires_javascript = False  # HTML statique
    rate_limit_seconds = 1.0
    default_category = "Autre / non classe"

    def __init__(self, fetcher=None, llm_extractor=None):
        # Surplex bloque les IPs datacenter (audit 12 mai : HTML vide en prod
        # Railway alors que fetch local Mohamed retourne 48 uniques). On route
        # via ScraperAPI (proxy IPs residentielles). render_js=False (defaut) :
        # HTML statique, 1 credit/req au lieu de 10-25.
        if fetcher is None:
            try:
                from scrapers.fetchers import ScraperApiFetcher
                fetcher = ScraperApiFetcher()
            except ValueError as e:
                # Cle SCRAPERAPI absente -> fallback HttpxFetcher (default base.py).
                # Le scraper retournera probablement 0 en prod datacenter, mais
                # au moins on ne crashe pas le worker.
                logger.warning(
                    "[Surplex] ScraperApiFetcher indisponible (%s), "
                    "fallback HttpxFetcher (sera probablement bloque en prod)",
                    e,
                )
        super().__init__(fetcher=fetcher, llm_extractor=llm_extractor)
        self._existing_urls = None
        # Cache: liste des URLs d'auctions découvertes
        self._auction_urls: Optional[list] = None

    def list_listing_urls(self) -> Iterator[str]:
        """Genere les URLs des pages de listing.

        Strategie en 2 phases :
        1. D'abord les pages /auctions?page=1..N (pour decouvrir les auctions)
        2. Puis pour chaque auction, ses pages de lots paginees
        """
        # Phase 1 : pages de la liste des auctions
        for page in range(1, MAX_AUCTION_LIST_PAGES + 1):
            if page == 1:
                yield f"{BASE_URL}/auctions"
            else:
                yield f"{BASE_URL}/auctions?page={page}"

        # Phase 2 : pour chaque auction decouverte, ses pages de lots
        if not self._auction_urls:
            logger.warning(
                "[%s] Aucune auction decouverte",
                self.source_nom,
            )
            return

        for auction_url in self._auction_urls[:MAX_AUCTIONS]:
            for page in range(1, MAX_PAGES_PER_AUCTION + 1):
                if page == 1:
                    yield auction_url
                else:
                    yield f"{auction_url}?page={page}"

    def parse_listing(
        self, html: str, listing_url: str
    ) -> Iterator[str]:
        """Parse une page liste et yield les URLs des lots.

        Si on est sur une page /auctions, on en profite pour extraire
        les URLs des auctions et les memoriser dans self._auction_urls.

        Si on est sur une page d'auction (/a/...), on extrait les URLs
        des lots individuels (/l/...) qui sont les vraies annonces.
        """
        # Detection du type de page via l'URL
        is_auctions_list = "/auctions" in listing_url and "/a/" not in listing_url
        is_auction_detail = "/a/" in listing_url

        if is_auctions_list:
            # On est sur /auctions ou /auctions?page=N
            # -> extraire les URLs des auctions
            auction_paths = AUCTION_URL_PATTERN.findall(html)
            unique_paths = list(dict.fromkeys(auction_paths))

            if not unique_paths:
                logger.debug(
                    "[%s] Aucune auction sur %s",
                    self.source_nom, listing_url,
                )
                return

            full_urls = [BASE_URL + p for p in unique_paths]

            # Memoriser pour la phase 2 (auctions)
            if self._auction_urls is None:
                self._auction_urls = []
            for url in full_urls:
                if url not in self._auction_urls:
                    self._auction_urls.append(url)

            logger.info(
                "[%s] %s : %d auctions trouvees (cumul: %d)",
                self.source_nom, listing_url,
                len(unique_paths), len(self._auction_urls),
            )
            # Une page d'auctions ne contient pas de lots directement
            return

        elif is_auction_detail:
            # On est sur /a/{slug}-A3-43501 (page detail d'une auction)
            # -> extraire les URLs des lots
            lot_paths = LOT_URL_PATTERN.findall(html)
            unique_paths = list(dict.fromkeys(lot_paths))

            if not unique_paths:
                logger.debug(
                    "[%s] page vide : %s",
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

        # === Titre : h1 prioritaire ===
        h1 = soup.find("h1")
        titre = ""
        if h1:
            titre = self.clean_text(h1.get_text())
        if not titre:
            title_tag = soup.find("title")
            if title_tag:
                titre = self.clean_text(title_tag.get_text())
                # Nettoyage : "Titre | Surplex" -> "Titre"
                if " | " in titre:
                    titre = titre.split(" | ")[0]

        # === Image : depuis le CDN tbauctions.com ===
        image_url = ""
        img_match = IMAGE_PATTERN.search(html)
        if img_match:
            image_url = img_match.group(1)

        # === Description : meta description + body text ===
        description = ""
        meta_desc = soup.find("meta", attrs={"name": "description"})
        if meta_desc:
            description = (meta_desc.get("content", "") or "").strip()

        # Enrichir avec body text si description courte
        if len(description) < 100 and soup.body:
            for tag in soup.body.find_all(["script", "style", "nav", "footer", "header"]):
                tag.decompose()
            body_text = self.clean_text(soup.body.get_text())[:1500]
            if body_text:
                description = (description + " " + body_text).strip()[:2000]

        # === Pays : extraction depuis le texte (ville, code pays) ===
        pays = "EU"  # default
        text_for_location = soup.get_text() if soup else html
        location_matches = LOCATION_PATTERN.findall(text_for_location)
        for ville, code_pays in location_matches:
            if code_pays in SUPPORTED_COUNTRIES:
                pays = code_pays
                break

        # === Prix : essayer plusieurs patterns ===
        prix = ""
        text_for_prix = soup.get_text() if soup else html
        for pattern in PRIX_PATTERNS:
            prix_match = pattern.search(text_for_prix)
            if prix_match:
                amount = prix_match.group(1).replace(".", "").replace(",", ".")
                # Si la valeur a un sens (entre 1 et 10M)
                try:
                    val = float(amount)
                    if 0 < val < 10_000_000:
                        prix = f"EUR {prix_match.group(1)}"
                        break
                except ValueError:
                    continue

        # === Type de vente : Surplex = enchères industrielles ===
        type_vente = "Enchere industrielle"

        # === source_id : extrait de l'URL (ex: A3-43501-191) ===
        source_id = ""
        id_match = LOT_ID_PATTERN.search(url)
        if id_match:
            source_id = id_match.group(1)

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
            "pays": pays,  # override le default source_pays
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

    # ================================================================
    # Mode hybride niveau 2 : extraction depuis __NEXT_DATA__
    # ================================================================

    def _parse_auction_json(
        self, html: str, url: str,
    ) -> Optional[tuple]:
        """Extrait la liste de lots depuis le JSON __NEXT_DATA__ d'une page
        auction.

        Retourne :
            tuple (results, has_next, total_size) si JSON OK :
                - results : list[dict] (peut etre vide en fin de pagination)
                - has_next : bool depuis lots.hasNext
                - total_size : int depuis lots.totalSize (0 si absent)
            None si JSON absent / schema cassé → caller doit fallback
        """
        soup = BeautifulSoup(html, "html.parser")
        tag = soup.find("script", id="__NEXT_DATA__")
        if not tag or not tag.string:
            logger.warning(
                "[%s] __NEXT_DATA__ absent sur %s, fallback regex",
                self.source_nom, url,
            )
            return None
        try:
            data = json.loads(tag.string)
            lots_block = data["props"]["pageProps"]["lots"]
            results = lots_block["results"]
            if not isinstance(results, list):
                logger.warning(
                    "[%s] __NEXT_DATA__.lots.results pas une liste sur %s",
                    self.source_nom, url,
                )
                return None
            has_next = bool(lots_block.get("hasNext"))
            total_size = int(lots_block.get("totalSize") or 0)
            return results, has_next, total_size
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as e:
            logger.warning(
                "[%s] __NEXT_DATA__ schema inattendu sur %s : %s",
                self.source_nom, url, e,
            )
            return None

    def _lot_to_raw(self, lot: dict) -> tuple:
        """Convertit un objet lot JSON en (raw_dict, url) compatible avec
        BaseScraper.normalize().

        Retourne (None, None) si lot mal-forme (pas d'urlSlug).
        """
        url_slug = (lot.get("urlSlug") or "").strip()
        if not url_slug:
            return None, None
        url = f"{BASE_URL}/l/{url_slug}"

        titre = (lot.get("title") or "").strip()
        description = (lot.get("description") or "").strip()

        # Image (premiere taille dispo via le champ JSON, pas de variantes)
        image_url = ""
        image_obj = lot.get("image")
        if isinstance(image_obj, dict):
            image_url = (image_obj.get("url") or "").strip()

        # Prix : currentBidAmount.cents / 100 -> "EUR 33,000"
        prix = ""
        bid = lot.get("currentBidAmount")
        if isinstance(bid, dict):
            cents = bid.get("cents")
            currency = bid.get("currency") or "EUR"
            if isinstance(cents, (int, float)) and cents > 0:
                amount = cents / 100
                if amount == int(amount):
                    prix = f"{currency} {int(amount):,}"
                else:
                    prix = f"{currency} {amount:,.2f}"

        # Pays : countryCode (lowercase ISO-2) -> uppercase
        pays = ""
        location = lot.get("location")
        if isinstance(location, dict):
            cc = (location.get("countryCode") or "").upper()
            if cc in SUPPORTED_COUNTRIES:
                pays = cc

        # date_fin : endDate (epoch UTC) -> ISO 8601
        date_fin = None
        end_epoch = lot.get("endDate")
        if isinstance(end_epoch, (int, float)) and end_epoch > 0:
            try:
                date_fin = datetime.fromtimestamp(
                    end_epoch, tz=timezone.utc,
                ).isoformat()
            except (ValueError, OSError):
                pass

        source_id = (lot.get("displayId") or "").strip()

        raw = {
            "titre": titre,
            "description": description,
            "prix": prix,
            "image_url": image_url,
            "type_vente": "Enchere industrielle",
            "categorie_native": None,
            "source_id": source_id,
            "pays": pays or "EU",
            "date_fin": date_fin,
        }
        return raw, url

    def run(self) -> Iterator[Annonce]:
        """Mode hybride niveau 2 : parse les lots depuis __NEXT_DATA__ JSON
        sur les pages d'auction. Pas de fetch niveau 3 par defaut.

        - Page /auctions (index) : memoize les auction URLs (inchangé).
        - Page /a/... avec JSON OK : extraction directe + yield Annonces.
        - Page /a/... avec JSON cassé : fallback pipeline standard
          (parse_listing -> fetch detail -> parse_detail).

        Pagination : on stoppe les pages d'une auction des que has_next=False
        (early stop sur __NEXT_DATA__.lots.hasNext). MAX_PAGES_PER_AUCTION
        reste un garde-fou anti-runaway.
        """
        seen_urls: set = set()
        completed_auctions: set = set()
        n_pages = 0
        n_lots_from_json = 0
        n_fallback_lots = 0
        n_yielded = 0
        try:
            for listing_url in self.list_listing_urls():
                # Early-stop : skip les pages d'une auction deja completee
                if "/a/" in listing_url:
                    auction_prefix = listing_url.split("?")[0]
                    if auction_prefix in completed_auctions:
                        continue

                n_pages += 1
                try:
                    html = self.fetch(listing_url)
                except Exception as e:
                    logger.warning(
                        "[%s] echec fetch listing %s : %s",
                        self.source_nom, listing_url, e,
                    )
                    continue

                is_index = (
                    "/auctions" in listing_url
                    and "/a/" not in listing_url
                )

                if is_index:
                    # Memoize auction URLs (side-effect de parse_listing)
                    for _ in self.parse_listing(html, listing_url):
                        pass
                    continue

                # Page auction : lazy-load Airtable dedup une fois
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

                parsed = self._parse_auction_json(html, listing_url)

                if parsed is None:
                    # JSON cassé -> fallback pipeline standard
                    for detail_url in self.parse_listing(html, listing_url):
                        if detail_url in seen_urls:
                            continue
                        seen_urls.add(detail_url)
                        n_fallback_lots += 1
                        try:
                            html_detail = self.fetch(detail_url)
                            raw = self.parse_detail(html_detail, detail_url)
                            yield self.normalize(raw, detail_url)
                            n_yielded += 1
                        except Exception as e:
                            status_code = getattr(
                                getattr(e, "response", None),
                                "status_code", None,
                            )
                            if status_code == 500:
                                logger.info(
                                    "[%s] 500 sur %s (skip)",
                                    self.source_nom, detail_url,
                                )
                            else:
                                logger.warning(
                                    "[%s] echec fallback %s : %s",
                                    self.source_nom, detail_url, e,
                                )
                    continue

                lots_json, has_next, total_size = parsed
                logger.info(
                    "[%s] %s : %d lots / totalSize=%d / hasNext=%s",
                    self.source_nom, listing_url,
                    len(lots_json), total_size, has_next,
                )

                for lot in lots_json:
                    raw, lot_url = self._lot_to_raw(lot)
                    if not raw or not lot_url:
                        continue
                    if lot_url in seen_urls:
                        continue
                    seen_urls.add(lot_url)
                    n_lots_from_json += 1
                    if lot_url in self._existing_urls:
                        continue
                    try:
                        yield self.normalize(raw, lot_url)
                        n_yielded += 1
                    except Exception as e:
                        logger.warning(
                            "[%s] echec normalize %s : %s",
                            self.source_nom, lot_url, e,
                        )

                # Early stop si plus de pages pour cette auction
                if not has_next and "/a/" in listing_url:
                    auction_prefix = listing_url.split("?")[0]
                    completed_auctions.add(auction_prefix)
        finally:
            logger.info(
                "[%s] run() recap : pages=%d | from_json=%d | "
                "fallback_lots=%d | yielded=%d | completed_auctions=%d",
                self.source_nom, n_pages, n_lots_from_json,
                n_fallback_lots, n_yielded, len(completed_auctions),
            )