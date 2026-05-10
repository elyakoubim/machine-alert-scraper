"""
Machine Alert — One-shot backfill : indexed_at = createdTime
=============================================================

Pour les ~2873 annonces legacy (creees avant l'ajout du champ indexed_at
le 2026-04-24), on initialise indexed_at avec la date de creation du
record (champ system Airtable `createdTime`, toujours present).

Consequence : au prochain run du lifecycle worker, ces records auront
un indexed_at parsable et seront classes correctement (la plupart en
"expirée" puisque createdTime > 30j d'age).

Usage :
    python scripts/backfill_indexed_at.py                # full backfill
    python scripts/backfill_indexed_at.py --dry-run      # log only
    python scripts/backfill_indexed_at.py --limit 5      # test
    python scripts/backfill_indexed_at.py --verbose      # logs DEBUG

Idempotent : le filtre `{indexed_at} = BLANK()` ne ramene QUE les records
sans indexed_at. Si on re-lance le script apres coup, fetch_candidates()
renvoie une liste vide (rien a faire).

NEVER touche `alerte_envoyee` (Make-managed, defendu par airtable.py).

Exit codes :
    0 = OK
    2 = record errors (au moins 1 batch PATCH a echoue)
    3 = exception infra non rattrapee
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path
from typing import Optional

# Permet l'execution depuis la racine du repo
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

import airtable

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("backfill_indexed_at")


# Filtre : annonces sans indexed_at (les ~2873 records legacy)
FILTER_BLANK_INDEXED = "{indexed_at} = BLANK()"


# =============================================================================
# Airtable I/O
# =============================================================================

def fetch_candidates() -> list:
    """Recupere les annonces sans indexed_at via Airtable.

    Retourne les records bruts (avec `createdTime` au niveau record, qui est
    un champ system Airtable toujours present, indep de fields[]=).
    """
    return airtable.list_all_annonces(
        fields=["indexed_at"],  # defensif : on vérifie qu'il est bien vide
        filter_formula=FILTER_BLANK_INDEXED,
    )


# =============================================================================
# Orchestration
# =============================================================================

def run_backfill(
    dry_run: bool = False,
    limit: Optional[int] = None,
) -> dict:
    """Execute le backfill.

    Retourne {"checked": N, "patched": M, "skipped": S, "errors": E}.
    """
    logger.info(
        "[backfill] start dry_run=%s limit=%s",
        dry_run, limit,
    )

    records = fetch_candidates()
    if limit:
        records = records[:limit]
        logger.info("[backfill] limit applique : %d records", limit)

    total = len(records)
    if total == 0:
        logger.info("[backfill] Aucun candidat (tous les indexed_at deja remplis), fin.")
        return {"checked": 0, "patched": 0, "skipped": 0, "errors": 0}

    updates = []
    skipped = 0

    for i, rec in enumerate(records, start=1):
        rec_id = rec.get("id")
        created_time = rec.get("createdTime")
        existing_indexed_at = rec.get("fields", {}).get("indexed_at")

        # Defense en profondeur : si une race condition fait que indexed_at
        # est rempli depuis le fetch, on skip. (Le filtre serveur garantit
        # deja BLANK, mais autant etre safe au cas ou.)
        if existing_indexed_at:
            skipped += 1
            logger.debug(
                "[backfill] %s : indexed_at deja rempli (%r), skip",
                rec_id, existing_indexed_at,
            )
            continue

        # createdTime est un champ system Airtable, toujours present.
        # S'il manque, c'est une anomalie reseau ou structure -> skip + warn.
        if not created_time:
            logger.warning(
                "[backfill] %s : createdTime ABSENT (anomalie), skip",
                rec_id,
            )
            skipped += 1
            continue

        updates.append({
            "id": rec_id,
            "fields": {"indexed_at": created_time},
        })
        logger.debug(
            "[backfill] %s : indexed_at <- createdTime=%s",
            rec_id, created_time,
        )

        if i % 100 == 0:
            logger.info(
                "[backfill] checked=%d patched_planned=%d skipped=%d (%d records traites)",
                i, len(updates), skipped, i,
            )

    logger.info(
        "[backfill] %d a patcher, %d skipped (sur %d total)",
        len(updates), skipped, total,
    )

    if dry_run:
        logger.info("[backfill] DRY-RUN : aucune ecriture Airtable")
        return {
            "checked": total,
            "patched": 0,
            "skipped": skipped,
            "errors": 0,
            "would_patch": len(updates),
        }

    if not updates:
        return {"checked": total, "patched": 0, "skipped": skipped, "errors": 0}

    # batch_update_annonces : 10 records par PATCH, strip `alerte_envoyee`
    # automatiquement (defense Make-managed).
    result = airtable.batch_update_annonces(updates)
    logger.info(
        "[backfill] termine : updated=%d errors=%d (sur %d demandes)",
        result["updated"], result["errors"], result["total"],
    )

    return {
        "checked": total,
        "patched": result["updated"],
        "skipped": skipped,
        "errors": result["errors"],
    }


# =============================================================================
# CLI
# =============================================================================

def main() -> int:
    parser = argparse.ArgumentParser(
        description="One-shot backfill : indexed_at = createdTime pour annonces legacy",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Calcule mais n'ecrit pas dans Airtable",
    )
    parser.add_argument(
        "--limit", type=int, default=None,
        help="Limite N records (pour tester)",
    )
    parser.add_argument(
        "--verbose", action="store_true",
        help="Active les logs DEBUG",
    )
    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)
        logger.setLevel(logging.DEBUG)

    try:
        result = run_backfill(
            dry_run=args.dry_run,
            limit=args.limit,
        )
    except Exception as e:
        # Exit 3 = erreur infra (Airtable down, MAX_PAGES, etc.).
        # Distinct des erreurs metier (exit 2).
        logger.error("[backfill] FATAL : %s", e, exc_info=True)
        return 3
    return 0 if result["errors"] == 0 else 2


if __name__ == "__main__":
    sys.exit(main())
