"""
Machine Alert — Client Airtable (v2)
======================================

Interface avec la base Airtable "Machine Alert MVP" :
    - Table Annonces  : push des annonces scrapées (13 colonnes)
    - Table Sources   : mise à jour du statut des runs

IMPORTANT — SÉCURITÉ :
    Le champ `alerte_envoyee` de la table Annonces est géré par Make.
    Ce module NE DOIT JAMAIS l'écrire, sinon on risque de re-déclencher
    des alertes déjà envoyées ou d'annuler des alertes en cours.

Rate limiting :
    Airtable impose 5 req/sec/base. Le batch de 10 records par POST
    permet de rester largement sous la limite.
"""

from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timezone
from typing import Iterable, Optional

import requests

from scrapers.base import Annonce

logger = logging.getLogger(__name__)


# =============================================================================
# Configuration depuis l'environnement
# =============================================================================

AIRTABLE_TOKEN = os.getenv("AIRTABLE_TOKEN")
BASE_ID = os.getenv("AIRTABLE_BASE_ID", "appQrNOm3Q7D9uEed")

# IDs réels des tables (vérifiés le 2026-04-24 via inspection directe)
ANNONCES_TABLE = os.getenv("AIRTABLE_ANNONCES_TABLE", "tbljV9HICBsPyqjQk")
SOURCES_TABLE = os.getenv("AIRTABLE_SOURCES_TABLE", "tblPaOrQekEMdaW5x")
PROFILS_TABLE = os.getenv("AIRTABLE_PROFILS_TABLE", "tblXhuS0M1TtRuSel")

API_BASE = f"https://api.airtable.com/v0/{BASE_ID}"

# Timeout des requêtes HTTP
HTTP_TIMEOUT = 20.0


def _headers() -> dict:
    """Construit les headers avec le token courant (lu à chaque appel
    pour supporter un changement de token sans redémarrer le process)."""
    token = os.getenv("AIRTABLE_TOKEN") or AIRTABLE_TOKEN
    if not token:
        raise RuntimeError(
            "airtable : AIRTABLE_TOKEN absent. Définis-le dans Railway "
            "Variables ou en local via .env."
        )
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }


# =============================================================================
# Lecture : déduplication
# =============================================================================

def get_existing_urls(source_nom: str) -> set[str]:
    """Récupère toutes les URLs déjà en base pour une source donnée.

    Sert à éviter de re-créer des records qui existent déjà.
    Attention : pagination Airtable par 100 max, on itère jusqu'à épuisement.
    """
    urls: set[str] = set()
    offset: Optional[str] = None
    page = 0

    while True:
        page += 1
        params = {
            "filterByFormula": f'{{source}}="{_escape_formula_value(source_nom)}"',
            "fields[]": "url",
            "pageSize": 100,
        }
        if offset:
            params["offset"] = offset

        try:
            resp = requests.get(
                f"{API_BASE}/{ANNONCES_TABLE}",
                headers=_headers(),
                params=params,
                timeout=HTTP_TIMEOUT,
            )
            resp.raise_for_status()
        except requests.RequestException as e:
            logger.error(
                "[airtable] get_existing_urls(%s) page %d : %s",
                source_nom, page, e,
            )
            break

        data = resp.json()
        for record in data.get("records", []):
            url = record.get("fields", {}).get("url", "")
            if url:
                urls.add(url)

        offset = data.get("offset")
        if not offset:
            break

        # Rate limit sécurité (5 req/sec)
        time.sleep(0.25)

    logger.info("[airtable] %d URLs existantes chargées pour %s", len(urls), source_nom)
    return urls


# =============================================================================
# Écriture : push des annonces
# =============================================================================

def push_annonces(annonces: Iterable[Annonce], source_nom: str) -> dict:
    """Pousse une liste d'annonces vers Airtable, en évitant les doublons.

    Flow :
        1. Charge les URLs déjà en base pour cette source (dédup)
        2. Filtre les nouvelles annonces
        3. Push par batches de 10 (limite Airtable)
        4. Retry une fois en cas d'erreur réseau

    Returns :
        { "pushed": int, "skipped": int, "errors": int, "total": int }
    """
    annonces = list(annonces)
    total = len(annonces)
    if total == 0:
        return {"pushed": 0, "skipped": 0, "errors": 0, "total": 0}

    existing = get_existing_urls(source_nom)

    # Filtrer : pas dans existing, pas en double dans le batch courant
    seen_in_batch: set[str] = set()
    new_annonces: list[Annonce] = []
    for a in annonces:
        if a.url in existing:
            continue
        if a.url in seen_in_batch:
            continue
        new_annonces.append(a)
        seen_in_batch.add(a.url)

    skipped = total - len(new_annonces)
    logger.info(
        "[%s] %d nouvelles annonces à pousser, %d doublons ignorés",
        source_nom, len(new_annonces), skipped,
    )

    # Push par batches de 10
    pushed = 0
    errors = 0
    for i in range(0, len(new_annonces), 10):
        batch = new_annonces[i:i + 10]
        records = [{"fields": a.to_airtable()} for a in batch]

        try:
            resp = _post_records_with_retry(records)
            if resp.status_code == 200:
                pushed += len(batch)
                logger.debug(
                    "[%s] bat