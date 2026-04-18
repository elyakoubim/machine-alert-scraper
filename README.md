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

1. Push sur GitHub
2. Créer un projet Railway depuis le repo
3. Ajouter les variables d'environnement dans Railway → Settings → Variables
4. Configurer un **Cron** : `0 2 * * *` (chaque nuit à 02h00)

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
