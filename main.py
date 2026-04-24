"""
Machine Alert — Orchestrateur principal (mode hybride)
=======================================================

Lance le scraping en MODE HYBRIDE pendant la phase de migration v1 -> v2 :

    1. NOUVEAU SYSTÈME : auto-discovery des scrapers dans scrapers/sites/
       Chaque fichier .py = 1 site. Architecture cible.

    2. ANCIEN SYSTÈME : exécution des SOURCES legacy listées dans config.py
       Via les anciennes classes StaticScraper / DynamicScraper.
       À supprimer dans une future PR quand tous les scrapers seront migrés.

Tant que la liste config.SOURCES n'est pas vide, on continue d'écouter
les anciens scrapers en parallèle des nouveaux. Aucun risque de doublons :
la dédup se fait sur l'URL dans Airtable, ainsi qu'au niveau scraper.

Usage :
    python main.py                          # tout : ancien + nouveau
    python main.py --source auctelia        # un seul scraper (nouveau OU ancien)
    python main.py --legacy-only            # seulement l'ancien système
    python main.py --new-only               # seulement le nouveau système
    python main.py --dry-run                # test sans écrire dans Airtable
    python main.py --list                   # liste tous les scrapers découverts
"""

from __future__ import annotations

import argparse
import importlib
import inspect
import logging
import pkgutil
import sys
import time
from dataclasses import dataclass
from typing import Optional

# Charge .env dès le début
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

import airtable
from scrapers.base import BaseScraper
from scrapers.llm_extractor import LLMExtractor

# ── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s : %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)


# =============================================================================
# Découverte automatique — NOUVEAU système (scrapers/sites/)
# =============================================================================

def discover_scrapers() -> dict[str, type[BaseScraper]]:
    """Parcourt scrapers/sites/ et renvoie un dict { source_nom: Classe }."""
    registry: dict[str, type[BaseScraper]] = {}

    try:
        import scrapers.sites as sites_pkg
    except ImportError:
        logger.warning("scrapers/sites/ package absent, aucun nouveau scraper chargé")
        return registry

    for module_info in pkgutil.iter_modules(sites_pkg.__path__):
        module_name = f"scrapers.sites.{module_info.name}"
        try:
            module = importlib.import_module(module_name)
        except Exception as e:
            logger.error("Impossible d'importer %s : %s", module_name, e)
            continue

        for _, obj in inspect.getmembers(module, inspect.isclass):
            if (
                issubclass(obj, BaseScraper)
                and obj is not BaseScraper
                and getattr(obj, "source_nom", "")
            ):
                slug = obj.source_nom
                if slug in registry:
                    logger.warning(
                        "Conflit nouveau registry : %s déjà enregistré", slug,
                    )
                    continue
                registry[slug] = obj
                logger.debug("Scraper v2 découvert : %s -> %s", slug, obj.__name__)

    return registry


# =============================================================================
# Découverte ANCIEN système (config.SOURCES + StaticScraper/DynamicScraper)
# =============================================================================

def discover_legacy_sources() -> list[dict]:
    """Charge la liste SOURCES depuis l'ancien config.py (si présent).

    Renvoie [] si config.py n'existe pas ou si SOURCES est absent/vide.
    """
    try:
        import config
    except ImportError:
        logger.info("Ancien config.py absent, pas de scrapers legacy")
        return []

    sources = getattr(config, "SOURCES", None)
    if not sources:
        logger.info("config.SOURCES vide ou absent, pas de scrapers legacy")
        return []

    if not isinstance(sources, list):
        logger.warning("config.SOURCES n'est pas une liste, ignoré")
        return []

    logger.info("%d sources legacy découvertes dans config.SOURCES", len(sources))
    return sources


def get_legacy_scraper_class(source_type: str):
    """Renvoie la classe scraper legacy (StaticScraper ou DynamicScraper) ou None."""
    try:
        if source_type == "static":
            from scrapers.static import StaticScraper
            return StaticScraper
        elif source_type == "dynamic":
            from scrapers.dynamic import DynamicScraper
            return DynamicScraper
        else:
            logger.warning("Type de scraper legacy inconnu : %r", source_type)
            return None
    except ImportError as e:
        logger.error("Impossible d'importer scraper legacy %s : %s", source_type, e)
        return None


# =============================================================================
# Résultat d'un run de scraper
# =============================================================================

@dataclass
class ScraperResult:
    source_nom: str
    system: str  # "v2" ou "legacy"
    total_annonces: int = 0
    pushed: int = 0
    skipped: int = 0
    errors: int = 0
    duration: float = 0.0
    fatal_error: Optional[str] = None

    def summary_line(self) -> str:
        sys_tag = f"[{self.system}]"
        if self.fatal_error:
            return (
                f"{sys_tag} [{self.source_nom}] ❌ FATAL ({self.duration:.1f}s) : "
                f"{self.fatal_error}"
            )
        return (
            f"{sys_tag} [{self.source_nom}] ✅ total={self.total_annonces} "
            f"pushed={self.pushed} skipped={self.skipped} "
            f"errors={self.errors} ({self.duration:.1f}s)"
        )


# =============================================================================
# Exécution d'un scraper NOUVEAU (BaseScraper v2)
# =============================================================================

def run_one_scraper_v2(
    scraper_cls: type[BaseScraper],
    llm: Optional[LLMExtractor] = None,
    dry_run: bool = False,
    max_annonces: Optional[int] = None,
) -> ScraperResult:
    result = ScraperResult(source_nom=scraper_cls.source_nom, system="v2")
    start = time.time()

    try:
        scraper = scraper_cls(llm_extractor=llm)
        logger.info(
            "[v2][%s] Démarrage (pays=%s, JS=%s)",
            scraper.source_nom, scraper.source_pays, scraper.requires_javascript,
        )

        collected = []
        for annonce in scraper.run():
            collected.append(annonce)
            if max_annonces and len(collected) >= max_annonces:
                logger.info(
                    "[v2][%s] Limite atteinte (%d), arrêt",
                    scraper.source_nom, max_annonces,
                )
                break

        result.total_annonces = len(collected)
        logger.info(
            "[v2][%s] %d annonces collectées",
            scraper.source_nom, len(collected),
        )

        if not dry_run and collected:
            stats = airtable.push_annonces(collected, scraper.source_nom)
            result.pushed = stats["pushed"]
            result.skipped = stats["skipped"]
            result.errors = stats["errors"]
            airtable.update_source_status(scraper.source_nom, status="OK")
        elif dry_run:
            logger.info(
                "[v2][%s] DRY-RUN : pas d'écriture Airtable",
                scraper.source_nom,
            )

        if hasattr(scraper, "_fetcher") and scraper._fetcher is not None:
            scraper._fetcher.close()

    except Exception as e:
        logger.exception("[v2][%s] Erreur fatale : %s", scraper_cls.source_nom, e)
        result.fatal_error = str(e)
        if not dry_run:
            airtable.update_source_status(
                scraper_cls.source_nom,
                status="ERR",
                error=str(e),
            )

    result.duration = time.time() - start
    return result


# =============================================================================
# Exécution d'un scraper LEGACY (ancien StaticScraper/DynamicScraper)
# =============================================================================

def run_one_scraper_legacy(
    source_config: dict,
    dry_run: bool = False,
) -> ScraperResult:
    """Exécute un scraper legacy via l'ancienne classe StaticScraper/DynamicScraper.

    Le format attendu de source_config est celui de l'ancien config.SOURCES.
    """
    source_nom = source_config.get("nom") or source_config.get("name") or "unknown"
    result = ScraperResult(source_nom=source_nom, system="legacy")
    start = time.time()

    try:
        source_type = source_config.get("type", "static")
        ScraperCls = get_legacy_scraper_class(source_type)
        if ScraperCls is None:
            result.fatal_error = f"Classe legacy introuvable pour type={source_type}"
            return result

        logger.info("[legacy][%s] Démarrage (type=%s)", source_nom, source_type)
        scraper = ScraperCls(source_config)

        # L'ancien API peut être .scrape(), .run(), ou itérable - on tente plusieurs
        annonces = None
        for method_name in ("scrape", "run", "fetch_annonces"):
            if hasattr(scraper, method_name):
                try:
                    annonces = list(getattr(scraper, method_name)())
                    break
                except Exception as e:
                    logger.debug(
                        "[legacy][%s] %s() a levé : %s, on continue",
                        source_nom, method_name, e,
                    )
                    continue

        if annonces is None:
            result.fatal_error = "Aucune méthode scrape()/run()/fetch_annonces() trouvée"
            return result

        result.total_annonces = len(annonces)
        logger.info(
            "[legacy][%s] %d annonces collectées",
            source_nom, len(annonces),
        )

        if not dry_run and annonces:
            # On suppose que l'ancien airtable.push_annonces accepte la liste
            stats = airtable.push_annonces(annonces, source_nom)
            result.pushed = stats.get("pushed", 0)
            result.skipped = stats.get("skipped", 0)
            result.errors = stats.get("errors", 0)
            airtable.update_source_status(source_nom, status="OK")
        elif dry_run:
            logger.info(
                "[legacy][%s] DRY-RUN : pas d'écriture Airtable",
                source_nom,
            )

    except Exception as e:
        logger.exception("[legacy][%s] Erreur fatale : %s", source_nom, e)
        result.fatal_error = str(e)
        if not dry_run:
            airtable.update_source_status(source_nom, status="ERR", error=str(e))

    result.duration = time.time() - start
    return result


# =============================================================================
# CLI
# =============================================================================

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Machine Alert — Orchestrateur scrapers (mode hybride v1+v2)",
    )
    parser.add_argument(
        "--source",
        help="Slug d'une source spécifique à scraper (cherche dans v2 puis legacy)",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="Liste les scrapers disponibles et sort",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="N'écrit pas dans Airtable (test uniquement)",
    )
    parser.add_argument(
        "--max-annonces",
        type=int,
        default=None,
        help="Limite le nombre d'annonces par scraper v2 (utile pour tests)",
    )
    parser.add_argument(
        "--no-llm",
        action="store_true",
        help="Désactive le fallback LLM (économie de crédits Anthropic)",
    )
    parser.add_argument(
        "--legacy-only",
        action="store_true",
        help="N'exécute que les scrapers legacy (config.SOURCES)",
    )
    parser.add_argument(
        "--new-only",
        action="store_true",
        help="N'exécute que les nouveaux scrapers (scrapers/sites/)",
    )
    args = parser.parse_args()

    # Découverte des deux systèmes
    new_registry = discover_scrapers()
    legacy_sources = discover_legacy_sources()

    if args.list:
        print(f"=== NOUVEAU SYSTÈME (scrapers/sites/) — {len(new_registry)} scraper(s) ===")
        for slug, cls in sorted(new_registry.items()):
            js = "JS" if cls.requires_javascript else "HTML"
            print(f"  - {slug:<25} {cls.source_pays}  {js:<4}  ({cls.__name__})")
        print(f"\n=== ANCIEN SYSTÈME (config.SOURCES) — {len(legacy_sources)} source(s) ===")
        for src in legacy_sources:
            nom = src.get("nom") or src.get("name") or "unknown"
            t = src.get("type", "?")
            print(f"  - {nom:<25} type={t}")
        return 0

    # Filtre par --source si demandé
    if args.source:
        slug = args.source.lower()
        if slug in new_registry:
            new_registry = {slug: new_registry[slug]}
            legacy_sources = []
        else:
            matching_legacy = [
                s for s in legacy_sources
                if (s.get("nom") or s.get("name") or "").lower() == slug
            ]
            if matching_legacy:
                new_registry = {}
                legacy_sources = matching_legacy
            else:
                logger.error("Source %r introuvable dans v2 ou legacy", args.source)
                return 1

    # Filtre par mode
    if args.legacy_only:
        new_registry = {}
    if args.new_only:
        legacy_sources = []

    # LLM partagé pour les nouveaux scrapers
    llm: Optional[LLMExtractor] = None
    if not args.no_llm and new_registry:
        try:
            llm = LLMExtractor()
            logger.info("LLM fallback activé (modèle: %s)", llm._model)
        except Exception as e:
            logger.warning(
                "LLM désactivé (init a échoué) : %s. "
                "Les catégories seront mappées sans fallback intelligent.",
                e,
            )

    # =========================================================================
    # Exécution
    # =========================================================================
    logger.info(
        "═══ Run : %d v2 + %d legacy %s═══",
        len(new_registry), len(legacy_sources),
        "(DRY-RUN) " if args.dry_run else "",
    )

    results: list[ScraperResult] = []

    # 1. Nouveaux scrapers (v2)
    for slug, cls in new_registry.items():
        result = run_one_scraper_v2(
            cls,
            llm=llm,
            dry_run=args.dry_run,
            max_annonces=args.max_annonces,
        )
        results.append(result)
        logger.info(result.summary_line())

    # 2. Anciens scrapers (legacy) — seulement si pas filtré par source
    for src in legacy_sources:
        # On évite de relancer un scraper legacy qui a un équivalent v2
        nom = (src.get("nom") or src.get("name") or "").lower()
        if nom in new_registry:
            logger.info(
                "[legacy][%s] Skip : déjà migré en v2", nom,
            )
            continue
        result = run_one_scraper_legacy(src, dry_run=args.dry_run)
        results.append(result)
        logger.info(result.summary_line())

    # Rapport final
    total_annonces = sum(r.total_annonces for r in results)
    total_pushed = sum(r.pushed for r in results)
    total_errors = sum(r.errors for r in results) + sum(
        1 for r in results if r.fatal_error
    )

    logger.info("═══ Rapport final ═══")
    logger.info(
        "Scrapers : %d | Annonces : %d | Pushed : %d | Errors : %d",
        len(results), total_annonces, total_pushed, total_errors,
    )
    for r in sorted(results, key=lambda x: (x.system, x.source_nom)):
        logger.info("  %s", r.summary_line())

    return 0 if total_errors == 0 else 2


if __name__ == "__main__":
    sys.exit(main())