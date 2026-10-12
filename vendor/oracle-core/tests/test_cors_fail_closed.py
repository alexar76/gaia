"""An explicitly empty CORS allowlist must mean CLOSED, not `*`.

`create_app` used to collapse the empty case onto the wide-open one:

    origins = [o.strip() for o in cors_origins.split(",") if o.strip()] or ["*"]

so an operator who set `X_CORS_ORIGINS=` to lock a service down got the exact
opposite, and so did every oracle whose entry point read the env with `""` as its
default. GAIA reached production that way — any page on the internet could read a
device's readings out of a visitor's browser.

Three states now, and the middle one is the point:

    None (nothing said)  -> "*"      historical default, unchanged
    "a.example,b.example" -> allowlist
    ""   (said "none")   -> closed

Run:  ../.venv/bin/python -m pytest tests/test_cors_fail_closed.py -q   (from oracles/core/)
"""

import pytest
from httpx import ASGITransport, AsyncClient

from oracle_core import Capability, OracleSpec, create_app

ORIGIN = "https://viewer.example"


def _spec(tmp_path):
    return OracleSpec(
        name="CORS Oracle",
        product_id="prod-cors",
        description="cors",
        public_url="http://localhost:9999",
        categories=["test"],
        signing_key_path=str(tmp_path / "key"),
        capabilities=[Capability("cors.echo@v1", "echo", handler=lambda d: d)],
    )


async def _health_headers(app, origin=ORIGIN):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as client:
        resp = await client.get("/api/health", headers={"Origin": origin})
    assert resp.status_code == 200
    return resp.headers


async def test_unset_keeps_the_wide_open_default(tmp_path):
    """Saying nothing about CORS is not the same as saying "none"."""
    headers = await _health_headers(create_app(_spec(tmp_path)))
    assert headers.get("access-control-allow-origin") == "*"


async def test_explicit_empty_string_allows_nobody(tmp_path):
    headers = await _health_headers(create_app(_spec(tmp_path), cors_origins=""))
    assert "access-control-allow-origin" not in headers


async def test_whitespace_only_is_empty_not_wildcard(tmp_path):
    """`X_CORS_ORIGINS=" , "` is a typo, and a typo must not open the service."""
    headers = await _health_headers(create_app(_spec(tmp_path), cors_origins=" , "))
    assert "access-control-allow-origin" not in headers


async def test_allowlist_admits_only_what_it_names(tmp_path):
    app = create_app(_spec(tmp_path), cors_origins=f"{ORIGIN},https://other.example")

    allowed = await _health_headers(app, origin=ORIGIN)
    assert allowed.get("access-control-allow-origin") == ORIGIN
    # A named allowlist is credentialed; "*" cannot be, per the CORS spec.
    assert allowed.get("access-control-allow-credentials") == "true"

    refused = await _health_headers(app, origin="https://stranger.example")
    assert "access-control-allow-origin" not in refused


@pytest.mark.parametrize("origins", ["", " , "])
async def test_closed_allowlist_refuses_the_preflight(tmp_path, origins):
    """The simple request carries no header; the preflight is refused outright."""
    transport = ASGITransport(app=create_app(_spec(tmp_path), cors_origins=origins))
    async with AsyncClient(transport=transport, base_url="http://t") as client:
        resp = await client.options(
            "/ai-market/v2/invoke",
            headers={
                "Origin": ORIGIN,
                "Access-Control-Request-Method": "POST",
            },
        )
    assert resp.status_code == 400
    assert "access-control-allow-origin" not in resp.headers
