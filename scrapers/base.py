"""
Machine Alert - Scraper de base
Classe generique dont heritent static.py et dynamic.py
"""

import hashlib
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


@dataclass
class Annonce:
    url: str
    titre: str
    source_nom: str
    pays: str
    type_vente: str = ""
    description: str = ""
    prix: str = ""
    image_url: str = ""
    date_scrappe: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    @property
    def id_unique(self) -> str:
        return hashlib.sha256(self.url.encode()).hexdigest()[:16]
    def to_airtable(self) -> dict:
                return {
                                "url": self.url,
                                "titre": self.titre[:255],
                                "source": self.source_nom,
                                "pays": self.pays,
                                "description": self.description[:500],
                                "prix": self.prix,
                                                "type_vente": self.type_vente,
                    "image_url": self.image_url,
                }
        

class BaseScraper:
    def __init__(self, config: dict):
        self.config = config
        self.nom = config["nom"]
        self.url = config["url"]
        self.pays = config["pays"]
        self.selectors = config.get("selectors", {})
        self.url_prefix = config.get("url_prefix", "")

    def clean_text(self, text: str) -> str:
        if not text:
            return ""
        text = re.sub(r'\s+', ' ', text).strip()
        return text[:255]

    def normalize_url(self, href: str) -> str:
        if not href:
            return ""
        href = href.strip()
        if href.startswith("http"):
            return href
        if href.startswith("//"):
            return "https:" + href
        if href.startswith("/"):
            return self.url_prefix.rstrip("/") + href
        return self.url_prefix.rstrip("/") + "/" + href

    def is_valid_url(self, url: str) -> bool:
        if not url or len(url) < 10:
            return False
        skip = [".css", ".js", ".png", ".jpg", ".svg", ".ico", "javascript:", "mailto:", "#"]
        return not any(s in url for s in skip)

    def scrape(self) -> list:
        raise NotImplementedError
