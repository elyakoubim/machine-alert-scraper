"""
Machine Alert — BaseScraper (v2)
================================

Classe abstraite que tous les scrapers de site doivent étendre.

Pipeline standard d'un scraper :
    list_listing_urls()  -> itère les URLs des pages catalogue
    parse_listing(html)  -> itère les URLs des annonces individuelles
    parse_detail(html)   -> renvoie un dict de champs bruts
    map_category(native) -> renvoie une catégorie Faillink (7 valeurs)

Le BaseScraper s'occupe ensuite de :
    - fetcher le HTML (via un Fetcher injecté : Httpx, Playwright, ScraperAPI)
    - normaliser les champs bruts (prix en centimes, dates en UTC)
    - catégoriser l'annonce (via LLMExtractor si la catégorie native ne suffit pas)
    - valider l'objet Annonce avant retour
    - dédupliquer sur l'URL

Auteur : Faillink (Mohamed El Yakoubi)
Licence : Privé
"""

from __future__ import annotations

import hashlib
import logging
import re
import time
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Iterator, Optional

logger = logging.getLogger(__name__)


# =============================================================================
# TAXONOMIE FAILLINK — LISTE FERMÉE (NE PAS MODIFIER SANS VALIDATION)
# =============================================================================
# Ces 7 valeurs DOIVENT correspondre EXACTEMENT aux options Single select
# créées dans Airtable table "Annonces" colonne "categorie".

CATEGORIES_FAILLINK = frozenset([
    "Immobilier",
    "Machines industrielles",
    "Matériel informatique",
    "Mobilier",
    "Stocks & liquidations",
    "Véhicules",
    "Autre / non classé",
])


# =============================================================================
# MODÈLE DE DONNÉES — Annonce
# =============================================================================

@dataclass
class Annonce:
    """Représentation normalisée d'une annonce scrapée.

    Les champs sans default sont OBLIGATOIRES. Les autres sont optionnels
    (None si le site source ne les expose pas).
    """

    # === Obligatoires ===
    url: str                          # URL absolue de la page détail
    titre: str                        # Titre brut tel qu'affiché sur le site
    source_nom: str                   # Slug du site (ex: "proveiling")
    pays: str                         # Code ISO 3166-1 alpha-2 (BE, FR, NL...)

    # === Recommandés (fill when possible) ===
    description: str = ""
    prix: str = ""                    # Prix affiché tel quel (ex: "€ 8.500")
    image_url: str = ""
    type_vente: str = ""              # enchere / vente_directe / liquidation
    categorie: str = "Autre / non classé"  # Taxonomie Faillink

    # === Métadonnées système ===
    indexed_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    date_publication: Optional[str] = None  # ISO 8601 UTC
    date_fin: Optional[str] = None          # ISO 8601 UTC (fin d'enchère)

    # === Champs techniques (non persistés dans Airtable directement) ===
    source_id: str = ""               # ID interne du site (optionnel)

    @property
    def id_unique(self) -> str:
        """Hash stable de l'URL — sert d'identifiant unique inter-sources."""
        return hashlib.sha256(self.url.encode("utf-8")).hexdigest()[:16]

    def to_airtable(self) -> dict:
        """Sérialisation pour Airtable.

        IMPORTANT : ne pas inclure `alerte_envoyee` (géré par Make).
        Les clés doivent matcher EXACTEMENT les noms des colonnes Airtable.
        """
        return {
            "url": self.url,
            "titre": self.titre[:255],  # Airtable Single-line text max
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
        """Vérifie les invariants. Lève ValueError si KO."""
        if not self.url or not self.url.startswith("http"):
            raise ValueError(f"url invalide : {self.url!r}")
        if not self.titre or len(self.titre.strip()) < 3:
            raise ValueError(f"titre trop court : {self.titre!r}")
        if not re.fullmatch(r"[A-Z]{2}", self.pays):
            raise ValueError(
                f"pays doit être ISO 3166-1 alpha-2, reçu : {self.pays!r}"
            )
        if self.categorie not in CATEGORIES_FAILLINK:
            raise ValueError(
                f"categorie hors taxonomie : {self.categorie!r}. "
                f"Valeurs autorisées : {sorted(CATEGORIES_FAILLINK)}"
            )


# =============================================================================
# BASESCRAPER — Classe abstraite
# =============================================================================

class BaseScraper(ABC):
    """Classe de base pour tous les scrapers de site Faillink.

    Configuration par sous-classe (attributs de classe à override) :
        source_nom            slug unique du site, ex "proveiling"
        source_pays           code pays principal, ex "NL"
        base_url              URL racine, ex "https://www.proveiling.nl"
        requires_javascript   True -> Playwright. False -> httpx (plus rapide)
        rate_limit_seconds    délai mini entre 2 requêtes (défaut : 2.0)
        default_category      catégorie Faillink par défaut si rien trouvé

    Méthodes abstraites à implémenter :
        list_listing_urls() -> itère les URLs des pages catalogue
        parse_listing(html) -> itère les URLs des annonces individuelles
        parse_detail(html)  -> renvoie un dict de champs bruts
        map_category(native) -> renvoie une catégorie Faillink
    """

    # --- À configurer par chaque sous-classe ---
    source_nom: str = ""
    source_pays: str = ""
    base_url: str = ""
    requires_javascript: bool = False
    rate_limit_seconds: float = 2.0
    default_category: str = "Autre / non classé"

    def __init__(self, fetcher=None, llm_extractor=None):
        """
        Args:
            fetcher : instance d'un Fetcher (HttpxFetcher, PlaywrightFetcher,
                      ScraperApiFetcher). Si None, un HttpxFetcher sera créé
                      par défaut au premier usage.
            llm_extractor : instance de