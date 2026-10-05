# -*- coding: utf-8 -*-
"""Fournisseur de paiement Mobupay -- couche Odoo 19.0.

Un seul champ visible pour le marchand : sa cle API. Le secret de signature des
webhooks est recupere automatiquement a l'enregistrement, avec cette meme cle qui y
donne deja acces. La logique commune aux quatre series vit dans `mobupay_common` et
`mobupay_logic` ; ce fichier ne porte que ce qui est propre au cadre 17/18 : l'etat
du fournisseur s'y lit encore sur le champ `state` (`disabled` / `test` / `enabled`),
qui disparait en 20.
"""

import logging

from odoo import _, api, fields, models
from odoo.exceptions import ValidationError

from . import mobupay_api
from . import mobupay_common as common
from . import mobupay_logic as logic

_logger = logging.getLogger(__name__)


class PaymentProvider(models.Model):
    _inherit = "payment.provider"

    code = fields.Selection(
        selection_add=[("mobupay", "Mobupay")],
        ondelete={"mobupay": "set default"},
    )
    # PLAN-960 (D5) — `copy=False` sur les trois identifiants. Odoo RECOPIE le
    # fournisseur dans chaque societe (`_setup_provider`, et `res.company.create` en
    # 19) : sans cela, une societe creee apres l'installation heritait de la cle de la
    # premiere, et, activee sans la changer, encaissait sur le compte Mobupay d'une
    # AUTRE societe. Odoo a pose `copy=False` sur les identifiants de ses propres
    # fournisseurs entre 18 et 19.
    mobupay_api_key = fields.Char(
        string="Clé API",
        help="Clé sk_test_… pour le mode test, sk_live_… en production. Espace marchand "
             "Mobupay, rubrique Développeurs, Clés API. C'est le seul secret à saisir.",
        groups="base.group_system",
        copy=False,
    )
    mobupay_webhook_secret = fields.Char(
        # Le libelle DIT qu'il est automatique : sans cela le marchand voit deux champs
        # masques cote a cote et croit devoir remplir les deux (constate le 2026-08-26).
        string="Secret de signature (rempli automatiquement)",
        readonly=True,
        help="Récupéré tout seul à partir de votre clé API. Vous n'avez rien à saisir "
             "ici : ce champ n'est affiché que pour vous montrer que la connexion a abouti.",
        groups="base.group_system",
        copy=False,
    )
    mobupay_api_base = fields.Char(
        string="Base API",
        default="https://api.mobupay.nc",
        help="Avancé. Ne modifier que sur instruction du support Mobupay.",
        groups="base.group_system",
    )
    mobupay_send_order_details = fields.Boolean(
        string="Détail de la commande",
        default=True,
        help="Transmettre les articles, les taxes, les frais de port et les remises. Le "
             "client voit le récapitulatif de son panier sur la page de paiement, et vos "
             "factures Mobupay détaillent chaque ligne.",
    )
    mobupay_send_customer_details = fields.Boolean(
        string="Coordonnées du client",
        default=True,
        help="Transmettre nom, adresse de facturation, téléphone et adresse de "
             "livraison. Nécessaire pour qu'une facture porte les mentions obligatoires. "
             "Tout est déduit de la commande.",
    )
    # PLAN-960 lot 5.1 — DEUX MODES EXCLUSIFS : jamais deux factures pour une vente
    # (arbitrage du 2026-10-05). Les valeurs techniques ne changent pas, une
    # base deja installee garde son reglage ; les libelles disent desormais QUI facture.
    mobupay_invoicing = fields.Selection(
        selection=[
            ("no", "Odoo établit mes factures (Mobupay n'en établit aucune)"),
            ("yes", "Mobupay établit la facture de chaque paiement"),
            ("yes_send", "Mobupay établit la facture et l'envoie au client"),
        ],
        string="Qui établit les factures",
        default="no",
        help="Si vous facturez dans Odoo, laissez Odoo : Mobupay n'établira aucune "
             "facture, pour qu'une vente n'en porte jamais deux. Choisissez Mobupay si "
             "vous encaissez depuis Odoo sans y facturer. Le module Facturation doit "
             "alors être activé dans votre espace marchand Mobupay.",
    )
    # PLAN-619 — Mention obligatoire de la facture (decret 2022-1299). Mobupay ne la
    # devine pas : biens et services ne portent pas les memes mentions.
    mobupay_operation_type = fields.Selection(
        selection=[
            ("GOODS", "Livraison de biens"),
            ("SERVICES", "Prestation de services"),
            ("MIXED", "Les deux"),
        ],
        string="Nature de l'opération",
        default="GOODS",
        help="Mention obligatoire de la facture. Une boutique qui vend des produits "
             "laisse « Livraison de biens ». Choisissez « Les deux » si vos commandes "
             "mêlent produits et prestations.",
    )
    # PLAN-621 — multi-boutique. Facultatif : la voie recommandee est de rattacher la
    # cle API a sa boutique depuis l'espace marchand.
    mobupay_store_id = fields.Char(
        string="Boutique Mobupay",
        help="Code de la boutique Mobupay à laquelle rattacher les paiements de ce site "
             "(par exemple PAITA). À remplir uniquement si la même clé API équipe "
             "plusieurs sites.",
        copy=False,
    )

    # ── Contrat du module `payment` ─────────────────────────────────────────

    def _compute_feature_support_fields(self):
        """Le remboursement, partiel compris (PLAN-960, D1).

        Odoo n'affiche le bouton « Rembourser » que si `support_refund` n'est pas
        `none`. Le module ne le declarait pas : le bouton n'est JAMAIS apparu, alors que
        `_send_refund_request` etait ecrit et que le guide disait de l'utiliser.
        `partial` : l'API accepte plusieurs remboursements partiels jusqu'au total
        (PLAN-960 lot R).
        """
        super()._compute_feature_support_fields()
        self.filtered(lambda p: p.code == "mobupay").update({"support_refund": "partial"})

    def _get_supported_currencies(self):
        """Mobupay n'encaisse qu'en EUR et en XPF : Odoo ne le propose pas ailleurs."""
        supported = super()._get_supported_currencies()
        if self.code == "mobupay":
            supported = supported.filtered(lambda c: c.name in ("EUR", "XPF"))
        return supported

    def _get_default_payment_method_codes(self):
        self.ensure_one()
        if self.code != "mobupay":
            return super()._get_default_payment_method_codes()
        return ["card"]

    # ── Etat propre a la serie : le champ `state` ───────────────────────────

    def _mobupay_is_live(self):
        """True en production, False en test, None si le fournisseur est desactive."""
        self.ensure_one()
        if self.state == "disabled":
            return None
        return self.state == "enabled"

    # ── Recuperation automatique du secret ──────────────────────────────────

    @api.model_create_multi
    def create(self, vals_list):
        providers = super().create(vals_list)
        common.refresh_secret(providers.filtered(lambda p: p.code == "mobupay"), silent=True)
        return providers

    def write(self, vals):
        result = super().write(vals)
        # On ne redemande le secret que si ce qui le determine a change.
        if {"mobupay_api_key", "state", "mobupay_api_base"} & set(vals):
            common.refresh_secret(self.filtered(lambda p: p.code == "mobupay"), silent=True)
        return result

    def action_mobupay_verify_connection(self):
        self.ensure_one()
        return common.verify_connection_action(self, bool(self._mobupay_is_live()))

    def action_mobupay_open_billing_activation(self):
        self.ensure_one()
        return common.activation_action(self)

    # ── Garde-fous ──────────────────────────────────────────────────────────

    @api.constrains("code", "state", "mobupay_api_key")
    def _mobupay_check_key_matches_state(self):
        common.check_environment(self, lambda p: p._mobupay_is_live())

    @api.constrains("code", "mobupay_invoicing")
    def _mobupay_check_single_invoicing(self):
        """Jamais deux factures pour une vente (PLAN-960 lot 5.1).

        Mobupay ne peut pas etablir les factures si Odoo les etablit deja tout seul a
        chaque paiement : refus explicite, qui nomme le reglage a couper.
        """
        for provider in self:
            if provider.code != "mobupay" or provider.mobupay_invoicing not in logic.INVOICING_BY_MOBUPAY:
                continue
            if provider._mobupay_odoo_auto_invoice():
                raise ValidationError(_(
                    "Odoo établit déjà une facture à chaque paiement (Ventes, Paramètres, "
                    "« Facturation automatique »). Mobupay ne peut pas en établir une "
                    "seconde pour la même vente : coupez ce réglage d'Odoo, ou laissez "
                    "Odoo établir vos factures."
                ))

    def _mobupay_odoo_auto_invoice(self):
        """Odoo 17 a 19 : un parametre systeme ; Odoo 20 : un champ de societe."""
        return bool(self.env["ir.config_parameter"].sudo().get_param("sale.automatic_invoice"))

    # ── Appel API ───────────────────────────────────────────────────────────

    def _mobupay_request(self, method, endpoint, payload=None, idempotency_key=None):
        self.ensure_one()
        return mobupay_api.request(
            self.mobupay_api_base,
            (self.mobupay_api_key or "").strip(),
            method,
            endpoint,
            payload=payload,
            idempotency_key=idempotency_key,
        )
