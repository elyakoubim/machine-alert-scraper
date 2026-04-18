"""
Machine Alert — Client Airtable
Push des annonces + déduplication
"""

import logging
import os
import time
import requests
from scrapers.base import Annonce

logger = logging.getLogger(__name__)

AIRTABLE_TOKEN = os.getenv("AIRTABLE_TOKEN")
BASE_ID = os.getenv("AIRTABLE_BASE_ID", "appQrNOm3Q7D9uEed")
ANNONCES_TABLE = os.getenv("AIRTABLE_ANNONCES_TABLE", "tblXVFA7xJ06Ar7Re")
SOURCES_TABLE = os.getenv("AIRTABLE_SOURCES_TABLE", "tblPaOrQekEMdaW5x")

API_BASE = f"https://api.airtable.com/v0/{BASE_ID}"
HEADERS = lambda: {
    "Authorization": f"Bearer {AIRTABLE_TOKEN}",
    "Content-Type": "application/json",
}


def get_existing_urls(source_nom: str) -> set[str]:
    """Récupère les URLs déjà dans Airtable pour cette source (déduplication)"""
    urls = set()
    offset = None
    
    while True:
        params = {
            "filterByFormula": f'{{source_nom}}="{source_nom}"',
            "fields[]": "url_annonce",
            "pageSize": 100,
        }
        if offset:
            params["offset"] = offset
            
        try:
            resp = requests.get(
                f"{API_BASE}/{ANNONCES_TABLE}",
                headers=HEADERS(),
                params=params,
                timeout=15,
            )
            data = resp.json()
            for record in data.get("records", []):
                url = record.get("fields", {}).get("url_annonce", "")
                if url:
                    urls.add(url)
            
            offset = data.get("offset")
            if not offset:
                break
                
        except Exception as e:
            logger.error(f"Error fetching existing URLs for {source_nom}: {e}")
            break
    
    return urls


def push_annonces(annonces: list[Annonce], source_nom: str) -> dict:
    """Push les nouvelles annonces vers Airtable, retourne stats"""
    if not annonces:
        return {"pushed": 0, "skipped": 0, "errors": 0}
    
    # Déduplication : récupérer URLs existantes
    existing_urls = get_existing_urls(source_nom)
    logger.info(f"[{source_nom}] {len(existing_urls)} URLs already in Airtable")
    
    # Filtrer les doublons + doublons dans le batch courant
    seen_in_batch = set()
    new_annonces = []
    for a in annonces:
        if a.url not in existing_urls and a.url not in seen_in_batch:
            new_annonces.append(a)
            seen_in_batch.add(a.url)
    
    skipped = len(annonces) - len(new_annonces)
    logger.info(f"[{source_nom}] {len(new_annonces)} new annonces to push, {skipped} duplicates skipped")
    
    # Push en batches de 10 (limite Airtable)
    pushed = 0
    errors = 0
    for i in range(0, len(new_annonces), 10):
        batch = new_annonces[i:i+10]
        records = [{"fields": a.to_airtable()} for a in batch]
        
        try:
            resp = requests.post(
                f"{API_BASE}/{ANNONCES_TABLE}",
                headers=HEADERS(),
                json={"records": records},
                timeout=15,
            )
            if resp.status_code == 200:
                pushed += len(batch)
                logger.debug(f"[{source_nom}] Pushed batch {i//10 + 1}: {len(batch)} records")
            else:
                errors += len(batch)
                logger.error(f"[{source_nom}] Airtable error: {resp.status_code} — {resp.text[:200]}")
        except Exception as e:
            errors += len(batch)
            logger.error(f"[{source_nom}] Push error: {e}")
        
        time.sleep(0.25)  # Rate limit Airtable
    
    return {"pushed": pushed, "skipped": skipped, "errors": errors}


def update_source_status(source_nom: str, status: str = "OK", error: str = "") -> None:
    """Met à jour dernier_scraping et statut dans la table Sources"""
    from datetime import datetime, timezone
    
    try:
        # Trouver le record
        resp = requests.get(
            f"{API_BASE}/{SOURCES_TABLE}",
            headers=HEADERS(),
            params={"filterByFormula": f'{{nom}}="{source_nom}"', "fields[]": "nom"},
            timeout=10,
        )
        records = resp.json().get("records", [])
        if not records:
            return
        
        record_id = records[0]["id"]
        
        fields = {
            "dernier_scraping": datetime.now(timezone.utc).isoformat(),
            "statut_dernier_run": status if not error else f"ERR: {error[:100]}",
        }
        
        requests.patch(
            f"{API_BASE}/{SOURCES_TABLE}/{record_id}",
            headers=HEADERS(),
            json={"fields": fields},
            timeout=10,
        )
    except Exception as e:
        logger.error(f"Error updating source status for {source_nom}: {e}")
