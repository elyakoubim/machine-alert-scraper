"""
Scraper Legal Brokers (BE/NL) - v2
====================================

Site Belgique + Pays-Bas. Enchères de véhicules, machines, équipements
industriels, immobilier suite a faillites/liquidations.

Strategie : HTTP statique + httpx (pas d'anti-bot, pas de JS).
1. Pagination sur /list/fr/sea/0a0/{page}/enchere-lot
2. Extrait les URLs /item/fr/{X}/{id}.../{slug}
3. Pour chaque lot, fetch + parse :
   - <h1> ou <title> = titre
   - <img> CDN assets.legalbrokers.eu = image
   - Breadcrumb = categorie native
   - Texte vendeur (Bilzen/Brecht=BE, Breda=NL) = pays

1 ligne Airtable = 1 lot individuel
"""

from __future__ import annotations

import logging
import re
import time
from datetime import datetime, timezone
from typing import Iterator, Optional

import httpx

from scrapers.base import BaseScraper, Annonce
import airtable as airtable_client

logger = logging.getLogger(__name__)


BASE_URL = "https://www.legalbrokers.eu"
# URL liste paginee : /list/fr/sea/0a0/{page}/enchere-lot
# 0a0 = tous secteurs, sea = par defaut, page commence a 1
LIST_URL_TEMPLATE = "https://www.legalbrokers.eu/list/fr/sea/0a0/{page}/enchere-lot"

# Pattern URL d'un lot individuel : /item/fr/{X}/{id}.{a}.{b}.{c}/{slug}
ITEM_URL_PATTERN = re.compile(
    r'href="(/item/fr/\w+/\d+\.\d+\.\d+\.\d+/[^"]+)"',
    re.IGNORECASE
)

# Pattern image du LOT (CDN, exclure banners/interface)
# Pattern : //assets.legalbrokers.eu/legalbrokersprod/img/{X}/{YYYYMM}/l{number}.jpg
LOT_IMAGE_PATTERN = re.compile(
    r'(//assets\.legalbrokers\.eu/legalbrokersprod/img/\d+/\d{6}/l\d+\.(?:jpg|jpeg|png|webp))',
    re.IGNORECASE
)

# Mapping vendeurs -> pays
VENDEUR_PAYS = {
    "bilzen": "BE",
    "brecht": "BE",
    "breda": "NL",
}

# Nombre max de pages a parcourir (securite)
MAX_PAGES = 30


class LegalBrokersScraper(BaseScraper):
    source_nom = "Legal Brokers"
    source_pays = "BE"  # par defaut, override par lot selon vendeur
    base_url = BASE_URL
    requires_javascript = True  # Anti-bot httpx detecte, Playwright passe
    rate_limit_seconds = 1.0
    default_category = "Autre / non classe"

    def __init__(self, fetcher=None, llm_extractor=None):
        super().__init__(fetcher=fetcher, llm_extractor=llm_extractor)
        self._existing_urls = None

    def list_listing_urls(self) -> Iterator[str]:
        """Genere les URLs des pages liste paginees."""
        # On commence par fetch la page 1 pour savoir s'il y a plus
        # On parcourt jusqu'a MAX_PAGES ou jusqu'a une page vide
        for page in range(1, MAX_PAGES + 1):
            yield LIST_URL_TEMPLATE.format(page=page)

    def parse_listing(
        self, html: str, listing_url: str
    ) -> Iterator[str]:
        """Parse la liste et yield les URLs des lots non deja en Airtable."""
        matches = ITEM_URL_PATTERN.findall(html)
        # Dedup
        unique_paths = list(dict.fromkeys(matches))
        logger.info(
            "[%s] page %s : %d lots trouves",
            self.source_nom, listing_url, len(unique_paths),
        )

        if not unique_paths:
            return

        # Charger les URLs deja en Airtable (une seule fois)
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
        Parse une page de detail Legal Brokers.
        Extrait : titre, image, description, pays, type_vente, categorie_native.
        """
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html, "lxml")

        # Titre depuis <h1> en priorite, sinon <title>
        h1 = soup.find("h1")
        if h1:
            titre = self.clean_text(h1.get_text())
        else:
            title_tag = soup.find("title")
            titre = self.clean_text(title_tag.get_text()) if title_tag else ""

        # Image principale du lot (1ere du CDN)
        image_url = ""
        img_match = LOT_IMAGE_PATTERN.search(html)
        if img_match:
            # Force https:// car le pattern est //
            image_url = "https:" + img_match.group(1)

        # Description : meta description + breadcrumb + body text
        meta_desc = soup.find("meta", attrs={"name": "description"})
        description = (
            meta_desc.get("content", "") if meta_desc else ""
        ).strip()

        # Enrichir avec body text
        body_text = ""
        if soup.body:
            # Retire scripts/styles avant extraction
            for tag in soup.body.find_all(["script", "style"]):
                tag.decompose()
            body_text = self.clean_text(soup.body.get_text())[:1500]
        if body_text:
            description = (description + " | " + body_text)[:2000]

        # Pays : depuis le breadcrumb / vendeur dans le body
        pays = "BE"  # default
        body_lower = body_text.lower()
        if "breda" in body_lower or "nl, breda" in body_lower:
            pays = "NL"
        elif "bilzen" in body_lower or "brecht" in body_lower:
            pays = "BE"

        # Type vente : "Enchère lot" si dans titre/breadcrumb
        type_vente = "Enchere lot"
        if "gré à gré" in body_lower or "gre a gre" in body_lower:
            type_vente = "Gre a gre"
        elif "overname" in body_lower:
            type_vente = "Overname"
        elif "immo" in body_lower and "véhicules" not in body_lower:
            type_vente = "Immo"

        # Categorie native : depuis le breadcrumb
        categorie_native = self._extract_categorie_native(body_text)

        # Date_fin : pattern DD-MM-YYYY dans le body
        date_fin = self._parse_date_fin(html)

        return {
            "titre": titre,
            "description": description,
            "image_url": image_url,
            "prix": "",  # legalbrokers n'expose pas de prix sur la page (encheres en cours)
            "type_vente": type_vente,
            "date_fin_brut": date_fin,
            "date_publication_brut": datetime.now(timezone.utc).isoformat(),
            "categorie_native": categorie_native,
            "_pays_override": pays,  # passé au scraper pour override
        }

    def normalize(self, raw: dict, url: str) -> Annonce:
        """Override normalize pour gerer le pays par lot (BE/NL)."""
        # Si parse_detail a determine un pays specifique, on l'utilise
        pays_override = raw.pop("_pays_override", None)
        if pays_override:
            # Hack temporaire : on override self.source_pays le temps de la normalisation
            original = self.source_pays
            self.source_pays = pays_override
            try:
                return super().normalize(raw, url)
            finally:
                self.source_pays = original
        return super().normalize(raw, url)

    def map_category(self, native_category: Optional[str]) -> str:
        """
        Mapping des categories Legal Brokers vers Faillink.
        Si rien ne match, retourne default → LLM Haiku decidera.
        """
        if not native_category:
            return self.default_category

        cat = native_category.lower()

        # Vehicules (et sous-categories : voitures, camionettes, camions...)
        if any(kw in cat for kw in [
            "véhicules", "vehicules", "voitures", "camionettes",
            "camions", "oldtimers", "caravanes", "mobile-homes",
            "moto", "scooter", "vélomoteur", "velomoteur",
            "karting", "quad", "autocar"
        ]):
            return "Vehicules"

        # Materiel informatique
        if any(kw in cat for kw in [
            "matériel informatique", "materiel informatique",
            "informatique", "bureau"
        ]):
            return "Materiel informatique"

        # Immobilier
        if any(kw in cat for kw in [
            "immobilier", "immo", "garage accomodation"
        ]):
            return "Immobilier"

        # Mobilier
        if any(kw in cat for kw in [
            "mobilier", "industrie du bois", "meuble"
        ]):
            return "Mobilier"

        # Stocks & liquidations
        if any(kw in cat for kw in [
            "stocks", "liquidation", "industrie alimentaire",
            "restauration", "textile", "cuire"
        ]):
            return "Stocks & liquidations"

        # Outillage / chantier
        if any(kw in cat for kw in [
            "équipement d'entrepreneur", "equipement d'entrepreneur",
            "industrie minière", "industrie miniere", "terrassement",
            "agricole", "logistic", "logistique"
        ]):
            return "Outillage & equipement chantier"

        # Machines industrielles (par defaut pour industrie XXX)
        if any(kw in cat for kw in [
            "metallurgie", "métallurgie",
            "industrie graphique", "papier",
            "plastic", "caoutchouc",
            "recyclage", "environnement",
            "industrie maritime", "aviation",
            "industrie", "transport"
        ]):
            return "Machines industrielles"

        return self.default_category

    def _extract_categorie_native(self, body_text: str) -> str:
        """
        Extrait la categorie native depuis le breadcrumb.
        Format observe : "Home | Enchère lot Véhicules - Voitures Kia Sorento"
        """
        if not body_text:
            return ""
        # On prend les 300 premiers chars du body apres "Home"
        # qui contiennent generalement le breadcrumb
        match = re.search(
            r"Home\s*\|\s*([^|]+?)(?:ventes aux enchères|cherche|$)",
            body_text, re.I,
        )
        if match:
            return match.group(1).strip()[:200]
        return body_text[:200]

    def _parse_date_fin(self, html: str) -> Optional[str]:
        """
        Parse les dates au format DD-MM-YYYY (format europeen).
        On prend la premiere date trouvee qui est >= aujourd'hui.
        """
        from datetime import date
        today = date.today()
        # Patterns possibles : 17-07-2025 ou 17/07/2025
        date_matches = re.findall(
            r"\b(\d{1,2})[/\-](\d{1,2})[/\-](\d{4})\b", html
        )
        for day, month, year in date_matches:
            try:
                d = date(int(year), int(month), int(day))
                if d >= today:
                    return d.isoformat()
            except ValueError:
                continue
        return None