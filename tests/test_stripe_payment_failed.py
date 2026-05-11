"""Tests unitaires pour webhook_server.py — endpoint Stripe payment_failed.

Cibles :
  - verify_stripe_signature : HMAC + anti-replay
  - build_payment_failed_email_html : composition pure (prenom / amount / next_attempt)

Pas de framework externe. Usage : python tests/test_stripe_payment_failed.py
"""

from __future__ import annotations

import hashlib
import hmac
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from webhook_server import (
    verify_stripe_signature,
    build_payment_failed_email_html,
)


# =============================================================================
# Helpers test
# =============================================================================

SECRET = "whsec_test_dummy"


def _make_stripe_header(payload: bytes, ts: int, secret: str = SECRET) -> str:
    """Forge un header `stripe-signature` valide pour un payload donne."""
    signed = f"{ts}".encode("utf-8") + b"." + payload
    sig = hmac.new(secret.encode("utf-8"), signed, hashlib.sha256).hexdigest()
    return f"t={ts},v1={sig}"


# =============================================================================
# verify_stripe_signature
# =============================================================================

def test_signature_valid():
    payload = b'{"id":"evt_1","type":"invoice.payment_failed"}'
    ts = int(time.time())
    header = _make_stripe_header(payload, ts)
    assert verify_stripe_signature(payload, header, SECRET) is True
    print("test_signature_valid: PASS")


def test_signature_wrong_secret():
    payload = b'{"id":"evt_1"}'
    ts = int(time.time())
    header = _make_stripe_header(payload, ts, secret="whsec_OTHER")
    assert verify_stripe_signature(payload, header, SECRET) is False
    print("test_signature_wrong_secret: PASS")


def test_signature_tampered_payload():
    """Signature signe payload A, on tente verif sur payload B."""
    original = b'{"amount_due":1000}'
    tampered = b'{"amount_due":9999}'
    ts = int(time.time())
    header = _make_stripe_header(original, ts)
    assert verify_stripe_signature(tampered, header, SECRET) is False
    print("test_signature_tampered_payload: PASS")


def test_signature_replay_old_timestamp():
    """Timestamp >5min dans le passe doit etre rejete (anti-replay)."""
    payload = b'{}'
    ts = int(time.time()) - 600  # -10 minutes
    header = _make_stripe_header(payload, ts)
    assert verify_stripe_signature(payload, header, SECRET) is False
    print("test_signature_replay_old_timestamp: PASS")


def test_signature_future_timestamp():
    """Timestamp >5min dans le futur doit aussi etre rejete."""
    payload = b'{}'
    ts = int(time.time()) + 600
    header = _make_stripe_header(payload, ts)
    assert verify_stripe_signature(payload, header, SECRET) is False
    print("test_signature_future_timestamp: PASS")


def test_signature_malformed_header():
    payload = b'{}'
    assert verify_stripe_signature(payload, "garbage", SECRET) is False
    assert verify_stripe_signature(payload, "", SECRET) is False
    assert verify_stripe_signature(payload, "t=,v1=", SECRET) is False
    assert verify_stripe_signature(payload, "t=notanumber,v1=abc", SECRET) is False
    print("test_signature_malformed_header: PASS")


def test_signature_empty_payload():
    """Edge case : payload vide -> False (Stripe envoie toujours un body)."""
    ts = int(time.time())
    header = _make_stripe_header(b"", ts)
    # Comme payload est vide, la fonction retourne False (garde defensive)
    assert verify_stripe_signature(b"", header, SECRET) is False
    print("test_signature_empty_payload: PASS")


# =============================================================================
# build_payment_failed_email_html
# =============================================================================

def test_html_with_prenom_and_next_attempt():
    subject, html = build_payment_failed_email_html(
        prenom="Mohamed",
        amount_eur=29.99,
        currency="eur",
        next_attempt_dt=datetime(2026, 5, 15, 10, 0, tzinfo=timezone.utc),
        portal_url="https://billing.stripe.com/p/login/xxx",
    )
    assert "⚠️" in subject
    assert "Échec de paiement" in subject
    assert "Bonjour Mohamed," in html
    assert "29.99 EUR" in html
    assert "15/05/2026" in html
    assert "Pas de panique" in html
    assert "https://billing.stripe.com/p/login/xxx" in html
    assert "Mettre à jour ma carte bancaire" in html
    print("test_html_with_prenom_and_next_attempt: PASS")


def test_html_without_prenom():
    subject, html = build_payment_failed_email_html(
        prenom=None,
        amount_eur=49.0,
        currency="eur",
        next_attempt_dt=datetime(2026, 5, 14, 12, 0, tzinfo=timezone.utc),
        portal_url="https://billing.stripe.com/p/login/xxx",
    )
    assert "Bonjour," in html
    assert "Bonjour None," not in html
    assert "49.00 EUR" in html
    print("test_html_without_prenom: PASS")


def test_html_empty_prenom_string_fallback():
    """Prenom = '' ou whitespace doit donner 'Bonjour,' sans Mohamed."""
    _, html = build_payment_failed_email_html(
        prenom="  ",
        amount_eur=10.0,
        currency="eur",
        next_attempt_dt=None,
        portal_url="https://x",
    )
    assert "Bonjour," in html
    print("test_html_empty_prenom_string_fallback: PASS")


def test_html_without_next_attempt():
    """next_attempt_dt=None -> texte generique 'dans les prochains jours'."""
    _, html = build_payment_failed_email_html(
        prenom="Mohamed",
        amount_eur=29.99,
        currency="eur",
        next_attempt_dt=None,
        portal_url="https://x",
    )
    assert "dans les prochains jours" in html
    assert "Stripe retentera" in html
    print("test_html_without_next_attempt: PASS")


def test_html_currency_uppercase():
    """La currency doit etre affichee en majuscules (USD, EUR, etc)."""
    _, html = build_payment_failed_email_html(
        prenom="X",
        amount_eur=12.34,
        currency="usd",
        next_attempt_dt=None,
        portal_url="https://x",
    )
    assert "12.34 USD" in html
    assert "12.34 usd" not in html
    print("test_html_currency_uppercase: PASS")


TESTS = [
    test_signature_valid,
    test_signature_wrong_secret,
    test_signature_tampered_payload,
    test_signature_replay_old_timestamp,
    test_signature_future_timestamp,
    test_signature_malformed_header,
    test_signature_empty_payload,
    test_html_with_prenom_and_next_attempt,
    test_html_without_prenom,
    test_html_empty_prenom_string_fallback,
    test_html_without_next_attempt,
    test_html_currency_uppercase,
]


def main() -> int:
    passed = 0
    failed = 0
    for t in TESTS:
        try:
            t()
            passed += 1
        except AssertionError as e:
            failed += 1
            print(f"{t.__name__}: FAIL — {e}")
        except Exception as e:
            failed += 1
            print(f"{t.__name__}: ERROR — {type(e).__name__}: {e}")
    total = passed + failed
    print(f"\n{passed}/{total} PASS")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
