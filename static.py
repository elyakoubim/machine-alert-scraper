"""
Machine Alert — Scraper HTML statique
Utilise requests + BeautifulSoup pour les sites sans JavaScript
"""

import logging
import requests
from bs4 import BeautifulSoup
from scrapers.base import BaseScraper, Annonce

logger = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept-Language": "fr-BE,fr;q=0.9,nl;q=0.8,en;q=0.7",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}


class StaticScraper(BaseScraper):
    def __init__(self, config: dict, timeout: int = 30):
        super().__init__(config)
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update(HEADERS)

    def fetch(self, url: str) -> BeautifulSoup | None:
        try:
            resp = self.session.get(url, timeout=self.timeout, allow_redirects=True)
            resp.raise_for_status()
            return BeautifulSoup(resp.text, "lxml")
        except Exception as e:
            logger.error(f"[{self.nom}] Fetch error for {url}: {e}")
            return None

    def extract_annonces(self, soup: BeautifulSoup) -> list[Annonce]:
        annonces = []
        items_sel = self.selectors.get("items", "article")
        url_sel = self.selectors.get("url", "a")
        titre_sel = self.selectors.get("titre", "h2, h3")
        type_sel = self.selectors.get("type_vente", "")

        items = soup.select(items_sel)
        logger.info(f"[{self.nom}] Found {len(items)} items with selector '{items_sel}'")

        if not items:
            # Fallback : chercher tous les liens avec un contexte
            return self._fallback_extract(soup)

        for item in items:
            try:
                # URL
                url_el = item.select_one(url_sel) if url_sel != "self" else item
                href = url_el.get("href", "") if url_el else ""
                url = self.normalize_url(href)
                if not self.is_valid_url(url):
                    continue

                # Titre
                titre_el = item.select_one(titre_sel)
                titre = self.clean_text(titre_el.get_text()) if titre_el else ""
                if not titre:
                    titre = self.clean_text(item.get_text())[:100]
                if not titre or len(titre) < 3:
                    continue

                # Type de vente
                type_vente = ""
                if type_sel:
                    type_el = item.select_one(type_sel)
                    type_vente = self.clean_text(type_el.get_text()) if type_el else ""

                annonces.append(Annonce(
                    url=url,
                    titre=titre,
                    source_nom=self.nom,
                    pays=self.pays,
                    type_vente=type_vente,
                ))
            except Exception as e:
                logger.debug(f"[{self.nom}] Error extracting item: {e}")

        return annonces

    def _fallback_extract(self, soup: BeautifulSoup) -> list[Annonce]:
        """Fallback : extrait tous les liens ayant un texte significatif"""
        annonces = []
        seen = set()
        
        for a in soup.find_all("a", href=True):
            url = self.normalize_url(a["href"])
            if not self.is_valid_url(url) or url in seen:
                continue
            if url == self.url or url == self.url + "/":
                continue
                
            titre = self.clean_text(a.get_text())
            if not titre or len(titre) < 5 or len(titre) > 200:
                continue
            
            # Filtrer les liens de navigation
            skip_words = ["accueil", "contact", "connexion", "login", "menu", 
                         "search", "recherche", "newsletter", "cookie", "cgv"]
            if any(w in titre.lower() for w in skip_words):
                continue

            seen.add(url)
            annonces.append(Annonce(
                url=url,
                titre=titre,
                source_nom=self.nom,
                pays=self.pays,
            ))
            
            if len(annonces) >= 50:
                break

        logger.info(f"[{self.nom}] Fallback extracted {len(annonces)} links")
        return annonces

    def scrape(self) -> list[Annonce]:
        logger.info(f"[{self.nom}] Starting static scrape of {self.url}")
        soup = self.fetch(self.url)
        if not soup:
            return []
        annonces = self.extract_annonces(soup)
        logger.info(f"[{self.nom}] Extracted {len(annonces)} annonces")
        return annonces
