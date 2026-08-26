# -*- coding: utf-8 -*-
{
    "name": "Mobupay Payment",
    # VERSION SANS PREFIXE DE SERIE, et ce n'est pas un detail : c'est la seule forme
    # qui installe sur les DEUX series. `adapt_version()` prefixe lui-meme la version
    # avec la serie courante quand elle n'en porte pas, et REFUSE une version prefixee
    # d'une AUTRE serie. Un manifeste en « 17.0.1.0.0 » s'installe donc sur la 17 et
    # echoue sur la 18 avec « invalid manifest », sans autre explication.
    # Constate en installant reellement le module sur les deux, le 2026-08-25.
    "version": "1.0.0",
    "summary": "Paiement par carte via Mobupay (Nouvelle-Calédonie et Pacifique)",
    # Rendue en reStructuredText par Odoo : listes a puces avec ligne vide avant et
    # apres, et AUCUNE ligne indentee sous un paragraphe, sinon le rendu casse et la
    # fiche sort avec une erreur de mise en forme.
    "description": """
Passerelle de paiement Mobupay pour Odoo
========================================

L'acheteur paie sur une page hébergée sécurisée : les données de carte ne transitent
jamais par le serveur du marchand. La commande est mise à jour par un webhook signé, et
un mécanisme de reprise garantit qu'un paiement encaissé ne laisse jamais une commande
en attente.

Un seul champ à renseigner : votre clé API. Le secret de signature des webhooks est
récupéré automatiquement, et l'enregistrement vérifie la clé et vous dit dans quel
environnement vous êtes, test ou production.

Le détail complet de la commande est transmis dès l'installation : articles, taxes par
ligne, frais de port, remises, et coordonnées du client. Vos factures Mobupay détaillent
donc chaque ligne et portent les mentions obligatoires, sans que vous ayez un seul champ
à remplir.

Mobupay est agent d'eZyness, établissement de monnaie électronique agréé par l'ACPR. Les
fonds sont reversés en XPF sur un compte bancaire local.

Données transmises à Mobupay
----------------------------

Ce module transmet des données à Mobupay (mobupay.nc), le service de paiement, pour
chaque paiement initié. Rien n'est transmis tant qu'aucun paiement n'est lancé.

- Toujours, car nécessaires à l'encaissement : la référence de la commande, son montant
  et sa devise.
- Si vous laissez l'option « Détail de la commande » active : les lignes de commande,
  leur libellé, leur quantité, leur prix et leurs taxes.
- Si vous laissez l'option « Coordonnées du client » active : le nom du client, son
  adresse de facturation, son adresse de livraison, son courriel et son téléphone. Ces
  informations servent aux mentions obligatoires d'une facture.

Les deux options se désactivent depuis la fiche du fournisseur de paiement. Les données
de carte ne transitent jamais par votre serveur : le client les saisit sur la page
hébergée de Mobupay.

Politique de confidentialité : https://mobupay.nc/privacy
""",
    "author": "Mobulia Payment Solutions",
    "website": "https://mobupay.nc",
    "license": "LGPL-3",
    "category": "Accounting/Payment Providers",
    "depends": ["payment", "sale"],
    "data": [
        "views/payment_mobupay_templates.xml",
        "views/payment_provider_views.xml",
        "data/payment_provider_data.xml",
        "data/mobupay_cron.xml",
    ],
    # Image de couverture de la fiche Odoo Apps. La cle `images` est ce que la place
    # de marche lit pour l'illustration : sans elle, la fiche sort sans visuel.
    "images": ["static/description/banner.png"],
    "post_init_hook": "post_init_hook",
    "uninstall_hook": "uninstall_hook",
    "installable": True,
    "application": False,
}
