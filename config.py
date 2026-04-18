"""
Machine Alert — Configuration des sources de scraping
Ajouter un site = ajouter un bloc dans SOURCES
"""

AIRTABLE_BASE_ID = "appQrNOm3Q7D9uEed"
AIRTABLE_ANNONCES_TABLE = "tblXVFA7xJ06Ar7Re"
AIRTABLE_SOURCES_TABLE = "tblPaOrQekEMdaW5x"

# type: "static" = HTML classique (BeautifulSoup)
#       "dynamic" = JS/React (Playwright)
#       "api" = endpoint JSON direct

SOURCES = [

    # ═══════════════════════════════════════════════
    # BELGIQUE
    # ═══════════════════════════════════════════════
    {
        "nom": "Clicpublic",
        "url": "https://www.clicpublic.be",
        "pays": "BE",
        "type": "static",
        "selectors": {
            "items": ".sale-block",
            "url": "a.sitelink",
            "titre": ".sale-header",
            "type_vente": ".label.label-custom.abs-top-right",
            "lots": ".label.label-custom.abs-top-left",
        },
        "url_prefix": "https://www.clicpublic.be",
        "actif": True,
    },
    {
        "nom": "Vavato",
        "url": "https://www.vavato.com/fr/auctions",
        "pays": "BE",
        "type": "dynamic",
        "selectors": {
            "items": "a[data-cy='item-link']",
            "url": "self",
            "titre": "p[class*='title'], h2, h3",
            "type_vente": "[class*='badge'], [class*='tag'], [class*='type']",
        },
        "url_prefix": "https://www.vavato.com",
        "actif": True,
    },
    {
        "nom": "Trans Executive",
        "url": "https://www.trans-executive.be/ventes-en-cours",
        "pays": "BE",
        "type": "static",
        "selectors": {
            "items": ".auction-item, .vente-item, article, .card",
            "url": "a",
            "titre": "h2, h3, .title, strong",
            "type_vente": ".badge, .type, .label",
        },
        "url_prefix": "https://www.trans-executive.be",
        "actif": True,
    },
    {
        "nom": "Faillites.info",
        "url": "https://www.faillites.info",
        "pays": "BE",
        "type": "static",
        "selectors": {
            "items": "article, .vente, .item, .listing-item",
            "url": "a",
            "titre": "h2, h3, .title",
            "type_vente": ".type, .badge, .categorie",
        },
        "url_prefix": "https://www.faillites.info",
        "actif": True,
    },
    {
        "nom": "FaillissementsDossier",
        "url": "https://www.faillissementsdossier.be/ventes",
        "pays": "BE",
        "type": "static",
        "selectors": {
            "items": "tr, .vente-row, article",
            "url": "a",
            "titre": "td:nth-child(2), h3, .name",
            "type_vente": "td:nth-child(3), .type",
        },
        "url_prefix": "https://www.faillissementsdossier.be",
        "actif": True,
    },
    {
        "nom": "Troostwijk",
        "url": "https://www.troostwijkauctions.com/fr/auctions/",
        "pays": "BE",
        "type": "dynamic",
        "selectors": {
            "items": "article, [class*='AuctionCard'], [class*='auction-card']",
            "url": "a",
            "titre": "h2, h3, [class*='title'], [class*='name']",
            "type_vente": "[class*='tag'], [class*='badge'], [class*='type']",
        },
        "url_prefix": "https://www.troostwijkauctions.com",
        "actif": True,
    },
    {
        "nom": "Hammertime",
        "url": "https://www.hammertime.be",
        "pays": "BE",
        "type": "dynamic",
        "selectors": {
            "items": "article, [class*='auction'], [class*='lot']",
            "url": "a",
            "titre": "h2, h3, [class*='title']",
            "type_vente": "[class*='badge'], [class*='tag']",
        },
        "url_prefix": "https://www.hammertime.be",
        "actif": True,
    },
    {
        "nom": "Auctim",
        "url": "https://auctim.com/fr/auctions",
        "pays": "BE",
        "type": "dynamic",
        "selectors": {
            "items": "[class*='auction'], [class*='lot'], article",
            "url": "a",
            "titre": "h2, h3, [class*='title']",
            "type_vente": "[class*='badge'], [class*='type']",
        },
        "url_prefix": "https://auctim.com",
        "actif": True,
    },
    {
        "nom": "Ventes-Publiques.be",
        "url": "https://www.ventes-publiques.be",
        "pays": "BE",
        "type": "static",
        "selectors": {
            "items": "article, .vente, .auction-item",
            "url": "a",
            "titre": "h2, h3, .title",
            "type_vente": ".type, .badge",
        },
        "url_prefix": "https://www.ventes-publiques.be",
        "actif": True,
    },
    {
        "nom": "Biddit",
        "url": "https://www.biddit.be/search",
        "pays": "BE",
        "type": "dynamic",
        "selectors": {
            "items": "[class*='PropertyCard'], [class*='property-card'], article",
            "url": "a",
            "titre": "h2, h3, [class*='title'], [class*='address']",
            "type_vente": "[class*='badge'], [class*='tag'], [class*='type']",
        },
        "url_prefix": "https://www.biddit.be",
        "actif": True,
    },
    {
        "nom": "Legal Brokers",
        "url": "https://www.legalbrokers.eu/fr/encheres",
        "pays": "BE",
        "type": "static",
        "selectors": {
            "items": "article, .auction, .lot-item",
            "url": "a",
            "titre": "h2, h3, .title",
            "type_vente": ".badge, .type",
        },
        "url_prefix": "https://www.legalbrokers.eu",
        "actif": True,
    },
    {
        "nom": "StockAuction",
        "url": "https://www.stockauction.be",
        "pays": "BE",
        "type": "static",
        "selectors": {
            "items": "article, .auction-item, .lot",
            "url": "a",
            "titre": "h2, h3, .title",
            "type_vente": ".badge, .type",
        },
        "url_prefix": "https://www.stockauction.be",
        "actif": True,
    },

    # ═══════════════════════════════════════════════
    # FRANCE
    # ═══════════════════════════════════════════════
    {
        "nom": "Interencheres",
        "url": "https://www.interencheres.com/ventes/",
        "pays": "FR",
        "type": "static",
        "selectors": {
            "items": "article.vente, .auction-item, [class*='vente-']",
            "url": "a",
            "titre": "h2, h3, .vente-title, .title",
            "type_vente": ".badge, .type, .categorie",
        },
        "url_prefix": "https://www.interencheres.com",
        "actif": True,
    },
    {
        "nom": "Actify",
        "url": "https://actify.fr/actifs",
        "pays": "FR",
        "type": "dynamic",
        "selectors": {
            "items": "[class*='AssetCard'], [class*='asset-card'], article",
            "url": "a",
            "titre": "h2, h3, [class*='title'], [class*='name']",
            "type_vente": "[class*='badge'], [class*='tag'], [class*='type']",
        },
        "url_prefix": "https://actify.fr",
        "actif": True,
    },
    {
        "nom": "Annonces-Liquidations",
        "url": "https://www.annonces-liquidations.fr",
        "pays": "FR",
        "type": "static",
        "selectors": {
            "items": "article, .annonce, .vente-item",
            "url": "a",
            "titre": "h2, h3, .title, .annonce-title",
            "type_vente": ".type, .badge, .categorie",
        },
        "url_prefix": "https://www.annonces-liquidations.fr",
        "actif": True,
    },
    {
        "nom": "Encheres-Publiques",
        "url": "https://www.encheres-publiques.com/ventes",
        "pays": "FR",
        "type": "static",
        "selectors": {
            "items": "article, .vente, .auction-item",
            "url": "a",
            "titre": "h2, h3, .title",
            "type_vente": ".badge, .type",
        },
        "url_prefix": "https://www.encheres-publiques.com",
        "actif": True,
    },
    {
        "nom": "BODACC",
        "url": "https://www.bodacc.fr/annonce/liste-annonces/",
        "pays": "FR",
        "type": "static",
        "selectors": {
            "items": "tr.odd, tr.even, .annonce-row",
            "url": "a",
            "titre": "td.annonce, .title, h3",
            "type_vente": "td.type, .categorie",
        },
        "url_prefix": "https://www.bodacc.fr",
        "actif": True,
    },
    {
        "nom": "LiquidationEncheres",
        "url": "https://www.liquidationencheres.com/ventes-aux-encheres",
        "pays": "FR",
        "type": "static",
        "selectors": {
            "items": "article, .vente-card, .lot-item",
            "url": "a",
            "titre": "h2, h3, .title",
            "type_vente": ".badge, .type-vente",
        },
        "url_prefix": "https://www.liquidationencheres.com",
        "actif": True,
    },

    # ═══════════════════════════════════════════════
    # PAYS-BAS
    # ═══════════════════════════════════════════════
    {
        "nom": "Troostwijk NL",
        "url": "https://www.troostwijkauctions.com/nl/auctions/",
        "pays": "NL",
        "type": "dynamic",
        "selectors": {
            "items": "article, [class*='AuctionCard']",
            "url": "a",
            "titre": "h2, h3, [class*='title']",
            "type_vente": "[class*='tag'], [class*='badge']",
        },
        "url_prefix": "https://www.troostwijkauctions.com",
        "actif": True,
    },
    {
        "nom": "BVA Auctions",
        "url": "https://www.bva-auctions.com/nl/auction",
        "pays": "NL",
        "type": "dynamic",
        "selectors": {
            "items": "[class*='AuctionCard'], [class*='auction-item'], article",
            "url": "a",
            "titre": "h2, h3, [class*='title'], [class*='name']",
            "type_vente": "[class*='badge'], [class*='tag']",
        },
        "url_prefix": "https://www.bva-auctions.com",
        "actif": True,
    },
    {
        "nom": "ProVeiling",
        "url": "https://www.proveiling.nl",
        "pays": "NL",
        "type": "static",
        "selectors": {
            "items": ".lot, .item, article, .kavel",
            "url": "a",
            "titre": "h2, h3, .title, .lot-title",
            "type_vente": ".badge, .type",
        },
        "url_prefix": "https://www.proveiling.nl",
        "actif": True,
    },
    {
        "nom": "HNVI Veilingen",
        "url": "https://www.hnvi.nl",
        "pays": "NL",
        "type": "static",
        "selectors": {
            "items": ".veiling, .lot, article",
            "url": "a",
            "titre": "h2, h3, .title",
            "type_vente": ".badge, .type",
        },
        "url_prefix": "https://www.hnvi.nl",
        "actif": True,
    },
    {
        "nom": "Plaats Je Bod",
        "url": "https://www.plaatsjebod.nl",
        "pays": "NL",
        "type": "static",
        "selectors": {
            "items": ".kavel, .lot, article, .auction-item",
            "url": "a",
            "titre": "h2, h3, .title",
            "type_vente": ".badge, .type",
        },
        "url_prefix": "https://www.plaatsjebod.nl",
        "actif": True,
    },
    {
        "nom": "CIR Rechtspraak",
        "url": "https://www.rechtspraak.nl/Registers/insolventieregister",
        "pays": "NL",
        "type": "dynamic",
        "selectors": {
            "items": "tr[class*='result'], .insolventie-row, tr",
            "url": "a",
            "titre": "td:nth-child(1), .naam, .name",
            "type_vente": "td:nth-child(3), .type",
        },
        "url_prefix": "https://www.rechtspraak.nl",
        "actif": True,
    },
]
