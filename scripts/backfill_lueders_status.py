"""
Machine Alert — One-shot backfill : marquer les Lueders fermées comme expirée
==============================================================================

Consomme le JSON d'audit produit par `_audit_lueders_active.py` (un mapping
record_id -> {url, site_status} obtenu en fetchant chaque page Lueders et
en regexant `Auktionsstatus: ...`).

Pour chaque record :

    - site_status == "abgeschlossen" / "erreur_reseau" / "404"
        → PATCH `statut_annonce = "expirée"` + `date_fin = now()` ISO
    - site_status == "aktiv"
        → skip silencieux (sera re-scrape par le worker avec le nouveau parser)
    - autre (unknown:*, no_url, http_*)
        → skip + log warning

Idempotent : on lit `statut_annonce` actuel et on skippe les records deja
`expirée` (economie d'API + safe sur re-run apres crash partiel).

NEVER touche `alerte_envoyee` (protege defensivement par
airtable.batch_update_annonces).

Usage :
    python scripts/backfill_lueders_status.py --dry-run --limit 5 --verbose
    python scripts/backfill_lueders_status.py --dry-run
    python scripts/backfill_lueders_status.py
    python scripts/backfill_lueders_status.py --audit-json /path/to/audit.json

Variables env :
    AIRTABLE_TOKEN, AIRTABLE_BASE_ID.

Exit codes :
    0 = OK
    2 = au moins 1 erreur PATCH
    3 = exception infra non rattrapee (audit absent, etc.)
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import tempfile
import time
from datetime import datetime, timezone
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
logger = logging.getLogger("backfill_lueders_status")


# Categorisation des site_status selon le JSON audit
PATCH_STATUSES = {"abgeschlossen", "erreur_reseau", "404"}
RESCRAPE_STATUSES = {"aktiv"}

# Chemin par defaut du JSON audit (cross-platform)
DEFAULT_AUDIT_JSON = Path(tempfile.gettempdir()) / "lueders_audit.json"

# Statut Airtable cible (valeur canonique avec accent — match Single Select)
EXPIRED_LABEL = "expirée"

# Sample d'AVANT/APRES affiche en INFO pour validation visuelle
SAMPLE_LOG_COUNT = 5


# =============================================================================
# I/O
# =============================================================================

def load_audit(path: Path) -> dict:
    if not path.exists():
        raise FileNotFoundError(
            f"Audit JSON absent : {path}. "
            "Lance d'abord scripts/_audit_lueders_active.py."
        )
    return json.loads(path.read_text(encoding="utf-8"))


def fetch_current_statuts() -> dict[str, Optional[str]]:
    """Retourne {rec_id: statut_annonce_actuel} pour tous les records Lueders.

    Un seul appel paginate vs N requetes : ~10s pour 5 327 records.
    """
    logger.info("[backfill] fetch statut_annonce actuel pour Lueders...")
    recs = airtable.list_all_annonces(
        fields=["statut_annonce"],
        filter_formula='{source} = "Lueders & Partner"',
    )
    statuts: dict[str, Optional[str]] = {}
    for r in recs:
        statuts[r["id"]] = r.get("fields", {}).get("statut_annonce")
    logger.info("[backfill] %d records Lueders charges", len(statuts))
    return statuts


# =============================================================================
# Orchestration
# =============================================================================

def run_backfill(
    dry_run: bool = False,
    limit: Optional[int] = None,
    audit_json: Path = DEFAULT_AUDIT_JSON,
) -> dict:
    start = time.time()
    logger.info(
        "[backfill] start dry_run=%s limit=%s audit_json=%s",
        dry_run, limit, audit_json,
    )

    audit = load_audit(audit_json)
    total_audit = len(audit)
    logger.info("[backfill] audit JSON charge : %d entrees", total_audit)

    current_statuts = fetch_current_statuts()

    # Categorisation
    to_patch_ids: list[str] = []
    to_rescrape = 0
    skipped_already_expired = 0
    skipped_warning = 0
    site_status_breakdown: dict[str, int] = {}

    for rec_id, info in audit.items():
        site_status = (info or {}).get("site_status", "")
        site_status_breakdown[site_status] = site_status_breakdown.get(site_status, 0) + 1

        if site_status in RESCRAPE_STATUSES:
            to_rescrape += 1
            continue

        if site_status not in PATCH_STATUSES:
            logger.warning(
                "[backfill] %s : site_status='%s' (skip warning)",
                rec_id, site_status,
            )
            skipped_warning += 1
            continue

        # Match PATCH_STATUSES — verifier idempotence
        current = current_statuts.get(rec_id)
        if current == EXPIRED_LABEL:
            skipped_already_expired += 1
            logger.debug(
                "[backfill] %s : deja '%s', skip",
                rec_id, EXPIRED_LABEL,
            )
            continue

        to_patch_ids.append(rec_id)

    logger.info(
        "[backfill] decoupage : to_patch=%d to_rescrape=%d "
        "skipped_already_expired=%d skipped_warning=%d",
        len(to_patch_ids), to_rescrape, skipped_already_expired, skipped_warning,
    )
    logger.info(
        "[backfill] site_status breakdown : %s",
        ", ".join(f"{k}={v}" for k, v in sorted(site_status_breakdown.items(), key=lambda x: -x[1])),
    )

    # --limit applique sur la liste des PATCH apres categorisation
    limited_away = 0
    if limit is not None and limit < len(to_patch_ids):
        limited_away = len(to_patch_ids) - limit
        to_patch_ids = to_patch_ids[:limit]
        logger.info("[backfill] --limit applique : %d records a patcher (sur %d eligibles)",
                    limit, limit + limited_away)

    # Echantillon visuel pour validation dry-run
    for i, rec_id in enumerate(to_patch_ids[:SAMPLE_LOG_COUNT]):
        info = audit.get(rec_id, {})
        logger.info(
            "[backfill] sample %d/%d : %s | site_status=%s | url=%s | current_statut=%r",
            i + 1, min(SAMPLE_LOG_COUNT, len(to_patch_ids)),
            rec_id,
            info.get("site_status"),
            (info.get("url") or "")[:80],
            current_statuts.get(rec_id),
        )

    # Construction des PATCH
    now_iso = datetime.now(timezone.utc).isoformat()
    updates = [
        {
            "id": rec_id,
            "fields": {
                "statut_annonce": EXPIRED_LABEL,
                "date_fin": now_iso,
            },
        }
        for rec_id in to_patch_ids
    ]

    skipped = skipped_already_expired + skipped_warning + limited_away

    if dry_run:
        logger.info(
            "[backfill] DRY-RUN : would patch %d records (statut_annonce='%s', date_fin=%s)",
            len(updates), EXPIRED_LABEL, now_iso,
        )
        duration = time.time() - start
        logger.info("[backfill] termine en %.1fs", duration)
        logger.info(
            "[backfill] checked=%d updated=0 skipped=%d errors=0 to_rescrape=%d (DRY-RUN)",
            total_audit, skipped + len(updates), to_rescrape,
        )
        return {
            "checked": total_audit,
            "updated": 0,
            "skipped": skipped + len(updates),
            "errors": 0,
            "to_rescrape": to_rescrape,
            "would_patch": len(updates),
        }

    if not updates:
        logger.info("[backfill] rien a patcher, fin.")
        logger.info(
            "[backfill] checked=%d updated=0 skipped=%d errors=0 to_rescrape=%d",
            total_audit, skipped, to_rescrape,
        )
        return {
            "checked": total_audit,
            "updated": 0,
            "skipped": skipped,
            "errors": 0,
            "to_rescrape": to_rescrape,
        }

    # PATCH reel via airtable.batch_update_annonces (batch 10, throttle 250ms,
    # strip alerte_envoyee defensivement).
    result = airtable.batch_update_annonces(updates)
    updated = result["updated"]
    errors = result["errors"]

    duration = time.time() - start
    logger.info(
        "[backfill] PATCH termine : updated=%d errors_patch=%d (sur %d)",
        updated, errors, result["total"],
    )
    logger.info("[backfill] termine en %.1fs", duration)
    logger.info(
        "[backfill] checked=%d updated=%d skipped=%d errors=%d to_rescrape=%d",
        total_audit, updated, skipped, errors, to_rescrape,
    )
    return {
        "checked": total_audit,
        "updated": updated,
        "skipped": skipped,
        "errors": errors,
        "to_rescrape": to_rescrape,
    }


# =============================================================================
# CLI
# =============================================================================

def main() -> int:
    parser = argparse.ArgumentParser(
        description="One-shot backfill : marquer les Lueders abgeschlossen en 'expirée'",
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
    parser.add_argument(
        "--audit-json", type=Path, default=DEFAULT_AUDIT_JSON,
        help=f"Chemin du JSON audit (defaut: {DEFAULT_AUDIT_JSON})",
    )
    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)
        logger.setLevel(logging.DEBUG)

    try:
        result = run_backfill(
            dry_run=args.dry_run,
            limit=args.limit,
            audit_json=args.audit_json,
        )
    except Exception as e:
        logger.error("[backfill] FATAL : %s", e, exc_info=True)
        return 3
    return 0 if result["errors"] == 0 else 2


if __name__ == "__main__":
    sys.exit(main())
