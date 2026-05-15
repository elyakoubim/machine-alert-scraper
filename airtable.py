"""
Machine Alert — Client Airtable (v2)
======================================

Interface avec la base Airtable "Machine Alert MVP" :
    - Table Annonces  : push des annonces scrapées (17 colonnes — dont marque,
                        modele, annee_fabrication, etat extraits par LLMExtractor)
    - Table Sources   : mise à jour du statut des runs

IMPORTANT — SÉCURITÉ :
    Le champ `alerte_envoyee` de la table Annonces est géré par Make.
    Ce module NE DOIT JAMAIS l'écrire, sinon on risque de re-déclencher
    des alertes déjà envoyées ou d'annuler des alertes en cours.

Rate limiting :
    Airtable impose 5 req/sec/base. Le batch de 10 records par POST
    permet de rester largement sous la limite.
"""

from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timezone
from typing import Iterable, Optional

import requests

from scrapers.base import Annonce

logger = logging.getLogger(__name__)


AIRTABLE_TOKEN = os.getenv("AIRTABLE_TOKEN")
BASE_ID = os.getenv("AIRTABLE_BASE_ID", "appQrNOm3Q7D9uEed")
ANNONCES_TABLE = os.getenv("AIRTABLE_ANNONCES_TABLE", "tbljV9HICBsPyqjQk")
SOURCES_TABLE = os.getenv("AIRTABLE_SOURCES_TABLE", "tblPaOrQekEMdaW5x")
PROFILS_TABLE = os.getenv("AIRTABLE_PROFILS_TABLE", "tblXhuS0M1TtRuSel")

API_BASE = f"https://api.airtable.com/v0/{BASE_ID}"
HTTP_TIMEOUT = 20.0

# Garde-fou anti boucle infinie pour list_all_annonces.
# MAX_PAGES * 100 records/page = plafond dur. 1000 pages = 100k records,
# donne ~12 mois de runway au rythme actuel (base ~34k au 2026-05-15 apres
# premier scrape Surplex hybride). Au-dessus = raise (vraie anomalie).
# WARN_PAGES declenche un log WARNING preventif pour anticiper l'echeance.
MAX_PAGES = 1000
WARN_PAGES = 500


def _headers() -> dict:
    token = os.getenv("AIRTABLE_TOKEN") or AIRTABLE_TOKEN
    if not token:
        raise RuntimeError(
            "airtable : AIRTABLE_TOKEN absent. Definis-le dans Railway "
            "Variables ou en local via .env."
        )
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }


def get_existing_urls(source_nom: str) -> set:
    urls = set()
    offset = None
    page = 0
    while True:
        page += 1
        params = {
            "filterByFormula": f'{{source}}="{_escape_formula_value(source_nom)}"',
            "fields[]": "url",
            "pageSize": 100,
        }
        if offset:
            params["offset"] = offset
        try:
            resp = requests.get(
                f"{API_BASE}/{ANNONCES_TABLE}",
                headers=_headers(),
                params=params,
                timeout=HTTP_TIMEOUT,
            )
            resp.raise_for_status()
        except requests.RequestException as e:
            logger.error("[airtable] get_existing_urls(%s) page %d : %s", source_nom, page, e)
            break
        data = resp.json()
        for record in data.get("records", []):
            url = record.get("fields", {}).get("url", "")
            if url:
                urls.add(url)
        offset = data.get("offset")
        if not offset:
            break
        time.sleep(0.25)
    logger.info("[airtable] %d URLs existantes chargees pour %s", len(urls), source_nom)
    return urls


def push_annonces(annonces: Iterable, source_nom: str) -> dict:
    annonces = list(annonces)
    total = len(annonces)
    if total == 0:
        return {"pushed": 0, "skipped": 0, "errors": 0, "total": 0}

    existing = get_existing_urls(source_nom)
    seen_in_batch = set()
    new_annonces = []
    for a in annonces:
        if a.url in existing:
            continue
        if a.url in seen_in_batch:
            continue
        new_annonces.append(a)
        seen_in_batch.add(a.url)

    skipped = total - len(new_annonces)
    logger.info(
        "[%s] %d nouvelles annonces a pousser, %d doublons ignores",
        source_nom, len(new_annonces), skipped,
    )

    pushed = 0
    errors = 0
    for i in range(0, len(new_annonces), 10):
        batch = new_annonces[i:i + 10]
        records = [{"fields": a.to_airtable()} for a in batch]
        try:
            resp = _post_records_with_retry(records)
            if resp.status_code == 200:
                pushed += len(batch)
            else:
                errors += len(batch)
                logger.error("[%s] Airtable HTTP %d : %s", source_nom, resp.status_code, resp.text[:300])
        except requests.RequestException as e:
            errors += len(batch)
            logger.error("[%s] erreur reseau push batch : %s", source_nom, e)
        time.sleep(0.25)

    return {"pushed": pushed, "skipped": skipped, "errors": errors, "total": total}


def _post_records_with_retry(records, max_retries=1):
    attempt = 0
    while True:
        try:
            resp = requests.post(
                f"{API_BASE}/{ANNONCES_TABLE}",
                headers=_headers(),
                json={"records": records, "typecast": True},
                timeout=HTTP_TIMEOUT,
            )
        except (requests.Timeout, requests.ConnectionError):
            if attempt >= max_retries:
                raise
            attempt += 1
            time.sleep(1.0 * attempt)
            continue
        if resp.status_code == 429 or 500 <= resp.status_code < 600:
            if attempt >= max_retries:
                return resp
            attempt += 1
            time.sleep(2.0 * attempt)
            continue
        return resp


def _get_records_page_with_retry(params: dict, max_retries: int = 2) -> dict:
    """GET une page de records depuis ANNONCES_TABLE avec retry sur erreurs transitoires.

    Retry sur : Timeout, ConnectionError, HTTP 429, HTTP 5xx.
    Backoff : 1s avant retry 1, 3s avant retry 2 (3 tentatives au total).
    Apres retries epuises : RAISE (ne retourne PAS partial silencieusement).
    Sur 4xx autres que 429 : RAISE immediatement (pas de retry).
    """
    backoffs = [1.0, 3.0]
    attempt = 0
    while True:
        try:
            resp = requests.get(
                f"{API_BASE}/{ANNONCES_TABLE}",
                headers=_headers(),
                params=params,
                timeout=HTTP_TIMEOUT,
            )
        except (requests.Timeout, requests.ConnectionError):
            if attempt >= max_retries:
                raise
            time.sleep(backoffs[attempt])
            attempt += 1
            continue
        if resp.status_code == 429 or 500 <= resp.status_code < 600:
            if attempt >= max_retries:
                resp.raise_for_status()  # raise HTTPError
            time.sleep(backoffs[attempt])
            attempt += 1
            continue
        resp.raise_for_status()  # raise pour 4xx autres que 429
        return resp.json()


def update_source_status(source_nom: str, status: str = "OK", error: Optional[str] = None) -> None:
    try:
        search = requests.get(
            f"{API_BASE}/{SOURCES_TABLE}",
            headers=_headers(),
            params={
                "filterByFormula": f'{{nom}}="{_escape_formula_value(source_nom)}"',
                "fields[]": "nom",
                "maxRecords": 1,
            },
            timeout=HTTP_TIMEOUT,
        )
        search.raise_for_status()
        records = search.json().get("records", [])
        if not records:
            logger.warning("[airtable] update_source_status : source %r absente, skip", source_nom)
            return
        record_id = records[0]["id"]
        fields = {
            "dernier_scraping": datetime.now(timezone.utc).isoformat(),
            "statut_dernier_run": status if not error else f"ERR: {str(error)[:100]}",
        }
        patch = requests.patch(
            f"{API_BASE}/{SOURCES_TABLE}/{record_id}",
            headers=_headers(),
            json={"fields": fields, "typecast": True},
            timeout=HTTP_TIMEOUT,
        )
        patch.raise_for_status()
    except Exception as e:
        logger.error("[airtable] update_source_status(%s) : %s", source_nom, e)


def list_all_annonces(
    fields: Optional[list] = None,
    only_missing_categorie: bool = False,
    filter_formula: Optional[str] = None,
) -> list:
    all_records = []
    offset = None
    page = 0
    while True:
        page += 1
        if page == WARN_PAGES:
            logger.warning(
                "[airtable] list_all_annonces : %d pages atteintes (~%d records) — "
                "plafond MAX_PAGES=%d, prevoir augmentation si croissance continue",
                page, len(all_records), MAX_PAGES,
            )
        if page > MAX_PAGES:
            raise RuntimeError(
                f"list_all_annonces : MAX_PAGES={MAX_PAGES} depasse "
                f"({len(all_records)} records charges, anomalie ?)"
            )
        params = {"pageSize": 100}
        if offset:
            params["offset"] = offset
        if fields:
            params["fields[]"] = fields
        if only_missing_categorie:
            params["filterByFormula"] = "NOT({categorie})"
        elif filter_formula:
            params["filterByFormula"] = filter_formula
        # _get_records_page_with_retry raise sur erreurs persistantes
        # (au lieu du break silencieux precedent qui retournait partial).
        data = _get_records_page_with_retry(params)
        all_records.extend(data.get("records", []))
        offset = data.get("offset")
        if not offset:
            break
        time.sleep(0.25)
    logger.info("[airtable] %d annonces totales chargees", len(all_records))
    return all_records


def update_annonce_fields(record_id: str, fields: dict) -> bool:
    if "alerte_envoyee" in fields:
        logger.warning("[airtable] tentative ecriture alerte_envoyee bloquee (gere par Make)")
        fields = {k: v for k, v in fields.items() if k != "alerte_envoyee"}
    if not fields:
        return True
    try:
        resp = requests.patch(
            f"{API_BASE}/{ANNONCES_TABLE}/{record_id}",
            headers=_headers(),
            json={"fields": fields, "typecast": True},
            timeout=HTTP_TIMEOUT,
        )
        resp.raise_for_status()
        return True
    except requests.RequestException as e:
        logger.error("[airtable] update_annonce_fields(%s) : %s", record_id, e)
        return False


def batch_update_annonces(updates: list) -> dict:
    if not updates:
        return {"updated": 0, "errors": 0, "total": 0}
    cleaned = []
    for u in updates:
        f = u.get("fields", {})
        if "alerte_envoyee" in f:
            logger.warning("[airtable] batch_update : alerte_envoyee bloque pour %s", u.get("id"))
            f = {k: v for k, v in f.items() if k != "alerte_envoyee"}
        cleaned.append({"id": u["id"], "fields": f})
    updated = 0
    errors = 0
    for i in range(0, len(cleaned), 10):
        batch = cleaned[i:i + 10]
        try:
            resp = requests.patch(
                f"{API_BASE}/{ANNONCES_TABLE}",
                headers=_headers(),
                json={"records": batch, "typecast": True},
                timeout=HTTP_TIMEOUT,
            )
            if resp.status_code == 200:
                updated += len(batch)
            else:
                errors += len(batch)
                logger.error("[airtable] batch_update HTTP %d : %s", resp.status_code, resp.text[:200])
        except requests.RequestException as e:
            errors += len(batch)
            logger.error("[airtable] batch_update erreur reseau : %s", e)
        time.sleep(0.25)
    return {"updated": updated, "errors": errors, "total": len(cleaned)}


def mark_annonces_alerte_envoyee(record_ids: list) -> dict:
    """Marque une liste de records Annonces avec alerte_envoyee=True.

    SEULE fonction du module autorisee a ecrire `alerte_envoyee`. Toutes les
    autres fonctions de mise a jour (update_annonce_fields,
    batch_update_annonces) strippent ce champ defensivement. Cette autorisation
    explicite remplace l'ancienne automation Make W7 (abandonnee 2026-05-11
    suite a la bascule vers le worker Python `alertes-worker`).

    Retourne {"updated": M, "errors": E, "total": N}.
    """
    if not record_ids:
        return {"updated": 0, "errors": 0, "total": 0}
    updated = 0
    errors = 0
    for i in range(0, len(record_ids), 10):
        batch_ids = record_ids[i:i + 10]
        batch_records = [
            {"id": rid, "fields": {"alerte_envoyee": True}}
            for rid in batch_ids
        ]
        try:
            resp = requests.patch(
                f"{API_BASE}/{ANNONCES_TABLE}",
                headers=_headers(),
                json={"records": batch_records, "typecast": True},
                timeout=HTTP_TIMEOUT,
            )
            if resp.status_code == 200:
                updated += len(batch_records)
            else:
                errors += len(batch_records)
                logger.error(
                    "[airtable] mark_alerte_envoyee HTTP %d : %s",
                    resp.status_code, resp.text[:200],
                )
        except requests.RequestException as e:
            errors += len(batch_records)
            logger.error("[airtable] mark_alerte_envoyee erreur reseau : %s", e)
        time.sleep(0.25)
    return {"updated": updated, "errors": errors, "total": len(record_ids)}


def _escape_formula_value(s: str) -> str:
    return (s or "").replace('"', "'")