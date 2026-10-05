# -*- coding: utf-8 -*-
"""Webhook et page de retour Mobupay -- COMMUNS aux quatre series.

Chaque couche branche `_mobupay_ingest` sur le cadre de SA serie (`_handle_notification_data`
en 17/18, `_process` en 19, `_record` en 20) : le controleur, lui, ne change pas.

Le webhook est la SEULE source de verite sur l'issue d'un paiement : le retour
navigateur ne prouve rien, un client pouvant fermer son onglet avant la redirection ou
la falsifier. Trois exigences, chacune tiree d'un piege reel :

  1. Le corps est lu BRUT (`get_data()`), jamais re-serialise : un `json.dumps()`
     change les espaces et l'ordre des cles, donc l'empreinte.
  2. La signature est verifiee AVANT tout effet, et avec le secret du fournisseur DE
     LA TRANSACTION visee (PLAN-960, D3). Le controleur prenait le PREMIER fournisseur
     Mobupay : avec deux societes et deux cles, les paiements de la seconde etaient
     verifies avec le secret de la premiere, rejetes en 403, et ses commandes
     restaient en attente.
  3. Reponse 200 quand l'evenement ne nous concerne pas (sinon Mobupay rejoue
     indefiniment) ; reponse 500 quand NOTRE code echoue, pour que Mobupay rejoue au
     lieu de perdre la confirmation.
"""

import json
import logging

from werkzeug.exceptions import Forbidden

from odoo import http
from odoo.exceptions import ValidationError
from odoo.http import request

from ..models import mobupay_api
from ..models import mobupay_logic as logic

_logger = logging.getLogger(__name__)


def verifier(raw_body, headers, tx):
    """Verifie la signature : secret du fournisseur de la transaction, ou de l'un d'eux.

    Un evenement qui ne designe aucune transaction de cette base (autre instance, rejeu
    tardif) est verifie contre chacun des fournisseurs Mobupay : on ne lit pas un
    contenu non authentifie, meme pour l'ignorer.
    """
    if tx:
        secrets = [tx.provider_id.mobupay_webhook_secret or ""]
    else:
        secrets = request.env["payment.provider"].sudo().search(
            [("code", "=", "mobupay")]
        ).mapped("mobupay_webhook_secret")
    derniere = None
    for secret in secrets:
        try:
            return mobupay_api.verify_signature(raw_body, headers, secret or "")
        except mobupay_api.MobupayError as exc:
            derniere = exc
    raise Forbidden(str(derniere or "aucun fournisseur Mobupay configuré"))


class MobupayController(http.Controller):

    @http.route("/payment/mobupay/webhook", type="http", auth="public", methods=["POST"],
                csrf=False, save_session=False)
    def mobupay_webhook(self, **_kwargs):
        raw_body = request.httprequest.get_data()
        headers = dict(request.httprequest.headers)
        Tx = request.env["payment.transaction"].sudo()

        # La reference est lue SANS confiance, seulement pour choisir le secret : rien
        # n'est applique avant que la signature ne soit verifiee.
        try:
            brut = json.loads(raw_body.decode("utf-8") or "{}")
        except (ValueError, UnicodeDecodeError):
            brut = {}
        reference = logic.reference_of((brut or {}).get("data") or {})
        tx = Tx.search([("reference", "=", reference), ("provider_code", "=", "mobupay")], limit=1) \
            if reference else Tx

        try:
            event = verifier(raw_body, headers, tx)
        except Forbidden as exc:
            # 403 : la livraison est REFUSEE, pas mal formee. Mobupay ne la rejoue pas.
            _logger.warning("Mobupay : webhook rejeté : %s", exc)
            return request.make_response("invalid signature", status=403)

        if not tx:
            _logger.info("Mobupay : évènement %s sans transaction dans cette base, ignoré.",
                         event.get("type"))
            return request.make_response("ignored", status=200)

        try:
            tx._mobupay_ingest(event.get("data") or {})
        except ValidationError as exc:
            _logger.warning("Mobupay : évènement %s non appliqué : %s", event.get("type"), exc)
            return request.make_response("ignored", status=200)
        except Exception:  # noqa: BLE001 -- NOTRE code a echoue : Mobupay doit rejouer
            _logger.exception("Mobupay : erreur en appliquant l'évènement %s", event.get("type"))
            return request.make_response("error", status=500)
        return request.make_response("ok", status=200)

    @http.route("/payment/mobupay/return", type="http", auth="public", methods=["GET"],
                csrf=False, save_session=False)
    def mobupay_return(self, odoo_reference=None, paymentId=None, reference=None, **_kwargs):
        """Retour du client depuis la page de paiement (PLAN-960 lot 0.3).

        Relit le paiement aupres de l'API AVANT d'afficher la page de statut : le
        client voit sa confirmation a l'arrivee, que le webhook soit deja passe ou non.
        La reprise par la page de statut ne marchait qu'en 17 -- la 18 n'appelle plus
        la methode surchargee --, et le client attendait la tache des dix minutes.

        Sans danger : la route ne fait que RELIRE l'etat reel aupres de Mobupay, avec
        la cle du marchand. Elle ne peut rien forger, et l'etrangleur borne les appels.
        """
        # `odoo_reference` est NOTRE parametre ; `paymentId` est ajoute par Mobupay ;
        # `reference` ne sert qu'aux sessions ouvertes avant la 1.2.1, ou Mobupay
        # l'ecrasait deja par sa propre reference de recu (`MBP-...`).
        Transactions = request.env["payment.transaction"].sudo()
        tx = Transactions.browse()
        for champ, valeur in (("reference", odoo_reference), ("provider_reference", paymentId),
                              ("reference", reference)):
            if valeur and not tx:
                tx = Transactions.search(
                    [(champ, "=", valeur), ("provider_code", "=", "mobupay")], limit=1
                )
        if tx:
            try:
                tx._mobupay_poll(min_interval_seconds=2)
            except Exception:  # noqa: BLE001 -- jamais casser la page du client
                _logger.exception("Mobupay : reprise au retour en échec pour %s", tx.reference)
        return request.redirect("/payment/status")
