"""
Machine Alert — Orchestrateur principal
========================================

Lance le scraping de toutes les sources actives et push vers Airtable.

Usage :
    python main.py                          # scrape tous les scrapers actifs
    python main.py --source auctelia        # scrape un seul scraper (par slug)
    python main.py --dry-run                # test sans écrire dans Airtable
    python main.py --source auctelia --dry-run
    python main.py --list                   # liste les scrapers disponibles

Découverte automatique :
    Chaque fichier .py dans scrapers/sites/ est inspecté. Toute classe qui
    hérite de BaseScraper et expose source_nom/source_pays/base_url est
    automatiquement enregistrée et exécutable.

    Il n'y a pas de config manuelle à maintenir — ajouter un nouveau
    scraper = créer un nouveau fichier dans scrapers/sites/.
"""

from __future__ import annotations

import argparse
import importlib
import inspect
import logging
import pkgutil
import sys
import time
from dataclasses import dataclass
from typing import Optional

# Charge .env dès le début
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

import airtable
from scrapers.base import BaseScraper
from scrapers.llm_extractor import LLMExtractor

# ── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s : %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)


# =============================================================================
# Découverte automatique des scrapers dans scrapers/sites/
# =============================================================================

def discover_scrapers() -> dict[str, type[BaseScraper]]:
    """Parcourt scrapers/sites/ et renvoie un dict { source_nom: Classe }.

    N'instancie PAS les classes ici — on les instanciera plus tard avec
    le fetcher et le LLMExtractor appropriés.
    """
    registry: dict[str, type[BaseScraper]] = {}

    try:
        import scrapers.sites as sites_pkg
    except ImportError:
        logger.warning("scrapers/sites/ package absent, aucun scraper chargé")
        return registry

    for module_info in pkgutil.iter_modules(sites_pkg.__path__):
        module_name = f"scrapers.sites.{module_info.name}"
        try:
            module = importlib.import_module(module_name)
        except Exception as e:
            logger.error("Impossible d'importer %s : %s", module_name, e)
            continue

        for _, obj in inspect.getmembers(module, inspect.isclass):
            # Doit hériter de BaseScraper, ne pas être BaseScraper lui-même,
            # et avoir un source_nom configuré
            if (
                issubclass(obj, BaseScraper)
                and obj is not BaseScraper
                and getattr(obj, "source_nom", "")
            ):
                slug = obj.source_nom
                if slug in registry:
                    logger.warning(
                        "Conflit : %s déjà enregistré, on garde le premier",
                        slug,
                    )
                    continue
                registry[slug] = obj
                logger.debug("Scraper découvert : %s -> %s", slug, obj.__name__)

    return registry


# =============================================================================
# Résu