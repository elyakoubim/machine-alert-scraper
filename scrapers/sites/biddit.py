"""
Scraper Biddit (BE) — Notarial real estate auctions
=====================================================

Plateforme : Biddit.be (Fednot — Fédération Royale du Notariat Belge)
Type : ventes immobilières aux enchères publiques notariales en Belgique
Volume estimé : ~1 450 biens actifs (totalElements de l'API au 2026-05-15)

Architecture : SPA Angular avec API JSON publique
   GET /api/eco/search-service/lot/_search?page=N&size=20
   → Spring Pageable response (totalElements, totalPages, last, etc.)
   → Pas d'auth, pas de captcha, pas de ScraperAPI requis

Strategie : httpx pur + API JSON. **Pas de niveau 3** : la search API
expose deja tous les champs utiles (titre, prix, image, address, surfaces,
chambres, DPE, etc.). Pas d'appel Haiku non plus : categorie = "Immobilier"
en dur (1/8 categories Faillink), pas de doute possible pour ce site.

Pagination : `?page=N` 1-indexed (page=0 retourne identique a page=1
par quirk Spring), `size` cappe a 20 cote serveur (force, impossible
d'augmenter). 73 fetches pour ~1450 lots.

1 ligne Airtable = 1 lot individuel.
"""

from __future__ import annotations

import logging
from typing import Iterator, Optional

import httpx

from scrapers.base import BaseScraper, Annonce
import airtable as airtable_client

logger = logging.getLogger(__name__)


BASE_URL = "https://www.biddit.be"
API_URL = f"{BASE_URL}/api/eco/search-service/lot/_search"

# Cappe par le serveur — impossible d'augmenter via ?size=N
PAGE_SIZE = 20
# Garde-fou anti-runaway. totalPages reel ~73, on s'arrete tot via
# response["last"] == True.
MAX_PAGES = 100

# Mapping handlingMethod -> type_vente lisible
HANDLING_METHOD_MAP = {
    "ONLINE_PUBLIC_SALE": "Vente publique en ligne",
    "ONLINE_PRIVATE_SALE": "Vente de gré à gré en ligne",
    "MUTUAL_AGREEMENT": "De gré à gré",
    "VIAGER": "Viager",
}


class _BidditFetcher:
    """Fetcher httpx minimal sans brotli (Python 3.14 local sans pkg brotli,
    et pas dans requirements.txt prod non plus). Headers Chrome standard.
    """

    def __init__(self, timeout: int = 30):
        self.timeout = timeout
        self.headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            "Accept": "application/json, text/plain, */*",
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

    def get_json(self, url: str, params: Optional[dict] = None) -> dict:
        """Helper API-specifique : retourne JSON directement."""
        with httpx.Client(
            timeout=self.timeout, follow_redirects=True, headers=self.headers,
        ) as client:
            r = client.get(url, params=params)
            r.raise_for_status()
            return r.json()


class BidditScraper(BaseScraper):
    source_nom = "Biddit"
    source_pays = "BE"
    base_url = BASE_URL
    requires_javascript = False
    rate_limit_seconds = 1.0
    # Categorie unique : pas de doute possible sur Biddit (uniquement immobilier).
    # On override map_category() pour retourner "Immobilier" tel quel, sans
    # passer par le fallback LLM de BaseScraper.normalize().
    default_category = "Immobilier"

    def __init__(self, fetcher=None, llm_extractor=None):
        # Le llm_extractor est IGNORE volontairement : categorie hardcodee
        # "Immobilier", pas besoin de Haiku. Economie de credits Anthropic.
        if fetcher is None:
            fetcher = _BidditFetcher()
        super().__init__(fetcher=fetcher, llm_extractor=None)
        self._existing_urls: Optional[set] = None
        self._fetcher_typed = fetcher  # acces direct au _BidditFetcher pour get_json

    # =========================================================
    # Methodes abstract BaseScraper : stubs (on override run())
    # =========================================================

    def list_listing_urls(self) -> Iterator[str]:
        """Stub : non utilise (run() override fait son propre loop API)."""
        return
        yield  # type: ignore[unreachable]

    def parse_listing(self, html: str, listing_url: str) -> Iterator[str]:
        """Stub : non utilise."""
        return
        yield  # type: ignore[unreachable]

    def parse_detail(self, html: str, url: str) -> dict:
        """Stub : non utilise."""
        return {}

    def map_category(self, native_category: Optional[str]) -> str:
        """Biddit = 100% immobilier. Toujours 'Immobilier'."""
        return "Immobilier"

    # =========================================================
    # Mapping JSON API -> raw dict
    # =========================================================

    @staticmethod
    def _pick_lang(multilang: Optional[dict], *fallback_keys: str) -> str:
        """Pour un dict multilangue {fr, nl, de, en}, retourne fr > nl > en (ou autre fallback)."""
        if not multilang or not isinstance(multilang, dict):
            return ""
        for k in ("fr", "nl", "en", "de", *fallback_keys):
            v = multilang.get(k)
            if v:
                return v.strip()
        return ""

    @staticmethod
    def _format_prix(content: dict, titre: str, ref_code: str) -> str:
        """Format prix avec fallbacks currentPrice > startingPrice > sellingPrice.

        Convention Biddit : sellingPrice == 1.0 = "Sur demande" (prix non publie).
        """
        prix_value = (
            content.get("currentPrice")
            or content.get("startingPrice")
            or content.get("sellingPrice")
        )
        if prix_value == 1.0:
            logger.info(
                "[Biddit] %s : prix=1.0 traite comme 'Sur demande' (ref=%s)",
                titre[:60], ref_code,
            )
            return "Sur demande"
        if prix_value and prix_value > 1:
            if prix_value == int(prix_value):
                return f"EUR {int(prix_value):,}"
            return f"EUR {prix_value:,.2f}"
        return ""

    def _build_description(self, property_obj: dict) -> str:
        """Construit une description structuree depuis les champs property.

        Format : "TYPE | rue, postalCode municipality | Chambres: N | ... | PEB: X"
        """
        parts: list = []

        # Type + sous-type
        ptype = property_obj.get("propertyType") or ""
        psubtype = property_obj.get("propertySubtype") or ""
        if ptype and psubtype and ptype != psubtype:
            parts.append(f"{ptype} ({psubtype})")
        elif ptype:
            parts.append(ptype)

        # Address : street, postalCode + municipality
        addr = property_obj.get("address") or {}
        street = self._pick_lang(addr.get("street"))
        muni = self._pick_lang(addr.get("municipality"))
        postal = addr.get("postalCode") or ""
        estate_num = addr.get("estateNumber") or ""
        estate_box = addr.get("estateBoxNumber") or ""
        addr_parts: list = []
        if street:
            street_full = street
            if estate_num:
                street_full = f"{street} {estate_num}"
                if estate_box:
                    street_full = f"{street_full}/{estate_box}"
            addr_parts.append(street_full)
        if postal or muni:
            addr_parts.append(f"{postal} {muni}".strip())
        if addr_parts:
            parts.append(", ".join(addr_parts))

        # Surfaces (m²)
        for label, key in [
            ("Surface habitable", "livingSurfaceArea"),
            ("Terrain", "terrainSurface"),
            ("Commerce", "businessSurface"),
            ("Garage", "garageSurface"),
        ]:
            v = property_obj.get(key)
            if v:
                if v == int(v):
                    parts.append(f"{label}: {int(v)} m²")
                else:
                    parts.append(f"{label}: {v} m²")

        # Pieces
        for label, key in [
            ("Chambres", "numberOfBedrooms"),
            ("Salles de bain", "numberOfBathrooms"),
            ("Façades", "numberOfFacades"),
        ]:
            v = property_obj.get(key)
            if v:
                parts.append(f"{label}: {v}")

        # Annee construction
        cy = property_obj.get("constructionYear")
        if cy:
            parts.append(f"Année construction: {cy}")

        # PEB par region (premier non-null : Bruxelles > Wallonie > Flandre)
        for region_label, key in [
            ("Bruxelles", "energeticClassRBC"),
            ("Wallonie", "energeticClassRW"),
            ("Flandre", "energeticClassRF"),
        ]:
            v = property_obj.get(key)
            if v:
                clean_v = v.replace("CLASS_", "").replace("_M", "-")
                parts.append(f"PEB ({region_label}): {clean_v}")
                break

        return " | ".join(parts)[:2000]

    def _map_lot_to_annonce(self, lot_wrapper: dict) -> Optional[Annonce]:
        """Convertit un wrapper lot JSON en Annonce. None si lot a skipper."""
        content = lot_wrapper.get("content") or {}

        # Skip si retire
        if content.get("withdrawn"):
            return None

        ref_code = content.get("referenceCode")
        if not ref_code:
            logger.warning(
                "[%s] lot sans referenceCode : %s",
                self.source_nom, content.get("lotId", "?"),
            )
            return None

        properties = content.get("properties") or []
        if not properties:
            logger.warning(
                "[%s] %s : properties vide, skip", self.source_nom, ref_code,
            )
            return None
        p = properties[0]

        # url canonique
        url = f"{BASE_URL}/fr/catalog/detail/{ref_code}"

        # Titre : title.fr > title.nl > title.en > fallback propertyType + municipality
        title_obj = p.get("title")
        titre = self._pick_lang(title_obj)
        if not titre:
            ptype = p.get("propertyType") or "Bien"
            muni_fr = self._pick_lang((p.get("address") or {}).get("municipality"))
            titre = f"{ptype} - {muni_fr}".strip(" -")
        if not titre:
            titre = f"Biddit ref {ref_code}"  # filet de securite

        # Description structuree
        description = self._build_description(p)

        # Prix
        prix = self._format_prix(content, titre, ref_code)

        # Image
        pic = p.get("picture") or {}
        image_url = (pic.get("bucketUrlMedium") or "").strip() if isinstance(pic, dict) else ""

        # Type de vente
        hm = content.get("handlingMethod") or ""
        type_vente = (
            HANDLING_METHOD_MAP.get(hm)
            or (hm.replace("_", " ").title() if hm else "Vente")
        )

        # date_fin : peut etre None (ventes privees)
        date_fin = content.get("biddingEndDateTime")

        raw = {
            "titre": titre,
            "description": description,
            "prix": prix,
            "image_url": image_url,
            "type_vente": type_vente,
            "categorie_native": None,
            "source_id": ref_code,
            "date_fin": date_fin,
        }
        try:
            return self.normalize(raw, url)
        except Exception as e:
            logger.warning(
                "[%s] %s : normalize echec : %s",
                self.source_nom, ref_code, e,
            )
            return None

    # =========================================================
    # Run override : loop API direct
    # =========================================================

    def run(self) -> Iterator[Annonce]:
        """Loop API : GET /api/eco/search-service/lot/_search?page=N&size=20.

        Yield des Annonces deja normalisees. Pas de fetch niveau detail, pas
        d'appel Haiku. Stop early sur response.last == True.
        """
        seen_urls: set = set()
        n_pages = 0
        n_yielded = 0
        n_skipped = 0
        total_in_api = None

        # Lazy-load dedup (1 seul GET paginate)
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

        try:
            for page in range(1, MAX_PAGES + 1):
                self._respect_rate_limit()
                try:
                    data = self._fetcher_typed.get_json(
                        API_URL, params={"page": page, "size": PAGE_SIZE},
                    )
                except Exception as e:
                    logger.warning(
                        "[%s] page=%d : echec fetch API : %s",
                        self.source_nom, page, e,
                    )
                    continue
                import time
                self._last_request_at = time.time()

                n_pages += 1
                if total_in_api is None:
                    total_in_api = data.get("totalElements")
                    logger.info(
                        "[%s] API totalElements=%s totalPages=%s",
                        self.source_nom, total_in_api, data.get("totalPages"),
                    )

                content_list = data.get("content") or []
                if not content_list:
                    logger.info(
                        "[%s] page=%d : 0 lots, stop", self.source_nom, page,
                    )
                    break

                for lot_wrapper in content_list:
                    content = lot_wrapper.get("content") or {}
                    ref_code = content.get("referenceCode")
                    if not ref_code:
                        continue
                    url = f"{BASE_URL}/fr/catalog/detail/{ref_code}"
                    if url in seen_urls:
                        continue
                    seen_urls.add(url)
                    if url in self._existing_urls:
                        n_skipped += 1
                        continue
                    annonce = self._map_lot_to_annonce(lot_wrapper)
                    if annonce is not None:
                        yield annonce
                        n_yielded += 1
                    else:
                        n_skipped += 1

                # Early-stop : page derniere annoncee par l'API
                if data.get("last") is True:
                    logger.info(
                        "[%s] page=%d : last=True, fin pagination",
                        self.source_nom, page,
                    )
                    break
        finally:
            logger.info(
                "[%s] run() recap : pages=%d | yielded=%d | skipped=%d | total_in_api=%s",
                self.source_nom, n_pages, n_yielded, n_skipped, total_in_api,
            )
