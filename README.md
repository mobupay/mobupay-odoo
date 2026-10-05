# Mobupay pour Odoo

Acceptez les paiements par carte sur votre Odoo, en Nouvelle-Calédonie et dans le
Pacifique. Vos clients paient sur une page hébergée sécurisée : **les données de carte
ne transitent jamais par votre serveur.**

Mobupay est agent d'eZyness, établissement de monnaie électronique agréé par l'ACPR. Les
fonds sont reversés en XPF sur un compte bancaire local.

Compatible **Odoo 17.0, 18.0, 19.0 et 20.0** : une branche de ce dépôt par série.

---

## Installation

Le parcours dépend de votre hébergement.

### Vous êtes sur Odoo.sh

Le plus simple. Depuis la fiche du module sur Odoo Apps, cliquez sur
**« Deploy on Odoo.sh »**. Odoo ajoute le module au dépôt de votre projet et
reconstruit. Le module apparaît ensuite dans vos applications : installez-le, puis
passez à la configuration ci-dessous.

### Vous hébergez votre Odoo vous-même

1. Téléchargez le module depuis Odoo Apps, ou clonez ce dépôt.
2. Placez le dossier `mobupay_payment/` dans le répertoire `addons` de votre instance.
3. Redémarrez le service Odoo.
4. **Activez le mode développeur** : Paramètres, section Outils du développeur,
   « Activer le mode développeur ». **Sans cette étape, l'étape suivante n'existe pas**,
   et c'est la raison la plus fréquente d'une installation qui semble échouer.
5. Applications, menu à trois points, **« Mettre à jour la liste des applications »**.
6. Cherchez « Mobupay » et installez.

### Vous êtes sur Odoo Online

Odoo Online n'accepte aucun module tiers contenant du code Python, quel qu'il soit et
d'où qu'il vienne. Ce module ne peut donc pas y être installé.

Deux solutions : encaisser par **lien de paiement Mobupay**, qui fonctionne depuis
n'importe quel site, ou migrer votre instance vers Odoo.sh. Écrivez-nous, nous vous
orientons.

---

## Configuration

Comptabilité (ou Site web), Configuration, **Fournisseurs de paiement**, **Mobupay**.

**Un seul champ à renseigner : votre clé API.** Puis enregistrez.

Le module vérifie alors la clé, récupère tout seul le secret de signature des webhooks,
et vous confirme dans quel environnement vous êtes, test ou production. Le bouton
**« Vérifier la connexion »** rejoue ce contrôle à tout moment.

| Réglage | Valeur |
|---|---|
| **Clé API** | `sk_test_…` pour tester, `sk_live_…` en production. Espace marchand Mobupay, rubrique Développeurs, Clés API |
| Secret de signature | En lecture seule, rempli automatiquement |
| Détail de la commande | Oui par défaut. Articles, taxes par ligne, frais de port, remises |
| Coordonnées du client | Oui par défaut. Nom, adresse de facturation, téléphone, adresse de livraison |
| Qui établit les factures | Odoo par défaut. Voir ci-dessous |

Le module s'installe **désactivé** (non publié en Odoo 20) : un moyen de paiement ne doit
jamais arriver prêt à encaisser. Passez en mode test, encaissez un paiement d'essai, puis
passez en production.

### Qui établit les factures

Une vente ne porte jamais deux factures. Choisissez :

- **Odoo** (par défaut) : vous facturez dans Odoo, Mobupay n'établit aucune facture.
- **Mobupay** : vous encaissez depuis Odoo sans y facturer. Mobupay établit la facture
  de chaque paiement, et peut l'envoyer au client. Le module Facturation doit être
  activé dans votre espace marchand Mobupay : le bouton « Activer la facturation
  Mobupay » y mène, et « Vérifier la connexion » vous dit s'il est prêt.

Si la facturation automatique d'Odoo est active, Mobupay ne peut pas facturer à sa place :
le module vous le dit à l'enregistrement. Le paiement d'une facture Odoo n'en produit
jamais une seconde.

**Votre instance doit être joignable depuis internet, en HTTPS.** Mobupay y livre ses
confirmations de paiement. Le bouton « Vérifier la connexion » vous prévient si votre
adresse publique ne convient pas.

---

## Ce que le module transmet

Tout est déduit de la commande : **vous n'avez aucun champ à remplir.**

- Les **lignes de commande** au prix taxe comprise, avec leur libellé tel que le client
  le voit sur son devis.
- Les **taxes par ligne**, avec le nom que vous leur avez donné : « TGC » en
  Nouvelle-Calédonie, « TVA » en France. Aucune table par pays.
- Les **frais de port**, qui sont chez Odoo une ligne de commande ordinaire.
- Les **coordonnées du client**, depuis le partenaire de facturation.

Les lignes de **section** et de **note** sont ignorées : ce sont des titres de mise en
page, et les envoyer produirait des lignes à zéro sur votre facture.

Les taxes en **montant fixe** sont omises du détail : elles ne se représentent pas en
pourcentage, et les inventer fausserait la base imposable. Leur montant reste inclus
dans ce que paie le client.

Les deux options de transmission se coupent depuis la fiche du fournisseur.

---

## Comment une commande se confirme

Le **webhook signé** de Mobupay est la source de vérité : votre commande se confirme
dès que le paiement est encaissé, sans que le client ait à revenir sur votre site.

Si ce webhook n'arrive pas (pare-feu, coupure réseau, instance arrêtée au mauvais
moment), **une reprise prend le relais** : au retour du client, le module interroge
Mobupay avant d'afficher la page de statut, et une tâche planifiée rattrape toutes les
dix minutes ce que personne n'a vu. **Un paiement encaissé ne laisse jamais une commande
en attente.**

---

## Remboursement

Depuis le paiement dans Odoo, bouton « Rembourser ». En une ou plusieurs fois, jusqu'au
montant payé : un article, puis un autre. Un remboursement fait depuis votre espace
marchand Mobupay apparaît aussi dans Odoo.

---

## Ce que le module ne fait pas

- **Pas de détail sur un paiement multi-commandes.** Odoo permet de régler plusieurs
  commandes d'un coup ; mélanger leurs lignes sur une même facture mélangerait deux
  ventes distinctes. Dans ce cas, seul le montant est transmis.
- **Pas d'enregistrement de carte.** Le modèle par redirection ne le permet pas.
- **EUR et XPF uniquement.** Mobupay n'apparaît pas au paiement d'une commande dans une
  autre devise, plutôt que d'être proposé puis de refuser.

---

## Assistance

<https://mobupay.nc> · Licence LGPL-3.
