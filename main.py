"""
Machine Alert — Orchestrateur principal
Lance le scraping de toutes les sources actives et push vers Airtable

Usage:
  python main.py                    # Scrape toutes les sources actives
  python main.py --source Clicpublic  # Scrape une source spécifique
  python main.py --dry-run          # Test sans écrire dans Airtable
  python main.py --source Clicpublic --dry-run  # Test d'une source
"""

import argparse
import logging
import sys
import time
from datetime import datetime

from config import SOURCES
from scrapers.static import StaticScraper
from scrapers.dynamic import DynamicScraper
import airtable

# ── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
      
    ],
)
logger = logging.getLogger(__name__)


def get_scraper(config: dict):
    scraper_type = config.get("type", "static")
    if scraper_type == "dynamic":
        return DynamicScraper(config)
    else:
        return StaticScraper(config)


def run_source(config: dict, dry_run: bool = False) -> dict:
    nom = config["nom"]
    start = time.time()
    result = {"nom": nom, "annonces": 0, "pushed": 0, "errors": 0, "duration": 0}

    try:
        scraper = get_scraper(config)
        annonces = scraper.scrape()
        result["annonces"] = len(annonces)

        if dry_run:
            logger.info(f"[{nom}] DRY RUN — {len(annonces)} annonces trouvées (non pushées)")
            for a in annonces[:5]:
                logger.info(f"  → {a.titre[:60]} | {a.url[:80]}")
        else:
            stats = airtable.push_annonces(annonces, nom)
            airtable.update_source_status(nom, "OK")
            result["pushed"] = stats["pushed"]
            result["errors"] = stats["errors"]
            logger.info(f"[{nom}] ✅ pushed={stats['pushed']} skipped={stats['skipped']} errors={stats['errors']}")

    except Exception as e:
        logger.error(f"[{nom}] ❌ Fatal error: {e}")
        result["errors"] = 1
        if not dry_run:
            airtable.update_source_status(nom, "ERR", str(e))

    result["duration"] = round(time.time() - start, 1)
    return result


def main():
    parser = argparse.ArgumentParser(description="Machine Alert Scraper")
    parser.add_argument("--source", help="Nom d'une source spécifique à scraper")
    parser.add_argument("--dry-run", action="store_true", help="Ne pas écrire dans Airtable")
    args = parser.parse_args()

    # Créer dossier logs
    import os
    os.makedirs("logs", exist_ok=True)

    # Sélectionner les sources
    if args.source:
        sources = [s for s in SOURCES if s["nom"].lower() == args.source.lower()]
        if not sources:
            logger.error(f"Source '{args.source}' non trouvée. Sources disponibles: {[s['nom'] for s in SOURCES]}")
            sys.exit(1)
    else:
        sources = [s for s in SOURCES if s.get("actif", True)]

    logger.info(f"{'='*60}")
    logger.info(f"Machine Alert — Scraping de {len(sources)} sources")
    logger.info(f"Mode: {'DRY RUN' if args.dry_run else 'PRODUCTION'}")
    logger.info(f"{'='*60}")

    results = []
    total_start = time.time()

    for i, source in enumerate(sources, 1):
        logger.info(f"\n[{i}/{len(sources)}] {source['nom']} ({source['type'].upper()})")
        result = run_source(source, dry_run=args.dry_run)
        results.append(result)
        # Pause entre sources pour ne pas surcharger
        if i < len(sources):
            time.sleep(2)

    # ── Rapport final ─────────────────────────────────────────────────────────
    total_duration = round(time.time() - total_start, 1)
    total_pushed = sum(r["pushed"] for r in results)
    total_annonces = sum(r["annonces"] for r in results)
    total_errors = sum(r["errors"] for r in results)

    logger.info(f"\n{'='*60}")
    logger.info(f"RAPPORT FINAL — {total_duration}s")
    logger.info(f"{'='*60}")
    logger.info(f"Sources scrapées : {len(results)}")
    logger.info(f"Annonces trouvées: {total_annonces}")
    logger.info(f"Annonces pushées : {total_pushed}")
    logger.info(f"Erreurs          : {total_errors}")
    logger.info(f"{'='*60}")

    # Détail par source
    for r in results:
        status = "✅" if r["errors"] == 0 else "❌"
        logger.info(f"{status} {r['nom']:<30} {r['annonces']:>3} trouvées | {r['pushed']:>3} pushées | {r['duration']}s")

    logger.info(f"{'='*60}\n")


if __name__ == "__main__":
    main()
