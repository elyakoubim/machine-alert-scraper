"""
Machine Alert — One-shot backfill : reparer les date_fin swappees MM/DD
=======================================================================

Consequence du bug `dateutil.parser.parse(s, dayfirst=True)` dans
scrapers/base.py:_iso_or_none() (fixe dans commit precedent) : tous les
records dont la source produit un ISO 8601 dans raw['date_fin']
(Surplex, AssetOrb, NetBid, Biddit, IVG, Lueders) ont eu leur date_fin
silencieusement swappee quand jour <= 12 et mois <= 12.

Exemple : un lot Surplex avec vraie endDate = 2026-06-09 a ete stocke
en Airtable comme 2026-09-06 (9 juin -> 6 septembre). Quand le swap
donnait un mois invalide (>12), dateutil rejetait et la date originale
ISO etait preservee, donc les dates avec jour > 12 sont OK.

STRATEGIE : swap-back algorithmique, PAS de re-scrape.

Pour chaque record cible :
    1. Parse date_fin stockee (YYYY-MM-DD).
    2. Si jour > 12 OU mois > 12 → SKIP-OK (la zone de swap est exclue,
       le bug ne s'est pas declenche dessus).
    3. Sinon, calcule date_swapped = annee, mois=jour, jour=mois.
    4. Sanity check : la date_fin "plausible" est dans la fenetre
       [indexed_at - 7j, indexed_at + 180j]. On compare :
         - Si date_old est plausible : SKIP-OK (probablement deja correcte)
         - Sinon si date_swapped est plausible : SWAP
         - Sinon : INVALID (skippe avec warning)
    5. Si SWAP : PATCH date_fin = date_swapped via batch_update_annonces.

Idempotent : un re-run sur des records deja swappes les detecte comme
SKIP-OK (jour > 12 ou date_old plausible).

NE TOUCHE PAS `alerte_envoyee` (protege par airtable.batch_update_annonces).
NE TOUCHE PAS `statut_annonce` (recalcule automatiquement au prochain cron
03h30 UTC par update_annonce_statuts.py).

Usage :
    python scripts/backfill_date_fin_swap.py --dry-run --limit 50
    python scripts/backfill_date_fin_swap.py --dry-run --source Surplex --verbose
    python scripts/backfill_date_fin_swap.py --execute              # apres validation

Variables env : AIRTABLE_TOKEN.

Exit codes :
    0 = OK
    2 = au moins 1 erreur PATCH en mode --execute
    3 = exception infra non rattrapee
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import tempfile
from datetime import date, datetime, timedelta, timezone
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
logger = logging.getLogger("backfill_date_fin_swap")


# Sources dont raw['date_fin'] est un ISO 8601 (donc impactees par le bug
# dayfirst=True). Auctelia / Restlos / Legalbrokers fournissent du
# DD/MM/YYYY natif, donc dayfirst=True etait LE COMPORTEMENT VOULU pour
# eux ; on les exclut explicitement.
DEFAULT_SOURCES = (
    "Surplex", "AssetOrb", "NetBid", "Biddit", "IVG", "Lueders",
)

# Fenetre de plausibilite : date_fin doit etre dans
# [indexed_at - PLAUSIBLE_PAST_DAYS, indexed_at + PLAUSIBLE_FUTURE_DAYS].
# Justification :
#   - past=7  : un lot peut etre indexe legerement apres sa cloture si le
#               scraper tournait en retard ; au-dela c'est tres improbable.
#   - future=60 : empiriquement sur le dry-run global (25 893 records),
#               la distribution des date_fin reelles plafonne a ~p90=25j
#               apres indexed_at (median=20j). Les valeurs entre 60-180j
#               sont quasi exclusivement des dates swappees (jour reel +
#               4-6 mois). Avec FUTURE=60 on disambigue 99% des cas
#               (2538/2558 ambigus deviennent SWAP automatiques). Les 20
#               restants sont 18 palindromes 7/7 (swap == old) + 2 vrais
#               ambigus IVG a auditer a la main.
PLAUSIBLE_PAST_DAYS = 7
PLAUSIBLE_FUTURE_DAYS = 60


def parse_date(s: Optional[str]) -> Optional[date]:
    """Parse 'YYYY-MM-DD' (format Airtable Date). Renvoie None si invalide."""
    if not s:
        return None
    try:
        return datetime.strptime(s[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def is_plausible(d: date, anchor: date) -> bool:
    delta = (d - anchor).days
    return -PLAUSIBLE_PAST_DAYS <= delta <= PLAUSIBLE_FUTURE_DAYS


def try_swap(d: date) -> Optional[date]:
    """Calcule la date avec jour/mois inverses. Retourne None si invalide
    (ex: jour=06 et mois=31 donnerait 31 juin)."""
    try:
        return date(d.year, d.day, d.month)
    except ValueError:
        return None


def classify_record(date_fin_str: Optional[str], indexed_at_str: Optional[str]) -> dict:
    """Decide quoi faire pour un record donne.

    Retourne un dict {action, old, new, reason} :
        action ∈ {"SWAP", "SKIP-OK", "SKIP-NO-DATE", "SKIP-NO-INDEX",
                  "INVALID", "SKIP-AMBIGUOUS"}
    """
    d_old = parse_date(date_fin_str)
    if d_old is None:
        return {"action": "SKIP-NO-DATE", "old": date_fin_str, "new": None,
                "reason": "date_fin absent ou unparsable"}

    # Zone hors swap : si jour > 12, dateutil n'aurait pas pu swapper
    # (mois > 12 invalide), donc la date est forcement OK.
    if d_old.day > 12:
        return {"action": "SKIP-OK", "old": str(d_old), "new": None,
                "reason": f"jour={d_old.day} > 12 hors zone de swap"}
    if d_old.month > 12:
        # Impossible mais defensif
        return {"action": "INVALID", "old": str(d_old), "new": None,
                "reason": f"mois={d_old.month} > 12 (??)"}

    d_swap = try_swap(d_old)
    if d_swap is None:
        return {"action": "INVALID", "old": str(d_old), "new": None,
                "reason": "swap produit une date invalide"}

    anchor = parse_date(indexed_at_str)
    if anchor is None:
        # Pas d'indexed_at parsable -> on ne peut pas faire de sanity check
        # plausibilite. Conservateur : on skip pour eviter de casser des
        # dates qui peuvent etre OK.
        return {"action": "SKIP-NO-INDEX", "old": str(d_old), "new": str(d_swap),
                "reason": "indexed_at absent, sanity impossible"}

    old_plausible = is_plausible(d_old, anchor)
    new_plausible = is_plausible(d_swap, anchor)

    if old_plausible and not new_plausible:
        return {"action": "SKIP-OK", "old": str(d_old), "new": str(d_swap),
                "reason": "date_old plausible vs indexed_at, swap eloignerait"}
    if new_plausible and not old_plausible:
        return {"action": "SWAP", "old": str(d_old), "new": str(d_swap),
                "reason": "date_old aberrante, swap plausible"}
    if old_plausible and new_plausible:
        # Les deux sont dans la fenetre -> ambigu, on garde l'original
        # par prudence (mieux vaut une date OK qu'une date corrigee a tort).
        return {"action": "SKIP-AMBIGUOUS", "old": str(d_old), "new": str(d_swap),
                "reason": "old et swap tous deux plausibles, ambigu"}
    # Aucune des deux plausibles -> probable lot ferme depuis longtemps,
    # on garde l'original (le lifecycle worker l'a deja marque expiree).
    return {"action": "INVALID", "old": str(d_old), "new": str(d_swap),
            "reason": "ni old ni swap plausibles vs indexed_at"}


def write_jsonl(path: Path, items: list) -> None:
    with path.open("w", encoding="utf-8") as f:
        for it in items:
            f.write(json.dumps(it, ensure_ascii=False, default=str) + "\n")


def fetch_candidates(sources: tuple, limit: Optional[int]) -> list:
    """Fetch les annonces a auditer depuis Airtable."""
    src_clause = ",".join([f"{{source}}='{s}'" for s in sources])
    formula = f"AND(OR({src_clause}), {{date_fin}})"
    fields = ["url", "date_fin", "indexed_at", "source", "statut_annonce"]
    logger.info("Fetch Airtable : sources=%s, limit=%s", sources, limit)
    records = airtable.list_all_annonces(
        fields=fields,
        filter_formula=formula,
    )
    if limit is not None and len(records) > limit:
        records = records[:limit]
    logger.info("  -> %d records recuperes", len(records))
    return records


def render_table(rows: list) -> None:
    """Print un tableau lisible des decisions."""
    headers = ("recId", "source", "indexed_at", "date_fin old",
               "date_fin new", "Action")
    widths = (18, 10, 12, 14, 14, 18)
    sep = "+" + "+".join("-" * (w + 2) for w in widths) + "+"
    def row(*cells):
        return "| " + " | ".join(str(c).ljust(w) for c, w in zip(cells, widths)) + " |"
    print(sep)
    print(row(*headers))
    print(sep)
    for r in rows:
        print(row(
            r["id"][:18],
            (r["source"] or "?")[:10],
            (r["indexed_at"] or "?")[:12],
            (r["old"] or "?")[:14],
            (r["new"] or "-")[:14],
            r["action"][:18],
        ))
    print(sep)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--dry-run", action="store_true", default=True,
                        help="(defaut) affiche le plan, ne patch rien")
    parser.add_argument("--execute", action="store_true",
                        help="applique les PATCH pour de vrai "
                             "(desactive --dry-run)")
    parser.add_argument("--source", action="append",
                        help="source a auditer (repetable). Defaut : "
                             + ", ".join(DEFAULT_SOURCES))
    parser.add_argument("--limit", type=int, default=None,
                        help="limite le nombre de records audites (test)")
    parser.add_argument("--verbose", action="store_true",
                        help="log chaque decision (SKIP / SWAP / INVALID)")
    parser.add_argument("--audit-output", type=Path, default=None,
                        help="dump JSONL de TOUTES les decisions (dry-run "
                             "et execute). Utile pour analyse posterieure "
                             "des SKIP-AMBIGUOUS. Defaut : pas de dump.")
    args = parser.parse_args()

    if args.execute:
        args.dry_run = False
    sources = tuple(args.source) if args.source else DEFAULT_SOURCES

    mode = "DRY-RUN" if args.dry_run else "EXECUTE"
    logger.info("=== backfill_date_fin_swap [%s] ===", mode)
    logger.info("  sources : %s", sources)
    logger.info("  limit   : %s", args.limit or "(aucune)")

    try:
        records = fetch_candidates(sources, args.limit)
    except Exception as e:
        logger.error("fetch_candidates KO : %s", e)
        return 3

    decisions = []
    counts = {"SWAP": 0, "SKIP-OK": 0, "SKIP-NO-DATE": 0, "SKIP-NO-INDEX": 0,
              "SKIP-AMBIGUOUS": 0, "INVALID": 0}
    for rec in records:
        f = rec.get("fields", {})
        decision = classify_record(f.get("date_fin"), f.get("indexed_at"))
        item = {
            "id": rec["id"],
            "source": f.get("source"),
            "indexed_at": f.get("indexed_at"),
            "url": f.get("url"),
            **decision,
        }
        decisions.append(item)
        counts[decision["action"]] = counts.get(decision["action"], 0) + 1
        if args.verbose:
            logger.info("  %s [%s] old=%s new=%s reason=%s",
                        item["id"], item["action"], item["old"],
                        item["new"], item["reason"])

    # Affichage : si dry-run ou peu de records, on print tout. Sinon, on
    # print les 25 premiers + les 5 derniers + un sample SWAP.
    swap_items = [d for d in decisions if d["action"] == "SWAP"]
    skip_ok_items = [d for d in decisions if d["action"] == "SKIP-OK"]
    invalid_items = [d for d in decisions if d["action"] == "INVALID"]

    print()
    print(f"=== Echantillon SWAP (premiers 15 sur {len(swap_items)}) ===")
    render_table(swap_items[:15])
    print()
    print(f"=== Echantillon SKIP-OK (premiers 5 sur {len(skip_ok_items)}) ===")
    render_table(skip_ok_items[:5])
    if invalid_items:
        print()
        print(f"=== Echantillon INVALID (premiers 10 sur {len(invalid_items)}) ===")
        render_table(invalid_items[:10])

    print()
    print("=== Compteurs ===")
    total = sum(counts.values())
    for k, v in sorted(counts.items()):
        pct = (100 * v / total) if total else 0
        print(f"  {k:18s} : {v:6d}  ({pct:5.1f}%)")
    print(f"  {'TOTAL':18s} : {total:6d}")

    # Par source : breakdown des SWAP
    by_source = {}
    for d in decisions:
        s = d["source"] or "?"
        by_source.setdefault(s, {"total": 0, "swap": 0, "skip_ok": 0})
        by_source[s]["total"] += 1
        if d["action"] == "SWAP":
            by_source[s]["swap"] += 1
        elif d["action"] == "SKIP-OK":
            by_source[s]["skip_ok"] += 1
    print()
    print("=== Par source ===")
    print(f"  {'source':<14} {'total':>7} {'SWAP':>7} {'SKIP-OK':>9} {'%SWAP':>7}")
    for s in sorted(by_source.keys()):
        b = by_source[s]
        pct = (100 * b["swap"] / b["total"]) if b["total"] else 0
        print(f"  {s:<14} {b['total']:>7} {b['swap']:>7} {b['skip_ok']:>9} {pct:>6.1f}%")

    # Dump audit (optionnel en dry-run, utile pour analyser SKIP-AMBIGUOUS)
    if args.audit_output:
        write_jsonl(args.audit_output, decisions)
        logger.info("Audit JSONL dumpe dans %s", args.audit_output)

    if args.dry_run:
        print()
        print("DRY-RUN : aucun PATCH applique. Relancer avec --execute pour patcher.")
        return 0

    # Mode --execute : applique les SWAP par batch via airtable.batch_update_annonces
    # (qui throttle deja 0.25s entre batches et strip alerte_envoyee).
    updates = [
        {"id": d["id"], "fields": {"date_fin": d["new"]}}
        for d in swap_items
    ]
    if not updates:
        logger.info("Rien a patcher (0 SWAP). Exit.")
        return 0

    # ROLLBACK LOG OBLIGATOIRE avant le PATCH. Si le execute crashe a mi-
    # chemin, on peut rejouer date_fin_old depuis ce JSONL. Path : %TEMP%/
    # backfill_date_fin_rollback_<sources>_<timestamp>.jsonl
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    src_tag = "-".join(sources)[:40]
    rollback_path = Path(tempfile.gettempdir()) / (
        f"backfill_date_fin_rollback_{src_tag}_{ts}.jsonl"
    )
    rollback_rows = [
        {
            "id": d["id"],
            "source": d["source"],
            "url": d.get("url"),
            "indexed_at": d["indexed_at"],
            "date_fin_old": d["old"],
            "date_fin_new": d["new"],
            "action": d["action"],
            "ts_utc": ts,
        }
        for d in swap_items
    ]
    write_jsonl(rollback_path, rollback_rows)
    logger.info("ROLLBACK LOG ecrit : %s (%d records)",
                rollback_path, len(rollback_rows))
    logger.info("  -> en cas de desastre : pour chaque ligne, PATCH "
                "date_fin = date_fin_old via airtable.batch_update_annonces.")

    logger.info("EXECUTE : %d PATCH en cours (batch 10, throttle 250ms)...",
                len(updates))
    try:
        result = airtable.batch_update_annonces(updates)
    except Exception as e:
        logger.error("batch_update_annonces KO : %s", e)
        logger.error("Rollback log dispo : %s", rollback_path)
        return 3
    logger.info("Result : %s", result)
    logger.info("Rollback log : %s", rollback_path)
    errors = result.get("errors", 0) if isinstance(result, dict) else 0
    return 2 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
