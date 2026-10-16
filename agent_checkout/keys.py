"""ES256 (P-256) keys, the algorithm AP2 uses for mandates, checkout JWTs and receipts."""

from __future__ import annotations

import json

from jwcrypto.jwk import JWK


def new_key(kid: str) -> JWK:
    key = JWK.generate(kty="EC", crv="P-256")
    data = json.loads(key.export())
    data["kid"] = kid
    return JWK.from_json(json.dumps(data))


def public(key: JWK) -> JWK:
    return JWK.from_json(key.export_public())


def cnf(key: JWK) -> dict:
    """The ``cnf`` claim that sender-constrains an open mandate to this key (RFC 7800)."""
    return {"jwk": json.loads(key.export_public())}


def thumbprint(key: JWK) -> str:
    return key.thumbprint()
