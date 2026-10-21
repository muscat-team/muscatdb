"""Cloudflare Access JWT validation endpoint (issue #50).

Keys are generated locally; the JWKS client is a stub, so nothing here talks to
Cloudflare. The point is the refusal matrix: a token is accepted only when its
signature, audience, issuer, expiry and email all check out.
"""

from __future__ import annotations

import time
from pathlib import Path
from types import SimpleNamespace

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from muscat_db import cf_access, web

TEAM = "myteam.cloudflareaccess.com"
AUD = "abc123aud"
SECRET = "proxy-secret"
_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_OTHER = rsa.generate_private_key(public_exponent=65537, key_size=2048)


class _Keys:
    """Stands in for PyJWKClient: hands back the public key."""

    def __init__(self, key=_KEY):
        self.key = key.public_key()

    def get_signing_key_from_jwt(self, token):
        return SimpleNamespace(key=self.key)


class _Broken:
    def get_signing_key_from_jwt(self, token):
        raise OSError("jwks unreachable")


def _token(key=_KEY, **over):
    now = int(time.time())
    claims = {
        "iss": f"https://{TEAM}",
        "aud": [AUD],
        "iat": now,
        "exp": now + 600,
        "email": "andrew.example@univ.edu",
    }
    claims.update(over)
    claims = {k: v for k, v in claims.items() if v is not None}
    return jwt.encode(claims, key, algorithm="RS256", headers={"kid": "k1"})


@pytest.fixture(autouse=True)
def cf_env(monkeypatch):
    monkeypatch.setenv("MUSCAT_CF_TEAM_DOMAIN", TEAM)
    monkeypatch.setenv("MUSCAT_CF_AUD", AUD)
    monkeypatch.setenv("MUSCAT_PROXY_SECRET", SECRET)
    monkeypatch.delenv("MUSCAT_REQUIRE_AUTH", raising=False)


def test_valid_token_yields_the_normalized_email():
    assert cf_access.verified_email(_token(email="Andrew.Example@Univ.EDU"), _Keys()) == (
        "andrew.example@univ.edu"
    )


@pytest.mark.parametrize(
    "token",
    [
        pytest.param(_token(key=_OTHER), id="wrong-signing-key"),
        pytest.param(_token(aud=["someone-else"]), id="wrong-audience"),
        pytest.param(_token(iss="https://evil.cloudflareaccess.com"), id="wrong-issuer"),
        pytest.param(_token(exp=int(time.time()) - 3600), id="expired"),
        pytest.param(_token(email=None), id="no-email-service-token"),
        pytest.param(_token(email="not-an-email"), id="malformed-email"),
        pytest.param(_token(exp=None), id="no-expiry"),
        pytest.param("garbage", id="not-a-jwt"),
        pytest.param("", id="empty"),
    ],
)
def test_refused_tokens_yield_no_identity(token):
    assert cf_access.verified_email(token, _Keys()) is None


def test_hs256_signed_with_the_public_key_is_refused():
    """The classic algorithm-confusion forgery."""
    pem = (
        _KEY.public_key()
        .public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        .decode()
    )
    forged = _forge_hs256(pem, int(time.time()))
    assert cf_access.verified_email(forged, _Keys()) is None


def _forge_hs256(pem: str, now: int) -> str:
    """PyJWT refuses to HMAC with a PEM key, so build the token by hand."""
    import base64
    import hashlib
    import hmac
    import json

    def b64(raw: bytes) -> str:
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()

    head = b64(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    body = b64(
        json.dumps(
            {
                "iss": f"https://{TEAM}",
                "aud": [AUD],
                "iat": now,
                "exp": now + 600,
                "email": "admin@univ.edu",
            }
        ).encode()
    )
    sig = hmac.new(pem.encode(), f"{head}.{body}".encode(), hashlib.sha256).digest()
    return f"{head}.{body}.{b64(sig)}"


def test_unreachable_key_endpoint_fails_closed():
    assert cf_access.verified_email(_token(), _Broken()) is None


def test_unconfigured_fails_closed(monkeypatch):
    monkeypatch.delenv("MUSCAT_CF_AUD")
    assert cf_access.verified_email(_token(), _Keys()) is None


# ── the endpoint ────────────────────────────────────────────────────────


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(cf_access, "_key_client", lambda domain: _Keys())
    return TestClient(web.app, client=("127.0.0.1", 12345))


def _h(token=None, secret=SECRET):
    headers = {}
    if token is not None:
        headers[cf_access.JWT_HEADER] = token
    if secret is not None:
        headers["X-MuSCAT-Proxy-Secret"] = secret
    return headers


def test_endpoint_returns_204_and_the_email(client):
    r = client.get(cf_access.AUTH_PATH, headers=_h(_token()))
    assert r.status_code == 204
    assert r.headers["x-auth-email"] == "andrew.example@univ.edu"


def test_endpoint_is_reachable_without_a_user_even_when_auth_is_required(client, monkeypatch):
    monkeypatch.setenv("MUSCAT_REQUIRE_AUTH", "1")
    assert client.get(cf_access.AUTH_PATH, headers=_h(_token())).status_code == 204


def test_endpoint_refuses_without_the_proxy_secret_or_from_a_non_loopback_peer(client, monkeypatch):
    assert client.get(cf_access.AUTH_PATH, headers=_h(_token(), secret=None)).status_code == 403
    assert client.get(cf_access.AUTH_PATH, headers=_h(_token(), secret="wrong")).status_code == 403
    remote = TestClient(web.app, client=("10.0.0.5", 1))
    assert remote.get(cf_access.AUTH_PATH, headers=_h(_token())).status_code == 403


def test_endpoint_401_for_a_bad_or_missing_token(client):
    assert client.get(cf_access.AUTH_PATH, headers=_h()).status_code == 401
    assert client.get(cf_access.AUTH_PATH, headers=_h(_token(key=_OTHER))).status_code == 401


def test_endpoint_503_when_unconfigured(client, monkeypatch):
    monkeypatch.delenv("MUSCAT_CF_TEAM_DOMAIN")
    assert client.get(cf_access.AUTH_PATH, headers=_h(_token())).status_code == 503


def test_endpoint_without_any_proxy_secret_configured_refuses(client, monkeypatch):
    monkeypatch.delenv("MUSCAT_PROXY_SECRET")
    monkeypatch.setenv("MUSCAT_PROXY_SECRET_FILE", "/nonexistent-secret")
    assert client.get(cf_access.AUTH_PATH, headers=_h(_token(), secret=None)).status_code == 403


# ── nginx listener ──────────────────────────────────────────────────────


def test_access_listener_never_forwards_a_client_identity():
    conf = (Path(__file__).resolve().parents[1] / "deploy" / "nginx-access.conf").read_text()
    code = "\n".join(l for l in conf.splitlines() if not l.lstrip().startswith("#"))
    assert "auth_basic" not in code
    # every proxied location authenticates through the subrequest and sets the
    # identity from it, never from $remote_user or a client header
    assert code.count("auth_request /_auth/cf;") == 2
    assert code.count("proxy_set_header X-Forwarded-User $cf_email;") == 2
    assert "$remote_user" not in code and "$http_x_forwarded_user" not in code.lower()
    assert 'proxy_set_header X-Forwarded-User "";' in code  # the subrequest itself


def test_access_listener_logs_the_validated_email():
    """Every request reaches this listener from cloudflared on loopback, so
    only the validated email and Cloudflare's client address attribute it."""
    conf = (Path(__file__).resolve().parents[1] / "deploy" / "nginx-access.conf").read_text()
    code = "\n".join(l for l in conf.splitlines() if not l.lstrip().startswith("#"))
    fmt = code[code.index("log_format muscat_access"):code.index("server {")]
    assert '"$cf_email"' in fmt and "$http_cf_connecting_ip" in fmt
    server = code[code.index("server {"):]
    assert "access_log /var/log/nginx/muscat-db-access.log muscat_access;" in server



@pytest.mark.parametrize("name", ["nginx.conf", "nginx-staging.conf"])
def test_htpasswd_listeners_do_not_expose_the_jwt_check(name):
    """These listeners proxy every path with the proxy secret, so without an
    exact-match 404 any htpasswd user could call the endpoint meant for the
    Access listener's auth_request subrequest."""
    conf = (Path(__file__).resolve().parents[1] / "deploy" / name).read_text()
    code = "\n".join(l for l in conf.splitlines() if not l.lstrip().startswith("#"))
    assert "auth_basic " in code  # still the htpasswd listener
    assert code.count(f"location = {cf_access.AUTH_PATH}") == 1
    block = code[code.index(f"location = {cf_access.AUTH_PATH}"):]
    block = block[:block.index("}") + 1]
    assert "return 404;" in block and "proxy_pass" not in block


def test_only_the_access_listener_proxies_the_jwt_check():
    deploy = Path(__file__).resolve().parents[1] / "deploy"
    proxying = []
    for conf in sorted(deploy.glob("nginx*.conf")):
        code = "\n".join(l for l in conf.read_text().splitlines() if not l.lstrip().startswith("#"))
        if f"proxy_pass http://127.0.0.1:8001{cf_access.AUTH_PATH}" in code:
            proxying.append(conf.name)
    assert proxying == ["nginx-access.conf"]
