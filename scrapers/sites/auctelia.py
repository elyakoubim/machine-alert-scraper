"""
Machine Alert — Scraper Auctelia (BE)
======================================

Site : https://www.auctelia.be
Type : Maison de ventes aux enchères généraliste belge.
Couverture : Faillites, liquidations, ventes volontaires (machines, mobilier,
             véhicules, immobilier, stocks).
Stratégie : HTML statique, parsable avec BeautifulSoup. Pas de JS requis.

POC : ce fichier sert à valider l'architecture du refactor v2 de bout en bout.
      Une fois validé, on en fera des copies adaptées pour les 96 autres sites.

Sélecteurs CSS :
    À ajuster après inspection live (lance `--dry-run --max-annonces 5`
    pour voir si on récupère bien des champs).
"""

from __future__ import annotations

import logging
import re
from typing import Iterator, Optional

from bs4 import BeautifulSoup

from scrapers.base import BaseScraper

logger = logging.getLogger(__name__)


class AucteliaScraper(BaseScraper):
    # === Configuration de classe (lue par BaseScraper et main.py) ===
    source_nom = "auctelia"
    source_pays = "BE"
    base_url = "https://www.auctelia.be"
    requires_javascript = False  # HTML statique
    rate_limit_seconds = 2.0
    default_category = "Autre / non classé"

    # === Constantes propres au scraper ===
    LISTING_PATH = "/fr/encheres"  # URL de la page catalogue principale
    MAX_PAGES = 5  # Limite de pages catalogue à parcourir (sécurité)

    # -------------------------------------------------------------------------
    # 1) Liste des pages catalogue
    # -------------------------------------------------------------------------

    def list_listing_urls(self) -> Iterator[str]:
        """Génère les URLs des pages catalogue d'Auctelia.

        Auctelia paginé classiquement avec ?page=N. On récupère les N premières
        pour rester dans des temps raisonnables.
        """
        for page in range(1, self.MAX_PAGES + 1):
            if page == 1:
                yield f"{self.base_url}{self.LISTING_PATH}"
            else:
                yield f"{self.base_url}{self.LISTING_PATH}?page={page}"

    # -------------------------------------------------------------------------
    # 2) Parse d'une page catalogue -> URLs d'annonces
    # -------------------------------------------------------------------------

    def parse_listing(self, html: str, listing_url: str) -> Iterator[str]:
        """Extrait les URLs des annonces individuelles depuis une page catalogue."""
        soup = BeautifulSoup(html, "lxml")

        # Heuristique : les liens vers des annonces contiennent généralement
        # "/lot/" ou "/vente/" dans leur URL chez Auctelia.
        seen: set[str] = set()
        for a in soup.select("a[href]"):
            href = a.get("href", "")
            url = self.normalize_url(href)

            # Garder uniquement les liens internes vers des annonces
            if not url.startswith(self.base_url):
                continue
            if not (
                "/lot/" in url
                or "/vente/" in url
                or re.search(r"/v\d+/", url)
            ):
                continue
            if not self.is_valid_annonce_url(url):
                continue
            if url in seen:
                continue

            seen.add(url)
            yield url

    # -------------------------------------------------------------------------
    # 3) Parse d'une page détail -> dict de champs bruts
    # -------------------------------------------------------------------------

    def parse_detail(self, html: str, url: str) -> dict:
        """Extrait les champs d'une page annonce détail."""
        soup = BeautifulSoup(html, "lxml")

        titre = self._extract_titre(soup)
        description = self._extract_description(soup)
        prix = self._extract_prix(soup)
        image_url = self._extract_image(soup)
        categorie_native = self._extract_categorie(soup)
        date_fin_brut = self._extract_date_fin(soup)

        # ID natif Auctelia : on tente de l'extraire de l'URL
        m = re.search(r"/(\d{4,})(?:[/_\-]|$)", url)
        source_id = m.group(1) if m else ""

        return {
            "titre": titre,
            "description": description,
            "prix": prix,
            "image_url": image_url,
            "type_vente": "enchere",  # Auctelia = maison d'enchères
            "categorie_native": categorie_native,
            "date_fin_brut": date_fin_brut,
            "source_id": source_id,
        }

    # -------------------------------------------------------------------------
    # 4) Mapping de catégorie native -> taxonomie Faillink
    # -------------------------------------------------------------------------

    def map_category(self, native_category: Optional[str]) -> str:
        """Mappe la catégorie affichée par Auctelia vers les 7 catégories Faillink.

        Si pas de match, retourne default_category et BaseScraper utilisera
        le fallback LLM Haiku automatiquement.
        """
        if not native_category:
            return self.default_category

        nat = native_category.lower().strip()

        # Mots-clés -> catégorie Faillink
        rules = [
            (["immobilier", "bâtiment", "batiment", "terrain", "appartement"],
             "Immobilier"),
            (["machine", "industriel", "outil", "atelier", "production"],
             "Machines industrielles"),
            (["informatique", "ordinateur", "serveur", "it", "réseau", "reseau"],
             "Matériel informatique"),
            (["mobilier", "bureau", "horeca", "rayonnage"],
             "Mobilier"),
            (["stock", "liquidation", "palette", "lot"],
             "Stocks & liquidations"),
            (["véhicule", "vehicule", "voiture", "camion", "utilitaire", "moto"],
             "Véhicules"),
        ]

        for keywords, faillink_cat in rules:
            if any(kw in nat for kw in keywords):
                return faillink_cat

        return self.default_category

    # -------------------------------------------------------------------------
    # Helpers privés (extraction des champs depuis le HTML)
    # -------------------------------------------------------------------------

    def _extract_titre(self, soup: BeautifulSoup) -> str:
        """Cherche le titre de l'annonce dans plusieurs sélecteurs candidats."""
        # Tentatives par ordre de spécificité décroissante
        selectors = [
            "h1.lot-title",
            "h1.product-title",
            "h1[itemprop='name']",
            "h1",
            "title",
        ]
        for sel in selectors:
            el = soup.select_one(sel)
            if el:
                txt = self.clean_text(el.get_text())
                if txt:
                    return txt
        return ""

    def _extract_description(self, soup: BeautifulSoup) -> str:
        """Description longue de l'annonce."""
        selectors = [
            "div.lot-description",
            "div.product-description",
            "div[itemprop='description']",
            "section.description",
            "meta[name='description']",
        ]
        for sel in selectors:
            el = soup.select_one(sel)
            if not el:
                continue
            if el.name == "meta":
                content = el.get("content", "")
                if content:
                    return self.clean_text(content)
            else:
                txt = self.clean_text(el.get_text(separator=" "))
                if txt:
                    return txt
        return ""

    def _extract_prix(self, soup: BeautifulSoup) -> str:
        """Prix actuel ou prix de départ."""
        selectors = [
            "span.current-bid",
            "span.lot-price",
            "[itemprop='price']",
            "div.bid-amount",
        ]
        for sel in selectors:
            el = soup.select_one(sel)
            if el:
                txt = self.clean_text(el.get_text())
                if txt:
                    return txt
        return ""

    def _extract_image(self, soup: BeautifulSoup) -> str:
        """URL de l'image principale."""
        # Priorité à la balise og:image (souvent la plus fiable)
        og = soup.select_one("meta[property='og:image']")
        if og and og.get("content"):
            return self.normalize_url(og["content"])

        # Fallback : première grosse image dans la zone produit
        for sel in ["img.lot-image", "img.product-image", "div.lot-gallery img"]:
            img = soup.select_one(sel)
            if img and img.get("src"):
                return self.normalize_url(img["src"])

        return ""

    def _extract_categorie(self, soup: BeautifulSoup) -> str:
        """Catégorie native (breadcrumb ou tag)."""
        # Breadcrumb : on prend le dernier (le plus spécifique)
        breadcrumb_items = soup.select("nav.breadcrumb a, ol.breadcrumb a")
        if breadcrumb_items:
            return self.clean_text(breadcrumb_items[-1].get_text())

        # Tag/badge de catégorie
        for sel in ["span.lot-category", "div.product-category", "a.category"]:
            el = soup.select_one(sel)
            if el:
                return self.clean_text(el.get_text())

        return ""

    def _extract_date_fin(self, soup: BeautifulSoup) -> str:
        """Date de fin d'enchère (sera parsée en ISO par BaseScraper)."""
        selectors = [
            "[itemprop='endDate']",
            "time.lot-end-date",
            "span.end-date",
            "div.countdown",
        ]
        for sel in selectors:
            el = soup.select_one(sel)
            if el:
                # Préférer l'attribut datetime si présent
                dt = el.get("datetime")
                if dt:
                    return dt
                txt = self.clean_text(el.get_text())
                if txt:
                    return txt
        return ""