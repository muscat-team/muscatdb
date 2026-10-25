"""Shared reverse-proxy authentication trust rule.

nginx performs HTTP Basic Auth and forwards the authenticated username in an
``X-Forwarded-User`` header. Trusting that header is ONLY safe for connections
that actually arrived from nginx's own loopback socket. We verify the immediate
TCP peer and the proxy's private shared-secret header before honoring it.

Production nginx mode also requires a shared secret header.  This prevents
another account on the same host from connecting straight to uvicorn's loopback
port and impersonating nginx.

The rule lives here (not inline in the HTTP middleware) because both the
middleware and the companion-app gateway (:mod:`muscat_db.proxy`) must apply it
identically -- and the gateway's WebSocket route bypasses HTTP middleware
entirely, so it has to recompute the trusted user from the handshake itself.
"""

from __future__ import annotations

import hmac
import os
import re
import sqlite3
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import Request
from fastapi.responses import JSONResponse

# Hosts whose X-Forwarded-User we trust: nginx's own loopback socket.
TRUSTED_PROXY_HOSTS = frozenset({"127.0.0.1", "::1"})
PROXY_SECRET_HEADER = "X-MuSCAT-Proxy-Secret"
DEFAULT_PROXY_SECRET_FILE = Path("/etc/muscat-db/proxy-secret")


def authentication_required() -> bool:
    """Whether the application must reject requests that bypass nginx auth."""
    return os.environ.get("MUSCAT_REQUIRE_AUTH", "0") == "1"


def configured_proxy_secret() -> str | None:
    """Read the nginx-to-application secret without caching secret material."""
    value = os.environ.get("MUSCAT_PROXY_SECRET", "").strip()
    if value:
        return value
    path = Path(os.environ.get("MUSCAT_PROXY_SECRET_FILE", DEFAULT_PROXY_SECRET_FILE))
    try:
        return path.read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


# Outcomes of :func:`resolve_forwarded_identity` other than "ok".
ANONYMOUS = "anonymous"  # no X-Forwarded-User header
UNTRUSTED = "untrusted"  # header present but peer/secret did not validate
UNKNOWN_EMAIL = "unknown_email"  # a valid email with no users row
ADMIN_EMAIL = "admin_email"  # a valid email whose users row is an admin
OK = "ok"

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def normalize_email(value: str | None) -> str | None:
    """Lower-cased, stripped email, or ``None`` when *value* is not one.

    ``lower()``, not ``casefold()``: casefold maps "ß" to "ss", which would put
    two different mailboxes (straße@, strasse@) on one account.
    """
    email = (value or "").strip().lower()
    return email if _EMAIL_RE.match(email) else None


def is_email_identity(value: str | None) -> bool:
    """Whether a forwarded identity is an email (Cloudflare Access, #50) rather
    than an htpasswd username. ``htpasswd add`` refuses usernames with ``@``."""
    return bool(value) and "@" in value


def account_for_email(email: str) -> tuple[str, bool] | None:
    """``(username, is_admin)`` of the row whose ``users.email`` is *email*
    (case-insensitive).

    ``None`` for an unknown or malformed email, and when the lookup cannot be
    made at all: an identity that cannot be mapped must never be admitted.
    """
    normalized = normalize_email(email)
    if normalized is None:
        return None
    from muscat_db.database import db_path, get_conn

    try:
        with get_conn(db_path()) as conn:
            row = conn.execute(
                "SELECT username, is_admin FROM users WHERE lower(email) = ?", (normalized,)
            ).fetchone()
    except sqlite3.Error:
        return None
    return (row[0], bool(row[1])) if row else None


def username_for_email(email: str) -> str | None:
    """The stable username for *email*, admin or not (see :func:`account_for_email`)."""
    account = account_for_email(email)
    return account[0] if account else None


def resolve_forwarded_identity(
    forwarded_user: str | None,
    client_host: str | None,
    presented_proxy_secret: str | None = None,
) -> tuple[str | None, str]:
    """``(username, outcome)`` for a request's ``X-Forwarded-User``.

    The trust rule is unchanged: the header is honored only from a loopback
    proxy peer presenting the shared secret. A trusted value that is an email
    (nginx copies it from a validated ``Cf-Access-Jwt-Assertion``) is then
    mapped to the stable username on ``users.email``; an email with no row is
    :data:`UNKNOWN_EMAIL` and is never auto-provisioned or treated as anonymous.
    An email that maps to an admin is :data:`ADMIN_EMAIL`: admins sign in over
    SSH + htpasswd only, so a one-time PIN to their mailbox is not admin rights.
    """
    user = forwarded_user or None
    if user is not None and client_host not in TRUSTED_PROXY_HOSTS:
        return None, UNTRUSTED
    expected = configured_proxy_secret()
    if user is not None and authentication_required() and expected is None:
        return None, UNTRUSTED
    if expected is not None and not hmac.compare_digest(
        presented_proxy_secret or "", expected
    ):
        return None, UNTRUSTED
    if user is None:
        return None, ANONYMOUS
    if is_email_identity(user):
        account = account_for_email(user)
        if account is None:
            return None, UNKNOWN_EMAIL
        username, is_admin = account
        if is_admin:
            return None, ADMIN_EMAIL
        return username, OK
    return user, OK


def trusted_forwarded_user(
    forwarded_user: str | None,
    client_host: str | None,
    presented_proxy_secret: str | None = None,
) -> str | None:
    """Return the authenticated username, or ``None`` if it cannot be trusted.

    ``forwarded_user`` is the raw ``X-Forwarded-User`` header value (may be
    ``None`` or empty). It is honored only when ``client_host`` is a loopback
    proxy address; a non-loopback peer that sets the header itself is ignored.
    An email is mapped to its username (see :func:`resolve_forwarded_identity`).
    """
    return resolve_forwarded_identity(forwarded_user, client_host, presented_proxy_secret)[0]


def request_user(request: Request) -> str | None:
    """Return the authenticated username the proxy middleware attached to
    ``request.state.user``, or ``None`` when the request is unauthenticated."""
    return getattr(request.state, "user", None) or None


def settings_auth_error() -> JSONResponse:
    """401 response for per-user settings endpoints when nginx auth is absent."""
    return JSONResponse(
        {
            "ok": False,
            "error": "login required",
            "detail": "Per-user settings require nginx authentication.",
        },
        status_code=401,
    )


def is_same_origin(request: Request) -> bool:
    """True if the request's Origin (or Referer) header matches this host.

    HTTP Basic Auth credentials are resent by the browser automatically on
    every request to the realm, so state-changing endpoints need their own
    CSRF defense. A CORS preflight is not sufficient here: FastAPI's
    ``Body(...)`` parses the request body as JSON regardless of the
    Content-Type the client declared, so a cross-origin "simple request"
    (e.g. Content-Type: text/plain, which browsers don't preflight) would
    still reach the handler with an attacker-controlled body.
    """
    origin = request.headers.get("origin") or request.headers.get("referer")
    if not origin:
        return False
    return urlsplit(origin).netloc == request.headers.get("host", "")


def csrf_error() -> JSONResponse:
    """403 response for state-changing endpoints that fail the same-origin check."""
    return JSONResponse({"ok": False, "error": (
        "The request was refused because it did not come from a MuSCAT-DB page "
        "(cross-origin request). Reload the page and try again."
    )}, status_code=403)


def unauthenticated_message(forwarded_user: str | None) -> str:
    """Why a request with no usable user was refused (401). No
    ``X-Forwarded-User`` at all reads as not logged in; one that failed the
    peer/secret check means a login did happen but reached the app some other
    way than through the proxy. The identity outcome cannot tell these apart:
    with a secret configured, a request with no header is UNTRUSTED as well."""
    if forwarded_user:
        return (
            "Your login could not be verified: the request did not come through the "
            "site's login proxy. Open MuSCAT-DB through its usual address and try again; "
            "please contact an admin if this keeps happening."
        )
    return (
        "You are not logged in. Open MuSCAT-DB through the address that asks you to "
        "log in, not the server's port directly, and try again."
    )


def admin_email_message(identity_email: str) -> str:
    """Why a validated identity that maps to an admin account was refused (403)."""
    return (
        f"You signed in as {identity_email}, which belongs to an admin account. Admin "
        "accounts cannot sign in this way; use the SSH tunnel and your MuSCAT-DB password."
    )


def unprovisioned_message(identity_email: str) -> str:
    """Why a validated identity with no ``users`` row was refused (403, #50)."""
    return (
        f"You signed in as {identity_email}, but no MuSCAT-DB account is linked to that "
        "address. Please contact an admin to create one."
    )
