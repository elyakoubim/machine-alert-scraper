"""
Machine Alert — Daily client alerts (alertes-worker)
======================================================

Envoie quotidiennement (cron 10h UTC) un email a chaque client actif
contenant les nouvelles annonces des dernieres 24h qui matchent son
profil (pays ∩ categories). Remplace l'ancienne automation Make W7.

Workflow :
    1. Fetch les Clients actifs (table Clients, filtre {actif} truthy)
    2. Fetch les Annonces des dernieres 24h non envoyees
       (filter indexed_at >= now-24h AND NOT alerte_envoyee)
    3. Pour chaque client, matche en memoire (pays ∩ categories)
    4. Top 3 trie par indexed_at DESC -> email Brevo HTML
    5. PATCH alerte_envoyee=true sur TOUTES les annonces envoyees
       (pas seulement les Top 3 affichees) via airtable.mark_annonces_alerte_envoyee
    6. Anti-doublon : si Brevo fail, on NE marque PAS -> retry le lendemain

Usage :
    python scripts/send_daily_alerts.py
    python scripts/send_daily_alerts.py --dry-run
    python scripts/send_daily_alerts.py --limit 5
    python scripts/send_daily_alerts.py --verbose

Exit codes :
    0 = OK
    2 = record errors (Brevo HTTP fail OU Airtable PATCH fail)
    3 = exception infra non rattrapee
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
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

import requests

import airtable

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("alerts")


# =============================================================================
# Configuration
# =============================================================================

AIRTABLE_TOKEN = os.getenv("AIRTABLE_TOKEN")
AIRTABLE_BASE_ID = os.getenv("AIRTABLE_BASE_ID", "appQrNOm3Q7D9uEed")
CLIENTS_TABLE_ID = os.getenv("CLIENTS_TABLE_ID", "tblFae1mWP3h1XOy1")

BREVO_API_KEY = os.getenv("BREVO_API_KEY")
BREVO_SENDER_EMAIL = os.getenv("BREVO_SENDER_EMAIL", "contact@faillink.be")
BREVO_SENDER_NAME = os.getenv("BREVO_SENDER_NAME", "Faillink")

API_BASE = f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}"
BREVO_API = "https://api.brevo.com/v3"
HTTP_TIMEOUT = 20.0

# Fenetre des annonces eligibles (24h par defaut, override via env pour debug)
RECENT_HOURS = int(os.getenv("RECENT_HOURS", "24"))
# Combien d'annonces detaillees dans l'email (le reste devient "+ X autres")
MAX_PER_EMAIL = 3
# Brevo : 14 req/sec max. On reste large : ~10 req/sec si >10 clients.
BREVO_RATE_LIMIT_SLEEP = 0.1
# Garde-fou pagination Clients table (50 pages * 100 = 5000 clients, large)
MAX_CLIENTS_PAGES = 50


# =============================================================================
# Filtre Airtable (annonces recentes non envoyees)
# =============================================================================

def make_recent_unsent_filter(now: datetime) -> str:
    """Annonces indexees dans les RECENT_HOURS dernieres heures, non envoyees.

    On gele le cutoff cote Python (au lieu de NOW() Airtable non-deterministe
    entre les pages, cf bug Chantier B).
    """
    cutoff = (now - timedelta(hours=RECENT_HOURS)).astimezone(timezone.utc).replace(microsecond=0)
    cutoff_str = cutoff.strftime("%Y-%m-%dT%H:%M:%SZ")
    return (
        'AND('
        'IS_AFTER({indexed_at}, DATETIME_PARSE("' + cutoff_str + '")),'
        'NOT({alerte_envoyee})'
        ')'
    )


# =============================================================================
# Airtable I/O
# =============================================================================

def _airtable_headers() -> dict:
    if not AIRTABLE_TOKEN:
        raise RuntimeError("AIRTABLE_TOKEN absent (env var)")
    return {
        "Authorization": f"Bearer {AIRTABLE_TOKEN}",
        "Content-Type": "application/json",
    }


def _fetch_active_clients() -> list:
    """Fetch tous les Clients avec {actif} truthy.

    Note : Clients table n'est pas dans airtable.py (qui est scope Annonces),
    donc on fait le GET inline. Pagination + retry simple (les Clients sont
    peu nombreux, max ~quelques centaines).
    """
    all_records: list = []
    offset = None
    page = 0
    while True:
        page += 1
        if page > MAX_CLIENTS_PAGES:
            raise RuntimeError(
                f"_fetch_active_clients : MAX_CLIENTS_PAGES={MAX_CLIENTS_PAGES} depasse"
            )
        params = {
            "filterByFormula": "{actif}",
            "pageSize": 100,
            "fields[]": ["email", "prenom", "pays", "categories"],
        }
        if offset:
            params["offset"] = offset
        resp = requests.get(
            f"{API_BASE}/{CLIENTS_TABLE_ID}",
            headers=_airtable_headers(),
            params=params,
            timeout=HTTP_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
        all_records.extend(data.get("records", []))
        offset = data.get("offset")
        if not offset:
            break
        time.sleep(0.25)
    logger.info("[alerts] %d clients actifs charges", len(all_records))
    return all_records


def _fetch_recent_unsent_annonces(now: datetime) -> list:
    """Fetch les annonces eligibles (24h, non envoyees) via airtable.list_all_annonces."""
    formula = make_recent_unsent_filter(now)
    fields = [
        "url", "titre", "prix", "pays", "categorie", "marque", "modele",
        "type_vente", "date_fin", "indexed_at", "source",
    ]
    annonces = airtable.list_all_annonces(
        fields=fields,
        filter_formula=formula,
    )
    logger.info("[alerts] %d annonces recentes non envoyees", len(annonces))
    return annonces


# =============================================================================
# Logique pure (testable sans I/O)
# =============================================================================

def _values_set(val) -> set:
    """Normalise une valeur Airtable (None / str / list) en set.

    Airtable renvoie les champs single-select comme str et multi-select comme
    list, parfois meme single-select comme list quand typecast=true a converti
    une valeur. On est defensif sur les deux formats.
    """
    if val is None:
        return set()
    if isinstance(val, list):
        return set(val)
    return {val}


def _format_field_value(val) -> str:
    """Affiche une valeur Airtable (str / list / None) en string lisible."""
    if val is None:
        return "—"
    if isinstance(val, list):
        return ", ".join(str(v) for v in val) if val else "—"
    s = str(val)
    return s if s else "—"


def match_client_to_annonces(client: dict, annonces: list) -> list:
    """Retourne les annonces qui matchent le profil du client.

    Match = intersection non vide sur pays ET sur categories.
    Robuste aux formats str / list cote Airtable (typecast multi-select).
    Sortie triee par indexed_at DESC. Pure : pas d'I/O.
    """
    fields = client.get("fields", {})
    pays_set = _values_set(fields.get("pays"))
    cats_set = _values_set(fields.get("categories"))
    if not pays_set or not cats_set:
        return []
    matches = []
    for annonce in annonces:
        af = annonce.get("fields", {})
        ap_set = _values_set(af.get("pays"))
        ac_set = _values_set(af.get("categorie"))
        if (ap_set & pays_set) and (ac_set & cats_set):
            matches.append(annonce)
    matches.sort(
        key=lambda a: a.get("fields", {}).get("indexed_at", ""),
        reverse=True,
    )
    return matches


def _format_date_fin(date_fin: str) -> str:
    """Convertit un ISO datetime en JJ/MM/AAAA, ou renvoie tel quel si parse fail."""
    if not date_fin:
        return ""
    try:
        from dateutil import parser as dateparser
        return dateparser.parse(date_fin).strftime("%d/%m/%Y")
    except Exception:
        return date_fin[:10]


def _format_annonce_card_html(annonce: dict) -> str:
    """Construit la carte HTML d'une annonce pour l'email."""
    f = annonce.get("fields", {})
    titre = (f.get("titre") or "Sans titre")[:120]
    marque = f.get("marque") or ""
    modele = f.get("modele") or ""
    prix = f.get("prix") or "—"
    # pays et type_vente peuvent etre str OU list (multi-select Airtable)
    pays = _format_field_value(f.get("pays"))
    type_vente = _format_field_value(f.get("type_vente"))
    if type_vente == "—":
        type_vente = "Vente off-market"
    date_fin = _format_date_fin(f.get("date_fin") or "")
    url = f.get("url") or "#"

    brand_parts = [p for p in (marque, modele) if p]
    brand_line = ""
    if brand_parts:
        brand_line = (
            '<div style="font-weight: 600; font-size: 14px; margin-top: 4px;">'
            + " ".join(brand_parts)
            + '</div>'
        )

    date_line = ""
    if date_fin:
        date_line = (
            '<div style="font-size: 12px; color: #888; margin-top: 4px;">'
            f'Fin de vente : {date_fin}'
            '</div>'
        )

    return (
        '<div style="margin: 16px 0; padding: 16px; border: 1px solid #e0e0e0; '
        'border-radius: 8px; background: #fafafa;">'
        f'<div style="font-size: 16px; font-weight: 600; color: #1a1a1a;">{titre}</div>'
        + brand_line +
        '<div style="margin-top: 8px; font-size: 14px; color: #444;">'
        f'<span>💰 {prix}</span> &nbsp;·&nbsp; '
        f'<span>📍 {pays}</span> &nbsp;·&nbsp; '
        f'<span>🏷️ {type_vente}</span>'
        '</div>'
        + date_line +
        f'<a href="{url}" style="display: inline-block; margin-top: 12px; '
        'color: #ffaa00; font-weight: 600; text-decoration: none;">'
        'Voir l\'annonce →</a>'
        '</div>'
    )


def build_alert_email_html(client: dict, top_n: list, total_count: int) -> str:
    """Construit le HTML complet de l'email d'alerte client."""
    f = client.get("fields", {})
    prenom = (f.get("prenom") or "cher entrepreneur").strip()

    cards_html = "".join(_format_annonce_card_html(a) for a in top_n)

    plural = total_count > 1
    intro = (
        f"Voici {total_count} nouvelle{'s' if plural else ''} "
        f"opportunité{'s' if plural else ''} "
        f"qui correspondent à votre profil aujourd'hui."
    )

    extras = total_count - len(top_n)
    extras_block = ""
    if extras > 0:
        extras_block = (
            '<div style="margin: 24px 0; padding: 16px; background: #f0f4ff; '
            'border-radius: 8px; text-align: center;">'
            '<div style="font-weight: 600; color: #1a1a1a;">'
            f'+ {extras} autre{"s" if extras > 1 else ""} annonce'
            f'{"s" if extras > 1 else ""} aujourd\'hui'
            '</div>'
            '<a href="https://app.faillink.be" '
            'style="display: inline-block; margin-top: 12px; background: #1a1a1a; '
            'color: white; padding: 10px 20px; border-radius: 6px; '
            'text-decoration: none; font-weight: 600;">'
            'Voir toutes mes alertes</a>'
            '</div>'
        )

    return f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"><title>Vos alertes Faillink</title></head>
<body style="font-family: -apple-system, Helvetica, sans-serif; max-width: 600px; margin: 0 auto; padding: 24px;">

  <h1 style="color: #1a1a1a;">Bonjour {prenom},</h1>

  <p>{intro}</p>

  {cards_html}

  {extras_block}

  <p style="color: #999; font-size: 12px; margin-top: 32px;">
    Faillink — Alertes B2B sur opportunités off-market en Europe<br>
    <a href="https://app.faillink.be/mon-compte" style="color: #999;">Gérer mes préférences</a>
  </p>

</body>
</html>"""


# =============================================================================
# Brevo (envoi email)
# =============================================================================

def send_alert_email(client: dict, top_n: list, total_count: int) -> bool:
    """Envoie l'email d'alerte au client via Brevo. Retourne True si succes.

    Sur echec (HTTP non-200, exception reseau, BREVO_API_KEY absent) : log + False.
    Le caller doit alors NE PAS marquer les annonces comme envoyees.
    """
    if not BREVO_API_KEY:
        logger.warning("[alerts] BREVO_API_KEY absent — email NON envoye (skip)")
        return False
    f = client.get("fields", {})
    to_email = f.get("email")
    if not to_email:
        logger.warning("[alerts] client %s sans email, skip", client.get("id"))
        return False
    to_name = f.get("prenom") or to_email
    plural = total_count > 1
    subject = (
        f"🔔 Faillink — {total_count} nouvelle{'s' if plural else ''} "
        f"opportunité{'s' if plural else ''} aujourd'hui"
    )
    html_content = build_alert_email_html(client, top_n, total_count)
    try:
        resp = requests.post(
            f"{BREVO_API}/smtp/email",
            headers={
                "api-key": BREVO_API_KEY,
                "Content-Type": "application/json",
                "accept": "application/json",
            },
            json={
                "sender": {"name": BREVO_SENDER_NAME, "email": BREVO_SENDER_EMAIL},
                "to": [{"email": to_email, "name": to_name}],
                "subject": subject,
                "htmlContent": html_content,
            },
            timeout=15,
        )
        if resp.status_code in (200, 201):
            msg_id = resp.json().get("messageId", "?")
            logger.info(
                "[alerts] envoye %s (msgId=%s, total=%d, top=%d)",
                to_email, msg_id, total_count, len(top_n),
            )
            return True
        logger.error(
            "[alerts] Brevo HTTP %d pour %s : %s",
            resp.status_code, to_email, resp.text[:300],
        )
        return False
    except requests.RequestException as e:
        logger.error("[alerts] erreur reseau Brevo pour %s : %s", to_email, e)
        return False


# =============================================================================
# Orchestration
# =============================================================================

def run_alerts(dry_run: bool = False, limit: Optional[int] = None) -> dict:
    """Execute le run quotidien. Retourne le dict de stats."""
    now = datetime.now(timezone.utc)
    logger.info(
        "[alerts] start dry_run=%s limit=%s now=%s RECENT_HOURS=%d MAX_PER_EMAIL=%d",
        dry_run, limit, now.isoformat(), RECENT_HOURS, MAX_PER_EMAIL,
    )

    clients = _fetch_active_clients()
    if limit:
        clients = clients[:limit]
        logger.info("[alerts] limit applique : %d clients", limit)
    if not clients:
        logger.info("[alerts] Aucun client actif, fin.")
        return _empty_result()

    annonces = _fetch_recent_unsent_annonces(now)
    if not annonces:
        logger.info("[alerts] Aucune annonce recente non envoyee, fin.")
        return {
            "checked_clients": len(clients),
            "clients_with_matches": 0,
            "emails_sent": 0,
            "annonces_marked": 0,
            "errors": 0,
        }

    clients_with_matches = 0
    emails_sent = 0
    annonce_ids_to_mark: set = set()
    errors = 0

    for client in clients:
        f = client.get("fields", {})
        email = f.get("email")
        prenom = f.get("prenom") or "(no prenom)"

        if not email:
            logger.warning("[alerts] client %s sans email, skip", client.get("id"))
            continue
        if not f.get("pays") or not f.get("categories"):
            logger.warning(
                "[alerts] client %s (%s) pays/categories vide, skip",
                client.get("id"), email,
            )
            continue

        matches = match_client_to_annonces(client, annonces)
        if not matches:
            logger.debug("[alerts] %s : 0 match", email)
            continue

        clients_with_matches += 1
        top_n = matches[:MAX_PER_EMAIL]
        total = len(matches)
        logger.info(
            "[alerts] %s (%s) : %d matches, %d affichees",
            email, prenom, total, len(top_n),
        )

        if dry_run:
            logger.info(
                "[alerts] DRY-RUN : email NON envoye, %d annonces NON marquees",
                total,
            )
            # Rendu HTML preview (visible en --verbose) pour validation visuelle
            rendered_html = build_alert_email_html(client, top_n, total)
            logger.debug(
                "[alerts] HTML preview (%d chars) pour %s :\n%s\n--- END HTML ---",
                len(rendered_html), email, rendered_html,
            )
            for a in matches:
                af = a.get("fields", {})
                logger.debug(
                    "  match: %s | marque=%s | modele=%s | prix=%s",
                    (af.get("titre") or "")[:60],
                    af.get("marque"), af.get("modele"), af.get("prix"),
                )
            continue

        sent = send_alert_email(client, top_n, total)
        if sent:
            emails_sent += 1
            for a in matches:
                aid = a.get("id")
                if aid:
                    annonce_ids_to_mark.add(aid)
        else:
            errors += 1

        # Rate limit Brevo seulement si volume eleve
        if len(clients) > 10:
            time.sleep(BREVO_RATE_LIMIT_SLEEP)

    annonces_marked = 0
    if dry_run:
        logger.info(
            "[alerts] DRY-RUN : %d annonces auraient ete marquees alerte_envoyee=true",
            len(annonce_ids_to_mark),
        )
    elif annonce_ids_to_mark:
        result = airtable.mark_annonces_alerte_envoyee(list(annonce_ids_to_mark))
        annonces_marked = result["updated"]
        errors += result["errors"]
        logger.info(
            "[alerts] marquees alerte_envoyee=true : %d (errors=%d)",
            annonces_marked, result["errors"],
        )

    logger.info(
        "[alerts] FINAL : checked_clients=%d | clients_with_matches=%d | "
        "emails_sent=%d | annonces_marked=%d | errors=%d",
        len(clients), clients_with_matches, emails_sent, annonces_marked, errors,
    )

    return {
        "checked_clients": len(clients),
        "clients_with_matches": clients_with_matches,
        "emails_sent": emails_sent,
        "annonces_marked": annonces_marked,
        "errors": errors,
    }


def _empty_result() -> dict:
    return {
        "checked_clients": 0,
        "clients_with_matches": 0,
        "emails_sent": 0,
        "annonces_marked": 0,
        "errors": 0,
    }


# =============================================================================
# CLI
# =============================================================================

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Envoie les alertes quotidiennes aux clients Faillink actifs",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="N'envoie pas Brevo, ne PATCH pas Airtable",
    )
    parser.add_argument(
        "--limit", type=int, default=None,
        help="Limite N clients (pour tester)",
    )
    parser.add_argument(
        "--verbose", action="store_true",
        help="Active les logs DEBUG (matchs detailles)",
    )
    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)
        logger.setLevel(logging.DEBUG)

    try:
        result = run_alerts(dry_run=args.dry_run, limit=args.limit)
    except Exception as e:
        logger.error("[alerts] FATAL : %s", e, exc_info=True)
        return 3
    return 0 if result["errors"] == 0 else 2


if __name__ == "__main__":
    sys.exit(main())
