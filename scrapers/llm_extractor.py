"""
Machine Alert — LLMExtractor (Claude Haiku)
============================================

Module de catégorisation intelligente des annonces via Claude Haiku 4.5.

Utilisation principale : assigner une catégorie Faillink à chaque annonce
scrapée quand le mapping natif (site source -> Faillink) ne suffit pas.

Coût typique : ~$0.001 par annonce catégorisée avec Haiku 4.5.
Sur 10 000 annonces/jour, coût mensuel ~$3.

Usage :
    from scrapers.llm_extractor import LLMExtractor
    llm = LLMExtractor()
    categorie = llm.categorize(
        titre="Chariot élévateur Linde H30 - 2019",
        description="Chariot thermique 3T, 4500h, état correct...",
    )
    # -> "Véhicules" ou "Machines industrielles" selon contexte

Sécurité de coût :
    - Appels ciblés uniquement (fallback, pas systématique)
    - Timeout strict (15s)
    - Retry limité (1 retry max)
    - Pas d'historique conversationnel (pas de cache état)
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Optional

logger = logging.getLogger(__name__)


# Liste fermée des catégories Faillink — DOIT matcher base.py et Airtable
CATEGORIES = [
    "Immobilier",
    "Machines industrielles",
    "Matériel informatique",
    "Mobilier",
    "Stocks & liquidations",
    "Véhicules",
    "Autre / non classé",
]


# =============================================================================
# Prompt système — ancrage de l'IA sur la taxonomie Faillink
# =============================================================================

SYSTEM_PROMPT = """Tu es un assistant de classification d'annonces d'enchères et de liquidations.

Tu classes chaque annonce dans UNE SEULE des catégories suivantes (liste fermée) :

1. Immobilier
   → Bâtiments, entrepôts, bureaux, terrains, hangars, appartements, maisons.

2. Machines industrielles
   → Tours, fraiseuses, presses, lasers, compresseurs, robots industriels,
     chariots élévateurs/transpalettes, machines d'imprimerie, machines bois,
     machines plastique, lignes de production.

3. Matériel informatique
   → Serveurs, ordinateurs, écrans, réseau, baies de datacenter, matériel IT.

4. Mobilier
   → Mobilier de bureau, mobilier horeca, rayonnages, stockage, mobilier
     de magasin, mobilier d'atelier.

5. Stocks & liquidations
   → Palettes de marchandises, surstocks, fins de série, lots mixtes,
     marchandises neuves en liquidation, vêtements, cosmétiques.

6. Véhicules
   → Voitures, camions, utilitaires, remorques, engins de chantier, motos,
     véhicules de loisirs.

7. Autre / non classé
   → Si l'annonce ne rentre dans AUCUNE catégorie ci-dessus, ou si le titre
     est trop vague pour décider (ex: "Lot divers", "Véhicule et autre").

Règles importantes :
- Les chariots élévateurs et engins de chantier -> "Machines industrielles"
  si industriels (usine, entrepôt), "Véhicules" si routiers (camion grue).
- Un bâtiment industriel vide -> "Immobilier".
- Un lot mélangeant machines + stocks -> "Machines industrielles" si les
  machines dominent, sinon "Stocks & liquidations".
- En cas de doute réel, réponds "Autre / non classé".

Tu réponds UNIQUEMENT en JSON strict de cette forme :
{"categorie": "<une des 7 valeurs exactes>"}

Pas de texte avant ni après, pas de markdown, pas d'explication."""


# =============================================================================
# LLMExtractor
# =============================================================================

class LLMExtractor:
    """Wrapper autour de l'API Anthropic pour catégorisation d'annonces.

    Initialisé une seule fois par run du scraper (réutilise le client HTTP).
    Thread-safe pour un usage séquentiel.
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        model: Optional[str] = None,
        max_tokens: int = 80,
        timeout: float = 15.0,
    ):
        """
        Args:
            api_key : clé API Anthropic. Si None, lue depuis ANTHROPIC_API_KEY.
            model : modèle à utiliser. Défaut lu depuis ANTHROPIC_MODEL,
                    sinon "claude-haiku-4-5-20251001".
            max_tokens : max tokens dans la réponse. 80 suffit largement
                         pour un JSON {"categorie": "xxx"}.
            timeout : timeout de la requête HTTP en secondes.
        """
        self._api_key = api_key or os.getenv("ANTHROPIC_API_KEY")
        if not self._api_key:
            raise RuntimeError(
                "LLMExtractor : ANTHROPIC_API_KEY absent. "
                "Définis la variable d'env ou passe api_key=... au constructeur."
            )
        self._model = model or os.getenv(
            "ANTHROPIC_MODEL", "claude-haiku-4-5-20251001"
        )
        self._max_tokens = max_tokens
        self._timeout = timeout
        self._client = None  # Lazy init

    def _get_client(self):
        """Lazy init du client Anthropic (évite import coûteux si non utilisé)."""
        if self._client is None:
            from anthropic import Anthropic
            self._client = Anthropic(
                api_key=self._api_key,
                timeout=self._timeout,
            )
        return self._client

    # -------------------------------------------------------------------------
    # Méthode principale
    # -------------------------------------------------------------------------

    def categorize(
        self,
        titre: str,
        description: str = "",
        categorie_native: str = "",
    ) -> str:
        """Classe une annonce dans une des 7 catégories Faillink.

        Args:
            titre : titre de l'annonce (obligatoire)
            description : description détaillée (optionnelle mais recommandée)
            categorie_native : catégorie affichée par le site source (indice)

        Returns:
            Une des valeurs de CATEGORIES. "Autre / non classé" en cas d'échec.
        """
        if not titre or len(titre.strip()) < 3:
            logger.debug("LLM categorize : titre trop court, fallback")