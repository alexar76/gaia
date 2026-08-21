"""Commercial-use source policy for GAIA/ATLAS public products.

This is deliberately a small *positive* registry.  A source absent from this
file is not silently treated as usable: an adapter must either be quarantined
or receive a reviewed entry before its observations can enter a billable rail.

The registry records the licence/terms boundary; it does not replace the
attribution included in each reading and receipt.
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlsplit


@dataclass(frozen=True, slots=True)
class SourcePolicy:
    source_id: str
    name: str
    licence: str
    commercial_basis: str
    attribution: str
    hosts: tuple[str, ...]
    redistribution: str = "derived_and_attributed"
    requires_operator_account: bool = False
    licence_url: str = ""

    def require_endpoint(self, url: str) -> None:
        """Fail closed when an adapter drifts away from its reviewed endpoint."""
        parsed = urlsplit(url)
        host = (parsed.hostname or "").lower().rstrip(".")
        if parsed.scheme != "https" or host not in self.hosts:
            raise ValueError(f"endpoint is not approved for source {self.source_id}: {url}")


# Only sources approved in docs/specs/05-atlas-gaia-gnss-pnt-integrity.md.
APPROVED_SOURCES: dict[str, SourcePolicy] = {
    "cybernews_gnss": SourcePolicy(
        "cybernews_gnss", "CyberNews GNSS interference registry", "CC BY 4.0",
        "Commercial reuse is permitted with attribution.",
        "CyberNews GNSS interference registry · cybernews.space · CC BY 4.0",
        ("www.cybernews.space",),
    ),
    "mirai_gnss": SourcePolicy(
        "mirai_gnss", "MIRAI GNSS observatory", "Provider terms: free commercial use with attribution",
        "Commercial use is explicitly permitted by the published provider terms.",
        "MIRAI GNSS observatory (attribution required)",
        ("mirai-gps.org", "api.mirai-gps.org"),
        requires_operator_account=True,
    ),
    "euref_epn": SourcePolicy(
        "euref_epn", "EUREF Permanent GNSS Network", "CC BY 4.0",
        "EPN Central Bureau material is reusable commercially under CC BY 4.0.",
        "EUREF Permanent GNSS Network / EPN Central Bureau · CC BY 4.0",
        ("www.epncb.oma.be", "gnss.be"),
        licence_url="https://creativecommons.org/licenses/by/4.0/",
    ),
    "ga_gnss": SourcePolicy(
        "ga_gnss", "Geoscience Australia GNSS data", "CC BY 3.0 Australia",
        "Public archive data and metadata are reusable commercially with attribution; "
        "subscription/case-by-case real-time streams are excluded unless separately authorised.",
        "Geoscience Australia GNSS data · CC BY 3.0 Australia",
        ("gws.geodesy.ga.gov.au", "data.gnss.ga.gov.au"),
        licence_url="https://creativecommons.org/licenses/by/3.0/au/",
    ),
    "earthscope_unlimited": SourcePolicy(
        "earthscope_unlimited", "EarthScope UNLIMITED GNSS mountpoints", "EarthScope commercial licence",
        "Only zero-seat UNLIMITED mountpoints under a current operator licence are eligible.",
        "EarthScope Consortium GNSS data · licensed UNLIMITED mountpoints",
        ("data.earthscope.org", "www.earthscope.org"),
        requires_operator_account=True,
    ),
    "southpan": SourcePolicy(
        "southpan", "SouthPAN", "Australian Government open data terms",
        "Publicly released SouthPAN status products may be reused with source attribution.",
        "SouthPAN · Australian and New Zealand Governments",
        ("southpan.com.au",),
    ),
    "nasa_cygnss": SourcePolicy(
        "nasa_cygnss", "NASA CYGNSS", "U.S. Government work / NASA open data",
        "Public NASA science products are commercially reusable; cite the mission and product.",
        "NASA CYGNSS mission data",
        ("podaac.jpl.nasa.gov", "cmr.earthdata.nasa.gov"),
    ),
    "adsb_lol": SourcePolicy(
        "adsb_lol", "adsb.lol", "ODbL 1.0",
        "Commercial reading is permitted; a public derived database is ODbL share-alike. "
        "Pin only api.adsb.lol — no OpenSky / ADSBx fallback.",
        "adsb.lol open API · ODbL 1.0 — cite ADSB.lol; isolate any derived ADS-B database",
        ("api.adsb.lol",),
        licence_url="https://opendatacommons.org/licenses/odbl/1-0/",
    ),
    "fintraffic_ais": SourcePolicy(
        "fintraffic_ais", "Fintraffic AIS", "CC BY 4.0",
        "Commercial reuse is permitted with attribution.",
        "Fintraffic maritime traffic data · CC BY 4.0",
        ("meri.digitraffic.fi", "www.digitraffic.fi"),
        licence_url="https://www.digitraffic.fi/en/terms-of-service/",
    ),
    "eccc_hydrometric": SourcePolicy(
        "eccc_hydrometric", "ECCC MSC GeoMet hydrometric",
        "MSC End-use Licence / Open Government Licence – Canada",
        "Commercial reuse is permitted with attribution to Environment and Climate Change Canada.",
        "Environment and Climate Change Canada hydrometric realtime",
        ("api.weather.gc.ca",),
        licence_url="https://eccc-msc.github.io/open-data/licence/readme_en/",
    ),
    "fmi_opendata": SourcePolicy(
        "fmi_opendata", "Finnish Meteorological Institute open data", "CC BY 4.0",
        "Commercial reuse is permitted with attribution.",
        "Finnish Meteorological Institute open observations · CC BY 4.0",
        ("opendata.fmi.fi",),
        licence_url="https://en.ilmatieteenlaitos.fi/open-data-licence",
    ),
    "smhi_hydro": SourcePolicy(
        "smhi_hydro", "SMHI open hydrology", "CC BY 4.0",
        "Commercial reuse is permitted with attribution.",
        "SMHI hydrology observations · CC BY 4.0",
        ("opendata-download-hydroobs.smhi.se", "opendata.smhi.se"),
        licence_url="https://creativecommons.org/licenses/by/4.0/",
    ),
    "kystverket_ais": SourcePolicy(
        "kystverket_ais", "Norwegian Coastal Administration AIS", "NLOD 2.0",
        "Commercial reuse is permitted under NLOD 2.0 with attribution. "
        "Free BarentsWatch AIS-client registration is required.",
        "Norwegian Coastal Administration AIS via BarentsWatch · NLOD 2.0",
        ("www.barentswatch.no", "id.barentswatch.no", "live.ais.barentswatch.no"),
        requires_operator_account=True,
        licence_url="https://data.norge.no/nlod/en/2.0",
    ),
    "nhc_cyclone": SourcePolicy(
        "nhc_cyclone", "NOAA National Hurricane Center", "U.S. Government public domain",
        "Public-domain NHC/CPHC storm products may be used commercially; provenance is retained.",
        "NOAA National Hurricane Center / CPHC CurrentStorms.json",
        ("www.nhc.noaa.gov",),
        licence_url="https://www.nhc.noaa.gov/",
    ),
    "emsc_fdsn": SourcePolicy(
        "emsc_fdsn", "EMSC-CSEM FDSN event service", "CC BY 4.0",
        "Commercial reuse is permitted with attribution to EMSC. Parameters are preliminary.",
        "EMSC-CSEM FDSN event service · CC BY 4.0 — cite EMSC",
        ("www.seismicportal.eu",),
        licence_url="https://creativecommons.org/licenses/by/4.0/",
    ),
    "uk_ea_flood": SourcePolicy(
        "uk_ea_flood", "UK Environment Agency flood monitoring", "Open Government Licence v3.0",
        "Commercial reuse is permitted with the published EA attribution. England only.",
        "Environment Agency flood and river level data from the real-time data API (Beta) · OGL v3.0",
        ("environment.data.gov.uk",),
        licence_url="https://www.nationalarchives.gov.uk/doc/open-government-licence/version/3/",
    ),
    "ptwc_tsunami": SourcePolicy(
        "ptwc_tsunami", "Pacific Tsunami Warning Center", "U.S. Government public domain",
        "Public-domain PTWC warning products may be used commercially; empty feed is offline.",
        "Pacific Tsunami Warning Center Atom · U.S. public domain",
        ("www.tsunami.gov",),
        licence_url="https://www.tsunami.gov/",
    ),
    "noaa_swpc": SourcePolicy(
        "noaa_swpc", "NOAA Space Weather Prediction Center", "U.S. Government public domain",
        "Public-domain observations may be used commercially; provenance is still retained.",
        "NOAA Space Weather Prediction Center",
        ("services.swpc.noaa.gov",),
    ),
}


def require_approved_source(source_id: str) -> SourcePolicy:
    """Return a reviewed policy or fail closed."""
    try:
        return APPROVED_SOURCES[source_id]
    except KeyError:
        raise ValueError(f"source is not approved for a commercial rail: {source_id}") from None


__all__ = ["SourcePolicy", "APPROVED_SOURCES", "require_approved_source"]
