"""
Machine Alert — Re-scrape ciblé des Lueders ACTIVES post-fix parser
====================================================================

Suite logique de `backfill_lueders_status.py` :
    - Ce script-la a marque les 5 223 lots fermes en `expirée`.
    - Reste 104 lots `aktiv` qui ont encore une description polluee
      par le cookie banner et un titre "Alle Auktionen" (legacy parser).
    - Ici on les re-scrape avec le PARSER CORRIGE (commits 355f3dc +
      226b0d4 : h2 titre, .ak-posinfo description, image regex fix,
      Auktionsstatus -> date_fin).

Workflow par record :

    1. Fetch HTML via HttpxFetcher
    2. parse_detail(html, url) -> dict raw
    3. normalize(raw, url) -> Annonce (avec LLMExtractor pour categoriser)
    4. PATCH Airtable :
        - titre, description, image_url, categorie : toujours ecrasees
          (le nouveau parser produit des donnees connues bonnes)
        - date_fin : ecrasee SI le parser en a extrait une (probablement
          rare sur les actives ; presente sur les fermes qui seraient
          tombees a l'aktiv -> fermes pendant l'audit, edge case)
        - marque, modele, annee_fabrication, etat, type_vente :
          ecrasees UNIQUEMENT si la cellule actuelle est vide
          (convention etablie pour eviter les regressions sur des
          donnees deja saisies)
    5. PAS de touche sur statut_annonce — lifecycle-worker (cron 03h30
       UTC) le recalculera au prochain run sur la base de date_fin.

NEVER touche `alerte_envoyee` (protege defensivement par
airtable.batch_update_annonces).

DRY-RUN : fetch + parse via httpx (gratuit) MAIS pas d'init LLMExtractor
ni d'appel Haiku (paid). Sortie informationnelle uniquement.

Usage :
    python scripts/backfill_lueders_active_rescrape.py --dry-run --limit 5 --verbose
    python scripts/backfill_lueders_active_rescrape.py --dry-run
    python scripts/backfill_lueders_active_rescrape.py
    python scripts/backfill_lueders_active_rescrape.py --audit-json /path/to/audit.json

Exit codes :
    0 = OK
    2 = au moins 1 erreur (scrape ou PATCH)
    3 = exception infra non rattrapee (audit absent, etc.)
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import tempfile
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
from scrapers.sites.lueders import LuedersScraper
from scrapers.fetchers import HttpxFetcher
from scrapers.llm_extractor import LLMExtractor
from scrapers.base import Annonce

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("backfill_lueders_rescrape")


DEFAULT_AUDIT_JSON = Path(tempfile.gettempdir()) / "lueders_audit.json"

# Status cible dans le JSON audit
ACTIVE_STATUS = "aktiv"

# Champs ecrits conditionnellement (no-overwrite si deja remplis)
CONDITIONAL_FIELDS = ("marque", "modele", "annee_fabrication", "etat", "type_vente")

# Sample affiche en INFO pour validation visuelle
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


def fetch_current_fields() -> dict[str, dict]:
    """Retourne {rec_id: fields_dict} pour tous les records Lueders.

    Permet la decision no-overwrite sur les champs conditionnels.
    """
    logger.info("[rescrape] fetch fields actuels des Lueders pour decisions no-overwrite...")
    recs = airtable.list_all_annonces(
        fields=list(CONDITIONAL_FIELDS),
        filter_formula='{source} = "Lueders & Partner"',
    )
    return {r["id"]: r.get("fields", {}) for r in recs}


# =============================================================================
# Patch builder
# =============================================================================

def build_patch(annonce: Annonce, current_fields: dict) -> dict:
    """Construit le dict de PATCH selon la regle :

    - titre, description, image_url, categorie : toujours ecrasees si
      le scraper a produit une valeur utilisable.
    - date_fin : ecrasee SI le parser en a extrait une.
    - 5 conditionnels : ecrasees uniquement si la cellule actuelle est vide.
    - statut_annonce : JAMAIS (lifecycle-worker handles it).
    """
    patch: dict = {}

    if annonce.titre and annonce.titre.strip():
        patch["titre"] = annonce.titre[:255]
    if annonce.description and annonce.description.strip():
        patch["description"] = annonce.description[:10000]
    if annonce.image_url and annonce.image_url.strip():
        patch["image_url"] = annonce.image_url
    if annonce.categorie:
        # base.normalize_categorie() garantit la valeur canonique
        patch["categorie"] = annonce.categorie
    if annonce.date_fin:
        patch["date_fin"] = annonce.date_fin

    for key in CONDITIONAL_FIELDS:
        if current_fields.get(key):
            continue  # deja rempli, on ne touche pas
        val = getattr(annonce, key, None)
        if val is None:
            continue
        if isinstance(val, str) and not val.strip():
            continue
        patch[key] = val

    return patch


# =============================================================================
# Orchestration
# =============================================================================

def run_rescrape(
    dry_run: bool = False,
    limit: Optional[int] = None,
    audit_json: Path = DEFAULT_AUDIT_JSON,
) -> dict:
    start = time.time()
    logger.info(
        "[rescrape] start dry_run=%s limit=%s audit_json=%s",
        dry_run, limit, audit_json,
    )

    audit = load_audit(audit_json)
    actives = [
        (rec_id, (info or {}).get("url", ""))
        for rec_id, info in audit.items()
        if (info or {}).get("site_status") == ACTIVE_STATUS
    ]
    # Filtre defensif : url presente
    actives = [(rid, u) for rid, u in actives if u]
    total_active = len(actives)
    logger.info("[rescrape] %d records actives candidats dans l'audit", total_active)

    if limit is not None and limit < total_active:
        actives = actives[:limit]
        logger.info("[rescrape] --limit applique : %d records traites", limit)

    if not actives:
        logger.info("[rescrape] rien a re-scraper, fin.")
        logger.info("[rescrape] rescraped=0 updated=0 errors=0")
        return {"rescraped": 0, "updated": 0, "errors": 0}

    # Init scraper. En dry-run : pas de LLMExtractor (economie credits Haiku).
    fetcher = HttpxFetcher()
    llm: Optional[LLMExtractor] = None
    if not dry_run:
        try:
            llm = LLMExtractor()
            logger.info("[rescrape] LLMExtractor initialise (model=%s)", llm._model)
        except Exception as e:
            logger.error("[rescrape] LLMExtractor init failed : %s", e)
            logger.info("[rescrape] rescraped=0 updated=0 errors=%d", total_active)
            return {"rescraped": 0, "updated": 0, "errors": total_active}
    else:
        logger.info("[rescrape] DRY-RUN : pas d'init LLMExtractor, pas d'appel Haiku")

    scraper = LuedersScraper(fetcher=fetcher, llm_extractor=llm)

    # Fetch des fields actuels pour decisions no-overwrite (1 seul GET paginate)
    current_fields_all = fetch_current_fields()

    rescraped = 0
    errors = 0
    updates: list[dict] = []

    for i, (rec_id, url) in enumerate(actives, start=1):
        try:
            html = scraper.fetch(url)
            raw = scraper.parse_detail(html, url)
            annonce = scraper.normalize(raw, url)
        except Exception as e:
            logger.warning("[rescrape] %s : echec scrape (%s) - %s", rec_id, type(e).__name__, e)
            errors += 1
            continue

        rescraped += 1
        current = current_fields_all.get(rec_id, {})
        patch = build_patch(annonce, current)

        # Sample en INFO pour validation visuelle (premiers SAMPLE_LOG_COUNT)
        if i <= SAMPLE_LOG_COUNT:
            logger.info(
                "[rescrape] sample %d/%d : %s",
                i, min(SAMPLE_LOG_COUNT, len(actives)), rec_id,
            )
            logger.info("    titre      : %s", (annonce.titre or "")[:120])
            logger.info("    desc len   : %d chars", len(annonce.description or ""))
            logger.info("    image_url  : %s", annonce.image_url or "(vide)")
            logger.info("    categorie  : %s", annonce.categorie)
            logger.info("    date_fin   : %s", annonce.date_fin or "(None)")
            logger.info("    patch keys : %s", sorted(patch.keys()))
        else:
            logger.debug("[rescrape] %s : patch keys=%s", rec_id, sorted(patch.keys()))

        if patch:
            updates.append({"id": rec_id, "fields": patch})

    logger.info(
        "[rescrape] %d records re-scrapes OK, %d echecs, %d patches prepares",
        rescraped, errors, len(updates),
    )

    if dry_run:
        logger.info("[rescrape] DRY-RUN : aucune ecriture Airtable")
        duration = time.time() - start
        logger.info("[rescrape] termine en %.1fs", duration)
        logger.info(
            "[rescrape] rescraped=%d updated=0 errors=%d (DRY-RUN)",
            rescraped, errors,
        )
        return {"rescraped": rescraped, "updated": 0, "errors": errors}

    updated = 0
    patch_errors = 0
    if updates:
        result = airtable.batch_update_annonces(updates)
        updated = result["updated"]
        patch_errors = result["errors"]
        logger.info(
            "[rescrape] PATCH termine : updated=%d errors_patch=%d (sur %d)",
            updated, patch_errors, result["total"],
        )
    else:
        logger.info("[rescrape] aucun patch a appliquer")

    duration = time.time() - start
    logger.info("[rescrape] termine en %.1fs", duration)
    total_errors = errors + patch_errors
    logger.info(
        "[rescrape] rescraped=%d updated=%d errors=%d",
        rescraped, updated, total_errors,
    )
    return {"rescraped": rescraped, "updated": updated, "errors": total_errors}


# =============================================================================
# CLI
# =============================================================================

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Re-scrape des Lueders actives avec le parser corrige (post-fix titre/desc/image)",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Fetch + parse (gratuit) mais pas d'appel Haiku ni de PATCH Airtable",
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
        result = run_rescrape(
            dry_run=args.dry_run,
            limit=args.limit,
            audit_json=args.audit_json,
        )
    except Exception as e:
        logger.error("[rescrape] FATAL : %s", e, exc_info=True)
        return 3
    return 0 if result["errors"] == 0 else 2


if __name__ == "__main__":
    sys.exit(main())
