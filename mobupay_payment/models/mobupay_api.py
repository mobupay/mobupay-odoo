# -*- coding: utf-8 -*-
"""Client HTTP minimal de l'API Mobupay, et verification de signature.

PLAN-599. Odoo embarque `requests` : inutile d'ajouter une dependance, et un module
publie sur Odoo Apps qui en exigerait une serait refuse a la revue.

La verification de signature reproduit exactement le schema du SDK PHP `Webhook` :

  - V2 (recommande, ANTI-REJEU) : `X-Mobupay-Signature-V2` =
    HMAC-SHA256("{X-Mobupay-Timestamp}.{corps_brut}", secret). Le timestamp etant
    signe, une livraison interceptee ne peut pas etre rejouee plus tard.
  - V1 (historique) : `X-Mobupay-Signature` = HMAC-SHA256(corps_brut, secret).

Deux points sur lesquels il ne faut RIEN improviser :

  1. Le corps doit etre le corps BRUT, jamais re-serialise. Un `json.dumps()` du
     dictionnaire decode change les espaces et l'ordre des cles, donc l'empreinte, et
     tous les webhooks seraient rejetes.
  2. La comparaison passe par `hmac.compare_digest`, jamais par `==`. Une comparaison
     naive fuit la signature attendue par son temps d'execution.
"""

import hashlib
import hmac
import json
import time

import requests

DEFAULT_TIMEOUT = 30
#: Fenetre de fraicheur du timestamp signe, en secondes.
SIGNATURE_TOLERANCE = 300


class MobupayError(Exception):
    """Erreur d'appel a l'API Mobupay.

    `code` porte le code d'erreur Mobupay quand la reponse en contient un : c'est lui
    qu'on teste pour distinguer un refus de FACTURATION d'un refus de paiement, jamais
    le message, qui est traduit et reformule.
    """

    def __init__(self, message, code="", status=0):
        super(MobupayError, self).__init__(message)
        self.code = code or ""
        self.status = status

    @property
    def is_invoicing_error(self):
        return bool(self.code) and (
            self.code.startswith("INVOICING") or self.code.startswith("BILLING")
        )


def request(api_base, api_key, method, endpoint, payload=None, idempotency_key=None,
            timeout=DEFAULT_TIMEOUT):
    """Appelle l'API Mobupay et rend le corps decode."""
    url = "%s%s" % ((api_base or "").rstrip("/"), endpoint)
    headers = {
        "Authorization": "Bearer %s" % (api_key or ""),
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": "Mobupay-Odoo/1.0",
    }
    if idempotency_key:
        headers["Idempotency-Key"] = idempotency_key

    try:
        response = requests.request(
            method, url, json=payload, headers=headers, timeout=timeout
        )
    except requests.exceptions.RequestException as exc:
        raise MobupayError("Mobupay injoignable : %s" % exc)

    try:
        body = response.json() if response.content else {}
    except ValueError:
        body = {}

    if response.status_code >= 400:
        raise MobupayError(_error_message(body, response.status_code),
                           code=body.get("code", ""),
                           status=response.status_code)
    return body


def _error_message(body, status):
    """Message d'erreur qui NOMME le champ fautif.

    L'API repond « La requête est invalide. » avec, a cote, un tableau `fields` qui dit
    precisement quel champ pose probleme et pourquoi. Ne remonter que le message
    general laisse le marchand devant un mur : constate en recette le 2026-08-26, ou
    une commande en USD -- la devise par defaut d'une base Odoo neuve -- echouait sans
    qu'on puisse deviner que Mobupay n'accepte que l'EUR et le XPF.
    """
    message = body.get("message") or "Appel Mobupay en échec (HTTP %s)" % status
    details = []
    for item in (body.get("fields") or []):
        if not isinstance(item, dict):
            continue
        field = item.get("field") or ""
        detail = item.get("message") or ""
        details.append("%s : %s" % (field, detail) if field else detail)
    if details:
        return "%s (%s)" % (message, " ; ".join(details[:5]))
    return message


def verify_signature(raw_body, headers, secret, tolerance=SIGNATURE_TOLERANCE):
    """Verifie la signature d'un webhook et rend l'evenement decode.

    :param raw_body: corps BRUT de la requete, en octets ou en texte. Jamais un
        dictionnaire re-serialise.
    :raises MobupayError: signature absente, invalide, ou horodatage hors fenetre.
    """
    if not secret:
        raise MobupayError("Secret de signature Mobupay manquant.")

    if isinstance(raw_body, bytes):
        body_bytes = raw_body
    else:
        body_bytes = (raw_body or "").encode("utf-8")

    lowered = {}
    for key, value in (headers or {}).items():
        lowered[str(key).lower()] = value

    signature_v2 = lowered.get("x-mobupay-signature-v2") or ""
    timestamp = lowered.get("x-mobupay-timestamp") or ""

    if signature_v2 and timestamp:
        try:
            age = abs(int(time.time()) - int(timestamp))
        except (TypeError, ValueError):
            raise MobupayError("Webhook Mobupay rejeté : horodatage illisible.")
        if age > tolerance:
            raise MobupayError(
                "Webhook Mobupay rejeté : horodatage hors fenêtre de tolérance (%ss)." % age
            )
        signed = str(timestamp).encode("utf-8") + b"." + body_bytes
        expected = hmac.new(secret.encode("utf-8"), signed, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, signature_v2):
            raise MobupayError("Signature Mobupay (V2) invalide.")
        return _decode(body_bytes)

    signature_v1 = lowered.get("x-mobupay-signature") or ""
    if not signature_v1:
        raise MobupayError("Signature Mobupay absente (ni V2 ni V1).")
    expected = hmac.new(secret.encode("utf-8"), body_bytes, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, signature_v1):
        raise MobupayError("Signature Mobupay (V1) invalide.")
    return _decode(body_bytes)


def _decode(body_bytes):
    try:
        event = json.loads(body_bytes.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise MobupayError("Corps de webhook Mobupay illisible (JSON invalide).")
    if not isinstance(event, dict):
        raise MobupayError("Corps de webhook Mobupay illisible (objet attendu).")
    return event
