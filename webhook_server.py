"""
Faillink — Webhook Server pour funnel essai gratuit (Tally → Make → Python)

Reçoit le webhook Tally du formulaire 5BEkrv et :
  1. Parse le payload (UUIDs MULTI_SELECT → labels textuels via options[])
  2. Cherche 5 annonces dans Airtable Annonces matchant catégories + pays du lead
  3. Envoie un email teasé via Brevo (template simple HTML)
  4. Crée le lead dans Airtable Leads_Essai
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
import time
from datetime import datetime, timezone
from typing import Optional

import requests
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)

# ============================================================================
# Configuration (depuis variables d'environnement)
# ============================================================================
AIRTABLE_TOKEN = os.getenv("AIRTABLE_TOKEN")
AIRTABLE_BASE_ID = os.getenv("AIRTABLE_BASE_ID", "appQrNOm3Q7D9uEed")
AIRTABLE_ANNONCES_TABLE = os.getenv("AIRTABLE_ANNONCES_TABLE", "tbljV9HICBsPyqjQk")
AIRTABLE_LEADS_ESSAI_TABLE = os.getenv("AIRTABLE_LEADS_ESSAI_TABLE", "tbl5LfC0pUmt4rUJl")

BREVO_API_KEY = os.getenv("BREVO_API_KEY")
BREVO_SENDER_EMAIL = os.getenv("BREVO_SENDER_EMAIL", "contact@faillink.be")
BREVO_SENDER_NAME = os.getenv("BREVO_SENDER_NAME", "Faillink")

# Stripe webhook (invoice.payment_failed -> migration de Make W8 vers Python)
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET", "")
STRIPE_CUSTOMER_PORTAL_URL = os.getenv("STRIPE_CUSTOMER_PORTAL_URL", "")
# Table Clients (lookup par stripe_customer_id)
AIRTABLE_CLIENTS_TABLE = os.getenv("CLIENTS_TABLE_ID", "tblFae1mWP3h1XOy1")
# Tolerance anti-replay sur le timestamp de la signature Stripe (±5 min)
STRIPE_SIGNATURE_TOLERANCE_SECONDS = 300

AIRTABLE_API = f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}"
BREVO_API = "https://api.brevo.com/v3"

# Mapping clé Tally → champ Lead Airtable
TALLY_KEYS = {
    "email": "question_eEZ5l0",
    "nom": "question_W09JBj",
    "entreprise": "question_aGNOgv",
    "categories": "question_6xJDyP",
    "pays": "question_7ZlXzP",
    "budget": "question_bOVZyg",
    "mots_cles": "question_A8PrG0",
}

# Domaines email "personnels" (pour scoring email_pro_ou_perso)
PERSO_EMAIL_DOMAINS = {
    "gmail.com", "yahoo.fr", "yahoo.com", "hotmail.fr", "hotmail.com",
    "outlook.fr", "outlook.com", "live.fr", "live.com", "free.fr",
    "orange.fr", "wanadoo.fr", "laposte.net", "sfr.fr", "icloud.com",
    "me.com", "aol.com", "protonmail.com",
}

# ============================================================================
# Helpers — Parsing payload Tally
# ============================================================================
def find_field(fields: list, key: str) -> Optional[dict]:
    """Trouve un field Tally par sa clé (question_xxxxx)."""
    return next((f for f in fields if f.get("key") == key), None)


def get_field_value(fields: list, key: str) -> str:
    """Récupère la valeur texte d'un field simple (INPUT_TEXT, EMAIL, TEXTAREA)."""
    field = find_field(fields, key)
    if not field:
        return ""
    val = field.get("value", "")
    return (val or "").strip()


def resolve_multi_select(fields: list, key: str) -> list:
    """
    Convertit un MULTI_SELECT/MULTIPLE_CHOICE Tally en liste de labels.
    Tally renvoie value=[UUIDs] + options=[{id, text}].
    """
    field = find_field(fields, key)
    if not field:
        return []
    value = field.get("value", []) or []
    options = field.get("options", []) or []
    options_map = {opt["id"]: opt["text"] for opt in options if "id" in opt}
    return [options_map[uid] for uid in value if uid in options_map]


def resolve_single_choice(fields: list, key: str) -> str:
    """Pour MULTIPLE_CHOICE (1 seul choix) — renvoie le label unique."""
    labels = resolve_multi_select(fields, key)
    return labels[0] if labels else ""


def format_value(val) -> str:
    """Convertit une valeur Airtable (string ou array) en string lisible."""
    if isinstance(val, list):
        return ", ".join(str(v) for v in val) if val else "—"
    return str(val) if val else "—"


def detect_email_type(email: str) -> str:
    """Retourne 'pro' ou 'perso' selon le domaine de l'email."""
    if not email or "@" not in email:
        return "perso"
    domain = email.split("@", 1)[1].lower().strip()
    return "perso" if domain in PERSO_EMAIL_DOMAINS else "pro"


# ============================================================================
# Helpers — Airtable
# ============================================================================
def airtable_headers() -> dict:
    if not AIRTABLE_TOKEN:
        raise RuntimeError("AIRTABLE_TOKEN absent")
    return {
        "Authorization": f"Bearer {AIRTABLE_TOKEN}",
        "Content-Type": "application/json",
    }


def search_annonces(categories: list, pays: list, max_results: int = 5) -> list:
    """
    Cherche dans Airtable Annonces les annonces matchant catégories ET pays du lead.
    Retourne max 5 résultats triés par indexed_at desc (plus récents).
    """
    if not categories or not pays:
        logger.warning("[search_annonces] catégories ou pays vides — pas de recherche")
        return []

    cat_clauses = ", ".join(
        f'FIND("{c}", ARRAYJOIN({{categorie}}, "|"))' for c in categories
    )
    pays_clauses = ", ".join(f'{{pays}}="{p}"' for p in pays)
    formula = f"AND(OR({cat_clauses}), OR({pays_clauses}))"

    try:
        resp = requests.get(
            f"{AIRTABLE_API}/{AIRTABLE_ANNONCES_TABLE}",
            headers=airtable_headers(),
            params={
                "filterByFormula": formula,
                "maxRecords": max_results,
                "sort[0][field]": "indexed_at",
                "sort[0][direction]": "desc",
                "fields[]": ["titre", "categorie", "pays", "type_vente", "indexed_at"],
            },
            timeout=20,
        )
        resp.raise_for_status()
        records = resp.json().get("records", [])
        logger.info("[search_annonces] %d annonces trouvées (max=%d)", len(records), max_results)
        return records
    except requests.RequestException as e:
        logger.error("[search_annonces] erreur Airtable : %s", e)
        return []


def find_client_by_stripe_customer_id(cus_id: str) -> Optional[dict]:
    """Cherche un client Airtable par stripe_customer_id.

    Retourne le record (dict avec 'id' et 'fields') ou None si introuvable
    / erreur reseau. Le caller decide quoi faire en cas de None
    (typiquement : log + return 200 OK pour eviter retry Stripe inutile).
    """
    if not cus_id:
        return None
    try:
        resp = requests.get(
            f"{AIRTABLE_API}/{AIRTABLE_CLIENTS_TABLE}",
            headers=airtable_headers(),
            params={
                "filterByFormula": f'{{stripe_customer_id}}="{cus_id}"',
                "maxRecords": 1,
                "fields[]": ["email", "prenom", "stripe_customer_id"],
            },
            timeout=20,
        )
        resp.raise_for_status()
        records = resp.json().get("records", [])
        return records[0] if records else None
    except requests.RequestException as e:
        logger.error("[stripe] find_client_by_stripe_customer_id(%s) : %s", cus_id, e)
        return None


def create_lead(lead_data: dict) -> Optional[str]:
    """Crée un nouveau record dans Airtable Leads_Essai. Retourne l'ID créé."""
    try:
        resp = requests.post(
            f"{AIRTABLE_API}/{AIRTABLE_LEADS_ESSAI_TABLE}",
            headers=airtable_headers(),
            json={"fields": lead_data, "typecast": True},
            timeout=20,
        )
        resp.raise_for_status()
        record = resp.json()
        logger.info("[create_lead] Lead créé : %s", record.get("id"))
        return record.get("id")
    except requests.RequestException as e:
        logger.error("[create_lead] erreur Airtable : %s", e)
        if hasattr(e, "response") and e.response is not None:
            logger.error("[create_lead] détail : %s", e.response.text[:500])
        return None


# ============================================================================
# Helpers — Brevo
# ============================================================================
def send_email_essai_simple(
    to_email: str,
    to_name: str,
    annonces: list,
    categories: list,
    pays: list,
) -> bool:
    """Envoie un email teasé simple via Brevo (HTML brut, pas de template)."""
    if not BREVO_API_KEY:
        logger.warning("[send_email] BREVO_API_KEY absent — email NON envoyé (skip)")
        return False

    annonces_html = ""
    for i, rec in enumerate(annonces, 1):
        f = rec.get("fields", {})
        cat = format_value(f.get("categorie"))
        py = format_value(f.get("pays"))
        type_v = format_value(f.get("type_vente")) if f.get("type_vente") else "Vente off-market"
        annonces_html += f"""
        <div style="margin: 16px 0; padding: 16px; border: 1px solid #e0e0e0; border-radius: 8px; background: #fafafa;">
          <div style="font-size: 14px; color: #666;">Annonce {i} sur {len(annonces)}</div>
          <div style="font-size: 16px; font-weight: 600; margin-top: 8px;">📍 {py} · 🏷️ {cat}</div>
          <div style="font-size: 14px; color: #444; margin-top: 8px;">Type : {type_v}</div>
        </div>
        """

    if not annonces:
        annonces_html = """
        <p style="color: #888;">
          Aucune annonce ne correspond exactement à votre profil aujourd'hui, mais notre base
          s'enrichit chaque nuit. Vous serez notifié dès qu'une opportunité matche vos critères.
        </p>
        """

    cat_str = ", ".join(categories) if categories else "—"
    pays_str = ", ".join(pays) if pays else "—"

    html_content = f"""
<!DOCTYPE html>
<html>
<head><meta charset="utf-8"><title>Votre essai Faillink</title></head>
<body style="font-family: -apple-system, Helvetica, sans-serif; max-width: 600px; margin: 0 auto; padding: 24px;">

  <h1 style="color: #1a1a1a;">Bonjour {to_name or 'cher entrepreneur'},</h1>

  <p>Merci pour votre intérêt pour Faillink. Voici un aperçu de notre flux d'alertes off-market en Europe.</p>

  <div style="background: #f0f4ff; padding: 16px; border-radius: 8px; margin: 16px 0;">
    <strong>Votre profil :</strong><br>
    Catégories : {cat_str}<br>
    Pays : {pays_str}
  </div>

  <h2 style="color: #1a1a1a; margin-top: 32px;">5 dernières annonces correspondant à votre profil</h2>

  <p style="color: #666; font-size: 14px;">
    <em>Pour préserver l'avantage concurrentiel de nos abonnés actifs, les détails (marque, modèle, prix exact, lien source)
    sont réservés aux clients payants. Voici un aperçu anonymisé.</em>
  </p>

  {annonces_html}

  <div style="margin-top: 32px; padding: 24px; background: #1a1a1a; color: white; border-radius: 8px; text-align: center;">
    <h3 style="margin: 0 0 12px 0;">Envie de l'accès complet ?</h3>
    <p style="margin: 0 0 16px 0;">Marque, modèle, prix, lien direct vers la source, alertes en temps réel.</p>
    <a href="https://faillink.be/tarifs-services-faillites-entreprises-belgique/" style="display: inline-block; background: #ffaa00; color: #1a1a1a; padding: 12px 24px; border-radius: 6px; text-decoration: none; font-weight: 600;">
      Voir les abonnements
    </a>
  </div>

  <p style="color: #999; font-size: 12px; margin-top: 32px;">
    Faillink — Alertes B2B sur opportunités off-market en Europe<br>
    Vous recevez cet email car vous avez demandé un essai sur faillink.be
  </p>

</body>
</html>
    """

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
                "to": [{"email": to_email, "name": to_name or to_email}],
                "subject": "🔔 Votre aperçu Faillink — 5 opportunités off-market",
                "htmlContent": html_content,
            },
            timeout=15,
        )
        if resp.status_code in (200, 201):
            msg_id = resp.json().get("messageId", "?")
            logger.info("[send_email] Envoyé à %s (Brevo msgId=%s)", to_email, msg_id)
            return True
        logger.error(
            "[send_email] HTTP %d : %s", resp.status_code, resp.text[:300]
        )
        return False
    except requests.RequestException as e:
        logger.error("[send_email] erreur réseau : %s", e)
        return False


# ============================================================================
# Helpers — Stripe payment_failed email
# ============================================================================

def build_payment_failed_email_html(
    prenom: Optional[str],
    amount_eur: float,
    currency: str,
    next_attempt_dt: Optional[datetime],
    portal_url: str,
) -> tuple[str, str]:
    """Construit (subject, html) pour l'email d'echec de paiement.

    Pure : pas d'I/O, facilement testable.
    """
    subject = "⚠️ Faillink — Échec de paiement de votre abonnement"
    salutation = (
        f"Bonjour {prenom.strip()},"
        if prenom and prenom.strip()
        else "Bonjour,"
    )
    amount_str = f"{amount_eur:.2f} {currency.upper()}"
    if next_attempt_dt:
        retry_line = (
            "Stripe retentera automatiquement le "
            f"{next_attempt_dt.strftime('%d/%m/%Y')}."
        )
    else:
        retry_line = "Stripe retentera automatiquement dans les prochains jours."

    html = f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"><title>Échec de paiement Faillink</title></head>
<body style="font-family: -apple-system, Helvetica, sans-serif; max-width: 600px; margin: 0 auto; padding: 24px;">

  <h1 style="color: #1a1a1a;">{salutation}</h1>

  <p>Une tentative de paiement de <strong>{amount_str}</strong> pour votre abonnement Faillink a échoué.</p>

  <p>{retry_line}</p>

  <p style="margin: 24px 0; padding: 16px; background: #fef7e6; border-left: 4px solid #ffaa00; border-radius: 4px;">
    Pas de panique, vos alertes continuent pendant environ 3 jours, le temps de mettre à jour votre paiement.
  </p>

  <div style="margin: 32px 0; text-align: center;">
    <a href="{portal_url}" style="display: inline-block; background: #1a1a1a; color: white; padding: 14px 28px; border-radius: 6px; text-decoration: none; font-weight: 600;">
      Mettre à jour ma carte bancaire
    </a>
  </div>

  <p style="color: #999; font-size: 12px; margin-top: 32px;">
    Faillink — Alertes B2B sur opportunités off-market en Europe<br>
    Vous recevez cet email suite à un échec de paiement de votre abonnement.
  </p>

</body>
</html>"""
    return subject, html


def send_payment_failed_email(
    to_email: str,
    prenom: Optional[str],
    amount_eur: float,
    currency: str,
    next_attempt_dt: Optional[datetime],
    portal_url: str,
) -> bool:
    """Envoie l'email d'echec de paiement via Brevo. Retourne True si OK."""
    if not BREVO_API_KEY:
        logger.warning("[stripe] BREVO_API_KEY absent — email NON envoye")
        return False
    subject, html = build_payment_failed_email_html(
        prenom=prenom,
        amount_eur=amount_eur,
        currency=currency,
        next_attempt_dt=next_attempt_dt,
        portal_url=portal_url,
    )
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
                "to": [{"email": to_email, "name": prenom or to_email}],
                "subject": subject,
                "htmlContent": html,
            },
            timeout=15,
        )
        if resp.status_code in (200, 201):
            msg_id = resp.json().get("messageId", "?")
            logger.info("[stripe] payment_failed envoye a %s (msgId=%s)", to_email, msg_id)
            return True
        logger.error(
            "[stripe] Brevo HTTP %d pour %s : %s",
            resp.status_code, to_email, resp.text[:300],
        )
        return False
    except requests.RequestException as e:
        logger.error("[stripe] erreur reseau Brevo pour %s : %s", to_email, e)
        return False


# ============================================================================
# Helpers — Stripe signature verification + handler
# ============================================================================

def verify_stripe_signature(
    payload: bytes,
    sig_header: str,
    secret: str,
    tolerance_seconds: int = STRIPE_SIGNATURE_TOLERANCE_SECONDS,
) -> bool:
    """Verifie la signature Stripe selon la spec officielle.

    Header format : `t=<timestamp>,v1=<sig>[,v0=<sig>]`
    Signed payload : `f"{timestamp}.{body}"` en bytes.
    Anti-replay : timestamp doit etre dans ±tolerance_seconds de maintenant.

    Retourne True si valide, False sinon (signature invalide, replay, malforme).
    """
    if not sig_header or not secret or not payload:
        return False
    parts: dict = {}
    for kv in sig_header.split(","):
        k, _, v = kv.partition("=")
        k, v = k.strip(), v.strip()
        if k and v:
            # Plusieurs v1=... possibles theoriquement, on garde le premier
            parts.setdefault(k, v)
    timestamp_str = parts.get("t")
    sig_v1 = parts.get("v1")
    if not timestamp_str or not sig_v1:
        return False
    try:
        ts = int(timestamp_str)
    except ValueError:
        return False
    # Anti-replay
    if abs(int(time.time()) - ts) > tolerance_seconds:
        return False
    signed_payload = timestamp_str.encode("utf-8") + b"." + payload
    expected = hmac.new(
        secret.encode("utf-8"), signed_payload, hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(expected, sig_v1)


def handle_payment_failed(invoice_obj: dict, dry_run: bool = False) -> dict:
    """Traite un evenement Stripe invoice.payment_failed.

    Retourne un dict avec :
        status : "ok" | "dry_run" | "skip" | "no_client" | "error"
        + details specifiques.

    Le caller (endpoint) doit mapper "error" + reason="brevo_failed"
    vers un HTTP 500 (Stripe RETRY auto). Les autres statuts -> 200 OK.
    """
    cus_id = invoice_obj.get("customer")
    amount_due_cents = invoice_obj.get("amount_due", 0) or 0
    currency = invoice_obj.get("currency", "eur") or "eur"
    next_attempt_ts = invoice_obj.get("next_payment_attempt")

    if not cus_id:
        logger.warning("[stripe] invoice sans customer id, skip")
        return {"status": "skip", "reason": "no_customer_id"}

    client = find_client_by_stripe_customer_id(cus_id)
    if not client:
        logger.warning(
            "[stripe] client %s introuvable dans Airtable, skip (no retry)",
            cus_id,
        )
        return {"status": "no_client", "stripe_customer_id": cus_id}

    fields = client.get("fields", {})
    to_email = fields.get("email")
    prenom = fields.get("prenom")
    if not to_email:
        logger.error("[stripe] client %s sans email", cus_id)
        return {"status": "error", "reason": "client_has_no_email"}

    amount_eur = amount_due_cents / 100.0
    next_attempt_dt = (
        datetime.fromtimestamp(next_attempt_ts, tz=timezone.utc)
        if next_attempt_ts else None
    )

    if not STRIPE_CUSTOMER_PORTAL_URL:
        logger.error(
            "[stripe] STRIPE_CUSTOMER_PORTAL_URL absent — bouton CTA cassé, skip envoi",
        )
        return {"status": "error", "reason": "portal_url_not_configured"}

    if dry_run:
        logger.info(
            "[stripe] DRY-RUN : would send to %s (amount=%.2f %s, next=%s)",
            to_email, amount_eur, currency,
            next_attempt_dt.isoformat() if next_attempt_dt else "n/a",
        )
        return {
            "status": "dry_run",
            "to": to_email,
            "prenom": prenom,
            "amount_eur": amount_eur,
            "currency": currency,
            "next_attempt": next_attempt_dt.isoformat() if next_attempt_dt else None,
        }

    sent = send_payment_failed_email(
        to_email=to_email,
        prenom=prenom,
        amount_eur=amount_eur,
        currency=currency,
        next_attempt_dt=next_attempt_dt,
        portal_url=STRIPE_CUSTOMER_PORTAL_URL,
    )
    if sent:
        return {"status": "ok", "to": to_email}
    return {"status": "error", "reason": "brevo_failed"}


# ============================================================================
# FastAPI app
# ============================================================================
app = FastAPI(title="Faillink Funnel Essai")


@app.get("/")
def root():
    return {"service": "faillink-funnel-essai", "status": "ok"}


@app.get("/health")
def health():
    """Health check pour Railway."""
    return {
        "status": "ok",
        "airtable_configured": bool(AIRTABLE_TOKEN),
        "brevo_configured": bool(BREVO_API_KEY),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


@app.post("/webhook/tally")
async def tally_webhook(request: Request):
    """Endpoint principal — reçoit le webhook Tally."""
    try:
        body = await request.json()
    except Exception as e:
        logger.error("[webhook] payload JSON invalide : %s", e)
        raise HTTPException(status_code=400, detail="Invalid JSON")

    event_type = body.get("eventType")
    if event_type != "FORM_RESPONSE":
        logger.info("[webhook] eventType=%s ignoré", event_type)
        return {"status": "ignored", "reason": f"eventType={event_type}"}

    data = body.get("data", {})
    fields = data.get("fields", []) or []
    form_id = data.get("formId")
    response_id = data.get("responseId")

    logger.info(
        "[webhook] FORM_RESPONSE reçu - formId=%s responseId=%s",
        form_id, response_id,
    )

    # Parse les champs du lead
    email = get_field_value(fields, TALLY_KEYS["email"]).lower()
    nom = get_field_value(fields, TALLY_KEYS["nom"])
    entreprise = get_field_value(fields, TALLY_KEYS["entreprise"])
    mots_cles = get_field_value(fields, TALLY_KEYS["mots_cles"])

    categories = resolve_multi_select(fields, TALLY_KEYS["categories"])
    pays = resolve_multi_select(fields, TALLY_KEYS["pays"])
    budget = resolve_single_choice(fields, TALLY_KEYS["budget"])

    if not email:
        logger.error("[webhook] email manquant — payload rejeté")
        raise HTTPException(status_code=400, detail="email is required")

    email_type = detect_email_type(email)

    logger.info(
        "[webhook] Lead parsed : %s (%s, %s) - %d cat / %d pays",
        email, nom, entreprise, len(categories), len(pays),
    )

    # 1. Cherche les annonces matchantes
    annonces = search_annonces(categories, pays, max_results=5)
    annonces_ids = [r.get("id") for r in annonces if r.get("id")]

    # 2. Envoie l'email teasé
    email_sent = send_email_essai_simple(
        to_email=email,
        to_name=nom,
        annonces=annonces,
        categories=categories,
        pays=pays,
    )

    # 3. Crée le Lead dans Airtable
    lead_data = {
        "email": email,
        "nom": nom,
        "entreprise": entreprise,
        "email_pro_ou_perso": email_type,
        "categories_recherchees": categories,
        "pays_interet": pays,
        "budget_tranche": budget,
        "mots_cles": mots_cles,
        "langue": "FR",
        "date_inscription": datetime.now(timezone.utc).isoformat(),
        "essai_envoye": email_sent,
        "annonces_envoyees_ids": ",".join(annonces_ids) if annonces_ids else "",
    }
    lead_id = create_lead(lead_data)

    return JSONResponse(
        status_code=200,
        content={
            "status": "ok",
            "lead_id": lead_id,
            "email": email,
            "annonces_count": len(annonces),
            "email_sent": email_sent,
        },
    )


@app.post("/webhook/stripe/payment-failed")
async def stripe_payment_failed(request: Request):
    """Endpoint webhook Stripe pour invoice.payment_failed (remplace Make W8).

    Comportement HTTP :
      - signature absente / invalide / replay -> 400 (Stripe ne retry pas)
      - event != invoice.payment_failed       -> 200 (ignore proprement)
      - client introuvable                    -> 200 (Stripe ne retry pas)
      - Brevo fail                            -> 500 (Stripe RETRY auto)
      - succes                                -> 200
    """
    body = await request.body()
    sig = request.headers.get("stripe-signature", "")

    if not STRIPE_WEBHOOK_SECRET:
        logger.error("[stripe] STRIPE_WEBHOOK_SECRET absent — config Railway manquante")
        raise HTTPException(status_code=500, detail="webhook not configured")

    if not verify_stripe_signature(body, sig, STRIPE_WEBHOOK_SECRET):
        logger.warning("[stripe] signature invalide ou replay")
        raise HTTPException(status_code=400, detail="invalid signature")

    try:
        event = json.loads(body)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="invalid JSON")

    event_type = event.get("type")
    event_id = event.get("id", "?")
    if event_type != "invoice.payment_failed":
        logger.info("[stripe] event %s (%s) ignore", event_type, event_id)
        return {"status": "ignored", "type": event_type, "event_id": event_id}

    invoice_obj = event.get("data", {}).get("object", {}) or {}
    result = handle_payment_failed(invoice_obj)
    result["event_id"] = event_id

    # Brevo fail -> 500 pour declencher le retry Stripe.
    # Tous les autres statuts -> 200 (Stripe ne retry pas).
    if result.get("status") == "error" and result.get("reason") == "brevo_failed":
        logger.error("[stripe] Brevo fail pour event %s, return 500 pour retry", event_id)
        raise HTTPException(status_code=500, detail="email send failed, retry me")

    return result


# Point d'entrée pour exécution directe (test local)
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", 8000)))
