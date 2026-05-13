"""
Machine Alert — One-shot backfill : decode HTML entities (&nbsp;) sur Hämmerle
=============================================================================

Repare les 5 746 annonces Hämmerle existantes dont la description (ou le
titre) contient des entites HTML non decodees (`&nbsp;`, mais aussi `&amp;`,
`&quot;`, etc. — `html.unescape()` les couvre toutes).

Pure transformation de chaine — aucun re-scrape du site source, aucun
appel LLM. Idempotent : le filtre serveur ne ramene que les records qui
contiennent encore `&nbsp;`, donc relancer apres succes ramene 0.

Le fix preventif global (cote ecriture) a ete livre dans le commit
`aef94a1` (`fix(base): html.unescape titre & description in normalize()`).
Ce script-ci traite uniquement le passif.

Usage :
    python scripts/backfill_hnbsp_haemmerle.py --dry-run --limit 5 --verbose
    python scripts/backfill_hnbsp_haemmerle.py --dry-run
    python scripts/backfill_hnbsp_haemmerle.py
    python scripts/backfill_hnbsp_haemmerle.py --verbose

Variables env :
    AIRTABLE_TOKEN, AIRTABLE_BASE_ID.

NEVER touche `alerte_envoyee` (protege defensivement par
airtable.batch_update_annonces).

Exit codes :
    0 = OK
    2 = au moins 1 erreur PATCH
    3 = exception infra non rattrapee
"""

from __future__ import annotations

import argparse
import html
import logging
import sys
from pathlib import Path
from typing import Optional

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
logger = logging.getLogger("backfill_hnbsp_haemmerle")


# Filtre serveur : source=Hammerle ET (titre OR description) contient "&nbsp;".
# Garantit l'idempotence : apres unescape, ces records ne matchent plus le filtre.
FILTER_FORMULA = (
    'AND('
    '{source} = "Hämmerle", '
    'OR(FIND("&nbsp;", {description}), FIND("&nbsp;", {titre}))'
    ')'
)

FIELDS_TO_FETCH = ["url", "titre", "description"]

# Nb de transformations AVANT/APRES a logger en INFO pour validation visuelle.
# Au-dela on log juste un compteur ; en --verbose tout est en DEBUG.
SAMPLE_LOG_COUNT = 5


# =============================================================================
# Transformation
# =============================================================================

def compute_patch(record: dict) -> Optional[dict]:
    """Construit le dict `fields` du PATCH si unescape change qqch.

    Retourne None si aucune transformation effective (idempotence locale).
    """
    f = record.get("fields", {})
    old_titre = f.get("titre") or ""
    old_desc = f.get("description") or ""
    new_titre = html.unescape(old_titre)
    new_desc = html.unescape(old_desc)
    patch: dict = {}
    if new_titre != old_titre:
        patch["titre"] = new_titre
    if new_desc != old_desc:
        patch["description"] = new_desc
    return patch or None


def preview(s: str, n: int = 200) -> str:
    """Tronque pour log lisible."""
    if len(s) <= n:
        return s
    return s[:n] + f"... [{len(s)} chars total]"


# =============================================================================
# Airtable I/O
# =============================================================================

def fetch_candidates(limit: Optional[int] = None) -> list:
    """Recupere les annonces Hammerle polluees par &nbsp; depuis Airtable."""
    logger.info("[backfill] fetch : filtre = %s", FILTER_FORMULA)
    records = airtable.list_all_annonces(
        fields=FIELDS_TO_FETCH,
        filter_formula=FILTER_FORMULA,
    )
    total_match = len(records)
    logger.info("[backfill] %d candidats matchent le filtre (avant --limit)", total_match)
    if limit:
        records = records[:limit]
        logger.info("[backfill] --limit applique : %d records traites sur %d", limit, total_match)
    return records


# =============================================================================
# Orchestration
# =============================================================================

def run_backfill(
    dry_run: bool = False,
    limit: Optional[int] = None,
) -> dict:
    """Execute le backfill.

    Retourne {"checked": N, "updated": M, "skipped": S, "errors": E}.
    """
    import time
    start = time.time()
    logger.info(
        "[backfill] start dry_run=%s limit=%s",
        dry_run, limit,
    )

    records = fetch_candidates(limit=limit)
    checked = len(records)
    if checked == 0:
        suffix = " (DRY-RUN)" if dry_run else ""
        logger.info("[backfill] Aucun candidat, fin.")
        logger.info("[backfill] checked=0 updated=0 skipped=0 errors=0%s", suffix)
        return {"checked": 0, "updated": 0, "skipped": 0, "errors": 0}

    updates: list[dict] = []
    skipped = 0

    for i, rec in enumerate(records):
        patch = compute_patch(rec)
        if patch is None:
            # Filtre serveur l'a ramene mais unescape n'a rien change.
            # Devrait etre impossible avec &nbsp; (toujours decode) mais defense.
            skipped += 1
            logger.debug(
                "[backfill] %s : skip (aucune transformation effective)",
                rec.get("id"),
            )
            continue

        # AVANT/APRES pour les 5 premiers records, ALWAYS en INFO
        # (pour valider visuellement le dry-run sans --verbose).
        if i < SAMPLE_LOG_COUNT:
            f = rec.get("fields", {})
            old_titre = f.get("titre") or ""
            old_desc = f.get("description") or ""
            new_titre = patch.get("titre", old_titre)
            new_desc = patch.get("description", old_desc)
            logger.info(
                "[backfill] sample %d/%d : %s",
                i + 1, min(SAMPLE_LOG_COUNT, checked), rec.get("id"),
            )
            if "titre" in patch:
                logger.info("  TITRE  AVANT : %s", preview(old_titre))
                logger.info("  TITRE  APRES : %s", preview(new_titre))
            if "description" in patch:
                logger.info("  DESC   AVANT : %s", preview(old_desc))
                logger.info("  DESC   APRES : %s", preview(new_desc))
        else:
            logger.debug(
                "[backfill] %s : patch fields=%s",
                rec.get("id"), list(patch.keys()),
            )

        updates.append({"id": rec["id"], "fields": patch})

    logger.info(
        "[backfill] %d patches prepares, %d records skip (sur %d)",
        len(updates), skipped, checked,
    )

    if dry_run:
        logger.info("[backfill] DRY-RUN : aucune ecriture Airtable")
        duration = time.time() - start
        logger.info("[backfill] termine en %.1f s", duration)
        logger.info(
            "[backfill] checked=%d updated=0 skipped=%d errors=0 (DRY-RUN)",
            checked, skipped,
        )
        return {"checked": checked, "updated": 0, "skipped": skipped, "errors": 0}

    if not updates:
        logger.info("[backfill] rien a patcher, fin.")
        logger.info("[backfill] checked=%d updated=0 skipped=%d errors=0", checked, skipped)
        return {"checked": checked, "updated": 0, "skipped": skipped, "errors": 0}

    # batch_update_annonces : batch de 10, throttle 250ms, strip alerte_envoyee.
    result = airtable.batch_update_annonces(updates)
    updated = result["updated"]
    errors = result["errors"]

    duration = time.time() - start
    logger.info(
        "[backfill] PATCH termine : updated=%d errors_patch=%d (sur %d)",
        updated, errors, result["total"],
    )
    logger.info("[backfill] termine en %.1f s", duration)
    logger.info(
        "[backfill] checked=%d updated=%d skipped=%d errors=%d",
        checked, updated, skipped, errors,
    )
    return {"checked": checked, "updated": updated, "skipped": skipped, "errors": errors}


# =============================================================================
# CLI
# =============================================================================

def main() -> int:
    parser = argparse.ArgumentParser(
        description="One-shot backfill : decode HTML entities sur descriptions/titres Hammerle",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Calcule mais n'ecrit pas dans Airtable",
    )
    parser.add_argument(
        "--limit", type=int, default=None,
        help="Limite N records (utile pour tests)",
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
        logger.error("[backfill] FATAL : %s", e, exc_info=True)
        return 3
    return 0 if result["errors"] == 0 else 2


if __name__ == "__main__":
    sys.exit(main())
