# -*- coding: utf-8 -*-
from . import models
from . import controllers

# VERIFIE contre la source des deux series (2026-08-25). Odoo 18 importe le module
# `payment` ENTIER et non ses fonctions, avec ce commentaire dans son propre code :
# « prevent circular import error with payment ». Le `from odoo.addons.payment import
# setup_provider` qui suffisait en 17 expose donc a un import circulaire en 18. On
# emploie le motif de la 18, qui fonctionne sur les deux.
import odoo.addons.payment as payment


def post_init_hook(env):
    payment.setup_provider(env, "mobupay")


def uninstall_hook(env):
    payment.reset_payment_provider(env, "mobupay")
