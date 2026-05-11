"""
Machine Alert - BaseScraper (v2.1 - patch cohérence catégories)

Classe abstraite que tous les scrapers de site doivent etendre.

CHANGEMENTS PAR RAPPORT À LA VERSION PRÉCÉDENTE :
- CATEGORIES_FAILLINK : 8 valeurs canoniques AVEC accents (au lieu de 7 sans accents)
  - Ajout de "Outillage & équipement chantier"
  - Ajout des accents : Matériel informatique, Véhicules, Autre / non classé
- default_category : "Autre / non classé" (avec accent é)
- Annonce.to_airtable() : VALIDE et NORMALISE la catégorie avant l'envoi
  → si la valeur n'est pas dans CATEGORIES_FAILLINK, fallback automatique
  → empêche typecast=true de créer des doublons silencieusement
"""
from __future__ import annotations

import hashlib
import logging
import re
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Iterator, Optional

logger = logging.getLogger(__name__)

# 8 catégories canoniques Faillink — AVEC accents
# C'est la liste de référence absolue : aucune autre valeur ne doit
# arriver dans Airtable.categorie. Si Haiku ou un parseur natif renvoie
# une valeur hors de cette liste, on bascule automatiquement sur
# default_category dans Annonce.to_airtable().
CATEGORIES_FAILLINK = frozenset([
    "Immobilier",
    "Machines industrielles",
    "Matériel informatique",
    "Mobilier",
    "Stocks & liquidations",
    "Véhicules",
    "Outillage & équipement chantier",
    "Autre / non classé",
])

DEFAULT_CATEGORY = "Autre / non classé"

# Mapping de tolérance pour les anciennes valeurs sans accents.
# Le code historique générait ces variantes ; ce mapping permet de les
# convertir vers la valeur canonique sans casser les annonces existantes
# pendant la phase de transition.
_CATEGORY_LEGACY_MAP = {
    "Materiel informatique": "Matériel informatique",
    "Vehicules": "Véhicules",
    "Outillage & equipement chantier": "Outillage & équipement chantier",
    "Autre / non classe": "Autre / non classé",
}


def normalize_categorie(value: Optional[str]) -> str:
    """
    Normalise une valeur de catégorie pour qu'elle corresponde EXACTEMENT
    à l'une des 8 valeurs canoniques de CATEGORIES_FAILLINK.

    Règles, dans l'ordre :
    1. Si vide / None → DEFAULT_CATEGORY
    2. Si déjà dans CATEGORIES_FAILLINK → renvoyée telle quelle
    3. Si dans le mapping legacy (sans accents) → version avec accents
    4. Sinon → DEFAULT_CATEGORY (avec un warning dans les logs)
    """
    if not value:
        return DEFAULT_CATEGORY
    value = value.strip()
    if value in CATEGORIES_FAILLINK:
        return value
    if value in _CATEGORY_LEGACY_MAP:
        canonical = _CATEGORY_LEGACY_MAP[value]
        logger.info(
            "[base] catégorie legacy '%s' normalisée en '%s'", value, canonical
        )
        return canonical
    logger.warning(
        "[base] catégorie inconnue '%s' → fallback sur '%s'",
        value, DEFAULT_CATEGORY,
    )
    return DEFAULT_CATEGORY


@dataclass
class Annonce:
    url: str
    titre: str
    source_nom: str
    pays: str
    description: str = ""
    prix: str = ""
    image_url: str = ""
    type_vente: str = ""
    categorie: str = DEFAULT_CATEGORY
    indexed_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    date_publication: Optional[str] = None
    date_fin: Optional[str] = None
    source_id: str = ""
    marque: Optional[str] = None
    modele: Optional[str] = None
    annee_fabrication: Optional[int] = None
    etat: str = "inconnu"

    @property
    def id_unique(self) -> str:
        return hashlib.sha256(self.url.encode("utf-8")).hexdigest()[:16]

    def to_airtable(self) -> dict:
        # Normalisation systématique de la catégorie au moment de l'envoi.
        # Garantit qu'aucune valeur hors-liste ne peut atteindre Airtable,
        # même si typecast=true est activé côté airtable.py.
        categorie_clean = normalize_categorie(self.categorie)
        payload = {
            "url": self.url,
            "titre": self.titre[:255],
            "source": self.source_nom,
            "pays": self.pays,
            "description": (self.description or "")[:10000],
            "prix": self.prix,
            "type_vente": self.type_vente,
            "image_url": self.image_url,
            "categorie": categorie_clean,
            "indexed_at": self.indexed_at,
            "date_publication": self.date_publication,
            "date_fin": self.date_fin,
            "etat": self.etat,
        }
        # Champs optionnels : si None, on omet la cle (Airtable laisse vide).
        if self.marque is not None:
            payload["marque"] = self.marque
        if self.modele is not None:
            payload["modele"] = self.modele
        if self.annee_fabrication is not None:
            payload["annee_fabrication"] = self.annee_fabrication
        return payload

    def validate(self) -> None:
        if not self.url or not self.url.startswith("http"):
            raise ValueError(f"url invalide : {self.url!r}")
        if not self.titre or len(self.titre.strip()) < 3:
            raise ValueError(f"titre trop court : {self.titre!r}")
        if not re.fullmatch(r"[A-Z]{2}", self.pays):
            raise ValueError(f"pays invalide : {self.pays!r}")
        # Note: la catégorie n'est PAS validée ici car normalize_categorie()
        # dans to_airtable() s'occupe du fallback.


class BaseScraper(ABC):
    source_nom: str = ""
    source_pays: str = ""
    base_url: str = ""
    requires_javascript: bool = False
    rate_limit_seconds: float = 2.0
    default_category: str = DEFAULT_CATEGORY

    def __init__(self, fetcher=None, llm_extractor=None):
        if not self.source_nom or not self.source_pays or not self.base_url:
            raise ValueError(
                "BaseScraper : source_nom, source_pays et base_url doivent "
                "etre configures dans la sous-classe."
            )
        self._fetcher = fetcher
        self._llm = llm_extractor
        self._last_request_at = 0.0

    @abstractmethod
    def list_listing_urls(self) -> Iterator[str]: ...

    @abstractmethod
    def parse_listing(self, html: str, listing_url: str) -> Iterator[str]: ...

    @abstractmethod
    def parse_detail(self, html: str, url: str) -> dict: ...

    @abstractmethod
    def map_category(self, native_category: Optional[str]) -> str: ...

    def fetch(self, url: str) -> str:
        self._respect_rate_limit()
        if self._fetcher is None:
            from scrapers.fetchers import HttpxFetcher, PlaywrightFetcher
            if self.requires_javascript:
                self._fetcher = PlaywrightFetcher()
            else:
                self._fetcher = HttpxFetcher()
        html = self._fetcher.fetch(url)
        self._last_request_at = time.time()
        return html

    def normalize(self, raw: dict, url: str) -> Annonce:
        titre = (raw.get("titre") or "").strip()
        description = (raw.get("description") or "").strip()
        prix = (raw.get("prix") or raw.get("prix_brut") or "").strip()
        image_url = (raw.get("image_url") or "").strip()
        type_vente_raw = (raw.get("type_vente") or "").strip()
        marque_raw = _coerce_str_field(raw.get("marque"))
        modele_raw = _coerce_str_field(raw.get("modele"))
        annee_raw = _coerce_year_field(raw.get("annee_fabrication"))
        etat_v = raw.get("etat")
        etat_raw = etat_v.strip().lower() if isinstance(etat_v, str) else ""

        # 1) Categorie : map natif d'abord (rapide, pas de LLM)
        categorie = self.map_category(raw.get("categorie_native"))

        # 2) Appel LLM systematique des que self._llm est dispo.
        # L'ancienne optimisation "skip si categorie OK et 5 champs remplis"
        # etait fragile (besoin que TOUS les 5 champs soient remplis cote
        # scraper, ce qu'aucun scraper actuel ne fait) et n'economisait
        # que des centimes/jour. On prefere du deterministe.
        extracted = None
        if self._llm is not None:
            # Log INFO temporaire pour debugger en prod (incident 2026-05-10).
            # A retirer une fois confirme que les calls Anthropic apparaissent
            # bien dans la console (clef ANTHROPIC_API_KEY correcte cote Railway).
            logger.info("[normalize] %s : extract() appele pour %s", self.source_nom, url)
            try:
                extracted = self._llm.extract(
                    titre=titre,
                    description=description,
                    categorie_native=raw.get("categorie_native") or "",
                )
            except Exception as e:
                logger.warning(
                    "[%s] LLM extract a echoue pour %s : %s",
                    self.source_nom, url, e,
                )
                extracted = None

        # 3) Fusion : raw gagne quand il est rempli, extracted comble les vides.
        # Categorie : on ne touche que si map_category a renvoye le default.
        if categorie == self.default_category and extracted is not None:
            categorie = extracted["categorie"]

        type_vente = type_vente_raw or (
            extracted["type_vente"] if extracted else ""
        )
        marque = marque_raw if marque_raw is not None else (
            extracted["marque"] if extracted else None
        )
        modele = modele_raw if modele_raw is not None else (
            extracted["modele"] if extracted else None
        )
        annee_fabrication = annee_raw if annee_raw is not None else (
            extracted["annee_fabrication"] if extracted else None
        )
        etat = etat_raw or (
            extracted["etat"] if extracted else "inconnu"
        )

        # Sécurité finale : on normalise dès la sortie de map_category/LLM.
        # Garantit qu'on ne propage jamais une catégorie hors-liste,
        # même si une sous-classe oublie de respecter la convention.
        categorie = normalize_categorie(categorie)

        date_pub = _iso_or_none(
            raw.get("date_publication_brut") or raw.get("date_publication")
        )
        date_fin = _iso_or_none(
            raw.get("date_fin_brut") or raw.get("date_fin")
        )
        source_id = raw.get("source_id") or _source_id_from_url(url)

        annonce = Annonce(
            url=url,
            titre=titre,
            source_nom=self.source_nom,
            pays=self.source_pays,
            description=description,
            prix=prix,
            image_url=image_url,
            type_vente=type_vente,
            categorie=categorie,
            date_publication=date_pub,
            date_fin=date_fin,
            source_id=source_id,
            marque=marque,
            modele=modele,
            annee_fabrication=annee_fabrication,
            etat=etat,
        )
        annonce.validate()
        logger.debug(
            "[DEBUG normalize] titre=%r | desc=%r | cat=%s type_vente=%s marque=%s modele=%s annee=%s etat=%s",
            titre[:80],
            (description or "")[:120],
            annonce.categorie, annonce.type_vente, annonce.marque, annonce.modele,
            annonce.annee_fabrication, annonce.etat,
        )
        return annonce

    def run(self) -> Iterator[Annonce]:
        seen_urls = set()
        # Compteurs diagnostiques : detecter ou disparaissent les URLs entre
        # parse_listing et le yield final (incident prod 2026-05-10/11 ou
        # "16-22 lots trouves" par page mais 0 annonces collectees).
        # Le try/finally garantit que le recap fire meme si main.py break early
        # (ex: --max-annonces N) en fermant le generateur via GeneratorExit.
        n_pages = 0
        n_from_parse_listing = 0
        n_after_seen_dedup = 0
        n_yielded = 0
        try:
            for listing_url in self.list_listing_urls():
                n_pages += 1
                try:
                    html_listing = self.fetch(listing_url)
                except Exception as e:
                    logger.warning(
                        "[%s] echec fetch listing %s : %s",
                        self.source_nom, listing_url, e,
                    )
                    continue
                for detail_url in self.parse_listing(html_listing, listing_url):
                    n_from_parse_listing += 1
                    if detail_url in seen_urls:
                        continue
                    seen_urls.add(detail_url)
                    n_after_seen_dedup += 1
                    try:
                        html_detail = self.fetch(detail_url)
                        raw = self.parse_detail(html_detail, detail_url)
                        yield self.normalize(raw, detail_url)
                        n_yielded += 1
                    except Exception as e:
                        logger.warning(
                            "[%s] echec parse %s : %s",
                            self.source_nom, detail_url, e,
                        )
                        continue
        finally:
            logger.info(
                "[%s] run() recap : pages=%d | from_parse_listing=%d | "
                "after_seen_dedup=%d | yielded=%d",
                self.source_nom, n_pages, n_from_parse_listing,
                n_after_seen_dedup, n_yielded,
            )

    def clean_text(self, text: Optional[str]) -> str:
        if not text:
            return ""
        return re.sub(r"\s+", " ", text).strip()

    def normalize_url(self, href: Optional[str]) -> str:
        if not href:
            return ""
        href = href.strip()
        if href.startswith("http"):
            return href
        if href.startswith("//"):
            return "https:" + href
        base = self.base_url.rstrip("/")
        if href.startswith("/"):
            return base + href
        return base + "/" + href

    def is_valid_annonce_url(self, url: str) -> bool:
        if not url or len(url) < 10:
            return False
        bad = [
            ".css", ".js", ".png", ".jpg", ".jpeg", ".svg", ".ico", ".gif",
            "javascript:", "mailto:", "tel:", "#",
            "/login", "/register", "/account", "/signin", "/signup",
            "/inloggen", "/registreren", "/anmelden",
            "/contact", "/mailinglist", "/newsletter",
            "/info.aspx", "/terms", "/privacy", "/cookies",
        ]
        low = url.lower()
        return not any(b in low for b in bad)

    def _respect_rate_limit(self) -> None:
        elapsed = time.time() - self._last_request_at
        if elapsed < self.rate_limit_seconds:
            time.sleep(self.rate_limit_seconds - elapsed)


def _iso_or_none(s) -> Optional[str]:
    if not s:
        return None
    if isinstance(s, datetime):
        dt = s
    else:
        try:
            from dateutil import parser as dateparser
            dt = dateparser.parse(str(s), dayfirst=True, fuzzy=True)
        except Exception:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


def _source_id_from_url(url: str) -> str:
    m = re.search(r"/(\d{3,})(?:[/_\-?#]|$)", url)
    if m:
        return m.group(1)
    return hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]


def _coerce_str_field(v) -> Optional[str]:
    """Normalise un champ string optionnel d'un raw dict : None si vide / sentinelle."""
    if not isinstance(v, str):
        return None
    s = v.strip()
    if len(s) < 2:
        return None
    if s.lower() in {"null", "none", "n/a", "na", "-", "?", "inconnu", "unknown"}:
        return None
    return s


def _coerce_year_field(v) -> Optional[int]:
    """Coerce vers int dans [1950, 2026], sinon None."""
    if v is None:
        return None
    try:
        n = int(v)
    except (TypeError, ValueError):
        return None
    return n if 1950 <= n <= 2026 else None
