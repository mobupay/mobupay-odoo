# -*- coding: utf-8 -*-
"""Webhook Mobupay.

PLAN-599 lot O6. C'est la SEULE source de verite sur l'issue d'un paiement : le retour
navigateur ne prouve rien, un client pouvant fermer son onglet avant la redirection ou
la falsifier.

Trois exigences, chacune tiree d'un piege reel :

  1. Le corps doit etre lu BRUT (`request.httprequest.get_data()`), jamais re-serialise
     depuis le JSON decode : un `json.dumps()` change les espaces et l'ordre des cles,
     donc l'empreinte, et TOUS les webhooks seraient rejetes.
  2. La signature est verifiee AVANT toute lecture du contenu. Sans cela, un attaquant
     forge un `payment.captured` et fait valider une commande jamais payee.
  3. On repond 200 des que l'evenement est traite ET quand il ne nous concerne pas :
     un 500 sur un evenement inconnu ferait rejouer indefiniment la livraison.
"""

import logging

from odoo import http
from odoo.http import request

from ..models import mobupay_api

_logger = logging.getLogger(__name__)


class MobupayController(http.Controller):

    @http.route(
        "/payment/mobupay/webhook",
        type="http",
        auth="public",
        methods=["POST"],
        csrf=False,
        save_session=False,
    )
    def mobupay_webhook(self, **_kwargs):
        raw_body = request.httprequest.get_data()
        headers = dict(request.httprequest.headers)

        provider = request.env["payment.provider"].sudo().search(
            [("code", "=", "mobupay")], limit=1
        )
        if not provider:
            _logger.warning("Mobupay : webhook reçu alors qu'aucun fournisseur n'est configuré.")
            return request.make_response("no provider", status=404)

        try:
            event = mobupay_api.verify_signature(
                raw_body, headers, provider.mobupay_webhook_secret or ""
            )
        except mobupay_api.MobupayError as exc:
            # 403 et non 400 : la livraison est REFUSEE, pas mal formee. Mobupay ne
            # doit pas la rejouer.
            _logger.warning("Mobupay : webhook rejeté : %s", exc)
            return request.make_response("invalid signature", status=403)

        event_type = str(event.get("type") or "")
        try:
            tx = request.env["payment.transaction"].sudo()._mobupay_find(event)
            tx._mobupay_apply(event)
        except Exception as exc:  # noqa: BLE001 - on ne fait JAMAIS rejouer pour ça
            # Une transaction introuvable n'est pas une erreur de Mobupay : c'est
            # souvent un evenement d'une autre base, ou un rejeu tardif. Renvoyer 500
            # ferait rejouer la livraison indefiniment.
            _logger.warning("Mobupay : évènement %s non appliqué : %s", event_type, exc)
            return request.make_response("ignored", status=200)

        return request.make_response("ok", status=200)
