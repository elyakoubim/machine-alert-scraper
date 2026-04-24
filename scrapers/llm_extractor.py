"""
Machine Alert - LLMExtractor (Claude Haiku)
Module de categorisation intelligente des annonces.
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
    "Autre / non classé",
]


SYSTEM_PROMPT = """Tu es un assistant de classification d'annonces d'encheres et de liquidations.

Tu classes chaque annonce dans UNE SEULE des categories suivantes (liste fermee) :

1. Immobilier : Batiments, entrepots, bureaux, terrains, hangars.
2. Machines industrielles : Tours, fraiseuses, presses, lasers, compresseurs, chariots elevateurs.
3. Materiel informatique : Serveurs, ordinateurs, ecrans, reseau, materiel IT.
4. Mobilier : Mobilier de bureau, horeca, rayonnages, magasin, atelier.
5. Stocks & liquidations : Palettes, surstocks, fins de serie, lots mixtes.
6. Vehicules : Voitures, camions, utilitaires, engins de chantier, motos.
7. Autre / non classe : Si rien ne correspond ou si le titre est trop vague.

Regles :
- Chariot elevateur en usine -> Machines industrielles. Camion grue -> Vehicules.
- Batiment industriel vide -> Immobilier.
- Lot mixte machines + stocks : selon dominante.
- En cas de doute reel, reponds "Autre / non classe".

Tu reponds UNIQUEMENT en JSON strict de cette forme :
{"categorie": "<une des 7 valeurs exactes avec accents>"}

Pas de texte avant ni apres, pas de markdown."""


class LLMExtractor:
    def __init__(
        self,
        api_key: Optional[str] = None,
        model: Optional[str] = None,
        max_tokens: int = 80,
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

    def categorize(
        self,
        titre: str,
        description: str = "",
        categorie_native: str = "",
    ) -> str:
        if not titre or len(titre.strip()) < 3:
            logger.debug("LLM categorize : titre trop court, fallback")
            return "Autre / non classé"

        user_message = self._build_user_message(titre, description, categorie_native)

        try:
            client = self._get_client()
            response = client.messages.create(
                model=self._model,
                max_tokens=self._max_tokens,
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": user_message}],
            )
        except Exception as e:
            logger.warning("LLM API call a echoue : %s", e)
            return "Autre / non classé"

        try:
            raw_text = response.content[0].text if response.content else ""
        except (AttributeError, IndexError) as e:
            logger.warning("LLM reponse malformee : %s", e)
            return "Autre / non classé"

        categorie = self._parse_json_categorie(raw_text)
        if categorie not in CATEGORIES:
            logger.warning(
                "LLM a renvoye une categorie invalide : %r (brut : %r)",
                categorie, raw_text,
            )
            return "Autre / non classé"

        return categorie

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
            parts.append(f"Categorie native du site : {categorie_native}")
        parts.append('\nRenvoie UNIQUEMENT ce JSON : {"categorie": "<valeur>"}')
        return "\n".join(parts)

    @staticmethod
    def _parse_json_categorie(raw_text: str) -> str:
        if not raw_text:
            return "Autre / non classé"

        text = raw_text.strip()
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)

        try:
            obj = json.loads(text)
            if isinstance(obj, dict) and "categorie" in obj:
                return str(obj["categorie"]).strip()
        except json.JSONDecodeError:
            pass

        m = re.search(r'"categorie"\s*:\s*"([^"]+)"', text)
        if m:
            return m.group(1).strip()

        return "Autre / non classé"

    def categorize_batch(self, items: list) -> list:
        if not items:
            return []

        MAX_BATCH_SIZE = 20
        if len(items) > MAX_BATCH_SIZE:
            results = []
            for i in range(0, len(items), MAX_BATCH_SIZE):
                chunk = items[i:i + MAX_BATCH_SIZE]
                results.extend(self.categorize_batch(chunk))
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
            "Classe chacune des annonces suivantes dans une des 7 categories Faillink.\n\n"
            + "\n".join(numbered)
            + "\n\nReponds UNIQUEMENT avec ce JSON (pas de texte autour) :\n"
            + '{"resultats": [{"n": 1, "categorie": "<valeur>"}, ...]}'
        )

        try:
            client = self._get_client()
            response = client.messages.create(
                model=self._model,
                max_tokens=80 + 30 * len(items),
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": user_message}],
            )
            raw_text = response.content[0].text
        except Exception as e:
            logger.warning("LLM batch call a echoue : %s", e)
            return ["Autre / non classé"] * len(items)

        results = ["Autre / non classé"] * len(items)
        text = raw_text.strip()
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
        try:
            obj = json.loads(text)
            for entry in obj.get("resultats", []):
                n = entry.get("n")
                cat = entry.get("categorie")
                if isinstance(n, int) and 1 <= n <= len(items):
                    if cat in CATEGORIES:
                        results[n - 1] = cat
        except (json.JSONDecodeError, TypeError, AttributeError):
            logger.warning("LLM batch : parse JSON echec, brut : %r", raw_text[:300])

        return results