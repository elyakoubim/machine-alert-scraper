"""
Machine Alert — Backfill des catégories (script one-shot)
==========================================================

Script à exécuter UNE seule fois pour catégoriser les ~2873 annonces
existantes dans Airtable qui n'ont pas encore de valeur dans le champ
`categorie` (colonne ajoutée le 2026-04-24).

Stratégie :
    1. Récupère toutes les annonces où `categorie` est vide
    2. Catégorise par batch de 20 via Claude Haiku (prompt groupé = 5x moins cher)
    3. PATCH Airtable par batch de 10 (limite API)
    4. Rate limit entre batches pour ne pas saturer Airtable ou Anthropic

Coût estimé :
    ~2873 annonces / 20 par batch = ~144 appels Haiku
    ~$0.02 par batch de 20 = ~$3 au total

Usage :
    python scripts/backfill_categories.py                    # tout traiter
    python scripts/backfill_categories.py --dry-run          # simulation sans write
    python scripts/backfill_categories.py --limit 50         # n'en traite que 50 (test)
    python scripts/backfill_categories.py --batch-size 20    # taille batch LLM

IMPORTANT :
    - Ne touche PAS au champ `alerte_envoyee` (protégé dans airtable.py)
    - Ne ré-écrit PAS les annonces qui ont déjà une catégorie
    - Peut être relancé plusieurs fois sans risque (idempotent)
"""

from __future__ import annotations

import argparse
import logging
import sys
import time

# Charge .env
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# Permet d'exécuter le script depuis la racine du repo
# (python scripts/backfill_categories.py)
from pathlib import Path
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import airtable
from scrapers.llm_extractor import LLMExtractor

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("backfill_categories")


# =============================================================================
# Logique principale
# =============================================================================

def fetch_annonces_to_backfill(limit: int = None) -> list[dict]:
    """Récupère les annonces sans catégorie depuis Airtable."""
    logger.info("Chargement des annonces sans catégorie...")
    records = airtable.list_all_annonces(
        fields=["url", "titre", "description"],
        only_missing_categorie=True,
    )

    if limit:
        records = records[:limit]
        logger.info("Limite appliquée : %d annonces", limit)

    logger.info("%d annonces à traiter", len(records))
    return records


def build_batch_input(records: list[dict]) -> list[dict]:
    """Transforme les records Airtable en input pour LLMExtractor.categorize_batch."""
    items = []
    for r in records:
        fields = r.get("fields", {})
        items.append({
            "titre": fields.get("titre", ""),
            "description": fields.get("description", ""),
        })
    return items


def run_backfill(
    batch_size: int = 20,
    dry_run: bool = False,
    limit: int = None,
) -> dict:
    """Exécute le backfill complet.

    Returns :
        { "total": int, "categorized": int, "updated": int, "errors": int }
    """
    start = time.time()

    # 1. Récupère les annonces
    records = fetch_annonces_to_backfill(limit=limit)
    if not records:
        logger.info("Aucune annonce à traiter, fin.")
        return {"total": 0, "categorized": 0, "updated": 0, "errors": 0}

    # 2. Init LLM
    try:
        llm = LLMExtractor()
    except Exception as e:
        logger.error("Impossible d'initialiser LLMExtractor : %s", e)
        return {"total": len(records), "categorized": 0, "updated": 0, "errors": len(records)}

    # 3. Traitement par batch
    total = len(records)
    categorized = 0
    updated = 0
    errors = 0
    all_updates: list[dict] = []

    logger.info(
        "Démarrage backfill : %d annonces, batch_size=%d, dry_run=%s",
        total, batch_size, dry_run,
    )

    for i in range(0, total, batch_size):
        batch = records[i:i + batch_size]
        batch_num = i // batch_size + 1
        total_batches = (total + batch_size - 1) // batch_size

        logger.info(
            "─── Batch %d/%d (%d annonces) ───",
            batch_num, total_batches, len(batch),
        )

        # Input LLM
        items = build_batch_input(batch)

        # Appel Haiku en batch
        try:
            categories = llm.categorize_batch(items)
        except Exception as e:
            logger.error("Batch %d : erreur LLM : %s", batch_num, e)
            errors += len(batch)
            continue

        if len(categories) != len(batch):
            logger.error(
                "Batch %d : mismatch %d catégories pour %d items, skip",
                batch_num, len(categories), len(batch),
            )
            errors += len(batch)
            continue

        # Stats par catégorie
        stats = {}
        for cat in categories:
            stats[cat] = stats.get(cat, 0) + 1
        logger.info(
            "Batch %d catégorisé : %s",
            batch_num,
            ", ".join(f"{k}={v}" for k, v in sorted(stats.items())),
        )
        categorized += len(batch)

        # Prépare les updates Airtable
        for record, cat in zip(batch, categories):
            all_updates.append({
                "id": record["id"],
                "fields": {"categorie": cat},
            })

        # Rate limit léger entre batches LLM
        time.sleep(0.5)

    # 4. Push les updates Airtable par batch de 10
    if dry_run:
        logger.info("DRY-RUN : %d updates préparées (non écrites)", len(all_updates))
        logger.info(
            "Exemple : %s",
            all_updates[0] if all_updates else "(aucun)",
        )
    else:
        logger.info("Push de %d updates vers Airtable...", len(all_updates))
        result = airtable.batch_update_annonces(all_updates)
        updated = result["updated"]
        errors += result["errors"]

    duration = time.time() - start
    logger.info("═══ Backfill terminé en %.1f s ═══", duration)
    logger.info(
        "Total=%d | Catégorisées=%d | Updated=%d | Errors=%d",
        total, categorized, updated, errors,
    )

    return {
        "total": total,
        "categorized": categorized,
        "updated": updated,
        "errors": errors,
    }


# =============================================================================
# CLI
# =============================================================================

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Backfill des catégories des annonces existantes"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Simule sans écrire dans Airtable",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Limite le nombre d'annonces (utile pour tests)",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=20,
        help="Nombre d'annonces par appel Haiku (défaut: 20)",
    )
    args = parser.parse_args()

    result = run_backfill(
        batch_size=args.batch_size,
        dry_run=args.dry_run,
        limit=args.limit,
    )

    return 0 if result["errors"] == 0 else 2


if __name__ == "__main__":
    sys.exit(main())