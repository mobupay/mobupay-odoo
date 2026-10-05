# -*- coding: utf-8 -*-
"""Transaction de paiement Mobupay -- couche Odoo 17.0 et 18.0.

Le cadre 17/18 : une notification passe par `_handle_notification_data`, le
remboursement s'appelle sur la transaction SOURCE (`_send_refund_request(
amount_to_refund=...)`, qui cree elle-meme la fille), et aucun controle de montant
n'est fait par le coeur. Les quatre methodes d'extension portent le meme nom dans les
deux series (verifie contre la source le 2026-08-25).

Toute la logique commune est dans `mobupay_common` : une seule implementation decide
des transitions, qu'elles viennent d'un webhook, de la page de retour ou de la reprise.
"""

import logging
from datetime import timedelta

from odoo import _, api, fields, models
from odoo.exceptions import ValidationError

from . import mobupay_api
from . import mobupay_common as common
from . import mobupay_logic as logic
from . import order_builder

_logger = logging.getLogger(__name__)

#: Noms des champs lus sur les modeles de CETTE serie (cf. `order_builder`).
CHAMPS = order_builder.CHAMPS_17_18


class PaymentTransaction(models.Model):
    _inherit = "payment.transaction"

    #: Derniere interrogation de l'API : etrangle la reprise par la page de retour.
    mobupay_polled_at = fields.Datetime(string="Dernière vérification Mobupay", readonly=True)

    # ── Depart du paiement ──────────────────────────────────────────────────

    def _get_specific_rendering_values(self, processing_values):
        result = super()._get_specific_rendering_values(processing_values)
        if self.provider_code != "mobupay":
            return result
        return common.rendering_values(self, CHAMPS, self.provider_id._mobupay_odoo_auto_invoice())

    # ── Notification : le contrat 17/18 ─────────────────────────────────────

    @api.model
    def _get_tx_from_notification_data(self, provider_code, notification_data):
        if provider_code != "mobupay":
            return super()._get_tx_from_notification_data(provider_code, notification_data)
        return self._mobupay_find(notification_data)

    def _process_notification_data(self, notification_data):
        if self.provider_code != "mobupay":
            return super()._process_notification_data(notification_data)
        common.apply_status(self, notification_data)

    @api.model
    def _mobupay_find(self, data):
        """Transaction visee par une donnee Mobupay. Leve si elle n'existe pas."""
        reference = logic.reference_of(data)
        if not reference:
            raise ValidationError(_("Mobupay : évènement sans référence exploitable."))
        tx = self.search([("reference", "=", reference), ("provider_code", "=", "mobupay")], limit=1)
        if not tx:
            raise ValidationError(_("Mobupay : aucune transaction pour la référence %s.", reference))
        return tx

    def _mobupay_ingest(self, data):
        """Fait appliquer une donnee Mobupay par le cadre de CETTE serie."""
        self.ensure_one()
        self._handle_notification_data("mobupay", data)

    # ── Reprise : ne jamais dependre du seul webhook ────────────────────────

    def _mobupay_poll(self, min_interval_seconds=2):
        """Relit l'etat REEL du paiement aupres de l'API, et l'applique.

        Ne leve JAMAIS : une reprise qui echoue doit laisser la page de statut
        s'afficher, pas la casser.
        """
        for tx in self:
            if tx.provider_code != "mobupay" or tx.state not in ("draft", "pending"):
                continue
            if not tx.provider_reference:
                continue
            if tx.mobupay_polled_at:
                age = (fields.Datetime.now() - tx.mobupay_polled_at).total_seconds()
                if age < min_interval_seconds:
                    continue
            common._ecrire(tx.sudo(), {"mobupay_polled_at": fields.Datetime.now()})
            try:
                payment = tx.provider_id._mobupay_request("GET", "/api/v1/payments/%s" % tx.provider_reference)
            except mobupay_api.MobupayError as exc:
                _logger.info("Mobupay : reprise impossible pour %s : %s", tx.reference, exc)
                continue
            data = logic.payment_to_data(payment, tx.reference)
            if data.get("status"):
                _logger.info("Mobupay : reprise de %s, l'API annonce « %s »", tx.reference, data["status"])
                tx._mobupay_ingest(data)

    @api.model
    def _cron_mobupay_poll_pending(self, batch_size=50):
        """Rattrape les transactions dont personne ne regarde la page de retour.

        Bornee des deux cotes : on laisse d'abord au webhook le temps d'arriver (deux
        minutes), et on ne remonte pas indefiniment (sept jours).
        """
        now = fields.Datetime.now()
        pending = self.search([
            ("provider_code", "=", "mobupay"),
            ("state", "in", ("draft", "pending")),
            ("provider_reference", "!=", False),
            ("create_date", "<", now - timedelta(minutes=2)),
            ("create_date", ">", now - timedelta(days=7)),
        ], limit=batch_size, order="create_date asc")
        if pending:
            _logger.info("Mobupay : reprise de %d transaction(s) en attente", len(pending))
        pending._mobupay_poll(min_interval_seconds=0)

    # ── Remboursement : contrat 17/18, appele sur la SOURCE ─────────────────

    def _send_refund_request(self, amount_to_refund=None):
        """`self` est la transaction SOURCE ; le parent cree la fille et la rend.

        PLAN-960 : la fille passe `done` des que l'API a rembourse, et porte
        l'identifiant du remboursement Mobupay (D6 : elle restait en brouillon pour
        toujours, ce qui ne se voyait pas tant que le bouton n'apparaissait pas, D1).
        """
        if self.provider_code != "mobupay":
            return super()._send_refund_request(amount_to_refund=amount_to_refund)

        common.verrou_remboursements(self)
        refund_tx = super()._send_refund_request(amount_to_refund=amount_to_refund)
        currency = (refund_tx.currency_id.name or "EUR").upper()
        # `amountCents`, PAS `amount` : l'API ignore les cles inconnues, et un `amount`
        # faisait rembourser la TOTALITE en repondant « succes » (defaut du 2026-08-24).
        payload = {"amountCents": order_builder.core.to_minor_units(abs(refund_tx.amount), currency)}
        try:
            body = self.provider_id._mobupay_request(
                "POST", "/api/v1/payments/%s/refund" % self.provider_reference,
                payload, "odoo-refund-%s" % refund_tx.reference,
            )
        except mobupay_api.MobupayError as exc:
            raise ValidationError(_("Mobupay : remboursement refusé : %s", exc))
        common.apply_status(refund_tx, {
            "status": "succeeded",
            "refundId": (body or {}).get("id"),
        })
        return refund_tx
