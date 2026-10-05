# -*- coding: utf-8 -*-
"""Logique commune aux trois generations du cadre de paiement d'Odoo.

PLAN-960. Le module existe en trois couches -- 17.0/18.0, 19.0, 20.0 -- parce que le
cadre `payment` d'Odoo a ete refondu deux fois (19 : `_process` et le controle du
montant ; 20 : `_record`, le traitement asynchrone, la disparition de `state`). Le
manifeste et les fichiers XML ne peuvent pas varier selon la serie : un seul arbre
pour les quatre series est impossible.

Ce fichier porte ce qui NE DEPEND PAS de la serie, et c'est l'essentiel des regles :
quels statuts Mobupay valent un encaissement, quel montant controler et dans quelle
unite, comment lire un paiement relu par l'API. Il est copie tel quel dans chaque
couche a la publication (`connectors/odoo/assembler.sh`), et un temoin verifie qu'il y
est identique a l'octet. Une regle ecrite trois fois finit par diverger : c'est le
mecanisme que ce decoupage ferme.

Aucun import d'Odoo ici : tout se verifie hors instance (`test-harness/logic_test.py`).
"""

#: Etats Mobupay qui valent un encaissement. `authorized` en fait partie : c'est le
#: defaut d'un connecteur qui n'ecoute que `captured`, corrige a l'echelle du produit
#: par PLAN-236, et il ne doit pas renaitre ici.
DONE_STATES = ("captured", "authorized", "succeeded")
PENDING_STATES = ("pending", "processing", "transit")
CANCEL_STATES = ("cancelled", "canceled", "expired")
ERROR_STATES = ("failed", "refused", "declined")
#: Etats d'un paiement REMBOURSE. Ils ne changent pas la transaction d'origine : ils
#: annoncent un remboursement, que la couche reflete par une transaction fille.
REFUND_STATES = ("refunded", "partially_refunded")

#: Devises sans unite mineure : un montant y est deja en unites entieres.
ZERO_DECIMAL = ("XPF",)


def transition(status):
    """Transition Odoo pour un statut Mobupay.

    :return: 'done' | 'pending' | 'cancel' | 'error' | 'refund' | None. `None` pour un
        statut inconnu : il ne doit RIEN decider, le taire vaut mieux que de marquer
        une commande payee sur un etat qu'on ne comprend pas.
    """
    s = str(status or "").strip().lower()
    if s in DONE_STATES:
        return "done"
    if s in PENDING_STATES:
        return "pending"
    if s in CANCEL_STATES:
        return "cancel"
    if s in ERROR_STATES:
        return "error"
    if s in REFUND_STATES:
        return "refund"
    return None


def reference_of(data):
    """Reference de la transaction Odoo visee par une donnee Mobupay.

    Le module envoie la reference de la transaction en `externalId`, et la meme en
    `order.reference` : l'une ou l'autre suffit.
    """
    data = data or {}
    return data.get("externalId") or (data.get("order") or {}).get("reference") or ""


def from_minor_units(amount, currency):
    """Unites mineures de la devise -> unites majeures (ce que compare Odoo)."""
    if amount is None:
        return None
    value = float(amount)
    return value if str(currency or "").upper() in ZERO_DECIMAL else value / 100.0


def amount_data(data):
    """Donnees de montant pour le controle d'Odoo (19 et 20, `_extract_amount_data`).

    LE PIEGE QUE CETTE FONCTION FERME. Un evenement Mobupay porte `amount` en
    CENTIMES EUR, l'unite interne, et `currency` vaut alors `EUR` meme pour un
    paiement en XPF. Le montant de la devise d'ORIGINE est dans `grossAmount`, sa
    devise dans `originalCurrency`. Comparer `amount` au montant d'une transaction
    XPF mettrait TOUS les paiements calédoniens en erreur.

    :return: {'amount', 'currency_code', 'precision_digits'}, ou `None` quand la donnee
        ne porte pas le montant d'origine. `None` SAUTE le controle : mieux vaut ne
        pas controler que de rejeter un paiement encaisse sur une donnee incomplete
        (un `{}` partiel, lui, met la transaction en erreur).
    """
    data = data or {}
    gross = data.get("grossAmount")
    currency = data.get("originalCurrency")
    if gross is None or not currency:
        return None
    currency = str(currency).upper()
    return {
        "amount": from_minor_units(gross, currency),
        "currency_code": currency,
        "precision_digits": 0 if currency in ZERO_DECIMAL else 2,
    }


def payment_to_data(payment, reference):
    """Forme un paiement relu par `GET /api/v1/payments/{id}` comme un evenement.

    La reprise (page de retour, tache planifiee) applique ce qu'elle relit par la MEME
    implementation qu'un webhook : une seule fonction decide des transitions.
    `GET` rend `originalAmount` en chaine et `amountCents` en centimes EUR ;
    `originalAmount` est absent d'un paiement saisi en EUR, ou il vaut `amountCents`.
    """
    payment = payment or {}
    original = payment.get("originalAmount")
    gross = original if original not in (None, "") else payment.get("amountCents")
    currency = payment.get("originalCurrency") or payment.get("currency")
    data = {
        "externalId": payment.get("externalId") or reference,
        "status": payment.get("status") or "",
        "paymentId": payment.get("id") or "",
    }
    if gross not in (None, "") and currency:
        data["grossAmount"] = int(gross)
        data["originalCurrency"] = currency
    return data


def refund_of(data):
    """Le remboursement qu'annonce un evenement `payment.(partially_)refunded`.

    :return: {'refund_id', 'amount', 'currency'} en unites majeures de la devise
        d'origine, ou `None` si l'evenement ne porte pas le montant d'origine
        (`refundOriginalAmount`, ajoute par PLAN-960 lot R). Sans lui on ne cree rien :
        reconvertir les centimes EUR internes produirait un montant faux d'un franc.
    """
    data = data or {}
    amount = data.get("refundOriginalAmount")
    currency = data.get("originalCurrency")
    if amount is None or not currency or not data.get("refundId"):
        return None
    return {
        "refund_id": str(data["refundId"]),
        "amount": from_minor_units(amount, currency),
        "currency": str(currency).upper(),
    }


def environment_conflict(key, is_live):
    """La cle et l'environnement d'Odoo doivent CONCORDER.

    Odoo porte son propre etat (test ou production) et Mobupay porte le sien dans le
    prefixe de la cle. Une boutique en production avec une cle `sk_test_` croit
    encaisser et n'encaisse rien ; l'inverse encaisse REELLEMENT une boutique qui se
    croit en essai.

    :return: 'test_key_in_live' | 'live_key_in_test' | None
    """
    key = (key or "").strip()
    if not key:
        return None
    if is_live and key.startswith("sk_test_"):
        return "test_key_in_live"
    if not is_live and key.startswith("sk_live_"):
        return "live_key_in_test"
    return None


def base_url_problem(base):
    """L'adresse publique de l'instance permet-elle a Mobupay d'y livrer ?

    :return: 'missing' | 'local' | 'not_https' | None
    """
    base = (base or "").strip()
    if not base:
        return "missing"
    host = base.split("//")[-1].split("/")[0].split(":")[0].lower()
    if host in ("localhost", "127.0.0.1", "0.0.0.0") or host.endswith(".local"):
        return "local"
    if not base.startswith("https://"):
        return "not_https"
    return None


#: Reglage « Facture Mobupay » : les deux modes EXCLUSIFS (PLAN-960 lot 5, arbitrage
#: du 2026-10-05 : « il ne faut pas qu'il y ait deux facturations »).
#: `no` = Odoo etablit les factures, Mobupay n'en etablit aucune.
INVOICING_BY_MOBUPAY = ("yes", "yes_send")


def invoicing_request(mode, operation_type, odoo_auto_invoice, pays_an_odoo_invoice):
    """Ce que la charge utile demande a Mobupay en matiere de facture.

    :param mode: reglage du fournisseur ('no' | 'yes' | 'yes_send')
    :param odoo_auto_invoice: la facturation automatique d'Odoo est-elle active ?
    :param pays_an_odoo_invoice: la transaction regle-t-elle une facture Odoo ?
    :return: (dict|None, raison|None). `None` quand AUCUNE facture ne doit etre
        demandee ; la raison dit pourquoi, pour la journaliser.

    Jamais deux factures pour une vente : si Odoo facture deja -- facturation
    automatique, ou paiement d'une facture Odoo existante -- Mobupay n'en etablit pas,
    quel que soit le reglage.
    """
    if mode not in INVOICING_BY_MOBUPAY:
        return None, None
    if pays_an_odoo_invoice:
        return None, "odoo_invoice_paid"
    if odoo_auto_invoice:
        return None, "odoo_auto_invoice"
    normalized = str(operation_type or "").strip().upper()
    request = {
        "enabled": True,
        "operationType": normalized if normalized in ("GOODS", "SERVICES", "MIXED") else "GOODS",
    }
    if mode == "yes_send":
        request["send"] = True
    return request, None
