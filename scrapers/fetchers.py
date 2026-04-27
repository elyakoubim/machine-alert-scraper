"""
Machine Alert — Fetchers
========================

Trois fetchers interchangeables pour récupérer le HTML d'une URL :

    HttpxFetcher        -> requêtes HTTP simples (sites HTML statiques).
    PlaywrightFetcher   -> navigateur headless (sites SPA / JS-rendered).
                           Utilise async_playwright dans un thread séparé
                           pour éviter le conflit asyncio loop.
    ScraperApiFetcher   -> proxy anti-bot managé.

Tous exposent la même interface :
    fetch(url: str) -> str
"""
from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
from typing import Optional

import httpx

logger = logging.getLogger(__name__)


# ============================================================
# HttpxFetcher
# ============================================================

class HttpxFetcher:
    """Fetcher HTTP simple via httpx (sync)."""

    def __init__(self, timeout: int = 30):
        self.timeout = timeout
        self.headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8",
            "Accept-Encoding": "gzip, deflate, br",
        }

    def fetch(self, url: str) -> str:
        with httpx.Client(timeout=self.timeout, follow_redirects=True, headers=self.headers) as client:
            r = client.get(url)
            r.raise_for_status()
            return r.text


# ============================================================
# PlaywrightFetcher (FIXED : thread + async_playwright)
# ============================================================

class PlaywrightFetcher:
    """
    Fetcher Playwright qui contourne le bug 'sync API in asyncio loop'.
    
    Solution : on lance async_playwright dans un thread séparé avec
    sa propre event loop. Ça permet de garder l'API sync côté scraper.
    """

    def __init__(self, timeout: int = 30000, wait_until: str = "networkidle"):
        self.timeout = timeout
        self.wait_until = wait_until
        self.user_agent = (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        )

    def fetch(self, url: str) -> str:
        """
        Fetch HTML via Playwright async, exécuté dans un thread séparé.
        Ça évite le bug 'sync API inside asyncio loop' qui apparaît quand
        le SDK Anthropic (ou autre) a déjà initialisé une event loop.
        """
        result_container = []
        exception_container = []

        def run_in_thread():
            try:
                from playwright.async_api import async_playwright

                async def _async_fetch():
                    async with async_playwright() as p:
                        browser = await p.chromium.launch(
                            headless=True,
                            args=[
                                "--no-sandbox",
                                "--disable-dev-shm-usage",
                                "--disable-blink-features=AutomationControlled",
                            ],
                        )
                        context = await browser.new_context(
                            user_agent=self.user_agent,
                            locale="fr-FR",
                            viewport={"width": 1920, "height": 1080},
                        )
                        page = await context.new_page()
                        try:
                            await page.goto(url, timeout=self.timeout, wait_until=self.wait_until)
                            html = await page.content()
                            return html
                        finally:
                            await context.close()
                            await browser.close()

                # Nouvelle event loop dédiée au thread
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                try:
                    html = loop.run_until_complete(_async_fetch())
                    result_container.append(html)
                finally:
                    loop.close()

            except Exception as e:
                exception_container.append(e)

        t = threading.Thread(target=run_in_thread, daemon=True)
        t.start()
        t.join(timeout=120)  # max 2 min par fetch

        if t.is_alive():
            raise TimeoutError(f"Playwright fetch timeout after 120s : {url}")

        if exception_container:
            raise exception_container[0]

        if not result_container:
            raise RuntimeError(f"Playwright fetch a renvoyé aucun résultat : {url}")

        return result_container[0]


# ============================================================
# ScraperApiFetcher
# ============================================================

class ScraperApiFetcher:
    """Fetcher via ScraperAPI (proxy anti-bot)."""

    def __init__(self, api_key: Optional[str] = None, timeout: int = 60, render_js: bool = False):
        self.api_key = api_key or os.getenv("SCRAPERAPI_KEY", "")
        if not self.api_key:
            raise ValueError("SCRAPERAPI_KEY manquante (env var ou param)")
        self.timeout = timeout
        self.render_js = render_js
        self.base_url = "https://api.scraperapi.com/"

    def fetch(self, url: str) -> str:
        params = {
            "api_key": self.api_key,
            "url": url,
        }
        if self.render_js:
            params["render"] = "true"

        with httpx.Client(timeout=self.timeout, follow_redirects=True) as client:
            r = client.get(self.base_url, params=params)
            r.raise_for_status()
            return r.text
