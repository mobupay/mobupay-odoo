# -*- coding: utf-8 -*-
"""Noyau arithmetique de construction de l'objet `order` Mobupay.

PLAN-599. Portage Python du noyau PHP `\\Mobupay\\OrderPayload` (PLAN-598 lot C3),
partage par WooCommerce, PrestaShop et Magento. Les regles sont les MEMES, et elles ne
sont pas des preferences : chacune vient d'un piege reel, trouve en recette.

Ce module n'importe RIEN d'Odoo, volontairement : il est ainsi verifiable par un banc
d'essai qui tourne sans instance, comme les trois bancs PHP. C'est le seul filet
disponible tant qu'un harnais Docker Odoo n'est pas monte.

─────────────────────────────────────────────────────────────────────────────
LES REGLES QUE CE NOYAU FAIT RESPECTER
─────────────────────────────────────────────────────────────────────────────

1. LES PRIX SONT TAXE COMPRISE. `isInclTaxAmount` vaut `true` par defaut cote API :
   les `unitPrice` recus sont compris comme des montants TTC, et c'est le serveur qui
   extrait le HT pour la facture.

2. LA SOMME DOIT TOMBER EXACTEMENT SUR LE MONTANT. `validateOrderAmounts()` exige
   Somme(unitPrice x quantity - remise de ligne) - remise de commande = amount, a
   l'unite pres et SANS MARGE. Un ecart d'une seule unite refuse le paiement.

3. UNE REMISE DE QUELQUES CENTIMES PEUT APPARAITRE SANS AUCUNE REMISE REELLE.
   `unitPrice` est un entier : 9,99 HT a 20 % font 11,988 TTC, non representable. On
   arrondit au superieur et l'ecart part en remise de ligne. Le net, la base imposable
   et le total restent EXACTS ; seul l'affichage porte une remise de un a quatre
   centimes. C'est le prix de l'encodage en entiers.

4. EN CAS DE DOUTE, ON RENONCE AU DETAIL, JAMAIS AU PAIEMENT. Chaque fonction rend
   `None` plutot qu'une valeur dont elle n'est pas sure, et l'appelant retombe sur la
   charge minimale. Un detail manquant est un desagrement ; un paiement refuse est une
   vente perdue.
"""

import math
import re

MAX_LABEL = 200

#: Devises sans sous-unite. Le XPF en fait partie : 5 000 XPF valent 5000, pas 500000.
#: Se tromper ici multiplie ou divise par cent tous les montants d'une boutique
#: caledonienne.
ZERO_DECIMAL_CURRENCIES = {"XPF"}


def to_minor_units(amount, currency_iso):
    """Convertit un montant decimal en unite mineure."""
    factor = 1 if (currency_iso or "").upper() in ZERO_DECIMAL_CURRENCIES else 100
    return int(round(float(amount or 0.0) * factor))


def trim_label(label, fallback="Article"):
    """Libelle propre, sans balise ni espaces multiples, borne a MAX_LABEL."""
    text = re.sub(r"<[^>]*>", "", str(label or ""))
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return fallback
    return text[:MAX_LABEL]


def percent_tax(rate, label, fallback_label="Taxe"):
    """Une taxe en pourcentage, exprimee en CENTIEMES DE POURCENT.

    Convention unique du depot : 1 % = 100. Une TGC a 11 % vaut donc 1100 et une TVA a
    5,5 % vaut 550. Le demi-point ne doit jamais se perdre.

    Le libelle vient des DONNEES de la boutique, jamais d'une table par pays : « TGC »
    pour une boutique caledonienne, « TVA » pour une francaise, sans une ligne de code
    par juridiction.
    """
    hundredths = int(round(float(rate or 0.0) * 100))
    if hundredths <= 0:
        return []
    return [{
        "id": trim_label(label, fallback_label),
        "type": "PERCENTAGE",
        "value": hundredths,
    }]


def compose_line(label, quantity, gross_ttc, net_ttc, tax_detail=None,
                 quantity_label="Quantité : %s", fallback_label="Article"):
    """Compose une ligne a partir de son brut et de son net, tous deux TAXE COMPRISE.

    Rend `None` si la ligne est incoherente : l'appelant degrade alors la charge.
    """
    if net_ttc < 0:
        return None

    description = None
    qty = int(round(quantity))
    if abs(quantity - qty) > 0.0001 or qty < 1:
        # Quantite fractionnaire (vente au poids, au metre) : l'API veut un entier. On
        # ramene a 1 et la quantite reelle passe en description, ou elle reste lisible
        # sur la facture du client.
        description = quantity_label % (_format_quantity(quantity),)
        qty = 1

    unit_price = int(math.ceil(max(gross_ttc, net_ttc) / qty))
    discount = unit_price * qty - net_ttc
    if discount < 0:
        return None  # ne devrait pas arriver, mais on ne devine pas

    line = {
        "product": trim_label(label, fallback_label),
        "unitPrice": unit_price,
        "quantity": qty,
    }
    if description is not None:
        line["description"] = description
    if discount > 0:
        line["discount"] = discount
    if tax_detail:
        line["taxDetail"] = tax_detail
    return line


def _format_quantity(quantity):
    """Quantite lisible : « 1.35 » et non « 1.3500000000000001 »."""
    text = ("%.4f" % float(quantity)).rstrip("0").rstrip(".")
    return text or "0"


def sum_items(items):
    """Somme des lignes, exactement comme le serveur la calcule."""
    total = 0
    for line in items:
        total += int(line["unitPrice"]) * int(line["quantity"]) - int(line.get("discount", 0))
    return total


def reconcile(items, order_discount, amount):
    """Rend la somme des lignes exactement egale au montant (regle 2).

    Deux cas, dans cet ordre de preference :
      - somme trop GRANDE : l'ecart part en remise de niveau commande, ce qui n'altere
        aucun prix affiche ;
      - somme trop PETITE : on compense sur la DERNIERE ligne, en augmentant son prix
        unitaire du minimum necessaire et en absorbant le depassement dans sa remise,
        de sorte que sa contribution augmente de l'ecart exact.

    Rend `None` si le compte ne tombe pas juste : on ne renvoie jamais une charge dont
    on n'est pas sur.
    """
    items = [dict(line) for line in items]
    expected = sum_items(items) - order_discount

    if expected == amount:
        return {"items": items, "discount": order_discount, "adjusted": False}

    if expected > amount:
        return {
            "items": items,
            "discount": order_discount + (expected - amount),
            "adjusted": True,
        }

    missing = amount - expected
    if not items:
        return None
    last = items[-1]
    qty = int(last["quantity"])
    if qty < 1:
        return None
    delta = int(math.ceil(missing / qty))
    last["unitPrice"] = int(last["unitPrice"]) + delta
    overshoot = delta * qty - missing
    if overshoot > 0:
        last["discount"] = int(last.get("discount", 0)) + overshoot

    if sum_items(items) - order_discount != amount:
        return None

    return {"items": items, "discount": order_discount, "adjusted": True}


def put(target, key, value):
    """N'ecrit que les valeurs non vides.

    L'API distingue « absent » de « vide », et une chaine vide sur un champ d'adresse
    compte comme une mention renseignee, donc comme une facture emissible alors qu'elle
    ne l'est pas.
    """
    text = str(value or "").strip()
    if text:
        target[key] = text


def compose_address(street="", complement="", city="", postal_code="", country="",
                    company="", first_name="", last_name="", phone="", email=""):
    """Adresse au format attendu par l'API, dans sa FORME CANONIQUE.

    `street` + `complement`, jamais `street2` ni `line1` : `OrderAddressSchema` ne
    definit pas `street2`, et Zod supprime les cles inconnues. C'est ainsi que le
    complement d'adresse de PrestaShop partait a la poubelle en silence avant PLAN-598
    lot C3. Une adresse incomplete, c'est une facture non emissible.

    Le pays part en code ISO 3166-1 alpha-2 : l'API refuse un nom en clair, et c'est
    aussi ce qu'attend le XML Factur-X.
    """
    out = {}
    put(out, "street", street)
    put(out, "complement", complement)
    put(out, "city", city)
    put(out, "postalCode", postal_code)
    put(out, "country", str(country or "").strip().upper())
    # La raison sociale, quand le client l'a saisie, sert de libelle : c'est elle qui
    # doit figurer sur la facture d'un professionnel.
    put(out, "label", company)
    put(out, "firstName", first_name)
    put(out, "lastName", last_name)
    put(out, "phone", phone)
    put(out, "email", email)
    return out
