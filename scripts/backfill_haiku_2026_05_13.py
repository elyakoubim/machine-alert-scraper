"""
Machine Alert — One-shot backfill Haiku (session 2026-05-13)
=============================================================

Recategorise + enrichit (marque/modele/annee_fabrication/etat/type_vente)
les annonces indexees a partir du 2026-05-13 dont `categorie` est vide.

Strategie :
    1. Liste les annonces ou {categorie} = BLANK() ET indexed_at >= 2026-05-13
    2. Appelle LLMExtractor.extract_batch() par lots de 20
    3. PATCH Airtable :
        - `categorie` : toujours (records filtres comme vides)
        - `marque`, `modele`, `annee_fabrication`, `etat`, `type_vente` :
          uniquement si la valeur actuelle est vide (no overwrite)

Idempotent : le filtre serveur ne ramene QUE les records `categorie` vide ;
relancer le script apres coup renvoie liste vide ou un sous-ensemble.

Usage :
    python scripts/backfill_haiku_2026_05_13.py --dry-run --limit 5
    python scripts/backfill_haiku_2026_05_13.py --dry-run
    python scripts/backfill_haiku_2026_05_13.py
    python scripts/backfill_haiku_2026_05_13.py --verbose

Variables env :
    AIRTABLE_TOKEN, AIRTABLE_BASE_ID, ANTHROPIC_API_KEY,
    ANTHROPIC_MODEL (defaut claude-haiku-4-5-20251001).

Anti-hallucination : reutilise SYSTEM_PROMPT de scrapers/llm_extractor.py
(null obligatoire si info absente, jamais d'inference).

NEVER touche `alerte_envoyee` (protege defensivement par airtable.py).

Exit codes :
    0 = OK
    2 = au moins 1 batch PATCH a echoue
    3 = exception infra non rattrapee
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
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
from scrapers.llm_extractor import LLMExtractor

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("backfill_haiku_2026_05_13")


# Bornes session
SESSION_START_ISO = "2026-05-13T00:00:00Z"
# IS_AFTER est strict : on borne juste avant pour obtenir >= 00:00:00Z.
FILTER_FORMULA_WITH_DATE = (
    "AND("
    "NOT({categorie}), "
    "IS_AFTER({indexed_at}, '2026-05-12T23:59:59.999Z')"
    ")"
)
# Variante "no-date-filter" : ramene TOUTES les annonces sans categorie,
# toutes dates confondues. Pour rattraper du backlog legacy via --no-date-filter.
FILTER_FORMULA_NO_DATE = "NOT({categorie})"

# Taille de lot pour l'extraction LLM (matchee a MAX_BATCH_SIZE de extract_batch)
BATCH_SIZE = 20

# Champs Airtable a recuperer pour decider du no-overwrite.
FIELDS_TO_FETCH = [
    "url",
    "titre",
    "description",
    "categorie",
    "indexed_at",
    "marque",
    "modele",
    "annee_fabrication",
    "etat",
    "type_vente",
]

# Champs conditionnellement remplis (no-overwrite si deja remplis).
CONDITIONAL_FIELDS = ("marque", "modele", "annee_fabrication", "etat", "type_vente")


# =============================================================================
# Airtable I/O
# =============================================================================

def fetch_candidates(
    limit: Optional[int] = None,
    no_date_filter: bool = False,
) -> list:
    """Recupere les annonces sans categorie depuis Airtable.

    `no_date_filter=True` ramene tout le backlog historique au lieu de
    juste la fenetre de la session 2026-05-13.
    """
    formula = FILTER_FORMULA_NO_DATE if no_date_filter else FILTER_FORMULA_WITH_DATE
    logger.info("[backfill] fetch : filtre = %s", formula)
    records = airtable.list_all_annonces(
        fields=FIELDS_TO_FETCH,
        filter_formula=formula,
    )
    total_match = len(records)
    logger.info("[backfill] %d candidats matchent le filtre (avant --limit)", total_match)
    if limit:
        records = records[:limit]
        logger.info("[backfill] --limit applique : %d records traites sur %d", limit, total_match)
    logger.info("[backfill] %d candidats a traiter", len(records))
    return records


# =============================================================================
# Construction du patch
# =============================================================================

def build_patch_fields(current_fields: dict, extracted: dict) -> dict:
    """Construit le dict `fields` du PATCH Airtable selon la regle :

    - `categorie` : toujours (le filtre garantit qu'elle est vide)
    - autres : on n'ecrit QUE si la cellule actuelle est vide ET que la valeur
      proposee par Haiku est utilisable (non None, non chaine vide).

    On evite ainsi tout overwrite de donnees deja saisies/scrappees.
    """
    patch: dict = {}

    cat = extracted.get("categorie")
    if cat:
        patch["categorie"] = cat

    for key in CONDITIONAL_FIELDS:
        if current_fields.get(key):
            continue
        new_val = extracted.get(key)
        if new_val is None:
            continue
        if isinstance(new_val, str) and not new_val.strip():
            continue
        patch[key] = new_val

    return patch


# =============================================================================
# Orchestration
# =============================================================================

def run_backfill(
    dry_run: bool = False,
    limit: Optional[int] = None,
    no_date_filter: bool = False,
) -> dict:
    """Execute le backfill.

    Retourne {"checked": N, "updated": M, "errors": E}.
    """
    start = time.time()
    logger.info(
        "[backfill] start dry_run=%s limit=%s no_date_filter=%s batch_size=%d",
        dry_run, limit, no_date_filter, BATCH_SIZE,
    )

    records = fetch_candidates(limit=limit, no_date_filter=no_date_filter)
    checked = len(records)
    if checked == 0:
        suffix = " (DRY-RUN)" if dry_run else ""
        logger.info("[backfill] Aucun candidat, fin.")
        logger.info("checked=0 updated=0 errors=0%s", suffix)
        return {"checked": 0, "updated": 0, "errors": 0}

    total_batches = (checked + BATCH_SIZE - 1) // BATCH_SIZE

    # ------------------------------------------------------------------
    # DRY-RUN : aucun appel Haiku, aucun PATCH Airtable.
    # On enumere juste les batches et un echantillon de titres pour
    # visualiser ce qui serait traite. Aucun credit Anthropic consomme.
    # ------------------------------------------------------------------
    if dry_run:
        logger.info(
            "[backfill] DRY-RUN actif : 0 appel Haiku, 0 ecriture Airtable",
        )
        sample = records[:5]
        if sample:
            logger.info("[backfill] echantillon (5 premiers titres) :")
            for r in sample:
                titre = (r.get("fields", {}).get("titre") or "(sans titre)")[:120]
                logger.info("[backfill]   - %s : %s", r.get("id"), titre)
        for i in range(0, checked, BATCH_SIZE):
            batch = records[i:i + BATCH_SIZE]
            batch_num = i // BATCH_SIZE + 1
            logger.info(
                "[backfill] DRY-RUN : batch %d/%d - would extract for %d records",
                batch_num, total_batches, len(batch),
            )
        logger.info("[backfill] termine en %.1f s", time.time() - start)
        logger.info("checked=%d updated=0 errors=0 (DRY-RUN)", checked)
        return {"checked": checked, "updated": 0, "errors": 0}

    # ------------------------------------------------------------------
    # Mode live : init LLM + boucle batch + PATCH Airtable.
    # ------------------------------------------------------------------
    try:
        llm = LLMExtractor()
    except Exception as e:
        logger.error("[backfill] init LLMExtractor a echoue : %s", e)
        logger.info("checked=%d updated=0 errors=%d", checked, checked)
        return {"checked": checked, "updated": 0, "errors": checked}

    all_updates: list[dict] = []
    errors = 0

    for i in range(0, checked, BATCH_SIZE):
        batch = records[i:i + BATCH_SIZE]
        batch_num = i // BATCH_SIZE + 1

        logger.info(
            "[backfill] batch %d/%d (%d records)",
            batch_num, total_batches, len(batch),
        )

        items = [
            {
                "titre": (r.get("fields", {}).get("titre") or "")[:200],
                "description": (r.get("fields", {}).get("description") or "")[:400],
            }
            for r in batch
        ]

        try:
            extracted_list = llm.extract_batch(items)
        except Exception as e:
            logger.error("[backfill] batch %d : extract_batch a echoue : %s", batch_num, e)
            errors += len(batch)
            continue

        if len(extracted_list) != len(batch):
            logger.error(
                "[backfill] batch %d : mismatch %d resultats pour %d items, skip batch",
                batch_num, len(extracted_list), len(batch),
            )
            errors += len(batch)
            continue

        for record, extracted in zip(batch, extracted_list):
            current_fields = record.get("fields", {})
            patch_fields = build_patch_fields(current_fields, extracted)

            logger.debug(
                "[backfill] %s : current=%s extracted=%s -> patch=%s",
                record.get("id"),
                {k: current_fields.get(k) for k in CONDITIONAL_FIELDS},
                extracted,
                patch_fields,
            )

            if not patch_fields:
                logger.warning(
                    "[backfill] %s : aucun champ a patcher (extracted=%r), skip",
                    record.get("id"), extracted,
                )
                continue

            all_updates.append({
                "id": record["id"],
                "fields": patch_fields,
            })

        # Rate limit leger entre appels Haiku.
        time.sleep(0.5)

    logger.info(
        "[backfill] %d patches prepares (sur %d candidats, %d erreurs LLM)",
        len(all_updates), checked, errors,
    )

    updated = 0
    if all_updates:
        result = airtable.batch_update_annonces(all_updates)
        updated = result["updated"]
        errors += result["errors"]
        logger.info(
            "[backfill] PATCH termine : updated=%d errors_patch=%d (sur %d)",
            result["updated"], result["errors"], result["total"],
        )

    duration = time.time() - start
    logger.info("[backfill] termine en %.1f s", duration)
    logger.info("checked=%d updated=%d errors=%d", checked, updated, errors)

    return {"checked": checked, "updated": updated, "errors": errors}


# =============================================================================
# CLI
# =============================================================================

def main() -> int:
    parser = argparse.ArgumentParser(
        description="One-shot backfill Haiku (session 2026-05-13 ou backlog legacy)",
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
        "--no-date-filter", action="store_true",
        help="Ignore la borne 2026-05-13 et ramene TOUS les records sans categorie (legacy catchup)",
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
            no_date_filter=args.no_date_filter,
        )
    except Exception as e:
        logger.error("[backfill] FATAL : %s", e, exc_info=True)
        return 3
    return 0 if result["errors"] == 0 else 2


if __name__ == "__main__":
    sys.exit(main())
