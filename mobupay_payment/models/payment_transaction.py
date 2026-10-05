# -*- coding: utf-8 -*-
"""Transaction de paiement Mobupay -- couche Odoo 19.0.

Le cadre 19 (odoo/odoo#209685) : une notification passe par `_search_by_reference`
puis `_process(code, donnee)`, qui CONTROLE LE MONTANT (`_validate_amount`) avant
`_apply_updates`. Le remboursement s'appelle sur la transaction FILLE, sans argument.
Verifie dans le code de l'image officielle `odoo:19.0` le 2026-10-02.

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
CHAMPS = order_builder.CHAMPS_19_20


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

    # ── Notification : le contrat 19 ────────────────────────────────────

    @api.model
    def _extract_reference(self, provider_code, payment_data):
        if provider_code != "mobupay":
            return super()._extract_reference(provider_code, payment_data)
        return logic.reference_of(payment_data)

    def _extract_amount_data(self, payment_data):
        """Le montant a controler, dans la devise d'ORIGINE (`grossAmount`).

        `amount` est en centimes EUR internes : le comparer a une transaction en XPF
        mettrait tous les paiements caledoniens en erreur. Une donnee sans montant
        d'origine SAUTE le controle (`None`) au lieu de le faire echouer.
        """
        if self.provider_code != "mobupay":
            return super()._extract_amount_data(payment_data)
        return logic.amount_data(payment_data)

    def _apply_updates(self, payment_data):
        if self.provider_code != "mobupay":
            return super()._apply_updates(payment_data)
        common.apply_status(self, payment_data)

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
        self._process("mobupay", data)

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

    # ── Remboursement : contrat 19, appele sur la FILLE ───────────────────

    def _send_refund_request(self):
        """`self` est la transaction de REMBOURSEMENT, creee par `_refund`.

        L'API rembourse le montant demande, et accepte plusieurs remboursements
        partiels jusqu'au total (PLAN-960 lot R). Une `ValidationError` est rattrapee
        par `_refund`, qui met la fille en erreur avec le message de l'API.
        """
        if self.provider_code != "mobupay":
            return super()._send_refund_request()

        source = self.source_transaction_id
        common.verrou_remboursements(self)
        currency = (self.currency_id.name or "EUR").upper()
        # `amountCents`, PAS `amount` : l'API ignore les cles inconnues, et un `amount`
        # faisait rembourser la TOTALITE en repondant « succes » (defaut du 2026-08-24).
        payload = {"amountCents": order_builder.core.to_minor_units(abs(self.amount), currency)}
        try:
            body = source.provider_id._mobupay_request(
                "POST", "/api/v1/payments/%s/refund" % source.provider_reference,
                payload, "odoo-refund-%s" % self.reference,
            ) or {}
        except mobupay_api.MobupayError as exc:
            raise ValidationError(_("Mobupay : remboursement refusé : %s", exc))
        donnee = {
            "status": "succeeded",
            "refundId": body.get("id"),
            # Le montant REMBOURSE, dans la devise d'origine : le coeur le controle
            # contre la fille (montant negatif, que le controle inverse).
            "grossAmount": body.get("originalAmount"),
            "originalCurrency": body.get("originalCurrency"),
        }
        self._process("mobupay", donnee)
