"""Tests unitaires de scripts/send_daily_alerts.py.

Cible : la fonction pure `match_client_to_annonces` (matching client/annonce).
Pas de framework externe : asserts + exit code 0|1.

Usage : python tests/test_send_daily_alerts.py
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.send_daily_alerts import match_client_to_annonces


def _client(pays, cats):
    return {
        "id": "rec_client",
        "fields": {"email": "c@x.com", "pays": pays, "categories": cats},
    }


def _annonce(rec_id, pays, cat, indexed_at="2026-05-11T00:00:00Z", titre="t"):
    return {
        "id": rec_id,
        "fields": {
            "pays": pays,
            "categorie": cat,
            "indexed_at": indexed_at,
            "titre": titre,
        },
    }


def test_match_basic_intersection():
    """Cas nominal : 4 annonces, 2 matchent (intersect pays + cat)."""
    c = _client(["BE", "FR"], ["Véhicules", "Machines industrielles"])
    annonces = [
        _annonce("a1", "BE", "Véhicules", indexed_at="2026-05-11T10:00:00Z"),
        _annonce("a2", "NL", "Véhicules"),       # FAIL pays
        _annonce("a3", "FR", "Mobilier"),        # FAIL cat
        _annonce("a4", "FR", "Machines industrielles", indexed_at="2026-05-11T11:00:00Z"),
    ]
    result = match_client_to_annonces(c, annonces)
    ids = [r["id"] for r in result]
    assert ids == ["a4", "a1"], f"expected ['a4','a1'] (tri DESC); got {ids}"
    print("test_match_basic_intersection: PASS")


def test_match_empty_client_pays():
    """Client sans pays renseigne -> 0 match (court-circuit)."""
    c = _client([], ["Véhicules"])
    result = match_client_to_annonces(c, [_annonce("a1", "BE", "Véhicules")])
    assert result == [], f"expected []; got {result}"
    print("test_match_empty_client_pays: PASS")


def test_match_empty_client_categories():
    """Client sans categories renseignees -> 0 match (court-circuit)."""
    c = _client(["BE"], [])
    result = match_client_to_annonces(c, [_annonce("a1", "BE", "Véhicules")])
    assert result == [], f"expected []; got {result}"
    print("test_match_empty_client_categories: PASS")


def test_match_sort_by_indexed_at_desc():
    """Tri par indexed_at DESC, les plus recentes en tete."""
    c = _client(["BE"], ["Véhicules"])
    annonces = [
        _annonce("old", "BE", "Véhicules", indexed_at="2026-05-09T00:00:00Z"),
        _annonce("new", "BE", "Véhicules", indexed_at="2026-05-11T12:00:00Z"),
        _annonce("mid", "BE", "Véhicules", indexed_at="2026-05-10T06:00:00Z"),
    ]
    result = match_client_to_annonces(c, annonces)
    ids = [r["id"] for r in result]
    assert ids == ["new", "mid", "old"], f"expected DESC order; got {ids}"
    print("test_match_sort_by_indexed_at_desc: PASS")


def test_match_annonce_missing_fields():
    """Annonce avec fields incomplets : ne crash pas, simplement no match."""
    c = _client(["BE"], ["Véhicules"])
    annonces = [
        {"id": "broken", "fields": {}},                  # pas pays/cat
        {"id": "no_fields"},                              # pas de fields du tout
        _annonce("ok", "BE", "Véhicules"),
    ]
    result = match_client_to_annonces(c, annonces)
    ids = [r["id"] for r in result]
    assert ids == ["ok"], f"expected ['ok']; got {ids}"
    print("test_match_annonce_missing_fields: PASS")


def test_match_pays_case_sensitive():
    """Le match est case-sensitive (cohérent avec Airtable single-select)."""
    c = _client(["BE"], ["Véhicules"])
    annonces = [
        _annonce("a1", "be", "Véhicules"),   # pays lowercase -> no match
        _annonce("a2", "BE", "véhicules"),   # cat lowercase -> no match
        _annonce("a3", "BE", "Véhicules"),   # OK
    ]
    result = match_client_to_annonces(c, annonces)
    ids = [r["id"] for r in result]
    assert ids == ["a3"], f"expected ['a3']; got {ids}"
    print("test_match_pays_case_sensitive: PASS")


def test_match_no_annonces():
    """Liste vide d'annonces -> liste vide."""
    c = _client(["BE"], ["Véhicules"])
    assert match_client_to_annonces(c, []) == []
    print("test_match_no_annonces: PASS")


def test_match_annonce_pays_as_list():
    """Annonce avec pays au format LIST (multi-select Airtable) doit aussi matcher."""
    c = _client(["BE", "FR"], ["Véhicules"])
    annonces = [
        # pays comme list (cas reel Airtable)
        {"id": "a1", "fields": {"pays": ["BE"], "categorie": "Véhicules", "indexed_at": "2026-05-11T00:00:00Z"}},
        # pays comme list multi-valeurs partial intersect
        {"id": "a2", "fields": {"pays": ["NL", "FR"], "categorie": "Véhicules", "indexed_at": "2026-05-11T01:00:00Z"}},
        # pays comme list sans intersect
        {"id": "a3", "fields": {"pays": ["DE", "AT"], "categorie": "Véhicules", "indexed_at": "2026-05-11T02:00:00Z"}},
    ]
    result = match_client_to_annonces(c, annonces)
    ids = [r["id"] for r in result]
    assert ids == ["a2", "a1"], f"expected ['a2','a1'] (a3 no intersect, DESC sort); got {ids}"
    print("test_match_annonce_pays_as_list: PASS")


def test_match_annonce_categorie_as_list():
    """Annonce avec categorie au format LIST doit matcher si intersect avec client.categories."""
    c = _client(["BE"], ["Véhicules", "Mobilier"])
    annonces = [
        {"id": "a1", "fields": {"pays": "BE", "categorie": ["Mobilier"], "indexed_at": "2026-05-11T00:00:00Z"}},
        {"id": "a2", "fields": {"pays": "BE", "categorie": ["Immobilier"], "indexed_at": "2026-05-11T01:00:00Z"}},
    ]
    result = match_client_to_annonces(c, annonces)
    ids = [r["id"] for r in result]
    assert ids == ["a1"], f"expected ['a1']; got {ids}"
    print("test_match_annonce_categorie_as_list: PASS")


TESTS = [
    test_match_basic_intersection,
    test_match_empty_client_pays,
    test_match_empty_client_categories,
    test_match_sort_by_indexed_at_desc,
    test_match_annonce_missing_fields,
    test_match_pays_case_sensitive,
    test_match_no_annonces,
    test_match_annonce_pays_as_list,
    test_match_annonce_categorie_as_list,
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
