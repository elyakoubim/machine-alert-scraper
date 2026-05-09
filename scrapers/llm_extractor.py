"""
Machine Alert - LLMExtractor (Claude Haiku)
Extraction structuree d'annonces : categorie + 5 champs additionnels.

API publique :
    extract(titre, description) -> dict
    extract_batch(items) -> list[dict]
    categorize(titre, description) -> str        (wrapper de compat)
    categorize_batch(items) -> list[str]          (wrapper de compat)
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Optional

logger = logging.getLogger(__name__)


# Liste fermee des categories Faillink (avec accents pour matcher Airtable)
CATEGORIES = [
    "Immobilier",
    "Machines industrielles",
    "Matériel informatique",
    "Mobilier",
    "Stocks & liquidations",
    "Véhicules",
    "Outillage & équipement chantier",
    "Autre / non classé",
]

# Enums fermes pour les nouveaux champs structures
TYPE_VENTE_VALUES = frozenset(["enchere", "vente_directe", "liquidation", "inconnu"])
ETAT_VALUES = frozenset(["neuf", "occasion", "inconnu"])

# Plage acceptable pour annee_fabrication
ANNEE_MIN = 1950
ANNEE_MAX = 2026

# Dict par defaut renvoye en cas d'echec / titre vide / etc.
# Garantit que extract() retourne TOUJOURS les 6 cles.
DEFAULT_EXTRACTED = {
    "categorie": "Autre / non classé",
    "type_vente": "inconnu",
    "marque": None,
    "modele": None,
    "annee_fabrication": None,
    "etat": "inconnu",
}


SYSTEM_PROMPT = """Tu es un assistant d'extraction structurée pour des annonces d'enchères et de liquidations.

Tu lis le TITRE et la DESCRIPTION fournis et tu extrais un objet JSON contenant 6 champs :
categorie, type_vente, marque, modele, annee_fabrication, etat.

==============================================================
RÈGLE ABSOLUE — anti-hallucination
==============================================================
Si une information n'est PAS écrite explicitement dans le titre ou la description,
tu renvoies null (ou "inconnu" pour les enums qui le prévoient).
Tu n'infères JAMAIS, tu ne devines JAMAIS. Mieux vaut null que faux.

==============================================================
1. categorie — UNE SEULE des 8 catégories suivantes (liste fermée)
==============================================================

1. Immobilier : Bâtiments, entrepôts, bureaux, terrains, hangars, locaux commerciaux, maisons, appartements.

2. Machines industrielles : Machines FIXES et lourdes pour usine ou production professionnelle.
   Exemples : tour CNC, fraiseuse, presse hydraulique, ligne de production, compresseur industriel,
   centre d'usinage, machine d'emballage, banderoleuse, four industriel, chaudière industrielle.

3. Matériel informatique : Serveurs, ordinateurs, écrans, équipement réseau, matériel IT professionnel.

4. Mobilier : Mobilier de bureau, horeca, rayonnages, magasin, atelier, mobilier de stockage.

5. Stocks & liquidations : Palettes, surstocks, fins de série, lots mixtes de marchandises,
   lots de vêtements, lots d'articles divers à revendre.

6. Véhicules : TOUT ce qui transporte ou permet de se déplacer.
   Exemples : voitures, camions, utilitaires, motos, scooters, mobylettes,
   VÉLOS (tous types : VTT, vélos pliants, vélos électriques, vélos de course),
   BATEAUX (à moteur, voiliers, jet-skis, embarcations), kayaks,
   engins de chantier mobiles (chariots élévateurs auto-tractés, mini-pelles, tracteurs),
   remorques, caravanes, trottinettes électriques.

7. Outillage & équipement chantier : Outillage électroportatif et manuel, équipement de chantier mobile.
   Exemples : perceuses, visseuses, scies (circulaires, sauteuses), ponceuses, meuleuses,
   échafaudages, étais, tréteaux, échelles, containers de chantier, bennes,
   machines à bois pour artisans (toupies, raboteuses, scies à ruban),
   bétonnières mobiles, brouettes pro, outillage main (clés, pinces, marteaux),
   matériel de soudure portable.

8. Autre / non classé : Si rien ne correspond ou si le titre est trop vague pour décider.
   Exemples : feux tricolores, articles ferroviaires divers, équipements très spécialisés
   sans catégorie claire.

Règles de distinction :
- "Machines industrielles" = machine FIXE pour usine professionnelle (lourde, gros volume).
- "Outillage & équipement chantier" = matériel MOBILE pour artisan/chantier (plus léger, transportable).
- Chariot élévateur = "Véhicules" (engin auto-tracté).
- Tour à bois pour menuisier = "Outillage & équipement chantier" (machine pour artisan).
- Tour CNC industriel = "Machines industrielles" (machine de production).
- Vélo, bateau, moto, scooter = TOUJOURS "Véhicules" (mobilité).
- Bâtiment commercial = "Immobilier".
- Serveur informatique = "Matériel informatique".
- En cas de doute réel, "Autre / non classé".

Les 8 valeurs valides exactes (à recopier telles quelles AVEC accents) :
- "Immobilier"
- "Machines industrielles"
- "Matériel informatique"
- "Mobilier"
- "Stocks & liquidations"
- "Véhicules"
- "Outillage & équipement chantier"
- "Autre / non classé"

==============================================================
2. type_vente — enum fermé
==============================================================
Valeurs autorisées : "enchere", "vente_directe", "liquidation", "inconnu".

- "enchere" : mots-clés "enchère", "auction", "veiling", "bid", "mise", "adjudication".
- "liquidation" : "liquidation", "faillite", "curateur", "fin de série", "déstockage", "judiciaire".
- "vente_directe" : "vente directe", "à vendre", "prix fixe", "buy now", "achat immédiat".
- "inconnu" : si rien de clair. PAS de fallback "enchere" par défaut.

==============================================================
3. marque — string libre OU null
==============================================================
Le nom du fabricant tel qu'écrit dans le texte (ex : "Atlas Copco", "Caterpillar", "Bosch").
Strict : seulement si une marque est nommée. Sinon null.
Ne PAS confondre avec le nom du vendeur, du curateur, du commissaire-priseur ou de la plateforme.

==============================================================
4. modele — string libre OU null
==============================================================
La référence ou désignation du modèle (ex : "GA 22", "320D", "GBH 2-26").
Seulement si présent textuellement. Sinon null.

==============================================================
5. annee_fabrication — entier OU null
==============================================================
Année de FABRICATION/CONSTRUCTION uniquement, dans l'intervalle [1950, 2026].
Mots-clés : "année 2018", "bj 2015", "construit en 2010", "yr 2020", "year of manufacture".
PAS l'année de la vente, PAS l'année de mise en service si elle diffère explicitement.
Hors plage, ambigu, ou non mentionné → null.

==============================================================
6. etat — enum fermé
==============================================================
Valeurs autorisées : "neuf", "occasion", "inconnu".

- "neuf" : explicitement "neuf", "new", "jamais utilisé", "scellé", "sous emballage".
- "occasion" : "occasion", "used", "d'occasion", "second-hand", "reconditionné".
- "inconnu" : par défaut si rien de clair.

==============================================================
FORMAT DE SORTIE
==============================================================
Tu réponds UNIQUEMENT par un objet JSON strict de cette forme exacte :

{"categorie":"<valeur>","type_vente":"<valeur>","marque":<string|null>,"modele":<string|null>,"annee_fabrication":<int|null>,"etat":"<valeur>"}

Pas de texte avant ni après. Pas de markdown. Pas de commentaires.
"""


class LLMExtractor:
    def __init__(
        self,
        api_key: Optional[str] = None,
        model: Optional[str] = None,
        max_tokens: int = 200,
        timeout: float = 15.0,
    ):
        self._api_key = api_key or os.getenv("ANTHROPIC_API_KEY")
        if not self._api_key:
            raise RuntimeError(
                "LLMExtractor : ANTHROPIC_API_KEY absent."
            )
        self._model = model or os.getenv(
            "ANTHROPIC_MODEL", "claude-haiku-4-5-20251001"
        )
        self._max_tokens = max_tokens
        self._timeout = timeout
        self._client = None

    def _get_client(self):
        if self._client is None:
            from anthropic import Anthropic
            self._client = Anthropic(
                api_key=self._api_key,
                timeout=self._timeout,
            )
        return self._client

    # ============================================================
    # API publique : extract / extract_batch
    # ============================================================

    def extract(
        self,
        titre: str,
        description: str = "",
        categorie_native: str = "",
    ) -> dict:
        """
        Extrait un dict structure a 6 champs depuis le titre + description.
        Renvoie TOUJOURS un dict avec les 6 cles, jamais d'exception non geree.
        """
        if not titre or len(titre.strip()) < 3:
            logger.debug("LLM extract : titre trop court, fallback")
            return dict(DEFAULT_EXTRACTED)

        user_message = self._build_user_message(titre, description, categorie_native)

        try:
            client = self._get_client()
            response = client.messages.create(
                model=self._model,
                max_tokens=max(self._max_tokens, 200),
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": user_message}],
            )
        except Exception as e:
            logger.warning("LLM extract API call a echoue : %s", e)
            return dict(DEFAULT_EXTRACTED)

        try:
            raw_text = response.content[0].text if response.content else ""
        except (AttributeError, IndexError) as e:
            logger.warning("LLM reponse malformee : %s", e)
            return dict(DEFAULT_EXTRACTED)

        parsed = self._parse_extract_json(raw_text)
        if not parsed:
            logger.warning(
                "LLM extract : parsing JSON a tout echoue, brut : %r",
                raw_text[:300],
            )
        return self._validate_extracted(parsed)

    def extract_batch(self, items: list) -> list:
        """
        Extrait par lots : retourne une liste de dicts (memes cles que extract()).
        Chunks de 20 max ; au-dela, recurse. Sur erreur reseau ou parse global,
        retourne DEFAULT_EXTRACTED pour chaque item du lot concerne.
        """
        if not items:
            return []

        MAX_BATCH_SIZE = 20
        if len(items) > MAX_BATCH_SIZE:
            results = []
            for i in range(0, len(items), MAX_BATCH_SIZE):
                chunk = items[i:i + MAX_BATCH_SIZE]
                results.extend(self.extract_batch(chunk))
            return results

        numbered = []
        for idx, item in enumerate(items, start=1):
            titre = (item.get("titre") or "").strip()[:200]
            desc = (item.get("description") or "").strip()[:400]
            entry = f"{idx}. Titre: {titre}"
            if desc:
                entry += f" | Description: {desc}"
            numbered.append(entry)

        user_message = (
            "Pour chaque annonce ci-dessous, extrais l'objet JSON a 6 champs.\n\n"
            + "\n".join(numbered)
            + "\n\nReponds UNIQUEMENT avec ce JSON (pas de texte autour) :\n"
            + '{"resultats":[{"n":1,"categorie":"<valeur>","type_vente":"<valeur>",'
            + '"marque":<string|null>,"modele":<string|null>,'
            + '"annee_fabrication":<int|null>,"etat":"<valeur>"}, ...]}'
        )

        fallback = [dict(DEFAULT_EXTRACTED) for _ in items]

        try:
            client = self._get_client()
            response = client.messages.create(
                model=self._model,
                max_tokens=80 + 80 * len(items),
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": user_message}],
            )
            raw_text = response.content[0].text
        except Exception as e:
            logger.warning("LLM extract_batch a echoue : %s", e)
            return fallback

        text = raw_text.strip()
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)

        results = list(fallback)
        try:
            obj = json.loads(text)
            for entry in obj.get("resultats", []):
                n = entry.get("n")
                if isinstance(n, int) and 1 <= n <= len(items):
                    results[n - 1] = LLMExtractor._validate_extracted(entry)
        except (json.JSONDecodeError, TypeError, AttributeError):
            logger.warning(
                "LLM extract_batch : parse JSON echec, brut : %r",
                raw_text[:300],
            )

        return results

    # ============================================================
    # API publique de compatibilite : categorize / categorize_batch
    # ============================================================

    def categorize(
        self,
        titre: str,
        description: str = "",
        categorie_native: str = "",
    ) -> str:
        """Wrapper de compatibilite : renvoie uniquement la categorie."""
        return self.extract(titre, description, categorie_native)["categorie"]

    def categorize_batch(self, items: list) -> list:
        """Wrapper de compatibilite : renvoie uniquement la liste des categories."""
        return [d["categorie"] for d in self.extract_batch(items)]

    # ============================================================
    # Helpers internes
    # ============================================================

    @staticmethod
    def _normalize_accents(categorie: str) -> str:
        """
        Mappe les versions sans accents vers les versions avec accents (Airtable).
        Robustesse : meme si Haiku renvoie sans accents, on recupere la bonne valeur.
        """
        mapping = {
            "Materiel informatique": "Matériel informatique",
            "Vehicules": "Véhicules",
            "Autre / non classe": "Autre / non classé",
            "Vehicule": "Véhicules",
            "Materiel": "Matériel informatique",
            "Outillage & equipement chantier": "Outillage & équipement chantier",
            "Outillage et equipement chantier": "Outillage & équipement chantier",
            "Outillage & équipement de chantier": "Outillage & équipement chantier",
        }
        return mapping.get(categorie, categorie)

    @staticmethod
    def _build_user_message(
        titre: str,
        description: str,
        categorie_native: str,
    ) -> str:
        desc_trunc = (description or "")[:1500]
        parts = [f"Titre : {titre.strip()}"]
        if desc_trunc:
            parts.append(f"Description : {desc_trunc}")
        if categorie_native:
            parts.append(f"Categorie native du site (indicative) : {categorie_native}")
        parts.append(
            '\nRenvoie UNIQUEMENT ce JSON :\n'
            '{"categorie":"<valeur>","type_vente":"<valeur>",'
            '"marque":<string|null>,"modele":<string|null>,'
            '"annee_fabrication":<int|null>,"etat":"<valeur>"}'
        )
        return "\n".join(parts)

    @staticmethod
    def _parse_extract_json(raw_text: str) -> dict:
        """
        Parse robuste d'une reponse JSON Haiku, en cascade :
            1. Strip code fences
            2. json.loads direct
            3. Premier objet {...} via regex puis json.loads
            4. Regex champ par champ (filet de securite)

        Retourne TOUJOURS un dict (potentiellement partiel ou vide).
        Ne valide PAS le contenu : c'est le role de _validate_extracted().
        """
        if not raw_text:
            return {}

        text = raw_text.strip()
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)

        # Tentative 1 : parse direct
        try:
            obj = json.loads(text)
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            pass

        # Tentative 2 : extraire le premier objet JSON dans le texte
        # Greedy sur DOTALL pour capturer du { au } le plus eloigne possible.
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if m:
            try:
                obj = json.loads(m.group(0))
                if isinstance(obj, dict):
                    return obj
            except json.JSONDecodeError:
                pass

        # Tentative 3 : extraction champ par champ
        result: dict = {}

        for key in ("categorie", "type_vente", "etat"):
            m = re.search(rf'"{key}"\s*:\s*"([^"]+)"', text)
            if m:
                result[key] = m.group(1)

        for key in ("marque", "modele"):
            m = re.search(rf'"{key}"\s*:\s*(?:"([^"]*)"|null)', text)
            if m:
                result[key] = m.group(1) if m.group(1) is not None else None

        m = re.search(r'"annee_fabrication"\s*:\s*(?:(\d{4})|null)', text)
        if m:
            result["annee_fabrication"] = int(m.group(1)) if m.group(1) else None

        return result

    @staticmethod
    def _validate_extracted(d: dict) -> dict:
        """
        Normalise et coerce un dict potentiellement partiel issu du parsing.
        Garantit la presence des 6 cles avec des valeurs valides.

        C'est le filet de securite final : meme si Haiku invente une valeur
        d'enum hors-liste ou un type incorrect, on n'ecrit jamais de saleté
        en aval.
        """
        out = dict(DEFAULT_EXTRACTED)

        cat = d.get("categorie") if isinstance(d, dict) else None
        if isinstance(cat, str):
            cat_norm = LLMExtractor._normalize_accents(cat.strip())
            if cat_norm in CATEGORIES:
                out["categorie"] = cat_norm

        tv = d.get("type_vente") if isinstance(d, dict) else None
        if isinstance(tv, str):
            tv_norm = tv.strip().lower()
            if tv_norm in TYPE_VENTE_VALUES:
                out["type_vente"] = tv_norm

        etat = d.get("etat") if isinstance(d, dict) else None
        if isinstance(etat, str):
            etat_norm = etat.strip().lower()
            if etat_norm in ETAT_VALUES:
                out["etat"] = etat_norm

        out["marque"] = LLMExtractor._coerce_optional_str(
            d.get("marque") if isinstance(d, dict) else None
        )
        out["modele"] = LLMExtractor._coerce_optional_str(
            d.get("modele") if isinstance(d, dict) else None
        )
        out["annee_fabrication"] = LLMExtractor._coerce_annee(
            d.get("annee_fabrication") if isinstance(d, dict) else None
        )

        return out

    @staticmethod
    def _coerce_optional_str(v) -> Optional[str]:
        """str non vide, sinon None. Filtre les valeurs sentinelles type 'null', 'n/a'."""
        if v is None or not isinstance(v, str):
            return None
        s = v.strip()
        if len(s) < 2:
            return None
        if s.lower() in {"null", "none", "n/a", "na", "-", "?", "inconnu", "unknown"}:
            return None
        return s

    @staticmethod
    def _coerce_annee(v) -> Optional[int]:
        """int dans [ANNEE_MIN, ANNEE_MAX], sinon None."""
        if v is None:
            return None
        try:
            n = int(v)
        except (TypeError, ValueError):
            return None
        if ANNEE_MIN <= n <= ANNEE_MAX:
            return n
        return None
