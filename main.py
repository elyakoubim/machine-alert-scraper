"""
Machine Alert - Orchestrateur principal v2-only
================================================

Lance tous les scrapers v2 trouves dans scrapers/sites/.
Chaque scraper herite de BaseScraper et est auto-decouvert.

CLI :
    python main.py                # tous les scrapers
    python main.py --scraper auctelia   # un seul scraper
    python main.py --no-llm       # sans categorisation Haiku
    python main.py --dry-run      # ne push pas dans Airtable
    python main.py --max-annonces 5    # limite a 5 annonces par scraper
"""

from __future__ import annotations

import argparse
import importlib
import logging
import os
import pkgutil
import sys
import time
from typing import List, Optional, Type

# Charger .env si present
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from scrapers.base import BaseScraper
import scrapers.sites
import airtable as airtable_client

# Configuration logging
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s [%(levelname)s] %(name)s : %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout)],
)

# Reduire le bruit de httpx (log toutes les requetes en INFO par defaut)
logging.getLogger("httpx").setLevel(logging.WARNING)

logger = logging.getLogger(__name__)


def discover_scrapers() -> List[Type[BaseScraper]]:
    """
    Auto-decouvre toutes les sous-classes de BaseScraper dans scrapers.sites.
    Retourne la liste des classes (pas instances).
    """
    discovered = []
    package = scrapers.sites
    for finder, name, ispkg in pkgutil.iter_modules(package.__path__):
        if ispkg:
            continue
        module_name = f"{package.__name__}.{name}"
        try:
            module = importlib.import_module(module_name)
        except Exception as e:
            logger.error("Impossible d'importer %s : %s", module_name, e)
            continue
        for attr_name in dir(module):
            attr = getattr(module, attr_name)
            if (
                isinstance(attr, type)
                and issubclass(attr, BaseScraper)
                and attr is not BaseScraper
            ):
                discovered.append(attr)
                logger.debug("Decouvert : %s.%s", module_name, attr_name)
    return discovered


def run_scraper(
    ScraperCls: Type[BaseScraper],
    llm_extractor=None,
    dry_run: bool = False,
    max_annonces: Optional[int] = None,
) -> dict:
    """
    Lance un seul scraper et push ses annonces dans Airtable.
    Retourne un dict avec les statistiques.
    """
    scraper = ScraperCls(llm_extractor=llm_extractor)
    source_nom = scraper.source_nom
    t0 = time.time()
    logger.info("[%s] Demarrage (pays=%s, JS=%s)", source_nom, scraper.source_pays, scraper.requires_javascript)

    annonces = []
    try:
        for annonce in scraper.run():
            annonces.append(annonce)
            if max_annonces and len(annonces) >= max_annonces:
                logger.info("[%s] Limite max_annonces=%d atteinte", source_nom, max_annonces)
                break
    except Exception as e:
        logger.error("[%s] Erreur fatale pendant run() : %s", source_nom, e, exc_info=True)
        try:
            airtable_client.update_source_status(source_nom, status="ERR", error=str(e))
        except Exception:
            pass
        return {"source": source_nom, "total": 0, "pushed": 0, "skipped": 0, "errors": 1, "duration": time.time() - t0}

    logger.info("[%s] %d annonces collectees", source_nom, len(annonces))

    if dry_run:
        logger.info("[%s] dry-run, aucun push Airtable", source_nom)
        return {"source": source_nom, "total": len(annonces), "pushed": 0, "skipped": 0, "errors": 0, "duration": time.time() - t0, "dry_run": True}

    if not annonces:
        try:
            airtable_client.update_source_status(source_nom, status="OK")
        except Exception as e:
            logger.warning("[%s] update_source_status echec : %s", source_nom, e)
        return {"source": source_nom, "total": 0, "pushed": 0, "skipped": 0, "errors": 0, "duration": time.time() - t0}

    try:
        push_result = airtable_client.push_annonces(annonces, source_nom)
    except Exception as e:
        logger.error("[%s] echec push_annonces : %s", source_nom, e, exc_info=True)
        try:
            airtable_client.update_source_status(source_nom, status="ERR", error=str(e))
        except Exception:
            pass
        return {"source": source_nom, "total": len(annonces), "pushed": 0, "skipped": 0, "errors": len(annonces), "duration": time.time() - t0}

    try:
        status = "OK" if push_result.get("errors", 0) == 0 else f"PARTIAL: {push_result['errors']} erreurs"
        airtable_client.update_source_status(source_nom, status=status)
    except Exception as e:
        logger.warning("[%s] update_source_status echec : %s", source_nom, e)

    return {
        "source": source_nom,
        "total": len(annonces),
        "pushed": push_result.get("pushed", 0),
        "skipped": push_result.get("skipped", 0),
        "errors": push_result.get("errors", 0),
        "duration": time.time() - t0,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Machine Alert - scraper v2")
    parser.add_argument("--scraper", help="Nom d'un seul scraper a lancer (ex: auctelia)")
    parser.add_argument("--no-llm", action="store_true", help="Desactive la categorisation Haiku")
    parser.add_argument("--dry-run", action="store_true", help="Ne push pas dans Airtable")
    parser.add_argument("--max-annonces", type=int, default=None, help="Limite N annonces par scraper")
    args = parser.parse_args()

    logger.info("======================================================")
    logger.info("Machine Alert - scraper v2 - demarrage")
    logger.info("======================================================")

    scrapers_classes = discover_scrapers()
    if not scrapers_classes:
        logger.warning("Aucun scraper decouvert dans scrapers/sites/")
        return 0

    if args.scraper:
        scrapers_classes = [
            cls for cls in scrapers_classes
            if cls.source_nom.lower() == args.scraper.lower()
        ]
        if not scrapers_classes:
            logger.error("Aucun scraper avec source_nom='%s' trouve", args.scraper)
            return 1

    logger.info("%d scraper(s) a lancer : %s",
        len(scrapers_classes),
        [c.source_nom for c in scrapers_classes])

    llm_extractor = None
    if not args.no_llm:
        try:
            from scrapers.llm_extractor import LLMExtractor
            llm_extractor = LLMExtractor()
            logger.info("LLMExtractor initialise (model=%s)", llm_extractor._model)
        except Exception as e:
            logger.warning("LLMExtractor non initialise : %s. Categorisation desactivee.", e)
            llm_extractor = None
    else:
        logger.info("Categorisation Haiku desactivee (--no-llm)")

    results = []
    t_global = time.time()
    for ScraperCls in scrapers_classes:
        result = run_scraper(
            ScraperCls,
            llm_extractor=llm_extractor,
            dry_run=args.dry_run,
            max_annonces=args.max_annonces,
        )
        results.append(result)
        status_icon = "OK" if result.get("errors", 0) == 0 else "ERR"
        logger.info(
            "[%s] [%s] total=%d pushed=%d skipped=%d errors=%d (%.1fs)",
            status_icon, result["source"],
            result["total"], result["pushed"],
            result["skipped"], result["errors"],
            result["duration"],
        )

    duration_global = time.time() - t_global
    total_pushed = sum(r["pushed"] for r in results)
    total_errors = sum(r["errors"] for r in results)

    logger.info("======================================================")
    logger.info("Resume : %d scrapers, %d annonces poussees, %d erreurs (%.1fs total)",
        len(results), total_pushed, total_errors, duration_global)
    logger.info("======================================================")

    return 0 if total_errors == 0 else 2


if __name__ == "__main__":
    sys.exit(main())