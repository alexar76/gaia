"""SSRF allowlist and Open-Meteo origin policy for GAIA live relays.

Live devices never fetch a buyer-supplied URL. Operator env may pick a resource
*on* an allowlisted host (or an operator-run Open-Meteo origin). This module is
the single place that encodes that rule so ``live.py`` can stay a catalog of
relays rather than a second copy of the fetch policy.
"""

from __future__ import annotations

import os
from urllib.parse import quote, urlparse

from gaia.devices.base import DeviceOffline

# Exact hostnames live devices may call. Operator env may pick resources *on*
# these hosts, never a free-form URL (SSRF).
ALLOWED_HOSTS = frozenset({
    "api.weather.gov",
    "api.opensensemap.org",
    "api.open-meteo.com",
    "air-quality-api.open-meteo.com",
    "marine-api.open-meteo.com",
    "api.carbonintensity.org.uk",
    "earthquake.usgs.gov",
    "waterservices.usgs.gov",
    "api.tidesandcurrents.noaa.gov",
    "www.ndbc.noaa.gov",
    "api.openaq.org",
    "airquality-frost.k8s.ilt-dmz.iosb.fraunhofer.de",
    # Free-to-commercialize open relays (no NC / BY-NC sources)
    "firms.modaps.eosdis.nasa.gov",
    "api.safecast.org",
    "www.cybernews.space",
    "www.epncb.oma.be",
    "gnss.be",
    "gws.geodesy.ga.gov.au",
    "data.gnss.ga.gov.au",
    # P0 commercially-clear relays (NASA / NOAA PD / ODbL / CC BY / Argo)
    "eonet.gsfc.nasa.gov",
    "services.swpc.noaa.gov",
    "noaa-goes16.s3.amazonaws.com",
    "noaa-goes18.s3.amazonaws.com",
    "noaa-goes19.s3.amazonaws.com",
    "data.sensor.community",
    "mesonet.agron.iastate.edu",
    "argovis-api.colorado.edu",
    "erddap.ifremer.fr",
    "data-argo.ifremer.fr",
    "api.met.no",
    "geomag.usgs.gov",
    # P1 (verified licences — see live_p1.py)
    "maps.effis.emergency.copernicus.eu",
    "maps.effis.jrc.ec.europa.eu",
    "volcanoes.usgs.gov",
    "api.brightsky.dev",
    "api.weather.gc.ca",
    "uk-air.defra.gov.uk",
    "api.geonet.org.nz",
    "api.eia.gov",
    "uhslc.soest.hawaii.edu",
    "api.dataplatform.knmi.nl",
    "api.erg.ic.ac.uk",
    # P2 (licence-pinned — see live_p2.py)
    "meri.digitraffic.fi",
    "www.digitraffic.fi",
    "opendata.fmi.fi",
    "opendata-download-hydroobs.smhi.se",
    "opendata.smhi.se",
    # P3 (licence-pinned — see live_p3.py)
    "www.nhc.noaa.gov",
    "www.seismicportal.eu",
    "environment.data.gov.uk",
    "www.tsunami.gov",
    "www.barentswatch.no",
    "live.ais.barentswatch.no",
    "id.barentswatch.no",
    "api.adsb.lol",
})

# Backward-compatible private alias used throughout live.py / tests.
_ALLOWED_HOSTS = ALLOWED_HOSTS

# ── Open-Meteo origin: hosted free API vs operator self-host ──────────────────
#
# The DATA is CC BY 4.0 and safe to resell with attribution. The HOSTED FREE
# endpoint is not: Open-Meteo's terms say "You may only use the free API services
# for non-commercial purposes", and name "integration into commercial products" as
# commercial use. Our own gaia/docs/LIVE-RELAYS.md records that decision.
#
# The server is AGPLv3 and self-hostable, so an operator who sells om-* readings
# points these at their own instance (or at the dedicated customer endpoint that
# comes with a paid plan) instead of billing calls against the free tier.
_OM_HOSTED_ORIGINS = {
    "weather": "https://api.open-meteo.com",
    "air_quality": "https://air-quality-api.open-meteo.com",
    "marine": "https://marine-api.open-meteo.com",
}
_OM_ORIGIN_ENV = {
    "weather": "GAIA_OM_BASE_URL",
    "air_quality": "GAIA_OM_AQ_BASE_URL",
    "marine": "GAIA_OM_MARINE_BASE_URL",
}


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, "").strip() or default


def _om_self_host_origins() -> set[str]:
    """Operator-configured Open-Meteo origins that are NOT the hosted free API."""
    out: set[str] = set()
    for kind, var in _OM_ORIGIN_ENV.items():
        configured = _env(var).rstrip("/")
        if configured and configured != _OM_HOSTED_ORIGINS[kind]:
            out.add(configured.lower())
    return out


def _is_om_self_host(url: str) -> bool:
    parsed = urlparse(url)
    origin = f"{parsed.scheme}://{parsed.netloc}".lower()
    return bool(origin) and origin in _om_self_host_origins()


def _om_selling_without_licence(origin: str) -> bool:
    """True when we would bill a call against Open-Meteo's non-commercial free tier.

    ``AIFACTORY_CRYPTO_ENABLED`` is the ecosystem's "payments are on" switch — with
    it off every invoke is served free, which is inside the free tier's terms.
    """
    if not origin.rstrip("/").lower().endswith("open-meteo.com"):
        return False
    if _env("GAIA_OM_ALLOW_HOSTED_COMMERCIAL", "0") in ("1", "true", "TRUE"):
        return False  # operator asserts they hold a commercial plan for this origin
    return _env("AIFACTORY_CRYPTO_ENABLED", "0") in ("1", "true", "TRUE")


def _om_origin(kind: str) -> str:
    """Resolve the Open-Meteo origin for one relay kind, failing closed on licence.

    Default is the hosted free endpoint, which keeps every non-commercial and demo
    deployment working exactly as before. Refusing at construction time means a
    commercial deployment cannot quietly bill readings fetched under a
    non-commercial ToS — it stops at boot with the three ways out.
    """
    origin = (_env(_OM_ORIGIN_ENV[kind]) or _OM_HOSTED_ORIGINS[kind]).rstrip("/")
    if _om_selling_without_licence(origin):
        raise ValueError(
            f"refusing to sell Open-Meteo readings from the hosted free API "
            f"({origin}): its ToS is non-commercial while payments are enabled "
            f"(AIFACTORY_CRYPTO_ENABLED=1). Either point {_OM_ORIGIN_ENV[kind]} at a "
            f"self-hosted instance (see gaia/docker-compose.om-selfhost.yml), or at "
            f"the customer endpoint of a paid plan, or set "
            f"GAIA_OM_ALLOW_HOSTED_COMMERCIAL=1 to assert you hold one."
        )
    return origin


def _om_apikey_suffix() -> str:
    """`&apikey=…` for Open-Meteo's paid customer endpoint; empty when unset."""
    key = _env("GAIA_OM_API_KEY")
    return f"&apikey={quote(key, safe='')}" if key else ""


def _om_auth_headers(origin: str) -> dict[str, str]:
    """Bearer for an operator-run instance reached ACROSS hosts.

    A self-hosted Open-Meteo has no authentication of its own, so when it lives on a
    different machine than GAIA the edge in front of it has to gate access — else we
    publish a free unauthenticated weather mirror at our own bandwidth, and turn
    third parties into remote users of an AGPL program.

    The token is only ever sent to an origin the operator configured. Never to
    open-meteo.com: a secret must not leak to a third party because someone left
    GAIA_OM_BASE_URL unset.
    """
    token = _env("GAIA_OM_AUTH_TOKEN")
    if not token or origin in _OM_HOSTED_ORIGINS.values():
        return {}
    return {"Authorization": f"Bearer {token}"}


def _om_source(hosted_source: str, origin: str) -> str:
    """Attribution for a reading, naming the relay we actually fetched from.

    CC BY 4.0 attribution to Open-Meteo and the national weather services behind it
    is required either way. But which host served the bytes is provenance, and this
    fleet's whole LIVE/SIM contract is that provenance is never implied — so a
    self-hosted relay says so instead of quietly reading like the public API.
    """
    if origin in _OM_HOSTED_ORIGINS.values():
        return hosted_source
    return f"{hosted_source[:-1]}; relayed via operator-run Open-Meteo instance {origin})"


def assert_url_allowed(url: str) -> str:
    """Reject anything that is not https://<allowlisted-host>/… .

    One exception: an operator-configured Open-Meteo self-host origin (see
    ``_om_origin``). That origin comes from the operator's own env, never from a
    buyer — the invariant this guard exists to protect is "operator env may pick
    resources ON an allowlisted host, never a free-form URL", and an env-supplied
    origin does not violate it. Internal services usually terminate no TLS, so
    that origin alone may be http.
    """
    parsed = urlparse(url)
    if parsed.username or parsed.password:
        raise DeviceOffline("upstream URL must not carry credentials")
    if _is_om_self_host(url):
        return url
    if parsed.scheme != "https":
        raise DeviceOffline("upstream URL must be https")
    host = (parsed.hostname or "").lower().rstrip(".")
    if host not in ALLOWED_HOSTS:
        raise DeviceOffline(f"upstream host not allowlisted: {host or '?'}")
    return url


# Private alias kept so live.py / live_open.py / tests keep their current names.
_assert_url_allowed = assert_url_allowed


__all__ = [
    "ALLOWED_HOSTS",
    "assert_url_allowed",
    "_ALLOWED_HOSTS",
    "_OM_HOSTED_ORIGINS",
    "_OM_ORIGIN_ENV",
    "_assert_url_allowed",
    "_env",
    "_is_om_self_host",
    "_om_apikey_suffix",
    "_om_auth_headers",
    "_om_origin",
    "_om_self_host_origins",
    "_om_selling_without_licence",
    "_om_source",
]
