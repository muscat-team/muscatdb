"""Security response headers on every nginx listener (#273 item 1).

The Access listener (#50) serves the app on a public hostname, so without
these any site could frame it (clickjacking). The htpasswd listeners get the
same set: the browser resends Basic Auth credentials on every request, which
makes a framed page just as dangerous there.
"""

from __future__ import annotations

from pathlib import Path

import pytest

DEPLOY = Path(__file__).resolve().parents[1] / "deploy"
LISTENERS = ["nginx.conf", "nginx-staging.conf", "nginx-access.conf"]

REQUIRED = {
    "X-Frame-Options": '"DENY"',
    "Content-Security-Policy": "\"frame-ancestors 'none'\"",
    "X-Content-Type-Options": '"nosniff"',
    "Referrer-Policy": '"strict-origin-when-cross-origin"',
}


def _code(name: str) -> str:
    text = (DEPLOY / name).read_text()
    return "\n".join(l for l in text.splitlines() if not l.lstrip().startswith("#"))


def _server_level(code: str) -> str:
    """The server block with every location block cut out."""
    server = code[code.index("server {"):]
    out, depth = [], 0
    for line in server.splitlines():
        stripped = line.strip()
        if stripped.startswith("location ") and stripped.endswith("{"):
            depth += 1
            continue
        if depth:
            depth += stripped.count("{") - stripped.count("}")
            continue
        out.append(line)
    return "\n".join(out)


@pytest.mark.parametrize("name", LISTENERS)
def test_listener_sets_security_headers_on_every_response(name):
    server = _server_level(_code(name))
    for header, value in REQUIRED.items():
        # `always` so 401/403/5xx pages (e.g. a failed auth_request) carry them too
        assert f"add_header {header} {value} always;" in server, header


@pytest.mark.parametrize("name", LISTENERS)
def test_no_location_drops_the_server_headers(name):
    """nginx inherits server-level add_header only into locations that have no
    add_header of their own, so one in a location would silently drop the set."""
    code = _code(name)
    assert code.count("add_header ") == _server_level(code).count("add_header ")


def test_only_the_public_listener_sends_hsts():
    """Cloudflare always serves the Access hostname over HTTPS; the htpasswd
    listeners are reached as plain http://localhost through `ssh -L`."""
    sending = [n for n in LISTENERS if "Strict-Transport-Security" in _code(n)]
    assert sending == ["nginx-access.conf"]
    assert 'add_header Strict-Transport-Security "max-age=31536000" always;' in _server_level(
        _code("nginx-access.conf")
    )
