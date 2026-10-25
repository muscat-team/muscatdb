"""Cloudflare Access JWT validation behind nginx ``auth_request`` (issue #50).

nginx here has ``auth_request`` but no njs/lua, so the signature check lives in
this endpoint. For every request on the Access listener nginx makes a
subrequest to ``/_auth/cf`` carrying the original ``Cf-Access-Jwt-Assertion``;
this module verifies it and answers with the email in ``X-Auth-Email``, which
nginx copies into ``X-Forwarded-User`` (``auth_request_set``). The app's
existing trust rule then applies unchanged and :mod:`muscat_db.auth` maps the
email to a username.

The endpoint is only for nginx: it requires the loopback peer and the shared
proxy secret, so a local account cannot use it as an oracle. Everything fails
closed: no configuration, no keys, a bad signature, a wrong audience or issuer,
an expired token, a token with no email (an Access service token) -> no email.

Configuration (both required, otherwise every request is refused):

* ``MUSCAT_CF_TEAM_DOMAIN`` -- e.g. ``myteam.cloudflareaccess.com``; the
  issuer is ``https://<domain>`` and the keys are
  ``https://<domain>/cdn-cgi/access/certs``
* ``MUSCAT_CF_AUD`` -- the Access application's audience (AUD) tag
"""

from __future__ import annotations

import hmac
import logging
import os
import re
import threading
from typing import Protocol

import jwt
from fastapi import APIRouter, Request, Response

from muscat_db.auth import (
    PROXY_SECRET_HEADER,
    TRUSTED_PROXY_HOSTS,
    configured_proxy_secret,
)

logger = logging.getLogger(__name__)

JWT_HEADER = "Cf-Access-Jwt-Assertion"
EMAIL_HEADER = "X-Auth-Email"
AUTH_PATH = "/_auth/cf"
_LEEWAY_S = 30
_JWKS_LIFESPAN_S = 3600
_JWKS_TIMEOUT_S = 5

router = APIRouter()

# Kept local so this module does not depend on the email->username mapping
# (#248); both use the same rule: stripped, lower-cased (not case-folded, see
# auth.normalize_email), one @ and a dotted host.
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def normalize_email(value) -> str | None:
    email = value.strip().lower() if isinstance(value, str) else ""
    return email if _EMAIL_RE.match(email) else None


class _KeyClient(Protocol):
    def get_signing_key_from_jwt(self, token: str): ...


def team_domain() -> str:
    raw = os.environ.get("MUSCAT_CF_TEAM_DOMAIN", "").strip().lower()
    for prefix in ("https://", "http://"):
        raw = raw.removeprefix(prefix)
    return raw.rstrip("/")


def audience() -> str:
    return os.environ.get("MUSCAT_CF_AUD", "").strip()


def configured() -> bool:
    return bool(team_domain() and audience())


_client_lock = threading.Lock()
_clients: dict[str, jwt.PyJWKClient] = {}


def _key_client(domain: str) -> _KeyClient:
    """One cached JWKS client per team domain (it caches the keys itself)."""
    with _client_lock:
        if domain not in _clients:
            _clients[domain] = jwt.PyJWKClient(
                f"https://{domain}/cdn-cgi/access/certs",
                cache_keys=True,
                lifespan=_JWKS_LIFESPAN_S,
                timeout=_JWKS_TIMEOUT_S,
            )
        return _clients[domain]


def verified_email(token: str | None, key_client: _KeyClient | None = None) -> str | None:
    """The normalized email of a valid Access JWT, else ``None``.

    Never raises: a refused token and an unreachable key endpoint are both
    "no identity". Only RS256 is accepted, so an HS256 token signed with the
    public key as the secret cannot pass.
    """
    if not token or not configured():
        return None
    domain = team_domain()
    try:
        signing_key = (key_client or _key_client(domain)).get_signing_key_from_jwt(token)
        claims = jwt.decode(
            token,
            signing_key.key,
            algorithms=["RS256"],
            audience=audience(),
            issuer=f"https://{domain}",
            leeway=_LEEWAY_S,
            options={"require": ["exp", "iat", "aud", "iss"]},
        )
    except jwt.PyJWTError as exc:
        logger.warning("refused Access JWT: %s", exc)
        return None
    except Exception:  # JWKS fetch/network trouble must not admit anyone
        logger.exception("Access JWT validation failed unexpectedly")
        return None
    return normalize_email(claims.get("email"))


def _from_nginx(request: Request) -> bool:
    """Loopback peer presenting the shared proxy secret, the same bar as
    ``X-Forwarded-User`` (and, unlike it, required even when auth is off)."""
    host = request.client.host if request.client else None
    expected = configured_proxy_secret()
    if host not in TRUSTED_PROXY_HOSTS or expected is None:
        return False
    return hmac.compare_digest(request.headers.get(PROXY_SECRET_HEADER, ""), expected)


@router.get(AUTH_PATH, include_in_schema=False)
def cf_auth(request: Request) -> Response:
    """``auth_request`` target: 204 + ``X-Auth-Email`` or 401/403/503."""
    if not _from_nginx(request):
        return Response(status_code=403)
    if not configured():
        logger.error("Cloudflare Access is not configured (MUSCAT_CF_TEAM_DOMAIN/MUSCAT_CF_AUD)")
        return Response(status_code=503)
    email = verified_email(request.headers.get(JWT_HEADER))
    if email is None:
        return Response(status_code=401)
    return Response(status_code=204, headers={EMAIL_HEADER: email})
