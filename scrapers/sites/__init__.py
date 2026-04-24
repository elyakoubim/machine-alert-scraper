"""
Machine Alert — Scrapers par site
==================================

Ce package contient UN fichier par site scrapé.

Convention : le nom du fichier = le slug du site, en minuscules.

Exemples :
    scrapers/sites/auctelia.py       -> AuctéliaScraper (BE)
    scrapers/sites/proveiling.py     -> ProVeilingScraper (NL)
    scrapers/sites/clicpublic.py     -> ClicpublicScraper (BE)
    scrapers/sites/troostwijk.py     -> TroostwijkScraper (BE/NL)

Chaque fichier :
    - Définit UNE classe qui hérite de BaseScraper
    - Implémente les 4 méthodes abstraites : list_listing_urls,
      parse_listing, parse_detail, map_category
    - Configure les 4 attributs de classe : source_nom, source_pays,
      base_url, requires_javascript

L'orchestrateur main.py découvre automatiquement tous les scrapers
de ce package et les exécute un par un.
"""