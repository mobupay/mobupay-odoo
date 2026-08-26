# -*- coding: utf-8 -*-
"""Transaction de paiement Mobupay.

PLAN-599 lots O3, O4, O5 et O7.

─────────────────────────────────────────────────────────────────────────────
COMPATIBILITE ODOO 17 / 18 : VERIFIEE CONTRE LA SOURCE
─────────────────────────────────────────────────────────────────────────────
Verification faite le 2026-08-25 en lisant le module `payment` des images officielles
`odoo:17` et `odoo:18`, puis en installant ce module dans les deux.

**Les quatre methodes d'extension portent le MEME nom dans les deux series** :
`_get_specific_rendering_values`, `_get_tx_from_notification_data`,
`_process_notification_data` et `_send_refund_request(amount_to_refund=None)`.

Une version anterieure de ce fichier declarait en plus `_search_by_reference` et
`_apply_updates`, sur la SUPPOSITION que la 18 les avait renommees. C'etait faux : ces
methodes n'existent dans aucune des deux series. Leurs surcharges appelaient un
`super()` inexistant, donc du code mort qui aurait leve une `AttributeError` le jour ou
quelque chose les aurait appelees, et qui surtout laissait croire a une compatibilite
verifiee alors qu'elle etait devinee. Elles sont retirees.

La lecon vaut au-dela d'Odoo : un pont de compatibilite ecrit sans lire la source n'est
pas defensif, il est decoratif.
"""

import logging
from datetime import timedelta

from odoo import _, api, fields, models
from odoo.exceptions import ValidationError

from . import mobupay_api
from . import order_builder

_logger = logging.getLogger(__name__)

#: Etats Mobupay qui valent un encaissement. `authorized` en fait partie : c'est le
#: defaut d'un connecteur qui n'ecoute que `captured`, corrige a l'echelle du produit
#: par PLAN-236, et il ne doit pas renaitre ici.
_DONE_STATES = ("captured", "authorized", "succeeded")
_PENDING_STATES = ("pending", "processing", "transit")
_CANCEL_STATES = ("cancelled", "canceled", "expired")
_ERROR_STATES = ("failed", "refused", "declined")


class PaymentTransaction(models.Model):
    _inherit = "payment.transaction"

    #: Derniere interrogation de l'API pour cette transaction. Sert d'etrangleur : la
    #: page de statut sonde en boucle pendant que le client attend, et sans cela chaque
    #: sondage partirait jusqu'a Mobupay.
    mobupay_polled_at = fields.Datetime(string="Dernière vérification Mobupay", readonly=True)

    # ── Depart du paiement ──────────────────────────────────────────────────

    def _get_specific_rendering_values(self, processing_values):
        """Cree la session Mobupay et rend l'URL de la page hebergee."""
        result = super()._get_specific_rendering_values(processing_values)
        if self.provider_code != "mobupay":
            return result

        provider = self.provider_id
        currency = (self.currency_id.name or "EUR").upper()
        order = self._mobupay_source_order()

        if order is not None:
            built = order_builder.build(
                order,
                currency,
                with_items=provider.mobupay_send_order_details,
                with_customer=provider.mobupay_send_customer_details,
                invoicing=provider.mobupay_invoicing,
                translate=lambda text: _(text),
            )
            for note in built["notes"]:
                _logger.info("Mobupay : charge utile de la commande %s : %s", self.reference, note)
            order_payload = built["order"]
            # La reference et le montant font foi cote transaction : une commande
            # modifiee entre le devis et le paiement ne doit pas faire diverger les
            # deux, et c'est la transaction qui sera rapprochee.
            order_payload["reference"] = self.reference
            order_payload["amount"] = _to_minor(self.amount, currency)
            order_payload["currency"] = currency
        else:
            # Aucune commande derriere la transaction (lien de paiement, acompte
            # saisi a la main) : charge minimale. Un detail manquant est un
            # desagrement, un paiement refuse est une vente perdue.
            order_payload = {
                "reference": self.reference,
                "amount": _to_minor(self.amount, currency),
                "currency": currency,
            }

        base_url = provider.get_base_url()
        payload = {
            "order": order_payload,
            "redirectUrl": "%s/payment/status" % base_url.rstrip("/"),
            "notificationUrl": "%s/payment/mobupay/webhook" % base_url.rstrip("/"),
            "externalId": self.reference,
        }
        email = self._mobupay_buyer_email()
        if email:
            payload["email"] = email

        session = self._mobupay_create_session(provider, payload)
        checkout_url = session.get("checkoutUrl") or session.get("linkUrl")
        if not checkout_url:
            raise ValidationError(_("Mobupay n'a pas renvoyé d'URL de paiement."))

        # Trace l'identifiant Mobupay des maintenant : il sert au rapprochement et au
        # remboursement, et l'attendre du webhook le perdrait si le client ferme son
        # navigateur.
        if session.get("paymentId"):
            self.provider_reference = session["paymentId"]

        return {"api_url": checkout_url}

    def _mobupay_create_session(self, provider, payload):
        """Cree la session, avec la CEINTURE DE SECURITE de facturation.

        Depuis PLAN-598 lot A, le serveur n'oppose plus de refus a un encaissement dont
        les mentions de facturation manquent : il encaisse et laisse un brouillon a
        completer. Ce repli ne devrait donc plus se declencher. On le garde pour les
        boutiques dont le serveur Mobupay serait en retard d'une version, car
        **un paiement ne doit JAMAIS echouer pour un motif de facturation.**
        """
        # Cle stable par tentative : un rejeu reseau ne cree pas deux paiements.
        idempotency_key = "odoo-%s" % self.reference
        try:
            return provider._mobupay_request(
                "POST", "/api/v1/payments/sessions", payload, idempotency_key
            )
        except mobupay_api.MobupayError as exc:
            if exc.is_invoicing_error and "invoicing" in payload.get("order", {}):
                fallback = dict(payload)
                fallback["order"] = dict(payload["order"])
                fallback["order"].pop("invoicing", None)
                _logger.warning(
                    "Mobupay : facturation refusée par l'API, nouvelle tentative sans "
                    "elle (transaction %s)", self.reference,
                )
                try:
                    session = provider._mobupay_request(
                        "POST", "/api/v1/payments/sessions", fallback, idempotency_key
                    )
                except mobupay_api.MobupayError as inner:
                    raise ValidationError(_("Mobupay : %s", inner))
                order = self._mobupay_source_order()
                if order is not None:
                    order.message_post(body=_(
                        "Paiement Mobupay accepté, mais la facture n'a pas pu être "
                        "demandée : les coordonnées du client sont incomplètes."
                    ))
                return session
            raise ValidationError(_("Mobupay : %s", exc))

    # ── Retour du paiement ──────────────────────────────────────────────────

    def _mobupay_apply(self, notification_data):
        """Applique un evenement Mobupay a la transaction. Implementation UNIQUE."""
        self.ensure_one()
        data = (notification_data or {}).get("data") or {}
        payment_id = data.get("paymentId") or data.get("id")
        if payment_id:
            self.provider_reference = payment_id

        status = str(data.get("status") or "").lower()
        if status in _DONE_STATES:
            self._set_done()
        elif status in _PENDING_STATES:
            self._set_pending()
        elif status in _CANCEL_STATES:
            self._set_canceled()
        elif status in _ERROR_STATES:
            self._set_error(_("Paiement refusé par Mobupay."))
        else:
            # Un statut inconnu ne doit RIEN décider : le taire vaut mieux que de
            # marquer une commande payée sur un état qu'on ne comprend pas.
            _logger.warning(
                "Mobupay : statut inconnu « %s » pour la transaction %s, aucune "
                "transition appliquée.", status, self.reference,
            )

    @api.model
    def _mobupay_find(self, notification_data):
        """Retrouve la transaction visee par un evenement. Implementation UNIQUE."""
        data = (notification_data or {}).get("data") or {}
        reference = data.get("externalId") or (data.get("order") or {}).get("reference")
        if not reference:
            raise ValidationError(_("Mobupay : évènement sans référence exploitable."))
        tx = self.search([("reference", "=", reference), ("provider_code", "=", "mobupay")], limit=1)
        if not tx:
            raise ValidationError(_("Mobupay : aucune transaction pour la référence %s.", reference))
        return tx

    # ── Contrat du module `payment` ─────────────────────────────────────────
    #
    # Ces deux noms sont ceux des DEUX series, verifies contre la source. Ils sont
    # appeles par `_handle_notification_data`, qui est le point d'entree public.

    @api.model
    def _get_tx_from_notification_data(self, provider_code, notification_data):
        if provider_code != "mobupay":
            return super()._get_tx_from_notification_data(provider_code, notification_data)
        return self._mobupay_find(notification_data)

    def _process_notification_data(self, notification_data):
        if self.provider_code != "mobupay":
            return super()._process_notification_data(notification_data)
        return self._mobupay_apply(notification_data)

    # ── Reprise : ne JAMAIS dependre du seul webhook ────────────────────────
    #
    # Le webhook est la source de verite, mais il peut ne pas arriver : adresse
    # publique mal renseignee, pare-feu, coupure reseau, instance arretee au mauvais
    # moment. Sans reprise, le paiement est encaisse et la commande reste en attente
    # POUR TOUJOURS -- le marchand voit son argent, le client voit sa commande non
    # confirmee, et personne ne comprend.
    #
    # Vecu le 2026-08-26 en recette : un paiement reellement capture (4 995 XPF) que
    # l'instance de test n'a jamais su, son adresse publique etant `localhost`.
    #
    # Deux reprises, qui se completent :
    #   - PENDANT que le client regarde la page de statut, qui sonde en boucle : il
    #     voit sa confirmation en quelques secondes, sans attendre quoi que ce soit ;
    #   - PAR UN CRON, pour le client qui a ferme son onglet.

    def _mobupay_poll(self, min_interval_seconds=2):
        """Demande a l'API l'etat REEL de cette transaction, et l'applique.

        Ne fait rien si l'etat est deja definitif, si aucun paiement n'est rattache, ou
        si l'API a ete interrogee il y a moins de `min_interval_seconds`.

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
            tx.sudo().mobupay_polled_at = fields.Datetime.now()
            try:
                payment = tx.provider_id._mobupay_request(
                    "GET", "/api/v1/payments/%s" % tx.provider_reference
                )
            except mobupay_api.MobupayError as exc:
                _logger.info(
                    "Mobupay : reprise impossible pour %s : %s", tx.reference, exc
                )
                continue
            status = str((payment or {}).get("status") or "")
            if status:
                _logger.info(
                    "Mobupay : reprise de %s, l'API annonce « %s »", tx.reference, status
                )
                # Meme forme qu'un webhook : une SEULE fonction decide des transitions.
                tx._mobupay_apply({"data": {
                    "externalId": tx.reference,
                    "status": status,
                    "paymentId": payment.get("id") or tx.provider_reference,
                }})

    def _get_post_processing_values(self):
        """Le client attend devant sa page de statut : on en profite pour verifier.

        La page sonde en boucle. Chaque sondage est l'occasion de demander a Mobupay ou
        en est reellement le paiement, ce qui rend la confirmation quasi immediate meme
        si le webhook tarde ou ne vient jamais.
        """
        if self.provider_code == "mobupay":
            self._mobupay_poll()
        return super()._get_post_processing_values()

    @api.model
    def _cron_mobupay_poll_pending(self, batch_size=50):
        """Rattrape les transactions dont personne ne regarde la page de statut.

        Borne des deux cotes : on laisse d'abord au webhook le temps d'arriver (deux
        minutes), et on ne remonte pas indefiniment (sept jours), sans quoi le cron
        rejouerait eternellement des abandons de panier.
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
        # `min_interval_seconds=0` : le cron est lui-meme espace, l'etrangleur ne sert
        # qu'a la page de statut.
        pending._mobupay_poll(min_interval_seconds=0)

    # ── Remboursement ───────────────────────────────────────────────────────

    def _send_refund_request(self, amount_to_refund=None):
        """Signature VERIFIEE contre la source des deux series : la methode est
        appelee en mot-cle (`_send_refund_request(amount_to_refund=...)`), et c'est
        `self` qui est la transaction SOURCE, le parent creant la transaction fille."""
        if self.provider_code != "mobupay":
            return super()._send_refund_request(amount_to_refund=amount_to_refund)

        refund_tx = super()._send_refund_request(amount_to_refund=amount_to_refund)
        currency = (refund_tx.currency_id.name or "EUR").upper()
        # `amountCents`, PAS `amount`. L'API ignore les cles inconnues : un `amount`
        # etait perdu en silence et le remboursement partait EN TOTALITE, en repondant
        # « succes ». Defaut vecu le 2026-08-24 sur les huit connecteurs.
        payload = {"amountCents": _to_minor(abs(refund_tx.amount), currency)}
        try:
            self.provider_id._mobupay_request(
                "POST",
                "/api/v1/payments/%s/refund" % self.provider_reference,
                payload,
                "odoo-refund-%s" % refund_tx.reference,
            )
        except mobupay_api.MobupayError as exc:
            raise ValidationError(_("Mobupay : remboursement refusé : %s", exc))
        return refund_tx

    # ── Lecture de la commande ──────────────────────────────────────────────

    def _mobupay_source_order(self):
        """Commande de vente derriere la transaction, s'il y en a UNE seule.

        Odoo permet de payer plusieurs commandes d'un coup. Dans ce cas on ne construit
        pas de detail : melanger les lignes de deux commandes sur une meme facture
        melangerait deux ventes distinctes.
        """
        orders = getattr(self, "sale_order_ids", False)
        if orders and len(orders) == 1:
            return orders[0]
        return None

    def _mobupay_buyer_email(self):
        partner = self.partner_id
        return (getattr(partner, "email", "") or "").strip()


def _to_minor(amount, currency_iso):
    from . import mobupay_order_payload as core
    return core.to_minor_units(amount, currency_iso)
