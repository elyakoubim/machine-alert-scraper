"""
Machine Alert - BaseScraper (v2)

Classe abstraite que tous les scrapers de site doivent etendre.
"""

from __future__ import annotations

import hashlib
import logging
import re
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Iterator, Optional

logger = logging.getLogger(__name__)


CATEGORIES_FAILLINK = frozenset([
    "Immobilier",
    "Machines industrielles",
    "Materiel informatique",
    "Mobilier",
    "Stocks & liquidations",
    "Vehicules",
    "Autre / non classe",
])


@dataclass
class Annonce:
    url: str
    titre: str
    source_nom: str
    pays: str
    description: str = ""
    prix: str = ""
    image_url: str = ""
    type_vente: str = ""
    categorie: str = "Autre / non classe"
    indexed_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    date_publication: Optional[str] = None
    date_fin: Optional[str] = None
    source_id: str = ""

    @property
    def id_unique(self) -> str:
        return hashlib.sha256(self.url.encode("utf-8")).hexdigest()[:16]

    def to_airtable(self) -> dict:
        return {
            "url": self.url,
            "titre": self.titre[:255],
            "source": self.source_nom,
            "pays": self.pays,
            "description": (self.description or "")[:10000],
            "prix": self.prix,
            "type_vente": self.type_vente,
            "image_url": self.image_url,
            "categorie": self.categorie,
            "indexed_at": self.indexed_at,
            "date_publication": self.date_publication,
            "date_fin": self.date_fin,
        }

    def validate(self) -> None:
        if not self.url or not self.url.startswith("http"):
            raise ValueError(f"url invalide : {self.url!r}")
        if not self.titre or len(self.titre.strip()) < 3:
            raise ValueError(f"titre trop court : {self.titre!r}")
        if not re.fullmatch(r"[A-Z]{2}", self.pays):
            raise ValueError(f"pays invalide : {self.pays!r}")
        # Note: on accepte aussi les categories avec accents pour souplesse
        # car certains scrapers peuvent les renvoyer accentuees


class BaseScraper(ABC):
    source_nom: str = ""
    source_pays: str = ""
    base_url: str = ""
    requires_javascript: bool = False
    rate_limit_seconds: float = 2.0
    default_category: str = "Autre / non classe"

    def __init__(self, fetcher=None, llm_extractor=None):
        if not self.source_nom or not self.source_pays or not self.base_url:
            raise ValueError(
                "BaseScraper : source_nom, source_pays et base_url doivent "
                "etre configures dans la sous-classe."
            )
        self._fetcher = fetcher
        self._llm = llm_extractor
        self._last_request_at = 0.0

    @abstractmethod
    def list_listing_urls(self) -> Iterator[str]:
        ...

    @abstractmethod
    def parse_listing(self, html: str, listing_url: str) -> Iterator[str]:
        ...

    @abstractmethod
    def parse_detail(self, html: str, url: str) -> dict:
        ...

    @abstractmethod
    def map_category(self, native_category: Optional[str]) -> str:
        ...

    def fetch(self, url: str) -> str:
        self._respect_rate_limit()
        if self._fetcher is None:
            from scrapers.fetchers import HttpxFetcher, PlaywrightFetcher
            if self.requires_javascript:
                self._fetcher = PlaywrightFetcher()
            else:
                self._fetcher = HttpxFetcher()
        html = self._fetcher.fetch(url)
        self._last_request_at = time.time()
        return html

    def normalize(self, raw: dict, url: str) -> Annonce:
        titre = (raw.get("titre") or "").strip()
        description = (raw.get("description") or "").strip()
        prix = (raw.get("prix") or raw.get("prix_brut") or "").strip()
        image_url = (raw.get("image_url") or "").strip()
        type_vente = (raw.get("type_vente") or "").strip()

        categorie = self.map_category(raw.get("categorie_native"))
        if categorie == self.default_category and self._llm is not None:
            try:
                categorie = self._llm.categorize(
                    titre=titre,
                    description=description,
                )
            except Exception as e:
                logger.warning(
                    "[%s] LLM categorize a echoue pour %s : %s",
                    self.source_nom, url, e,
                )
                categorie = self.default_category

        date_pub = _iso_or_none(
            raw.get("date_publication_brut") or raw.get("date_publication")
        )
        date_fin = _iso_or_none(
            raw.get("date_fin_brut") or raw.get("date_fin")
        )

        source_id = raw.get("source_id") or _source_id_from_url(url)

        annonce = Annonce(
            url=url,
            titre=titre,
            source_nom=self.source_nom,
            pays=self.source_pays,
            description=description,
            prix=prix,
            image_url=image_url,
            type_vente=type_vente,
            categorie=categorie,
            date_publication=date_pub,
            date_fin=date_fin,
            source_id=source_id,
        )
        annonce.validate()
        return annonce

    def run(self) -> Iterator[Annonce]:
        seen_urls = set()
        for listing_url in self.list_listing_urls():
            try:
                html_listing = self.fetch(listing_url)
            except Exception as e:
                logger.warning(
                    "[%s] echec fetch listing %s : %s",
                    self.source_nom, listing_url, e,
                )
                continue

            for detail_url in self.parse_listing(html_listing, listing_url):
                if detail_url in seen_urls:
                    continue
                seen_urls.add(detail_url)
                try:
                    html_detail = self.fetch(detail_url)
                    raw = self.parse_detail(html_detail, detail_url)
                    yield self.normalize(raw, detail_url)
                except Exception as e:
                    logger.warning(
                        "[%s] echec parse %s : %s",
                        self.source_nom, detail_url, e,
                    )
                    continue

    def clean_text(self, text: Optional[str]) -> str:
        if not text:
            return ""
        return re.sub(r"\s+", " ", text).strip()

    def normalize_url(self, href: Optional[str]) -> str:
        if not href:
            return ""
        href = href.strip()
        if href.startswith("http"):
            return href
        if href.startswith("//"):
            return "https:" + href
        base = self.base_url.rstrip("/")
        if href.startswith("/"):
            return base + href
        return base + "/" + href

    def is_valid_annonce_url(self, url: str) -> bool:
        if not url or len(url) < 10:
            return False
        bad = [
            ".css", ".js", ".png", ".jpg", ".jpeg", ".svg", ".ico", ".gif",
            "javascript:", "mailto:", "tel:", "#",
            "/login", "/register", "/account", "/signin", "/signup",
            "/inloggen", "/registreren", "/anmelden",
            "/contact", "/mailinglist", "/newsletter",
            "/info.aspx", "/terms", "/privacy", "/cookies",
        ]
        low = url.lower()
        return not any(b in low for b in bad)

    def _respect_rate_limit(self) -> None:
        elapsed = time.time() - self._last_request_at
        if elapsed < self.rate_limit_seconds:
            time.sleep(self.rate_limit_seconds - elapsed)


def _iso_or_none(s) -> Optional[str]:
    if not s:
        return None
    if isinstance(s, datetime):
        dt = s
    else:
        try:
            from dateutil import parser as dateparser
            dt = dateparser.parse(str(s), dayfirst=True, fuzzy=True)
        except Exception:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


def _source_id_from_url(url: str) -> str:
    m = re.search(r"/(\d{3,})(?:[/_\-?#]|$)", url)
    if m:
        return m.group(1)
    return hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]