"""
Machine Alert — Fetchers
========================

Trois fetchers interchangeables pour récupérer le HTML d'une URL :

    HttpxFetcher        -> requêtes HTTP simples (sites HTML statiques).
                           Rapide, gratuit, 60% des sites passent avec ça.

    PlaywrightFetcher   -> navigateur headless (sites SPA / JS-rendered).
                           Gratuit, ~30% des sites nécessitent ça.

    ScraperApiFetcher   -> proxy anti-bot managé (Cloudflare, captcha).
                           Payant au-delà de 5000 requêtes/mois.
                           Fallback pour ~10% des sites coriaces.

Tous exposent la même interface :
    fetch(url: str) -> str    # renvoie le HTML brut

Un scraper peut en instancier un et le passer au BaseScraper, ou laisser
BaseScraper créer un HttpxFetcher ou PlaywrightFetcher par défaut selon
son attribut `requires_javascript`.
"""

from __future__ import annotations

import logging
import os
from abc import ABC, abstractmethod
from typing import Optional

logger = logging.getLogger(__name__)


# =============================================================================
# Interface commune
# =============================================================================

class BaseFetcher(ABC):
    """Interface commune à tous les fetchers."""

    @abstractmethod
    def fetch(self, url: str) -> str:
        """Récupère le HTML d'une URL. Lève une exception si échec."""
        ...

    def close(self) -> None:
        """Libère les ressources (navigateur, clients HTTP). Optionnel."""
        pass


# =============================================================================
# 1) HttpxFetcher — HTTP simple (défaut pour sites statiques)
# =============================================================================

class HttpxFetcher(BaseFetcher):
    """Fetcher HTTP basé sur httpx.

    Avantages : très rapide, zéro coût, pas de navigateur à lancer.
    Limitations : ne rend pas le JavaScript, peut être bloqué par anti-bot.
    """

    def __init__(
        self,
        user_agent: Optional[str] = None,
        timeout: float = 30.0,
    ):
        import httpx
        ua = user_agent or os.getenv(
            "USER_AGENT",
            "Mozilla/5.0 (compatible; FaillinkBot/1.0; "
            "+https://faillink.be/bot)",
        )
        self._client = httpx.Client(
            headers={"User-Agent": ua},
            timeout=timeout,
            follow_redirects=True,
        )

    def fetch(self, url: str) -> str:
        response = self._client.get(url)
        response.raise_for_status()
        return response.text

    def close(self) -> None:
        self._client.close()


# =============================================================================
# 2) PlaywrightFetcher — Navigateur headless (sites JS)
# =============================================================================

class PlaywrightFetcher(BaseFetcher):
    """Fetcher navigateur basé sur Playwright + stealth.

    Avantages : rend le JavaScript, passe sous les radars anti-bot simples.
    Limitations : 5-10x plus lent que httpx, consomme de la RAM.

    Le navigateur est créé de façon paresseuse (au premier fetch) pour
    éviter un coût d'init si on ne l'utilise jamais.
    """

    def __init__(
        self,
        user_agent: Optional[str] = None,
        timeout_ms: int = 30000,
        use_stealth: bool = True,
    ):
        self._user_agent = user_agent or os.getenv(
            "USER_AGENT",
            "Mozilla/5.0 (compatible; FaillinkBot/1.0; "
            "+https://faillink.be/bot)",
        )
        self._timeout_ms = timeout_ms
        self._use_stealth = use_stealth
        self._playwright = None
        self._browser = None
        self._context = None

    def _ensure_browser(self) -> None:
        """Lance Chromium au premier usage."""
        if self._browser is not None:
            return
        from playwright.sync_api import sync_playwright
        self._playwright = sync_playwright().start()
        self._browser = self._playwright.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-blink-features=AutomationControlled",
            ],
        )
        self._context = self._browser.new_context(
            user_agent=self._user_agent,
            viewport={"width": 1280, "height": 800},
            locale="fr-BE",
        )

    def fetch(self, url: str) -> str:
        self._ensure_browser()
        page = self._context.new_page()
        try:
            if self._use_stealth:
                try:
                    from playwright_stealth import stealth_sync
                    stealth_sync(page)
                except ImportError:
                    logger.debug(
                        "playwright_stealth non installé, on continue sans"
                    )
                except Exception as e:
                    logger.debug("stealth_sync a échoué (non bloquant) : %s", e)

            page.goto(url, timeout=self._timeout_ms, wait_until="domcontentloaded")
            # Petit délai pour laisser le JS hydrater le DOM
            page.wait_for_timeout(1500)
            html = page.content()
            return html
        finally:
            page.close()

    def close(self) -> None:
        try:
            if self._context is not None:
                self._context.close()
        except Exception:
            pass
        try:
            if self._browser is not None:
                self._browser.close()
        except Exception:
            pass
        try:
            if self._playwright is not None:
                self._playwright.stop()
        except Exception:
            pass
        self._context = None
        self._browser = None
        self._playwright = None


# =============================================================================
# 3) ScraperApiFetcher — Fallback anti-bot managé
# =============================================================================

class ScraperApiFetcher(BaseFetcher):
    """Fetcher via ScraperAPI (https://www.scraperapi.com).

    À utiliser UNIQUEMENT quand HttpxFetcher et PlaywrightFetcher se font
    bloquer (Cloudflare strict, captcha, ban d'IP). Coût : des crédits
    sur ton compte ScraperAPI.

    Plan Free : 5000 crédits/mois. Un site avec rendu JS coûte ~10 crédits
    par page, un site simple 1 crédit. Donc ~500 pages JS/mois en gratuit.

    Usage :
        fetcher = ScraperApiFetcher(render_js=False)  # pour sites simples
        fetcher = ScraperApiFetcher(render_js=True)   # pour sites JS+protégés
    """

    BASE_URL = "https://api.scraperapi.com"

    def __init__(
        self,
        api_key: Optional[str] = None,
        render_js: bool = False,
        country_code: Optional[str] = None,
        timeout: float = 90.0,
    ):
        import httpx
        self._api_key = api_key or os.getenv("SCRAPERAPI_KEY")
        if not self._api_key:
            raise RuntimeError(
                "ScraperApiFetcher : SCRAPERAPI_KEY absent. "
                "Définis la variable d'env ou passe api_key=... au constructeur."
            )
        self._render_js = render_js
        self._country_code = country_code
        self._client = httpx.Client(timeout=timeout, follow_redirects=True)

    def fetch(self, url: str) -> str:
        params = {
            "api_key": self._api_key,
            "url": url,
        }
        if self._render_js:
            params["render"] = "true"
        if self._country_code:
            params["country_code"] = self._country_code

        response = self._client.get(self.BASE_URL, params=params)
        # ScraperAPI renvoie 200 avec le HTML de la cible, ou 4xx/5xx si échec
        response.raise_for_status()
        return response.text

    def close(self) -> None:
        self._client.close()


# =============================================================================
# Helper de sélection automatique
# =============================================================================

def make_default_fetcher(requires_javascript: bool = False) -> BaseFetcher:
    """Renvoie le fetcher approprié selon le besoin.

    Par défaut :
        - pas de JS -> HttpxFetcher (rapide)
        - JS requis -> PlaywrightFetcher (gratuit, local)

    Si tu veux forcer ScraperAPI sur un scraper spécifique, instancie-le
    directement dans le __init__ du scraper :
        super().__init__(fetcher=ScraperApiFetcher(render_js=True))
    """
    if requires_javascript:
        return PlaywrightFetcher()
    return HttpxFetcher()