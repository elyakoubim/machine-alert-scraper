# Machine Alert — Scraper universel

Scraper Python universel pour la veille enchères Europe.  
Alimente directement la table **Annonces** dans Airtable.

## Architecture

```
main.py          ← orchestrateur, rapport final
config.py        ← configuration de chaque site (sélecteurs CSS)
scrapers/
  base.py        ← classe Annonce + BaseScraper
  static.py      ← BeautifulSoup (sites HTML classiques)
  dynamic.py     ← Playwright (sites React/Next.js)
airtable.py      ← push + déduplication
```

## Installer

```bash
# 1. Cloner et installer
pip install -r requirements.txt
playwright install chromium

# 2. Config
cp .env.example .env
# Editer .env avec ton token Airtable

# 3. Tester une source
python main.py --source Clicpublic --dry-run

# 4. Lancer toutes les sources
python main.py --dry-run    # test
python main.py              # production
```

## Ajouter un nouveau site

Dans `config.py`, ajouter un bloc dans la liste `SOURCES` :

```python
{
    "nom": "NomDuSite",
    "url": "https://www.exemple.com/ventes",
    "pays": "BE",                    # BE / FR / NL / DE / UK / IT / ES
    "type": "static",                # "static" ou "dynamic" (JS/React)
    "selectors": {
        "items": ".auction-card",    # sélecteur CSS du conteneur d'annonce
        "url": "a",                  # sélecteur du lien (dans items)
        "titre": "h2",               # sélecteur du titre
        "type_vente": ".badge",      # sélecteur du type (optionnel)
    },
    "url_prefix": "https://www.exemple.com",  # pour les URLs relatives
    "actif": True,
}
```

**Quand utiliser `type: "dynamic"` ?**
- Le site est construit en React, Next.js, Vue, Angular
- La page est vide sans JavaScript activé
- Les annonces chargent après un spinner

## Déploiement Railway

Le repo alimente 4 services Railway distincts :

| Service | Source | Cron | Rôle |
|---|---|---|---|
| `machine-alert-scraper` | `python main.py` (Procfile worker) | `0 2 * * *` | Scrape les sites, push annonces |
| `machine-alert-scraper` (web) | `uvicorn webhook_server:app` | — | Webhook Tally funnel essai |
| `lifecycle-worker` | `python scripts/update_annonce_statuts.py` | `30 3 * * *` | Recalcule `statut_annonce` quotidien |
| `alertes-worker` | `python scripts/send_daily_alerts.py` | `0 10 * * *` | Envoie les emails d'alerte aux clients actifs |

1. Push sur GitHub
2. Créer chaque service Railway depuis le repo (4 services partagent le même repo)
3. Ajouter les variables d'environnement dans Railway → Settings → Variables (les 4 services partagent `AIRTABLE_TOKEN`, `ANTHROPIC_API_KEY`, `BREVO_API_KEY`)
4. Configurer le **Cron schedule** sur chaque service worker (voir tableau)

## Service `alertes-worker` — alertes quotidiennes clients

Remplace l'ancienne automation Make "W7" (abandonnée 2026-05-11).

```bash
# Test local sans écrire ni envoyer
python scripts/send_daily_alerts.py --dry-run --verbose

# Limiter à 1 client (debug)
python scripts/send_daily_alerts.py --dry-run --limit 1 --verbose

# Tests unitaires de la fonction de matching
python tests/test_send_daily_alerts.py
```

Logique : pour chaque client `actif=true`, intersection `client.pays ∩ annonce.pays` + `client.categories ∩ annonce.categorie` sur les annonces des dernières 24h non encore envoyées. Top 3 dans l'email Brevo, lien `app.faillink.be` pour les autres. PATCH `alerte_envoyee=true` SEULEMENT si Brevo a confirmé l'envoi (anti-doublon).

## Commandes utiles

```bash
# Scraper un seul site
python main.py --source "BVA Auctions"

# Tester sans écrire dans Airtable
python main.py --source Vavato --dry-run

# Scraper tous les sites actifs
python main.py

# Voir les logs
tail -f logs/scraping_*.log
```
