"""
Scraper Faillitimmo (BE) — Insolvency real estate via WordPress
================================================================

Plateforme : Faillitimmo.be — annonces de biens immobiliers issus de
faillites en Belgique, publiees en direct par les Mandataires de Justice.

Stack technique :
   - WordPress 6.9.4 + theme wpresidence (HTML SSR, jQuery basique)
   - PHP 8.5, pas de SPA
   - Volume actif : ~42 biens (3 pages residentiel + 4 pages business)
   - Pas de captcha sur les pages publiques

Strategie : 2 niveaux pattern standard BaseScraper.
1. /residentiel/ + /business/ avec pagination /page/N/ (max 10 par garde-fou)
2. Chaque fiche /properties/{slug}/ : h1, description longue, prix label
   (Faire offre / Faire offre a partir de), image principale, mandataire.

Pas de Haiku : categorie = "Immobilier" en dur (1/8 categories Faillink).
Pas de ScraperAPI : Mohamed home IP suffit (pas d'IP block observe).

Skip "VENDU" :
   - Detecte au niveau listing (presence de "VENDU" dans le HTML du
     .property_listing block via les share-links Facebook/Twitter).
   - Evite le fetch detail inutile pour les biens vendus.
   - Defense en profondeur : si la fiche detail expose un h1 commencant
     par "VENDU", on retourne raw vide (rejete par Annonce.validate).

Mandataire (curateur) : extrait depuis le lien
/mandataire/maitre-{nom-prenom}/ et appendu en fin de description avec
prefixe "Mandataire : Maitre X" pour visibilite cote utilisateur.
"""

from __future__ import annotations

import logging
import re
from typing import Iterator, Optional

import httpx
from bs4 import BeautifulSoup

from scrapers.base import BaseScraper, Annonce
import airtable as airtable_client

logger = logging.getLogger(__name__)


BASE_URL = "https://www.faillitimmo.be"
LISTING_URLS = [
    f"{BASE_URL}/residentiel/",
    f"{BASE_URL}/business/",
]
MAX_PAGES_PER_LISTING = 5   # Garde-fou. Pagination reelle observee : 3 pages.

# Slug d'une URL detail : /properties/{slug}/
SLUG_PATTERN = re.compile(r"/properties/([^/?]+)/?")

# Patterns de filtre images : exclure logos/banners/icons partages
# (NB : on garde 'shutterstock' meme si placeholder, mieux qu'aucune image)
IMAGE_BLACKLIST_KEYWORDS = (
    "banner-", "logo", "icon", "footer-", "header-", "favicon",
    "placeholder", "default", "noimage",
)


class _FaillitimmoFetcher:
    """Fetcher httpx sans brotli (Python 3.14 local + requirements.txt prod
    sans pkg brotli). Headers Chrome standard.
    """

    def __init__(self, timeout: int = 30):
        self.timeout = timeout
        self.headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "fr-BE,fr;q=0.9,nl;q=0.8,en;q=0.7",
            "Accept-Encoding": "gzip, deflate",  # PAS de brotli
        }

    def fetch(self, url: str) -> str:
        with httpx.Client(
            timeout=self.timeout, follow_redirects=True, headers=self.headers,
        ) as client:
            r = client.get(url)
            r.raise_for_status()
            return r.text


class FaillitimmoScraper(BaseScraper):
    source_nom = "Faillitimmo"
    source_pays = "BE"
    base_url = BASE_URL
    requires_javascript = False
    rate_limit_seconds = 1.0
    # Categorie unique : 100% immobilier.
    default_category = "Immobilier"

    def __init__(self, fetcher=None, llm_extractor=None):
        # Le llm_extractor est IGNORE volontairement : categorie hardcodee
        # "Immobilier", pas besoin de Haiku. Economie de credits Anthropic.
        if fetcher is None:
            fetcher = _FaillitimmoFetcher()
        super().__init__(fetcher=fetcher, llm_extractor=None)
        self._existing_urls: Optional[set] = None

    def list_listing_urls(self) -> Iterator[str]:
        """Iterate /residentiel/ + /business/ avec pagination /page/N/."""
        for base_listing in LISTING_URLS:
            for page in range(1, MAX_PAGES_PER_LISTING + 1):
                if page == 1:
                    yield base_listing
                else:
                    yield f"{base_listing}page/{page}/"

    def parse_listing(
        self, html: str, listing_url: str,
    ) -> Iterator[str]:
        """Yield URLs des fiches detail depuis une page liste wpresidence.

        Skip VENDU au niveau listing block (les share-links Facebook
        contiennent "VENDU" dans leur titre quand le bien est vendu).
        """
        soup = BeautifulSoup(html, "html.parser")
        listings = soup.select(".property_listing")
        if not listings:
            logger.debug(
                "[%s] pas de .property_listing sur %s",
                self.source_nom, listing_url,
            )
            return

        # Lazy-load dedup une fois
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

        logger.info(
            "[%s] %s : %d listings dans la page",
            self.source_nom, listing_url, len(listings),
        )

        for listing in listings:
            # Detection VENDU dans le HTML du block (incluant share-links).
            # Evite un fetch detail inutile.
            block_html = str(listing)
            if "VENDU" in block_html:
                a = listing.select_one("a[href*='/properties/']")
                url_for_log = a.get("href", "?") if a else "?"
                logger.info(
                    "[%s] VENDU detecte au listing, skip %s",
                    self.source_nom, url_for_log,
                )
                continue

            # Premier lien /properties/ non-share = URL detail
            for a in listing.select("a[href*='/properties/']"):
                href = (a.get("href") or "").strip()
                if not href:
                    continue
                # Filter share-links (Facebook/Twitter/etc qui contiennent
                # aussi /properties/ dans leur query string &u=...)
                if any(x in href.lower() for x in [
                    "sharer", "twitter.com/intent", "pinterest.com/pin",
                    "whatsapp", "wa.me/", "mailto:", "linkedin.com",
                ]):
                    continue
                full_url = href if href.startswith("http") else BASE_URL + href
                if full_url in self._existing_urls:
                    continue
                yield full_url
                break  # 1 URL detail par listing block

    def parse_detail(self, html: str, url: str) -> dict:
        """Parse fiche detail Faillitimmo, retourne raw dict pour normalize()."""
        soup = BeautifulSoup(html, "html.parser")

        # Titre (h1). Defense en profondeur : skip si VENDU.
        h1 = soup.find("h1")
        h1_text = h1.get_text(" ", strip=True) if h1 else ""
        if h1_text.upper().startswith("VENDU"):
            # Edge case : listing pas filtre. On log + retourne dict vide
            # qui sera rejete par Annonce.validate() (titre trop court).
            logger.info(
                "[%s] %s : VENDU detecte au detail (listing rate ?), skip",
                self.source_nom, url,
            )
            return {"titre": "", "description": ""}

        titre = h1_text

        # Description longue via [class*='descripti'] (theme wpresidence)
        description = ""
        desc_el = soup.select_one("[class*='descripti']")
        if desc_el:
            desc_text = desc_el.get_text(" ", strip=True)
            # Strip prefix inline "Description " ajoute par le theme
            if desc_text.startswith("Description "):
                desc_text = desc_text[len("Description "):]
            description = desc_text

        # Fallback si vide : meta description
        if not description:
            md = soup.find("meta", attrs={"name": "description"})
            if md:
                description = (md.get("content") or "").strip()

        # Fallback ultime si toujours vide : reprendre h1
        if not description:
            description = titre

        # Mandataire (curateur) : extraction depuis le SLUG de l'URL plutot
        # que le text visible. Le theme wpresidence tronque parfois le texte
        # avec text-overflow CSS (observe sur lot 5 Tournai : visible
        # "Maitre Ysabelle Ensch", URL "maitre-ysabelle-enschede"). Le slug
        # est canonique et jamais tronque.
        mandataire_text = ""
        for a in soup.select("a[href*='/mandataire/maitre-']"):
            href = a.get("href") or ""
            m = re.search(r"/mandataire/(maitre-[a-z0-9\-]+)/?", href, re.IGNORECASE)
            if m:
                slug = m.group(1)
                # "maitre-ysabelle-enschede" -> "Maitre Ysabelle Enschede"
                mandataire_text = slug.replace("-", " ").title()
                break

        # Mandataire prioritaire : on reserve l'espace AVANT de tronquer
        # pour garantir que le nom complet du curateur reste intact.
        if mandataire_text:
            mandataire_suffix = f"\n\nMandataire : {mandataire_text}"
            max_desc_chars = 2000 - len(mandataire_suffix)
            description = description[:max_desc_chars] + mandataire_suffix
        else:
            description = description[:2000]

        # Prix (texte brut, pas de parsing) : .price_label puis fallback
        prix = ""
        for sel in [".price_label", ".property_price", ".price_class"]:
            el = soup.select_one(sel)
            if el:
                t = el.get_text(" ", strip=True)
                if t:
                    prix = t
                    break

        # Image : premiere img /wp-content/uploads/ non-banner
        image_url = ""
        for img in soup.select("img[src*='/wp-content/uploads/']"):
            src = (img.get("src") or img.get("data-src") or "").strip()
            if not src:
                continue
            src_lower = src.lower()
            if any(kw in src_lower for kw in IMAGE_BLACKLIST_KEYWORDS):
                continue
            image_url = src
            break

        # source_id : slug from URL
        slug_match = SLUG_PATTERN.search(url)
        source_id = slug_match.group(1) if slug_match else ""

        return {
            "titre": titre,
            "description": description,
            "prix": prix,
            "image_url": image_url,
            "type_vente": "Vente sur offre",
            "categorie_native": None,
            "source_id": source_id,
            # date_fin : laisse None (Faillitimmo n'a pas de date d'echeance)
        }

    def map_category(self, native_category: Optional[str]) -> str:
        """Faillitimmo = 100% immobilier."""
        return "Immobilier"
