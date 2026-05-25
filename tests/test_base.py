"""Tests unitaires de scrapers/base.py.

Cible principale : _iso_or_none(), regression du bug "MM/DD swap" cause par
dateutil.parser.parse(dayfirst=True) sur les ISO 8601 ambigus (jour <= 12).
Voir l'investigation : tous les scrapers qui mettent un ISO dans
raw['date_fin'] (Surplex, AssetOrb, NetBid, Biddit, IVG, Lueders) avaient
date_fin swappee en base quand le jour reel <= 12.

Compatible pytest ET execution directe :
    pytest tests/test_base.py
    python tests/test_base.py
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scrapers.base import _iso_or_none


# ---------------------------------------------------------------------------
# Regression du bug MM/DD swap sur ISO 8601
# ---------------------------------------------------------------------------

def test_iso_full_day_le_12_no_swap():
    """ISO 8601 complet avec jour <= 12 : NE DOIT PAS swap MM/DD."""
    assert _iso_or_none("2026-06-09T10:00:00+00:00") == "2026-06-09T10:00:00+00:00"


def test_iso_full_day_gt_12_unchanged():
    """ISO 8601 complet avec jour > 12 : doit rester identique."""
    assert _iso_or_none("2026-06-25T10:00:00+00:00") == "2026-06-25T10:00:00+00:00"


def test_iso_full_with_z_suffix():
    """ISO 8601 avec Z suffix : converti en +00:00, pas de swap."""
    assert _iso_or_none("2026-06-09T10:00:00Z") == "2026-06-09T10:00:00+00:00"


def test_iso_date_only():
    """ISO date seule YYYY-MM-DD : doit rester (assume minuit UTC)."""
    assert _iso_or_none("2026-06-09") == "2026-06-09T00:00:00+00:00"


def test_iso_date_only_day_gt_12():
    """ISO date seule jour > 12 : doit rester (sanity)."""
    assert _iso_or_none("2026-06-25") == "2026-06-25T00:00:00+00:00"


def test_iso_with_space_separator():
    """Format YYYY-MM-DD HH:MM:SS (IVG) : doit etre traite comme ISO."""
    # Pas de timezone -> assume UTC
    assert _iso_or_none("2026-06-09 10:00:00") == "2026-06-09T10:00:00+00:00"


# ---------------------------------------------------------------------------
# Formats europeens (Auctelia, Restlos, Legalbrokers) : dayfirst=True doit s'appliquer
# ---------------------------------------------------------------------------

def test_european_slash_format_unambiguous():
    """Format DD/MM/YYYY europeen, ambigu (jour et mois <= 12) :
    doit etre interprete comme jour-en-premier."""
    # '09/06/2026' = 9 juin 2026 (PAS 6 septembre)
    assert _iso_or_none("09/06/2026") == "2026-06-09T00:00:00+00:00"


def test_european_dash_format():
    """Format DD-MM-YYYY europeen : meme regle."""
    assert _iso_or_none("09-06-2026") == "2026-06-09T00:00:00+00:00"


def test_european_day_gt_12_forced():
    """Format avec jour > 12 : pas ambigu, doit donner la bonne date.
    '13/06/2026' ne peut etre que 13 juin 2026."""
    assert _iso_or_none("13/06/2026") == "2026-06-13T00:00:00+00:00"


# ---------------------------------------------------------------------------
# Cas limites : None / vide / garbage
# ---------------------------------------------------------------------------

def test_none_returns_none():
    assert _iso_or_none(None) is None


def test_empty_string_returns_none():
    assert _iso_or_none("") is None


def test_garbage_returns_none():
    assert _iso_or_none("not a date at all blah blah") is None


def test_zero_returns_none():
    """0 est falsy -> shortcut return None (pas d'erreur)."""
    assert _iso_or_none(0) is None


# ---------------------------------------------------------------------------
# Entree datetime native
# ---------------------------------------------------------------------------

def test_datetime_aware_passthrough():
    """datetime avec tz : doit retourner ISO UTC."""
    dt = datetime(2026, 6, 9, 10, 0, 0, tzinfo=timezone.utc)
    assert _iso_or_none(dt) == "2026-06-09T10:00:00+00:00"


def test_datetime_naive_assumes_utc():
    """datetime naive : doit assumer UTC."""
    dt = datetime(2026, 6, 9, 10, 0, 0)  # naive
    assert _iso_or_none(dt) == "2026-06-09T10:00:00+00:00"


# ---------------------------------------------------------------------------
# Regression : la valeur exacte que produit le scraper Surplex/AssetOrb
# ---------------------------------------------------------------------------

def test_surplex_pipeline_no_swap():
    """Simulation pipeline complet : epoch -> isoformat -> _iso_or_none.
    L'epoch 1780999200 correspond a 2026-06-09 10:00 UTC. Avant le fix,
    la sortie etait '2026-09-06T10:00:00+00:00' (swap)."""
    epoch = 1780999200
    raw = datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat()
    assert raw == "2026-06-09T10:00:00+00:00"
    normalized = _iso_or_none(raw)
    assert normalized == "2026-06-09T10:00:00+00:00"
    # La date stockee par Airtable (typecast date) = premiers 10 chars
    assert normalized[:10] == "2026-06-09"


# ---------------------------------------------------------------------------
# Runner pour execution directe (sans pytest)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    funcs = [
        (name, obj) for name, obj in sorted(globals().items())
        if name.startswith("test_") and callable(obj)
    ]
    failures = 0
    for name, fn in funcs:
        try:
            fn()
            print(f"  PASS  {name}")
        except AssertionError as e:
            failures += 1
            print(f"  FAIL  {name}  {e}")
        except Exception as e:
            failures += 1
            print(f"  ERR   {name}  {type(e).__name__}: {e}")
    print(f"\n{len(funcs) - failures}/{len(funcs)} passed")
    sys.exit(1 if failures else 0)
