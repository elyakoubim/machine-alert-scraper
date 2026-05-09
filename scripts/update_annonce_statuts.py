"""
Machine Alert — Update lifecycle statuts (statut_annonce)
==========================================================

Recompute statut_annonce sur les annonces Airtable, basee sur date_fin
et indexed_at.

Statuts (Single select Airtable) : active / expirant_24h / expirée / inconnu

Regles dans l'ordre :
    1. date_fin connu :
         - passee                     → "expirée"
         - dans <= 24h                → "expirant_24h"
         - dans le futur              → "active"
    2. date_fin manquant, indexed_at connu :
         - indexed_at + 30j passe                 → "expirée"
         - indexed_at + 30j dans <= 24h           → "expirant_24h"
         - indexed_at + 30j futur                 → "active"
    3. Ni date_fin ni indexed_at parsable          → "inconnu"

Defauts (override possible via env) :
    EXPIRATION_DAYS = 30
    EXPIRING_SOON_HOURS = 24

Usage :
    python scripts/update_annonce_statuts.py                 # daily (filtre 7j)
    python scripts/update_annonce_statuts.py --backfill      # backfill initial
    python scripts/update_annonce_statuts.py --dry-run       # log only
    python scripts/update_annonce_statuts.py --limit 100     # test
    python scripts/update_annonce_statuts.py --verbose       # logs DEBUG

Idempotent : on PATCH uniquement quand le statut calcule != statut courant.
NEVER touche `alerte_envoyee` (Make-managed, defendu par airtable.py).
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import datetime, timedelta, timezone
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

from dateutil import parser as dateparser

import airtable

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("lifecycle")


# =============================================================================
# Configuration
# =============================================================================

EXPIRATION_DAYS = int(os.getenv("EXPIRATION_DAYS", "30"))
EXPIRING_SOON_HOURS = int(os.getenv("EXPIRING_SOON_HOURS", "24"))

VALID_STATUTS = {"active", "expirant_24h", "expirée", "inconnu"}

# Filtres construits dynamiquement avec une cutoff date pre-calculee cote Python.
# On EVITE NOW() Airtable qui est non-deterministe (recalcule ~15 min) :
# sur un fetch pagine long (>100 records), des pages successives auraient
# une fenetre 7j legerement differente. En injectant une date literale,
# la formule est identique pour toutes les pages.

def make_daily_filter(now: datetime) -> str:
    """Filtre v1 : nuls + non-expirees + expirees re-scrapees dans les 7 derniers jours.

    `now` (aware UTC) sert a freezer le cutoff -7j cote Python.
    """
    cutoff = (now - timedelta(days=7)).astimezone(timezone.utc).replace(microsecond=0)
    cutoff_str = cutoff.strftime("%Y-%m-%dT%H:%M:%SZ")
    return (
        'OR('
        'NOT({statut_annonce}),'
        '{statut_annonce}!="expirée",'
        'AND({statut_annonce}="expirée", '
        'IS_AFTER({indexed_at}, DATETIME_PARSE("' + cutoff_str + '")))'
        ')'
    )


def make_backfill_filter(now: datetime) -> str:  # noqa: ARG001 (now non utilise, signature coherente)
    """Filtre backfill : tout sauf deja-expiree (pas de fenetre 7j).

    `now` non utilise (pas de cutoff dans ce filtre), mais signature coherente
    avec make_daily_filter pour permettre un dispatch uniforme dans run_update.
    """
    return 'OR(NOT({statut_annonce}), {statut_annonce}!="expirée")'


# =============================================================================
# Helpers
# =============================================================================

def _parse_iso(s: Optional[str]) -> Optional[datetime]:
    """Parse une chaine ISO 8601 vers un datetime aware UTC, ou None.

    Supporte le suffixe Z d'Airtable et les datetime naifs (taggees UTC).
    """
    if not s or not isinstance(s, str):
        return None
    try:
        dt = dateparser.parse(s)
    except (ValueError, TypeError, OverflowError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


# =============================================================================
# Logique pure (testable sans Airtable)
# =============================================================================

def compute_statut(
    date_fin: Optional[str],
    indexed_at: Optional[str],
    now: datetime,
    expiration_days: int = EXPIRATION_DAYS,
    expiring_soon_hours: int = EXPIRING_SOON_HOURS,
) -> str:
    """Calcule le statut lifecycle a partir de date_fin / indexed_at.

    Fonction pure : pas d'I/O, pas d'effet de bord.
    Retourne toujours une valeur de VALID_STATUTS (garde-fou via assert final).
    """
    soon = timedelta(hours=expiring_soon_hours)

    df = _parse_iso(date_fin)
    if df is not None:
        if df < now:
            statut = "expirée"
        elif df - now <= soon:
            statut = "expirant_24h"
        else:
            statut = "active"
    else:
        ia = _parse_iso(indexed_at)
        if ia is not None:
            deadline = ia + timedelta(days=expiration_days)
            if deadline < now:
                statut = "expirée"
            elif deadline - now <= soon:
                statut = "expirant_24h"
            else:
                statut = "active"
        else:
            statut = "inconnu"

    assert statut in VALID_STATUTS, f"compute_statut: valeur invalide {statut!r}"
    return statut


# =============================================================================
# Airtable I/O
# =============================================================================

def fetch_candidates(filter_formula: str) -> list:
    """Recupere les annonces correspondant au filtre via Airtable."""
    return airtable.list_all_annonces(
        fields=["statut_annonce", "date_fin", "indexed_at"],
        filter_formula=filter_formula,
    )


# =============================================================================
# Orchestration
# =============================================================================

def run_update(
    dry_run: bool = False,
    limit: Optional[int] = None,
    backfill: bool = False,
) -> dict:
    """Calcule et applique les updates de statut_annonce.

    Retourne {"checked": N, "changed": M, "skipped": S, "errors": E}.
    """
    now = datetime.now(timezone.utc)
    mode = "BACKFILL" if backfill else "DAILY"
    formula = make_backfill_filter(now) if backfill else make_daily_filter(now)

    logger.info(
        "[lifecycle] start mode=%s dry_run=%s limit=%s now=%s "
        "EXPIRATION_DAYS=%d EXPIRING_SOON_HOURS=%d",
        mode, dry_run, limit, now.isoformat(),
        EXPIRATION_DAYS, EXPIRING_SOON_HOURS,
    )

    records = fetch_candidates(formula)
    if limit:
        records = records[:limit]
        logger.info("[lifecycle] limit applique : %d annonces", limit)

    total = len(records)
    if total == 0:
        logger.info("[lifecycle] Aucun candidat, fin.")
        return {"checked": 0, "changed": 0, "skipped": 0, "errors": 0}

    counts = {"active": 0, "expirant_24h": 0, "expirée": 0, "inconnu": 0}
    updates = []
    skipped = 0

    for i, rec in enumerate(records, start=1):
        fields = rec.get("fields", {})
        current = fields.get("statut_annonce")
        new_statut = compute_statut(
            fields.get("date_fin"),
            fields.get("indexed_at"),
            now,
        )
        counts[new_statut] = counts.get(new_statut, 0) + 1

        if new_statut == current:
            skipped += 1
        else:
            updates.append({
                "id": rec["id"],
                "fields": {"statut_annonce": new_statut},
            })
            logger.debug(
                "[lifecycle] %s : %r -> %r",
                rec.get("id"), current, new_statut,
            )

        if i % 100 == 0:
            logger.info(
                "[lifecycle] checked=%d changed=%d skipped=%d errors=0 (%d annonces traitees)",
                i, len(updates), skipped, i,
            )

    logger.info(
        "[lifecycle] repartition calculee : active=%d expirant=%d expiree=%d inconnu=%d",
        counts["active"], counts["expirant_24h"], counts["expirée"], counts["inconnu"],
    )
    logger.info(
        "[lifecycle] %d a mettre a jour, %d inchangees (sur %d total)",
        len(updates), skipped, total,
    )

    if dry_run:
        logger.info("[lifecycle] DRY-RUN : aucune ecriture Airtable")
        return {
            "checked": total,
            "changed": 0,
            "skipped": skipped,
            "errors": 0,
            "would_change": len(updates),
        }

    if not updates:
        return {"checked": total, "changed": 0, "skipped": skipped, "errors": 0}

    result = airtable.batch_update_annonces(updates)
    logger.info(
        "[lifecycle] termine : updated=%d errors=%d (sur %d demandes)",
        result["updated"], result["errors"], result["total"],
    )

    return {
        "checked": total,
        "changed": result["updated"],
        "skipped": skipped,
        "errors": result["errors"],
    }


# =============================================================================
# CLI
# =============================================================================

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Update statut_annonce dans Airtable (lifecycle)",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Calcule mais n'ecrit pas dans Airtable",
    )
    parser.add_argument(
        "--backfill", action="store_true",
        help="Mode backfill : pas de fenetre 7j sur les expirees",
    )
    parser.add_argument(
        "--limit", type=int, default=None,
        help="Limite N annonces (pour tester)",
    )
    parser.add_argument(
        "--verbose", action="store_true",
        help="Active les logs DEBUG (transitions individuelles)",
    )
    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)
        logger.setLevel(logging.DEBUG)

    try:
        result = run_update(
            dry_run=args.dry_run,
            limit=args.limit,
            backfill=args.backfill,
        )
    except Exception as e:
        # Exit code 3 = erreur infra (exception non rattrapee, ex: Airtable down,
        # MAX_PAGES depasse, parse JSON casse, etc.). Cron Railway logguera la
        # stack trace via exc_info=True. Distinct des erreurs metier (exit 2).
        logger.error("[lifecycle] FATAL : %s", e, exc_info=True)
        return 3
    return 0 if result["errors"] == 0 else 2


if __name__ == "__main__":
    sys.exit(main())
