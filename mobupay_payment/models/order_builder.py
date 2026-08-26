# -*- coding: utf-8 -*-
"""Lecture d'une commande Odoo vers l'objet `order` de l'API Mobupay.

PLAN-599 lot O4 et O5. Odoo est la QUATRIEME plateforme de Nouvelle-Caledonie, 8,7 %
du parc, et c'est un ERP autant qu'une boutique : le profil type est la PME qui tient
deja sa comptabilite, donc exactement celle qui a besoin du module Facturation.

C'est pourquoi ce connecteur remonte le detail de commande **des sa premiere
version**. On ne refait pas l'erreur des huit autres, livres pendant un an sans jamais
envoyer un article.

Ce fichier ne fait que LIRE une commande Odoo. Toute l'arithmetique vit dans
`mobupay_order_payload`, portage du noyau PHP partage par les trois connecteurs PHP :
une divergence sur ce calcul ne se voit pas en revue, elle se voit en production sous
la forme d'un paiement refuse.

─────────────────────────────────────────────────────────────────────────────
DEUX REGLES PROPRES A ODOO
─────────────────────────────────────────────────────────────────────────────

A. LES MONTANTS SE LISENT TAXE COMPRISE. `price_total` d'une ligne de commande est le
   total TTC, `price_subtotal` le HT. L'API comprend les `unitPrice` comme des
   montants TTC et c'est le serveur qui en extrait le HT pour la facture : on prend
   donc TOUJOURS `price_total`.

B. LES FRAIS DE PORT SONT UNE LIGNE DE COMMANDE ORDINAIRE. Chez Odoo, la livraison est
   deja une `sale.order.line` portant `is_delivery = True`. Il n'y a donc rien de
   special a faire : elle traverse la boucle comme les autres, avec ses propres taxes.
   C'est exactement ce que la regle demande, `delivery.fee` etant purement descriptif
   cote API et n'entrant dans aucun calcul.
"""

from . import mobupay_order_payload as core


def build(order, currency_iso, with_items=True, with_customer=True, invoicing="no",
          translate=lambda text: text):
    """Construit la charge utile depuis une `sale.order`.

    :param order: `sale.order` Odoo
    :param currency_iso: code devise ISO (`XPF`, `EUR`)
    :param with_items: transmettre le detail des lignes
    :param with_customer: transmettre les coordonnees du client
    :param invoicing: 'no' | 'yes' | 'yes_send'
    :param translate: fonction de traduction des libelles visibles du client. Le noyau
        ne connait pas le systeme de traduction d'Odoo, les libelles lui sont passes.
    :return: dict {'order': ..., 'degraded': bool, 'notes': [str]}
    """
    amount = core.to_minor_units(order.amount_total, currency_iso)
    notes = []
    degraded = False

    # Socle : ce qu'un connecteur minimal enverrait, et qui ne doit jamais regresser
    # quoi qu'il arrive plus bas.
    payload = {
        "reference": str(order.name or ""),
        "amount": amount,
        "currency": currency_iso,
    }

    if with_items:
        built = _build_items(order, currency_iso, translate)
        if built is None:
            degraded = True
            notes.append("items: aucune ligne exploitable, detail abandonne")
        else:
            reconciled = core.reconcile(built["items"], built["discount"], amount)
            if reconciled is None:
                degraded = True
                notes.append("items: reconciliation impossible, detail abandonne")
            else:
                payload["items"] = reconciled["items"]
                if reconciled["discount"] > 0:
                    payload["discount"] = reconciled["discount"]
                if reconciled["adjusted"]:
                    notes.append("items: ecart d'arrondi resorbe")

    if with_customer:
        buyer = _build_buyer(order)
        if buyer:
            payload["buyer"] = buyer
        delivery = _build_delivery(order)
        if delivery:
            payload["delivery"] = delivery

    if invoicing in ("yes", "yes_send"):
        payload["invoicing"] = {"enabled": True}
        if invoicing == "yes_send":
            payload["invoicing"]["send"] = True

    return {"order": payload, "degraded": degraded, "notes": notes}


def _build_items(order, currency_iso, translate):
    """Lignes de commande, frais de port compris (regle B)."""
    items = []
    quantity_label = translate("Quantité : %s")
    fallback_label = translate("Article")
    tax_fallback = translate("Taxe")

    for line in order.order_line:
        # Les lignes de SECTION et de NOTE n'ont pas de prix : ce sont des titres de
        # mise en page du devis. Les envoyer produirait des lignes a zero sur la
        # facture, et une ligne a zero sur une facture est une anomalie comptable.
        if getattr(line, "display_type", False):
            continue

        qty = float(line.product_uom_qty or 0.0)
        gross_ttc = core.to_minor_units(line.price_total, currency_iso)
        if gross_ttc <= 0:
            continue  # ligne offerte a zero : sans objet sur la facture

        composed = core.compose_line(
            _line_label(line),
            qty,
            gross_ttc,
            gross_ttc,
            _line_taxes(line, tax_fallback),
            quantity_label,
            fallback_label,
        )
        if composed is not None:
            items.append(composed)

    if not items:
        return None

    # La remise d'Odoo est portee par la ligne (`discount`, en pourcent) et se trouve
    # DEJA reflechie dans `price_total`. Il n'y a donc aucune remise de niveau commande
    # a ajouter : l'ajouter la compterait deux fois et ferait tomber la charge sous le
    # montant, donc refuser le paiement.
    return {"items": items, "discount": 0}


def _line_label(line):
    """Libelle de ligne : le nom saisi sur la commande d'abord.

    `line.name` est la description telle que le client la verra sur son devis ; c'est
    elle qui doit figurer sur la facture, pas le nom interne du produit.
    """
    label = getattr(line, "name", "") or ""
    if not str(label).strip():
        product = getattr(line, "product_id", None)
        label = getattr(product, "display_name", "") if product else ""
    # Une description Odoo est souvent multiligne : la facture attend un libelle.
    return str(label or "").replace("\n", " ")


def _line_taxes(line, fallback_label):
    """Taxes d'une ligne, en centiemes de pourcent.

    Odoo permet PLUSIEURS taxes sur une meme ligne, et des taxes en montant fixe. On
    ne retient que les taxes en POURCENTAGE : une taxe fixe ne se represente pas dans
    `taxDetail` de type PERCENTAGE, et l'inventer fausserait la base imposable. Son
    montant reste inclus dans le total de la ligne, donc dans ce que le client paie ;
    seule sa VENTILATION est perdue, ce qui est le moindre mal.
    """
    detail = []
    for tax in getattr(line, "tax_id", []) or []:
        if getattr(tax, "amount_type", "percent") != "percent":
            continue
        rate = float(getattr(tax, "amount", 0.0) or 0.0)
        # Le libelle vient des DONNEES : c'est le nom que le marchand a donne a sa
        # taxe, « TGC » en Nouvelle-Caledonie, « TVA 20 % » en France.
        label = getattr(tax, "name", "") or fallback_label
        detail.extend(core.percent_tax(rate, label, fallback_label))
    return detail


def _build_buyer(order):
    """Coordonnees de l'acheteur, depuis le partenaire de FACTURATION.

    C'est lui qui porte les mentions obligatoires, pas l'adresse de livraison.
    """
    buyer = {}
    partner = getattr(order, "partner_invoice_id", None) or getattr(order, "partner_id", None)
    if partner is None:
        return buyer

    if getattr(partner, "id", False):
        buyer["id"] = str(partner.id)
    core.put(buyer, "email", getattr(partner, "email", ""))
    core.put(buyer, "phone", getattr(partner, "mobile", "") or getattr(partner, "phone", ""))

    # Odoo ne separe pas prenom et nom sur un partenaire : `name` porte les deux pour
    # une personne, et la raison sociale pour une societe. On ne DEVINE pas un
    # decoupage, qui serait faux des qu'un nom compte plusieurs mots ; on renseigne la
    # raison sociale, que la facture sait afficher seule.
    is_company = bool(getattr(partner, "is_company", False))
    name = str(getattr(partner, "name", "") or "")
    if not is_company and name:
        parts = name.split(" ", 1)
        core.put(buyer, "firstName", parts[0])
        if len(parts) > 1:
            core.put(buyer, "lastName", parts[1])

    address = core.compose_address(
        street=getattr(partner, "street", "") or "",
        complement=getattr(partner, "street2", "") or "",
        city=getattr(partner, "city", "") or "",
        postal_code=getattr(partner, "zip", "") or "",
        country=_country_code(partner),
        company=name if is_company else (getattr(partner, "commercial_company_name", "") or ""),
    )
    if address:
        buyer["billingAddress"] = address
    return buyer


def _build_delivery(order):
    """Livraison : le mode et l'adresse, JAMAIS le montant.

    Les frais de port sont deja une ligne de commande (regle B) : les compter aussi
    ici ferait echouer le controle de coherence des montants.
    """
    delivery = {}
    carrier = getattr(order, "carrier_id", None)
    if carrier is not None and getattr(carrier, "id", False):
        # `mode`, PAS `method` : c'est la cle que `OrderDeliverySchema` definit. Un
        # `method` serait supprime en silence par la validation.
        core.put(delivery, "mode", getattr(carrier, "name", ""))

    partner = getattr(order, "partner_shipping_id", None)
    if partner is not None and getattr(partner, "id", False):
        address = core.compose_address(
            street=getattr(partner, "street", "") or "",
            complement=getattr(partner, "street2", "") or "",
            city=getattr(partner, "city", "") or "",
            postal_code=getattr(partner, "zip", "") or "",
            country=_country_code(partner),
            company=getattr(partner, "commercial_company_name", "") or "",
        )
        if address:
            delivery["address"] = address
    return delivery


def _country_code(partner):
    """Code ISO alpha-2 du pays du partenaire, chaine vide s'il n'y en a pas."""
    country = getattr(partner, "country_id", None)
    if country is None or not getattr(country, "id", False):
        return ""
    return str(getattr(country, "code", "") or "")
