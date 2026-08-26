# -*- coding: utf-8 -*-
"""Fournisseur de paiement Mobupay.

PLAN-599 lots O1 et O2. Un seul champ visible pour le marchand : sa cle API. Le secret
de signature des webhooks est recupere automatiquement a l'enregistrement, avec cette
meme cle qui y donne deja acces, donc sans accorder aucun droit nouveau.

C'est la lecon des trois autres connecteurs PHP : le marchand y saisissait DEUX
secrets, et le second, colle a la main, ne se voyait defaillant qu'au premier
paiement, quand le webhook partait en 403 et que la commande restait en attente.
"""

import logging

from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError

from . import mobupay_api

_logger = logging.getLogger(__name__)


class PaymentProvider(models.Model):
    _inherit = "payment.provider"

    code = fields.Selection(
        selection_add=[("mobupay", "Mobupay")],
        ondelete={"mobupay": "set default"},
    )
    mobupay_api_key = fields.Char(
        string="Clé API",
        help="Clé sk_test_… pour le mode test, sk_live_… en production. "
             "Espace marchand Mobupay, rubrique Développeurs, Clés API. "
             "C'est le seul secret à saisir.",
        groups="base.group_system",
    )
    mobupay_webhook_secret = fields.Char(
        # Le libelle DIT qu'il est automatique. Sans cela, le marchand voit deux champs
        # masques cote a cote et croit devoir remplir les deux : c'est exactement ce que
        # ce module cherchait a supprimer, et ce serait le premier reflexe devant la
        # fiche. Constate en preparant les captures de la fiche Odoo Apps le 2026-08-26.
        string="Secret de signature (rempli automatiquement)",
        readonly=True,
        help="Récupéré tout seul à partir de votre clé API. Vous n'avez rien à saisir "
             "ici : ce champ n'est affiché que pour vous montrer que la connexion a "
             "abouti.",
        groups="base.group_system",
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
        help="Transmettre les articles, les taxes, les frais de port et les remises. "
             "Le client voit le récapitulatif de son panier sur la page de paiement, "
             "et vos factures Mobupay détaillent chaque ligne.",
    )
    mobupay_send_customer_details = fields.Boolean(
        string="Coordonnées du client",
        default=True,
        help="Transmettre nom, adresse de facturation, téléphone et adresse de "
             "livraison. Nécessaire pour qu'une facture porte les mentions "
             "obligatoires. Tout est déduit de la commande.",
    )
    mobupay_invoicing = fields.Selection(
        selection=[
            ("no", "Ne pas établir de facture"),
            ("yes", "Établir une facture pour chaque paiement"),
            ("yes_send", "Établir et envoyer la facture au client"),
        ],
        string="Facture Mobupay",
        default="no",
        help="Si des mentions obligatoires manquent, le paiement aboutit quand même "
             "et la facture reste en brouillon, à compléter depuis votre espace "
             "marchand Mobupay.",
    )

    # ── Contrat du module `payment` ─────────────────────────────────────────

    def _get_supported_currencies(self):
        """Mobupay n'encaisse qu'en EUR et en XPF.

        Le declarer ici plutot que de laisser l'API refuser : Odoo n'affiche alors
        simplement pas Mobupay au paiement d'une commande dans une autre devise, au
        lieu de le proposer puis d'echouer. Une base Odoo neuve est en USD, donc le cas
        n'a rien de theorique -- c'est le premier mur rencontre en recette.
        """
        supported = super()._get_supported_currencies()
        if self.code == "mobupay":
            supported = supported.filtered(lambda c: c.name in ("EUR", "XPF"))
        return supported

    def _get_default_payment_method_codes(self):
        """Moyens de paiement proposes. Mobupay presente la carte sur sa page hebergee."""
        self.ensure_one()
        if self.code != "mobupay":
            return super()._get_default_payment_method_codes()
        return ["card"]

    # ── Recuperation automatique du secret (lot B, transpose a Odoo) ────────

    @api.model_create_multi
    def create(self, vals_list):
        providers = super().create(vals_list)
        providers.filtered(lambda p: p.code == "mobupay")._mobupay_refresh_secret(silent=True)
        return providers

    def write(self, vals):
        result = super().write(vals)
        # On ne redemande le secret que si quelque chose qui le determine a change :
        # la cle, l'environnement, ou la base API. Le redemander a chaque ecriture
        # ferait un appel sortant a chaque changement de libelle.
        if {"mobupay_api_key", "state", "mobupay_api_base"} & set(vals):
            self.filtered(lambda p: p.code == "mobupay")._mobupay_refresh_secret(silent=True)
        return result

    def action_mobupay_verify_connection(self):
        """Bouton « Vérifier la connexion » de la fiche fournisseur.

        Le meme appel qu'a l'enregistrement, mais celui-ci PARLE : il leve une erreur
        lisible si la cle est mauvaise, et affiche l'environnement si elle est bonne.
        Un marchand qui croit etre en production alors qu'il est en test est un
        incident garanti.
        """
        self.ensure_one()
        self._mobupay_refresh_secret(silent=False)
        environment = _("TEST : aucun paiement réel ne sera encaissé") if self.state == "test" \
            else _("PRODUCTION : les paiements seront réels")
        message = _("Vous êtes en environnement de %s.", environment)

        # Une connexion valide ne suffit pas : si Mobupay ne peut pas nous joindre en
        # retour, les paiements aboutiront et les commandes resteront en attente.
        warning = self._mobupay_check_base_url()
        if warning:
            return {
                "type": "ir.actions.client",
                "tag": "display_notification",
                "params": {
                    "type": "warning",
                    "sticky": True,
                    "title": _("Connexion vérifiée, mais une confirmation ne pourra pas arriver"),
                    "message": "%s\n\n%s" % (message, warning),
                },
            }

        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "type": "success",
                "sticky": False,
                "title": _("Connexion à Mobupay vérifiée"),
                "message": message,
            },
        }

    # ── Deux garde-fous que la recette du 2026-08-26 a fait surgir ──────────

    @api.constrains("code", "state", "mobupay_api_key")
    def _mobupay_check_key_matches_state(self):
        """La cle et l'environnement doivent CONCORDER.

        Odoo porte son propre etat (« Mode test » / « Activé ») et Mobupay porte le
        sien dans le prefixe de la cle. Rien n'empechait les deux de diverger : une
        boutique en « Activé » avec une cle `sk_test_` croit encaisser pour de vrai et
        n'encaisse rien, et l'inverse encaisse REELLEMENT une boutique qui se croit en
        essai. C'est le pire des deux, et il ne se voit qu'au releve bancaire.

        On refuse la combinaison au lieu de l'avertir : c'est une incoherence, pas une
        preference, et la corriger est immediat.
        """
        for provider in self:
            if provider.code != "mobupay":
                continue
            key = (provider.mobupay_api_key or "").strip()
            if not key or provider.state == "disabled":
                continue
            if provider.state == "enabled" and key.startswith("sk_test_"):
                raise ValidationError(_(
                    "Vous activez Mobupay en PRODUCTION avec une clé de TEST "
                    "(sk_test_…). Aucun paiement ne serait réellement encaissé. "
                    "Renseignez votre clé sk_live_…, ou repassez en mode test."
                ))
            if provider.state == "test" and key.startswith("sk_live_"):
                raise ValidationError(_(
                    "Vous êtes en mode TEST avec une clé de PRODUCTION (sk_live_…). "
                    "Les paiements seraient réellement encaissés. Renseignez votre clé "
                    "sk_test_…, ou passez en Activé."
                ))

    def _mobupay_check_base_url(self):
        """L'URL publique de l'instance doit etre joignable depuis internet.

        Le module transmet son `notificationUrl` a chaque paiement, construit depuis
        `get_base_url()`. Si cette URL pointe sur `localhost` ou n'est pas en HTTPS,
        Mobupay ne peut RIEN livrer : les paiements aboutissent, et les commandes
        restent eternellement en attente. Le marchand conclut « ça ne marche pas »
        sans qu'aucune erreur ne soit jamais apparue.

        ATTENTION AU REMEDE QU'ON CONSEILLE. `website_payment` SURCHARGE
        `get_base_url()` sur `payment.provider` et donne la priorite a
        `request.httprequest.url_root`, c'est-a-dire a **l'adresse par laquelle on
        navigue**, pour gerer les installations multi-sites. Ni `web.base.url` ni le
        domaine du site web ne changent donc quoi que ce soit tant qu'on accede a Odoo
        par une autre adresse. Conseiller « renseignez l'adresse dans les parametres »
        envoyait le marchand modifier un reglage sans effet : constate le 2026-08-26 en
        preparant les captures, ou trois reglages successifs n'ont rien change.

        On ne bloque pas -- une instance de developpement est un cas legitime -- mais on
        le DIT, au moment ou le marchand verifie sa connexion.

        :return: un message d'avertissement, ou None si tout va bien.
        """
        self.ensure_one()
        base = (self.get_base_url() or "").strip()
        if not base:
            return _("L'adresse publique de votre instance n'est pas renseignée.")
        host = base.split("//")[-1].split("/")[0].split(":")[0].lower()
        if host in ("localhost", "127.0.0.1", "0.0.0.0") or host.endswith(".local"):
            return _(
                "Vous accédez à Odoo par « %s ». Mobupay ne pourra pas y livrer ses "
                "confirmations de paiement, et vos commandes resteraient en attente. "
                "C'est normal sur un poste de développement. Refaites cette "
                "vérification en accédant à Odoo par l'adresse publique de votre "
                "boutique, celle que vos clients utilisent.", base,
            )
        if not base.startswith("https://"):
            return _(
                "Vous accédez à Odoo par « %s », qui n'est pas en HTTPS. Mobupay n'y "
                "livrera pas ses confirmations de paiement. Refaites cette "
                "vérification en accédant à Odoo par son adresse HTTPS.", base,
            )
        return None

    def _mobupay_refresh_secret(self, silent=True):
        """Recupere le secret de signature avec la seule cle API.

        L'appel sert deux choses d'un coup : il pose le secret, et il PROUVE que la
        cle est valide.

        En mode silencieux (enregistrement), un echec ne bloque JAMAIS : les valeurs
        sont deja ecrites, et une API momentanement injoignable ne doit pas empecher
        un marchand de configurer sa boutique. On journalise, et la prochaine
        sauvegarde reessaiera.
        """
        for provider in self:
            if provider.code != "mobupay":
                continue
            key = (provider.mobupay_api_key or "").strip()
            if not key:
                if not silent:
                    raise UserError(_(
                        "Aucune clé API renseignée : la boutique ne pourra pas encaisser."
                    ))
                continue
            try:
                body = mobupay_api.request(
                    provider.mobupay_api_base, key, "GET", "/api/v1/webhooks/signing-secret"
                )
            except mobupay_api.MobupayError as exc:
                _logger.warning("Mobupay : récupération du secret de signature en échec : %s", exc)
                if not silent:
                    raise UserError(_(
                        "Mobupay n'a pas pu être contacté pour vérifier votre clé : %s",
                        exc,
                    ))
                continue

            secret = (body or {}).get("webhookSecret") or ""
            if not secret:
                _logger.warning("Mobupay : aucun secret de signature renvoyé.")
                if not silent:
                    raise UserError(_(
                        "Aucun secret de signature n'a été renvoyé. Les paiements "
                        "fonctionneront, mais les confirmations ne pourront pas être "
                        "vérifiées."
                    ))
                continue

            # `sudo` : le champ est reserve au groupe systeme, et cette ecriture est
            # faite par le module lui-meme, pas par l'utilisateur.
            provider.sudo().mobupay_webhook_secret = secret

    def _mobupay_request(self, method, endpoint, payload=None, idempotency_key=None):
        """Appel API porte par ce fournisseur."""
        self.ensure_one()
        return mobupay_api.request(
            self.mobupay_api_base,
            (self.mobupay_api_key or "").strip(),
            method,
            endpoint,
            payload=payload,
            idempotency_key=idempotency_key,
        )
