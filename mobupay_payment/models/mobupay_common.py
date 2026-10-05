# -*- coding: utf-8 -*-
"""Colle Odoo commune aux trois couches du module (PLAN-960).

`mobupay_logic` porte les REGLES, sans Odoo. Ce fichier-ci porte ce qui touche aux
enregistrements Odoo mais ne depend pas de la serie : recuperer le secret, verifier
la connexion, lire l'etat du module Facturation, construire et creer la session de
paiement, appliquer un statut. Chaque couche n'a plus qu'a brancher ces fonctions
sur le contrat `payment` de SA serie.

Deux conventions valent pour les quatre series :

- toute ecriture sur une transaction passe par `_ecrire(tx, valeurs)`, qui pose le
  contexte `payment_safe_write`. Odoo 20 refuse toute ecriture sans lui ; 17, 18 et
  19 ignorent la cle. Un seul chemin, aucune branche par serie ;
- ce fichier ne leve JAMAIS vers un webhook : une erreur de notre code se journalise
  en erreur et remonte au controleur, qui repond 500 pour que Mobupay rejoue.
"""

import logging
from urllib.parse import parse_qsl, quote, urlsplit

from odoo import _
from odoo.exceptions import UserError, ValidationError

from . import mobupay_api
from . import mobupay_logic as logic
from . import order_builder

_logger = logging.getLogger(__name__)


def _ecrire(records, valeurs):
    """Ecriture sur des transactions, toleree par Odoo 20 (cf. en-tete)."""
    records.with_context(payment_safe_write=True).write(valeurs)


# ── Fournisseur : secret, connexion, environnement, facturation ─────────────

def refresh_secret(providers, silent=True):
    """Recupere le secret de signature avec la seule cle API.

    L'appel sert deux choses d'un coup : il pose le secret, et il PROUVE que la cle
    est valide. En mode silencieux (enregistrement), un echec ne bloque JAMAIS : une
    API momentanement injoignable ne doit pas empecher un marchand de configurer sa
    boutique. On journalise, et la prochaine sauvegarde reessaiera.
    """
    for provider in providers:
        if provider.code != "mobupay":
            continue
        key = (provider.mobupay_api_key or "").strip()
        if not key:
            if not silent:
                raise UserError(_("Aucune clé API renseignée : la boutique ne pourra pas encaisser."))
            continue
        try:
            body = mobupay_api.request(provider.mobupay_api_base, key, "GET", "/api/v1/webhooks/signing-secret")
        except mobupay_api.MobupayError as exc:
            _logger.warning("Mobupay : récupération du secret de signature en échec : %s", exc)
            if not silent:
                raise UserError(_("Mobupay n'a pas pu être contacté pour vérifier votre clé : %s", exc))
            continue
        secret = (body or {}).get("webhookSecret") or ""
        if not secret:
            _logger.warning("Mobupay : aucun secret de signature renvoyé.")
            if not silent:
                raise UserError(_(
                    "Aucun secret de signature n'a été renvoyé. Les paiements fonctionneront, "
                    "mais les confirmations ne pourront pas être vérifiées."
                ))
            continue
        # `sudo` : le champ est reserve au groupe systeme, et cette ecriture est faite
        # par le module lui-meme, pas par l'utilisateur.
        provider.sudo().mobupay_webhook_secret = secret


def check_environment(providers, is_live_of):
    """Refuse une cle qui contredit l'environnement d'Odoo (test / production).

    :param is_live_of: fonction provider -> True (production), False (test), None
        (fournisseur desactive : rien a controler).
    """
    for provider in providers:
        if provider.code != "mobupay":
            continue
        is_live = is_live_of(provider)
        if is_live is None:
            continue
        conflit = logic.environment_conflict(provider.mobupay_api_key, is_live)
        if conflit == "test_key_in_live":
            raise ValidationError(_(
                "Vous activez Mobupay en PRODUCTION avec une clé de TEST (sk_test_…). "
                "Aucun paiement ne serait réellement encaissé. Renseignez votre clé "
                "sk_live_…, ou repassez en mode test."
            ))
        if conflit == "live_key_in_test":
            raise ValidationError(_(
                "Vous êtes en mode TEST avec une clé de PRODUCTION (sk_live_…). Les "
                "paiements seraient réellement encaissés. Renseignez votre clé sk_test_…, "
                "ou passez en production."
            ))


def base_url_warning(provider):
    """Avertissement si Mobupay ne pourra pas joindre l'instance, sinon None.

    `website_payment` donne la priorite a l'adresse PAR LAQUELLE ON NAVIGUE : ni
    `web.base.url` ni le domaine du site ne changent rien tant qu'on accede a Odoo par
    une autre adresse. Le message dit donc de refaire la verification depuis
    l'adresse publique, pas de modifier un reglage sans effet (constate le 2026-08-26).
    """
    base = (provider.get_base_url() or "").strip()
    probleme = logic.base_url_problem(base)
    if probleme == "missing":
        return _("L'adresse publique de votre instance n'est pas renseignée.")
    # Sans notification, une commande se confirme quand meme : au retour du client
    # (`/payment/mobupay/return`), ou par la tache de reprise, toutes les dix minutes.
    # Ce qui ne remonte PAS, c'est ce qui se passe hors d'Odoo : un remboursement fait
    # depuis l'espace Mobupay. Le message disait l'inverse (« vos commandes
    # resteraient en attente »), corrige en 1.2.2.
    if probleme == "local":
        return _(
            "Vous accédez à Odoo par « %s ». Mobupay ne pourra pas y livrer ses "
            "notifications : vos commandes se confirmeront au retour du client, ou par la "
            "vérification automatique qui passe toutes les dix minutes. C'est normal sur "
            "un poste de développement. En production, refaites cette vérification en "
            "accédant à Odoo par l'adresse publique de votre boutique, en HTTPS.", base,
        )
    if probleme == "not_https":
        return _(
            "Vous accédez à Odoo par « %s », qui n'est pas en HTTPS. Mobupay n'y livrera "
            "pas ses notifications : vos commandes se confirmeront au retour du client, ou "
            "par la vérification automatique qui passe toutes les dix minutes, mais un "
            "remboursement fait depuis votre espace Mobupay n'apparaîtra pas dans Odoo. "
            "Refaites cette vérification en accédant à Odoo par son adresse HTTPS.", base,
        )
    return None


def billing_state(provider):
    """Etat du module Facturation Mobupay, tel que l'API le rend (PLAN-960 lot 5.2).

    :return: dict {'message': str, 'issuable': bool|None, 'activation_url': str|None}.
        UN seul message, calcule ici a partir de TOUT ce que l'API dit (regle 28 du
        depot) : jamais « actif » d'un cote et « suspendu » de l'autre.
    """
    try:
        body = mobupay_api.request(
            provider.mobupay_api_base, (provider.mobupay_api_key or "").strip(), "GET",
            "/api/v1/billing/settings",
        ) or {}
    except mobupay_api.MobupayError as exc:
        if getattr(exc, "status", 0) in (401, 403):
            return {
                "message": _(
                    "Votre clé API n'a pas le droit de lire la facturation : l'état du "
                    "module Facturation ne peut pas être vérifié."
                ),
                "issuable": None,
                "activation_url": None,
            }
        return {"message": _("État du module Facturation indisponible : %s", exc),
                "issuable": None, "activation_url": None}

    etat = body.get("moduleState") or ("ouverte" if body.get("enabled") else "a_activer")
    manques = [m.get("label") or m.get("field") for m in ((body.get("completeness") or {}).get("missing") or [])
               if isinstance(m, dict)]
    activation = body.get("activationUrl")
    if etat == "a_activer":
        message = _(
            "Le module Facturation Mobupay n'est pas activé : aucune facture ne sera "
            "établie. Activez-le depuis votre espace marchand."
        )
    elif etat in ("attente_carte", "attente_premier_paiement"):
        message = _(
            "Le module Facturation Mobupay est en attente de son premier règlement : "
            "aucune facture ne sera établie d'ici là."
        )
    elif etat == "suspendu_impaye":
        message = _(
            "Le module Facturation Mobupay est suspendu pour une échéance impayée : "
            "aucune facture ne sera établie tant qu'elle ne sera pas réglée."
        )
    elif manques:
        message = _(
            "Le module Facturation Mobupay est actif, mais des mentions obligatoires "
            "manquent : %s. Les factures resteront en brouillon.", ", ".join(manques[:5]),
        )
    else:
        message = _("Le module Facturation Mobupay est actif : vos factures seront établies.")
    return {
        "message": message,
        "issuable": bool(body.get("issuable", etat == "ouverte" and not manques)),
        "activation_url": activation if etat != "ouverte" or manques else None,
    }


def verify_connection_action(provider, is_live):
    """Le bouton « Vérifier la connexion » : clé, environnement, adresse, facturation.

    UNE notification, UN niveau (succes ou avertissement), qui rassemble tout : deux
    messages qui se contrediraient a l'ecran seraient la signature d'un defaut.
    """
    refresh_secret(provider, silent=False)
    environnement = _("PRODUCTION : les paiements seront réels") if is_live \
        else _("TEST : aucun paiement réel ne sera encaissé")
    lignes = [_("Vous êtes en environnement de %s.", environnement)]
    alerte = False

    probleme_url = base_url_warning(provider)
    if probleme_url:
        alerte = True
        lignes.append(probleme_url)

    if provider.mobupay_invoicing in logic.INVOICING_BY_MOBUPAY:
        facturation = billing_state(provider)
        lignes.append(facturation["message"])
        if facturation["issuable"] is False:
            alerte = True

    return {
        "type": "ir.actions.client",
        "tag": "display_notification",
        "params": {
            "type": "warning" if alerte else "success",
            "sticky": alerte,
            "title": _("Connexion à Mobupay vérifiée"),
            "message": "\n\n".join(lignes),
        },
    }


def activation_action(provider):
    """Ouvre la page d'activation du module Facturation, dans l'espace marchand.

    L'activation est un geste HUMAIN (conditions generales, grille tarifaire, moyen
    de prelevement) : arbitrage du 2026-10-05. Le module n'active rien, il
    mene a la page. L'adresse vient du serveur (`activationUrl`), jamais recomposee
    ici.
    """
    etat = billing_state(provider)
    url = etat.get("activation_url")
    if not url:
        raise UserError(etat["message"])
    return {"type": "ir.actions.act_url", "url": url, "target": "new"}


# ── Transaction : la session de paiement ────────────────────────────────────

def source_order(tx):
    """Commande de vente derriere la transaction, s'il y en a UNE seule.

    Odoo permet de payer plusieurs commandes d'un coup : melanger leurs lignes sur une
    meme facture melangerait deux ventes distinctes.
    """
    orders = getattr(tx, "sale_order_ids", False)
    if orders and len(orders) == 1:
        return orders[0]
    return None


def source_invoice(tx):
    """Facture Odoo reglee par la transaction, s'il y en a UNE seule."""
    moves = getattr(tx, "invoice_ids", False)
    if moves and len(moves) == 1:
        return moves[0]
    return None


def session_payload(tx, champs, odoo_auto_invoice):
    """Corps de `POST /api/v1/payments/sessions` pour cette transaction.

    :param odoo_auto_invoice: la facturation automatique d'Odoo est-elle active ? Lue
        par la couche, car son emplacement change en 20.
    """
    provider = tx.provider_id
    currency = (tx.currency_id.name or "EUR").upper()
    order = source_order(tx)
    invoice = source_invoice(tx)
    invoicing, raison = logic.invoicing_request(
        provider.mobupay_invoicing,
        provider.mobupay_operation_type,
        odoo_auto_invoice,
        pays_an_odoo_invoice=bool(getattr(tx, "invoice_ids", False)),
    )
    if raison:
        _logger.info("Mobupay : aucune facture demandée pour %s (%s).", tx.reference, raison)

    traduire = lambda text: _(text)  # noqa: E731 -- le noyau ne connait pas `_`
    if order is not None:
        built = order_builder.build(
            order, currency,
            with_items=provider.mobupay_send_order_details,
            with_customer=provider.mobupay_send_customer_details,
            invoicing=invoicing, translate=traduire, champs=champs,
        )
    elif invoice is not None:
        built = order_builder.build_from_invoice(
            invoice, currency,
            with_items=provider.mobupay_send_order_details,
            with_customer=provider.mobupay_send_customer_details,
            translate=traduire,
        )
    else:
        built = None

    if built is not None:
        for note in built["notes"]:
            niveau = logging.ERROR if note.startswith("ERREUR") else logging.INFO
            _logger.log(niveau, "Mobupay : charge utile de %s : %s", tx.reference, note)
        order_payload = built["order"]
    else:
        # Aucune commande ni facture (lien de paiement libre, acompte saisi a la
        # main) : charge minimale. Un detail manquant est un desagrement, un paiement
        # refuse est une vente perdue.
        order_payload = {}
    # La reference et le montant font foi cote transaction : une commande modifiee
    # entre le devis et le paiement ne doit pas faire diverger les deux.
    order_payload["reference"] = tx.reference
    order_payload["amount"] = order_builder.core.to_minor_units(tx.amount, currency)
    order_payload["currency"] = currency

    base_url = provider.get_base_url().rstrip("/")
    payload = {
        "order": order_payload,
        # PLAN-960 lot 0.3 — retour par NOTRE route : elle relit le paiement avant
        # d'afficher la page de statut, sur les quatre series. Le parametre ne
        # s'appelle PAS `reference` : Mobupay ajoute au retour `status`, `paymentId`
        # et `reference` (SA reference de recu, `MBP-...`), en REMPLACANT un parametre
        # du meme nom. La transaction n'etait alors jamais retrouvee, et le client
        # attendait la tache des dix minutes (constate en recette le 2026-10-05).
        "redirectUrl": "%s/payment/mobupay/return?odoo_reference=%s" % (base_url, quote(tx.reference)),
        "notificationUrl": "%s/payment/mobupay/webhook" % base_url,
        "externalId": tx.reference,
    }
    email = (getattr(tx.partner_id, "email", "") or "").strip()
    if email:
        payload["email"] = email
    # PLAN-621 — boutique emettrice. Champ de PREMIER NIVEAU, surtout PAS dans
    # `order` : le schema de commande supprime les cles inconnues en silence.
    store_id = (provider.mobupay_store_id or "").strip()
    if store_id:
        payload["storeId"] = store_id
    return payload


def create_session(tx, payload):
    """Cree la session, avec la ceinture de securite de facturation.

    Depuis PLAN-598 lot A, le serveur n'oppose plus de refus a un encaissement dont les
    mentions de facturation manquent. Le repli reste pour un serveur en retard d'une
    version : **un paiement ne doit JAMAIS echouer pour un motif de facturation.**
    """
    provider = tx.provider_id
    idempotency_key = "odoo-%s-%s" % (identifiant_de_base(tx.env), tx.reference)
    try:
        return provider._mobupay_request("POST", "/api/v1/payments/sessions", payload, idempotency_key)
    except mobupay_api.MobupayError as exc:
        if not (exc.is_invoicing_error and "invoicing" in payload.get("order", {})):
            raise ValidationError(_("Mobupay : %s", exc))
        fallback = dict(payload)
        fallback["order"] = dict(payload["order"])
        fallback["order"].pop("invoicing", None)
        _logger.warning(
            "Mobupay : facturation refusée par l'API, nouvelle tentative sans elle (transaction %s)",
            tx.reference,
        )
        try:
            session = provider._mobupay_request("POST", "/api/v1/payments/sessions", fallback, idempotency_key)
        except mobupay_api.MobupayError as inner:
            raise ValidationError(_("Mobupay : %s", inner))
        order = source_order(tx)
        if order is not None:
            order.message_post(body=_(
                "Paiement Mobupay accepté, mais la facture n'a pas pu être demandée : les "
                "coordonnées du client sont incomplètes."
            ))
        return session


def identifiant_de_base(env):
    """Identifiant unique de la base Odoo, pour la cle d'idempotence de la session.

    La reference d'une transaction n'est unique que DANS sa base : toute base neuve
    repart de `S00001`. Or Mobupay garde la cle (marchand, cle) SANS limite de duree.
    Avec `odoo-S00001` seul, une seconde base branchee sur le meme compte Mobupay (une
    autre boutique, une base de test, une reinstallation) recevait la session, voire
    le paiement DEJA REGLE, de la premiere : la commande se confirmait sans que son
    client ait paye. Constate en recette le 2026-10-05, entre deux instances.
    `database.uuid` est pose par Odoo a la creation de la base, dans les quatre series.
    """
    parametres = env["ir.config_parameter"].sudo()
    # Odoo 20 a remplace `get_param` par des lectures typees.
    lire = getattr(parametres, "get_str", None) or parametres.get_param
    return lire("database.uuid") or env.cr.dbname


def rendering_values(tx, champs, odoo_auto_invoice):
    """`_get_specific_rendering_values` des quatre series : session, puis URL."""
    session = create_session(tx, session_payload(tx, champs, odoo_auto_invoice))
    checkout_url = session.get("checkoutUrl") or session.get("linkUrl")
    if not checkout_url:
        raise ValidationError(_("Mobupay n'a pas renvoyé d'URL de paiement."))
    # L'identifiant Mobupay sert au rapprochement et au remboursement : l'attendre du
    # webhook le perdrait si le client ferme son navigateur.
    if session.get("paymentId"):
        _ecrire(tx, {"provider_reference": session["paymentId"]})
    # Le formulaire de redirection est en GET, et un navigateur REMPLACE la partie
    # `?...` de son adresse par ses champs : `/checkout?session=ses_...` arrivait sur
    # `/checkout`, que la page hebergee renvoie sur « Lien expire ». Les parametres de
    # l'adresse partent donc en champs caches (meme forme que les modules de paiement
    # d'Odoo eux-memes). Constate en recette le 2026-10-05.
    return {
        "api_url": checkout_url,
        "url_params": parse_qsl(urlsplit(checkout_url).query, keep_blank_values=True),
    }


# ── Transaction : appliquer ce que Mobupay annonce ──────────────────────────

def apply_status(tx, data):
    """Applique un statut Mobupay a la transaction. Implementation UNIQUE.

    Appelee par le webhook, la route de retour et la tache de reprise, sur les quatre
    series (directement en 17/18, depuis `_apply_updates` en 19 et 20).
    """
    data = data or {}
    if tx.operation == "refund":
        # Une transaction de remboursement porte l'identifiant du REMBOURSEMENT
        # Mobupay : c'est lui qui la distingue d'un remboursement fait hors d'Odoo.
        refund_id = data.get("refundId")
        if refund_id and tx.provider_reference != refund_id:
            _ecrire(tx, {"provider_reference": refund_id})
    else:
        payment_id = data.get("paymentId") or data.get("id")
        if payment_id and tx.provider_reference != payment_id:
            _ecrire(tx, {"provider_reference": payment_id})

    transition = logic.transition(data.get("status"))
    if transition == "done":
        tx._set_done()
    elif transition == "pending":
        tx._set_pending()
    elif transition == "cancel":
        tx._set_canceled()
    elif transition == "error":
        tx._set_error(_("Paiement refusé par Mobupay."))
    elif transition == "refund":
        reflect_refund(tx, data)
    else:
        _logger.warning(
            "Mobupay : statut inconnu « %s » pour la transaction %s, aucune transition appliquée.",
            data.get("status"), tx.reference,
        )


def verrou_remboursements(source_tx):
    """Serialise les remboursements d'UNE transaction d'origine.

    LA COURSE QUE CE VERROU FERME. Un remboursement lance depuis Odoo cree sa
    transaction fille, appelle l'API, puis se valide a la fin de la requete. Mobupay
    emet son webhook des le remboursement fait : il peut arriver AVANT cette
    validation, ne pas voir la fille encore invisible, et en creer une seconde -- un
    remboursement compte deux fois dans la comptabilite d'Odoo.

    Les deux chemins prennent ce verrou : celui qui arrive second attend que le
    premier ait valide, puis voit sa fille. Verrou de transaction PostgreSQL, relache
    a la validation ou a l'annulation, propre a la transaction d'origine.
    """
    tx = source_tx.source_transaction_id or source_tx
    source_tx.env.cr.execute("SELECT pg_advisory_xact_lock(%s, %s)", (960_000, tx.id))


def reflect_refund(tx, data):
    """Reflete dans Odoo un remboursement fait HORS d'Odoo (espace marchand Mobupay).

    Un remboursement lance depuis Odoo cree deja sa transaction fille, qui porte
    l'identifiant du remboursement Mobupay : on la retrouve et on ne refait rien. Un
    remboursement lance ailleurs n'existe pas dans Odoo : sans cette fonction, la
    commande resterait payee en totalite dans la comptabilite d'Odoo alors que
    l'argent est rendu. C'est le motif du module Stripe du coeur d'Odoo.
    """
    remboursement = logic.refund_of(data)
    if remboursement is None or tx.operation == "refund":
        return
    verrou_remboursements(tx)
    deja = tx.search([
        ("source_transaction_id", "=", tx.id),
        ("operation", "=", "refund"),
        ("provider_reference", "=", remboursement["refund_id"]),
    ], limit=1)
    if deja:
        return
    fille = tx._create_child_transaction(remboursement["amount"], is_refund=True)
    _ecrire(fille, {"provider_reference": remboursement["refund_id"]})
    fille._set_done()
