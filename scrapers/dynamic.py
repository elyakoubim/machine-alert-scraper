"""
Machine Alert — Scraper dynamique
Utilise Playwright pour les sites React/Next.js/Vue
"""

import logging
import asyncio
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout
from bs4 import BeautifulSoup
from scrapers.base import BaseScraper, Annonce
from scrapers.static import StaticScraper

logger = logging.getLogger(__name__)


class DynamicScraper(BaseScraper):
    def __init__(self, config: dict, timeout: int = 30000):
        super().__init__(config)
        self.timeout = timeout  # ms pour Playwright

    def fetch_rendered(self, url: str) -> str | None:
        """Charge la page avec Playwright et retourne le HTML rendu"""
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(
                    headless=True,
                    args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"]
                )
                context = browser.new_context(
                    user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                    locale="fr-BE",
                    viewport={"width": 1280, "height": 800},
                )
                page = context.new_page()
                
                # Bloquer ressources inutiles pour aller plus vite
                page.route("**/*.{png,jpg,jpeg,gif,svg,ico,woff,woff2,ttf}", 
                           lambda route: route.abort())
                page.route("**/analytics/**", lambda route: route.abort())
                page.route("**/gtag/**", lambda route: route.abort())
                
                page.goto(url, wait_until="networkidle", timeout=self.timeout)
                
                # Attendre que le contenu principal soit chargé
                selectors_to_try = [
                    self.selectors.get("items", ""),
                    "article", "main", ".content", "#content"
                ]
                for sel in selectors_to_try:
                    if not sel:
                        continue
                    try:
                        page.wait_for_selector(sel, timeout=5000)
                        break
                    except PlaywrightTimeout:
                        continue
                
                # Scroll pour charger le lazy loading
                page.evaluate("window.scrollTo(0, document.body.scrollHeight / 2)")
                page.wait_for_timeout(1500)
                
                html = page.content()
                browser.close()
                return html
                
        except Exception as e:
            logger.error(f"[{self.nom}] Playwright error for {url}: {e}")
            return None

    def scrape(self) -> list[Annonce]:
        logger.info(f"[{self.nom}] Starting dynamic scrape of {self.url}")
        
        html = self.fetch_rendered(self.url)
        if not html:
            logger.warning(f"[{self.nom}] No HTML returned, trying static fallback")
            static = StaticScraper(self.config)
            return static.scrape()
        
        # Réutiliser la logique d'extraction de StaticScraper
        soup = BeautifulSoup(html, "lxml")
        static = StaticScraper(self.config)
        annonces = static.extract_annonces(soup)
        
        logger.info(f"[{self.nom}] Extracted {len(annonces)} annonces")
        return annonces
