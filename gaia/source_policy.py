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
    "ndw_ev_charging": SourcePolicy(
        "ndw_ev_charging", "NDW DOT-NL public charging availability", "CC0 1.0",
        "NDW open data may be used and redistributed commercially; use the public DOT-NL feeds.",
        "NDW DOT-NL · CC0 1.0", ("dotnl.ndw.nu", "opendata.ndw.nu"),
        licence_url="https://docs.ndw.nu/algemeen/data-gebruiksvoorwaarden/",
    ),
    "bc_wildfire": SourcePolicy(
        "bc_wildfire", "BC Wildfire Service current fires and perimeters", "Open Government Licence – British Columbia 2.0",
        "The provincial open data catalogue permits free commercial reuse with attribution.",
        "Contains information licensed under the Open Government Licence – British Columbia.",
        ("services6.arcgis.com",),
        licence_url="https://www2.gov.bc.ca/gov/content/data/policy-standards/data-policies/open-data/open-government-licence-bc",
    ),
    "meteoswiss_pollen": SourcePolicy(
        "meteoswiss_pollen", "MeteoSwiss automatic pollen stations", "CC BY 4.0",
        "MeteoSwiss Open Data may be processed and redistributed commercially with source credit.",
        "Source: MeteoSwiss · CC BY 4.0", ("data.geo.admin.ch",),
        licence_url="https://opendatadocs.meteoswiss.ch/general/terms-of-use",
    ),
    "irish_weather_buoys": SourcePolicy(
        "irish_weather_buoys", "Irish Marine Institute Weather Buoy Network", "CC BY 4.0",
        "The IWBNetwork dataset permits commercial reuse with attribution.",
        "Marine Institute and Met Éireann · Irish Weather Buoy Network · CC BY 4.0",
        ("erddap.marine.ie",), licence_url="https://erddap.marine.ie/erddap/info/IWBNetwork/index.html",
    ),
    "aodn_wave_buoys": SourcePolicy(
        "aodn_wave_buoys", "Australian Ocean Data Network real-time wave observations", "CC BY 4.0",
        "The public dataset permits commercial reuse with attribution to IMOS and contributors.",
        "Data was sourced from Australia’s Integrated Marine Observing System (IMOS) - IMOS is enabled by "
        "the National Collaborative Research Infrastructure Strategy (NCRIS) · AODN and contributors · CC BY 4.0",
        ("thredds.aodn.org.au", "aodn-cloud-optimised.s3.ap-southeast-2.amazonaws.com"),
        licence_url="https://registry.opendata.aws/aodn_wave_buoy_realtime_nonqc/",
    ),
    "geonet_volcano": SourcePolicy(
        "geonet_volcano", "GeoNet volcanic alert levels and camera images", "CC BY 3.0 NZ",
        "GeoNet data and images may be reused commercially with GeoNet and sponsor credit.",
        "GeoNet / Earth Sciences New Zealand and GeoNet sponsors · CC BY 3.0 NZ",
        ("api.geonet.org.nz", "images.geonet.org.nz"), licence_url="https://www.geonet.org.nz/policy",
    ),
    "noaa_spc_storm_reports": SourcePolicy(
        "noaa_spc_storm_reports", "NOAA/NWS SPC preliminary local storm reports", "U.S. Government public domain",
        "NWS published factual fields are free for lawful reuse. Relay only structured event data.",
        "NOAA / NWS Storm Prediction Center", ("www.spc.noaa.gov",),
        licence_url="https://www.weather.gov/disclaimer",
    ),
    "noaa_washington_vaac": SourcePolicy(
        "noaa_washington_vaac", "NOAA Washington VAAC volcanic ash advisories", "U.S. Government public domain",
        "Use the NOAA-authored Washington VAAC advisories and their source geometry.",
        "NOAA / NESDIS Washington Volcanic Ash Advisory Center", ("www.ospo.noaa.gov",),
        licence_url="https://www.noaa.gov/office-education/outreach-communication/faq",
    ),
    "dea_hotspots": SourcePolicy(
        "dea_hotspots", "Digital Earth Australia public Hotspots", "CC BY 4.0",
        "Geoscience Australia's public product permits commercial reuse with attribution.",
        "Geoscience Australia · Digital Earth Australia Hotspots · CC BY 4.0",
        ("hotspots.dea.ga.gov.au",), licence_url="https://knowledge.dea.ga.gov.au/data/product/dea-hotspots/",
    ),
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
    "noaa_hms_smoke": SourcePolicy(
        "noaa_hms_smoke", "NOAA/NESDIS Hazard Mapping System smoke",
        "U.S. Government public domain",
        "The qualitative HMS smoke analysis may be reused commercially; retain NOAA/NESDIS attribution.",
        "NOAA/NESDIS Hazard Mapping System (HMS)",
        ("www.ospo.noaa.gov",),
        licence_url="https://www.noaa.gov/disclaimer",
    ),
    "usgs_water_quality": SourcePolicy(
        "usgs_water_quality", "USGS Water Data for the Nation continuous data",
        "U.S. Government public domain",
        "Automated continuous observations may be reused commercially; preserve USGS citation and provisional status.",
        "U.S. Geological Survey Water Data for the Nation",
        ("api.waterdata.usgs.gov",),
        licence_url="https://www.usgs.gov/information-policies-and-instructions/copyrights-and-credits",
    ),
    "usgs_groundwater": SourcePolicy(
        "usgs_groundwater", "USGS Water Data for the Nation continuous groundwater",
        "U.S. Government public domain",
        "USGS-produced data may be reused commercially; retain USGS credit and provisional status.",
        "U.S. Geological Survey Water Data for the Nation",
        ("api.waterdata.usgs.gov",),
        licence_url="https://www.usgs.gov/information-policies-and-instructions/copyrights-and-credits",
    ),
    "usgs_rtfi": SourcePolicy(
        "usgs_rtfi", "USGS Real-Time Flood Impacts and Water Data for the Nation",
        "U.S. Government public domain",
        "Commercial reuse of USGS-produced thresholds and measurements is permitted. "
        "Attribute USGS and identify exceedances as provisional.",
        "U.S. Geological Survey Real-Time Flood Impacts / Water Data for the Nation",
        ("api.waterdata.usgs.gov",),
        licence_url="https://www.usgs.gov/information-policies-and-instructions/copyrights-and-credits",
    ),
    "hubeau_groundwater": SourcePolicy(
        "hubeau_groundwater", "Hub'Eau / BRGM Piézométrie raw telemetry",
        "Etalab Open Licence 2.0",
        "Commercial reuse is permitted; credit BRGM / Hub'Eau and retain the last update date.",
        "BRGM / Hub'Eau Piézométrie · Licence Ouverte 2.0",
        ("hubeau.eaufrance.fr",),
        licence_url="https://assistance.brgm.fr/fr/aide/HubEau",
    ),
    "noaa_dart": SourcePolicy(
        "noaa_dart", "NOAA/NDBC DART", "U.S. Government public domain",
        "Real-time DART water-column height may be reused commercially; retain NOAA/NDBC attribution.",
        "NOAA National Data Buoy Center DART®",
        ("www.ndbc.noaa.gov",),
        licence_url="https://www.noaa.gov/disclaimer",
    ),
    "nasa_imerg": SourcePolicy(
        "nasa_imerg", "NASA GPM IMERG Early Run V07", "NASA open data / U.S. Government work",
        "Commercial reuse of NASA science data is permitted; cite NASA GPM IMERG and do not imply endorsement.",
        "NASA GPM IMERG Early Run V07",
        ("cmr.earthdata.nasa.gov", "data.gesdisc.earthdata.nasa.gov"),
        requires_operator_account=True,
        licence_url="https://www.earthdata.nasa.gov/engage/open-data-services-and-software/data-and-information-policy",
    ),
    "noaa_nexrad_status": SourcePolicy(
        "noaa_nexrad_status", "NOAA/NWS NEXRAD station status", "U.S. Government public domain",
        "NEXRAD operational status may be reused commercially; retain NOAA/NWS attribution.",
        "NOAA/NWS NEXRAD Radar Operations Center",
        ("api.weather.gov",),
        licence_url="https://www.weather.gov/disclaimer",
    ),
    "epa_radnet": SourcePolicy(
        "epa_radnet", "U.S. EPA RadNet", "U.S. Government public data",
        "EPA RadNet monitoring results may be reused commercially; cite EPA RadNet and identify channel aggregation.",
        "U.S. EPA RadNet",
        ("radnet.epa.gov",),
        licence_url="https://www.epa.gov/web-policies-and-procedures/epa-disclaimers",
    ),
    "copernicus_clms_swi": SourcePolicy(
        "copernicus_clms_swi", "Copernicus CLMS Soil Water Index global v3",
        "Copernicus free, full and open access",
        "The product is free for any purpose; attribution and modification notice are required.",
        "Contains modified Copernicus Land Monitoring Service information",
        ("identity.dataspace.copernicus.eu", "sh.dataspace.copernicus.eu"),
        requires_operator_account=True,
        licence_url="https://land.copernicus.eu/en/data-policy",
    ),
    "nasa_power_solar": SourcePolicy(
        "nasa_power_solar", "NASA POWER daily solar irradiation",
        "NASA open data / CC0 unless marked otherwise",
        "Commercial reuse and redistribution are permitted; cite NASA POWER and do not imply NASA endorsement.",
        "NASA POWER", ("power.larc.nasa.gov",),
        licence_url="https://www.earthdata.nasa.gov/engage/open-data-services-software/data-use-policy",
    ),
    "noaa_nohrsc_snow": SourcePolicy(
        "noaa_nohrsc_snow", "NOAA/NWS NOHRSC National Snow Analysis",
        "U.S. Government public domain",
        "NOHRSC/SNODAS grid values may be reused commercially; retain NOAA/NWS attribution and provisional/model status.",
        "NOAA/NWS NOHRSC National Snow Analysis (SNODAS)",
        ("noaadata.apps.nsidc.org",),
        licence_url="https://www.weather.gov/disclaimer",
    ),
    "noaa_nsidc_sea_ice": SourcePolicy(
        "noaa_nsidc_sea_ice", "NOAA/NSIDC Sea Ice Index v4",
        "U.S. Government public data; dataset citation required",
        "Commercial reuse is permitted; the published Fetterer et al. dataset citation must accompany the subset.",
        "Fetterer et al. (2025), NOAA/NSIDC Sea Ice Index v4, doi:10.7265/a98x-0f50",
        ("noaadata.apps.nsidc.org",),
        licence_url="https://nsidc.org/data/g02135/versions/4",
    ),
    "copernicus_s3_lst": SourcePolicy(
        "copernicus_s3_lst", "Copernicus Sentinel-3 SLSTR Level-2 LST",
        "Copernicus free, full and open access",
        "Commercial reuse and redistribution are permitted; attribution and modification notice are required.",
        "Contains modified Copernicus Sentinel-3 SLSTR Level-2 information",
        ("identity.dataspace.copernicus.eu", "sh.dataspace.copernicus.eu"),
        requires_operator_account=True,
        licence_url="https://dataspace.copernicus.eu/terms-and-conditions",
    ),
    "pegelonline": SourcePolicy(
        "pegelonline", "WSV PEGELONLINE", "DL-DE-Zero-2.0",
        "Commercial reuse is permitted without restriction under DL-DE-Zero-2.0.",
        "WSV PEGELONLINE · DL-DE-Zero-2.0",
        ("www.pegelonline.wsv.de",),
        licence_url="https://www.govdata.de/dl-de/zero-2-0",
    ),
    "aviation_metar": SourcePolicy(
        "aviation_metar", "NOAA/NWS Aviation Weather Center METAR",
        "U.S. Government public domain",
        "Public-domain METAR observations may be used commercially; provenance is retained.",
        "NOAA/NWS Aviation Weather Center METAR",
        ("aviationweather.gov",),
        licence_url="https://aviationweather.gov/",
    ),
    "digitraffic_road": SourcePolicy(
        "digitraffic_road", "Fintraffic Digitraffic road weather", "CC BY 4.0",
        "Commercial reuse is permitted with attribution.",
        "Fintraffic Digitraffic road weather · CC BY 4.0",
        ("tie.digitraffic.fi", "www.digitraffic.fi"),
        licence_url="https://www.digitraffic.fi/en/terms-of-service/",
    ),
    "digitraffic_road_events": SourcePolicy(
        "digitraffic_road_events", "Fintraffic Digitraffic road notices", "CC BY 4.0",
        "Commercial reuse is permitted with attribution and indication of changes.",
        "Fintraffic Digitraffic · CC BY 4.0",
        ("tie.digitraffic.fi",),
        licence_url="https://www.digitraffic.fi/en/terms-of-service/",
    ),
    "ndw_road_events": SourcePolicy(
        "ndw_road_events", "NDW Dutch road incidents and closures", "CC0 1.0",
        "The public NDW SRTI and temporary closures feeds permit commercial reuse under CC0.",
        "Nationaal Dataportaal Wegverkeer (NDW)", ("opendata.ndw.nu",),
        licence_url="https://docs.ndw.nu/algemeen/data-gebruiksvoorwaarden/",
    ),
    "fmi_marine_waves": SourcePolicy(
        "fmi_marine_waves", "Finnish Meteorological Institute wave buoy observations", "CC BY 4.0",
        "Commercial reuse of FMI open observations is permitted with attribution and indication of changes.",
        "Finnish Meteorological Institute", ("opendata.fmi.fi",),
        licence_url="https://en.ilmatieteenlaitos.fi/download-observations-questions",
    ),
    "bfs_gamma_odl": SourcePolicy(
        "bfs_gamma_odl", "BfS Gamma ODL ambient dose rate", "Datenlizenz Deutschland – Namensnennung 2.0",
        "Commercial reuse is permitted with attribution to Bundesamt für Strahlenschutz and indication of changes.",
        "Bundesamt für Strahlenschutz (BfS) · dl-de/by-2-0 · units converted and coordinates rounded",
        ("www.imis.bfs.de",), licence_url="https://www.govdata.de/dl-de/by-2-0",
    ),
    "chmi_hydrometry": SourcePolicy(
        "chmi_hydrometry", "ČHMÚ / CHMI preliminary river hydrometry", "CC BY 4.0",
        "Commercial reuse is permitted with attribution to ČHMÚ and indication of changes.",
        "Czech Hydrometeorological Institute (ČHMÚ) · CC BY 4.0 · stage units converted",
        ("opendata.chmi.cz",),
        licence_url="https://www.chmi.cz/-/kde-najdu-otev%C5%99en%C3%A1-data-%C4%8Dhm%C3%BA-",
    ),
    "swpc_drap": SourcePolicy(
        "swpc_drap", "NOAA SWPC D-Region Absorption Prediction", "U.S. Government public domain",
        "Commercial reuse is permitted. Credit NOAA SWPC and identify modeled absorption thresholds.",
        "NOAA Space Weather Prediction Center · U.S. Government public domain", ("services.swpc.noaa.gov",),
        licence_url="https://www.weather.gov/disclaimer",
    ),
    "smhi_ocean_observations": SourcePolicy(
        "smhi_ocean_observations", "SMHI Oceanographic Observations", "CC BY 4.0",
        "Commercial reuse is permitted with attribution to SMHI and indication of changes.",
        "SMHI Open Data · CC BY 4.0", ("opendata-download-ocobs.smhi.se",),
        licence_url="https://www.smhi.se/data/om-smhis-data/villkor-for-anvandning",
    ),
    "eccc_raqdps_smoke": SourcePolicy(
        "eccc_raqdps_smoke", "ECCC RAQDPS FireWork smoke forecast", "MSC End-use Licence / Open Government Licence – Canada",
        "Commercial reuse is permitted with attribution to Environment and Climate Change Canada.",
        "Environment and Climate Change Canada · MSC FireWork", ("geo.weather.gc.ca",),
        licence_url="https://eccc-msc.github.io/open-data/licence/readme_en/",
    ),
    "geonet_coastal": SourcePolicy(
        "geonet_coastal", "GeoNet coastal sea-level observations", "CC BY 3.0 NZ",
        "Commercial reuse is permitted with attribution to GeoNet and its sponsors.",
        "New Zealand GeoNet programme and its sponsors NHC, Earth Sciences NZ, LINZ, NEMA and MBIE · CC BY 3.0 NZ",
        ("tilde.geonet.org.nz",), licence_url="https://www.geonet.org.nz/policy",
    ),
    "entur_rail": SourcePolicy(
        "entur_rail", "Entur VYG train positions", "Norwegian Licence for Open Government Data (NLOD)",
        "Commercial reuse is permitted with attribution to Entur and indication of changes.",
        "Entur · Norwegian Licence for Open Government Data (NLOD)", ("api.entur.io",),
        licence_url="https://developer.entur.no/terms-of-service",
    ),
    "arso_hydrometry": SourcePolicy(
        "arso_hydrometry", "ARSO preliminary river observations", "ARSO public information reuse terms",
        "Commercial reuse is permitted with citation of the source.",
        "Agencija Republike Slovenije za okolje (ARSO)", ("www.arso.gov.si",),
        licence_url="https://kazalci.arso.gov.si/en/content/legal-note",
    ),
    "kartverket_tide": SourcePolicy(
        "kartverket_tide", "Kartverket observed coastal water level", "CC BY 4.0",
        "Commercial reuse is permitted with attribution to Kartverket and indication of changes.",
        "Kartverket (Norwegian Hydrographic Service) · CC BY 4.0", ("vannstand.kartverket.no",),
        licence_url="https://vannstand.kartverket.no/tideapi_en.html",
    ),
    "noaa_aoml_drifters": SourcePolicy(
        "noaa_aoml_drifters", "NOAA AOML / OSMC real-time drifter observations", "CC0 1.0",
        "Commercial reuse is permitted. Credit NOAA AOML / OSMC.",
        "NOAA AOML / OSMC · CC0 1.0", ("osmc.noaa.gov",),
        licence_url="https://osmc.noaa.gov/erddap/info/OSMC_flattened/index.json",
    ),
    "noaa_ports_air_gap": SourcePolicy(
        "noaa_ports_air_gap", "NOAA NOS CO-OPS PORTS air gap observations", "U.S. Government public domain",
        "Commercial reuse is permitted. Credit NOAA NOS CO-OPS and identify preliminary observations.",
        "NOAA NOS CO-OPS · U.S. Government public domain", ("api.tidesandcurrents.noaa.gov",),
        licence_url="https://opendap.co-ops.nos.noaa.gov/erddap/info/IOOS_Air_Gap/index.html",
    ),
    "toronto_road_restrictions": SourcePolicy(
        "toronto_road_restrictions", "City of Toronto road restrictions", "Open Government Licence – Toronto",
        "Commercial reuse is permitted with the prescribed City of Toronto attribution.",
        "City of Toronto · Contains information licensed under the Open Government Licence – Toronto.",
        ("secure.toronto.ca",), licence_url="https://open.toronto.ca/open-data-licence/",
    ),
    "toronto_bikeshare": SourcePolicy(
        "toronto_bikeshare", "City of Toronto Bike Share GBFS", "Open Government Licence – Toronto",
        "Commercial reuse is permitted with the prescribed City of Toronto attribution.",
        "City of Toronto · Contains information licensed under the Open Government Licence – Toronto.",
        ("tor.publicbikesystem.net",), licence_url="https://open.toronto.ca/open-data-licence/",
    ),
    "ukpn_live_faults": SourcePolicy(
        "ukpn_live_faults", "UK Power Networks live faults", "CC BY 4.0",
        "Commercial reuse is permitted with attribution to UK Power Networks and indication of changes.",
        "UK Power Networks · CC BY 4.0", ("ukpowernetworks.opendatasoft.com",),
        licence_url="https://creativecommons.org/licenses/by/4.0/",
    ),
    "usda_nrcs_snotel": SourcePolicy(
        "usda_nrcs_snotel", "USDA NRCS SNOTEL hourly observations", "U.S. Government public domain",
        "The public USDA NRCS SNOTEL observations may be reused commercially. Credit USDA NRCS.",
        "USDA NRCS National Water and Climate Center", ("wcc.sc.egov.usda.gov",),
        licence_url="https://catalog.data.gov/dataset/snowpack-telemetry-network-snotel",
    ),
    "awc_gairmet": SourcePolicy(
        "awc_gairmet", "NOAA/NWS Aviation Weather Center G-AIRMET",
        "U.S. Government public domain",
        "Commercial reuse is permitted. Credit NOAA/NWS and distinguish forecasts from observations.",
        "NOAA/NWS Aviation Weather Center G-AIRMET",
        ("aviationweather.gov",),
        licence_url="https://www.weather.gov/disclaimer",
    ),
    "digitraffic_rail": SourcePolicy(
        "digitraffic_rail", "Fintraffic Digitraffic rail locations", "CC BY 4.0",
        "Commercial reuse is permitted with attribution.",
        "Fintraffic Digitraffic rail locations · CC BY 4.0",
        ("rata.digitraffic.fi", "www.digitraffic.fi"),
        licence_url="https://www.digitraffic.fi/en/terms-of-service/",
    ),
    "usdm": SourcePolicy(
        "usdm", "U.S. Drought Monitor", "Open data + NDMC attribution",
        "Commercial reuse is permitted with required attribution to NDMC / USDA / NOAA / NASA.",
        "U.S. Drought Monitor · NDMC / USDA / NOAA / NASA",
        ("usdmdataservices.unl.edu", "droughtmonitor.unl.edu"),
        licence_url="https://droughtmonitor.unl.edu/About/Permission.aspx",
    ),
    "geoshake": SourcePolicy(
        "geoshake", "GeoShake earthquake catalog", "CC BY 4.0",
        "Commercial reuse is permitted with attribution to GeoShake.",
        "GeoShake community earthquake catalog · CC BY 4.0",
        ("api.geoshake.org",),
        licence_url="https://creativecommons.org/licenses/by/4.0/",
    ),
    "naad_alerts": SourcePolicy(
        "naad_alerts", "Canada NAAD / Alert Ready Atom",
        "CAP-CP public alert redistribution",
        "Public CAP-CP alert redistribution may be relayed commercially; cite the issuing authority.",
        "Canadian National Alert Aggregation & Dissemination System (NAAD) · CAP-CP",
        ("rss.naad-adna.pelmorex.com",),
        licence_url="https://www.alertready.ca/",
    ),
    "nasa_donki": SourcePolicy(
        "nasa_donki", "NASA DONKI notifications", "NASA open data",
        "Commercial reuse of NASA science products is permitted; cite NASA/CCMC DONKI and do not imply endorsement.",
        "NASA DONKI notifications",
        ("api.nasa.gov",),
        licence_url="https://www.earthdata.nasa.gov/engage/open-data-services-and-software/data-and-information-policy",
    ),
    "ea_hydrology": SourcePolicy(
        "ea_hydrology", "Environment Agency Hydrology API", "Open Government Licence v3.0",
        "Commercial reuse is permitted with Environment Agency attribution. England only.",
        "Environment Agency Hydrology API · OGL v3",
        ("environment.data.gov.uk",),
        licence_url="https://www.nationalarchives.gov.uk/doc/open-government-licence/version/3/",
    ),
    "ea_tide_gauge": SourcePolicy(
        "ea_tide_gauge", "Environment Agency Tide Gauge API", "Open Government Licence v3.0",
        "Commercial reuse is permitted. Retain the EA attribution and the Newlyn vertical datum.",
        "This uses Environment Agency tide gauge data from the real-time data API (Beta)",
        ("environment.data.gov.uk",),
        licence_url="https://www.nationalarchives.gov.uk/doc/open-government-licence/version/3/",
    ),
    "noaa_nwps_forecast": SourcePolicy(
        "noaa_nwps_forecast", "NOAA/NWS National Water Prediction Service forecasts",
        "U.S. Government public domain",
        "Commercial reuse of NWS forecasts is permitted. Attribute NOAA/NWS and identify derived peaks.",
        "NOAA/NWS National Water Prediction Service",
        ("api.water.noaa.gov",),
        licence_url="https://www.weather.gov/disclaimer",
    ),
    "rws_water": SourcePolicy(
        "rws_water", "Rijkswaterstaat WaterWebservices", "CC0",
        "CC0 permits commercial reuse and redistribution; source provenance is retained.",
        "Rijkswaterstaat WaterWebservices · CC0",
        ("ddapi20-waterwebservices.rijkswaterstaat.nl",),
        licence_url="https://creativecommons.org/publicdomain/zero/1.0/",
    ),
    "usace_cwms": SourcePolicy(
        "usace_cwms", "USACE CWMS Data API", "U.S. Government public domain",
        "USACE public data may be reused commercially; provenance is retained.",
        "U.S. Army Corps of Engineers CWMS Data API",
        ("cwms-data.usace.army.mil",),
    ),
    "epa_uv": SourcePolicy(
        "epa_uv", "EPA Envirofacts UV hourly forecast", "U.S. Government public domain",
        "EPA public forecast data may be reused commercially; provenance and forecast status are retained.",
        "U.S. EPA Envirofacts UV hourly forecast",
        ("data.epa.gov",),
        licence_url="https://www.epa.gov/web-policies-and-procedures/epa-disclaimers",
    ),
    "met_eireann": SourcePolicy(
        "met_eireann", "Met Éireann observations", "CC BY 4.0",
        "Commercial reuse is permitted with attribution, source link, licence link, and change notice.",
        "Contains modified Met Éireann observations · CC BY 4.0",
        ("www.met.ie", "opendata2.met.ie"),
        licence_url="https://creativecommons.org/licenses/by/4.0/",
    ),
    "met_norway_frost": SourcePolicy(
        "met_norway_frost", "MET Norway Frost observations", "CC BY 4.0 + NLOD",
        "Commercial reuse is permitted with MET Norway attribution. In-situ stations only — not METAR, not locationforecast.",
        "MET Norway Frost · CC BY 4.0 + NLOD",
        ("frost.met.no",),
        licence_url="https://creativecommons.org/licenses/by/4.0/",
    ),
    "dmi_metobs": SourcePolicy(
        "dmi_metobs", "DMI metObs", "CC BY 4.0",
        "Commercial reuse is permitted with Danish Meteorological Institute attribution.",
        "DMI Open Data metObs · CC BY 4.0",
        ("opendataapi.dmi.dk",),
        licence_url="https://creativecommons.org/licenses/by/4.0/",
    ),
    "sepa_kiwis": SourcePolicy(
        "sepa_kiwis", "SEPA KiWIS hydrology", "Open Government Licence",
        "Commercial reuse is permitted with SEPA attribution. Scotland only — not EA England or NRW Wales.",
        "SEPA KiWIS · OGL",
        ("timeseries.sepa.org.uk",),
        licence_url="https://www.nationalarchives.gov.uk/doc/open-government-licence/version/3/",
    ),
    "nrw_river": SourcePolicy(
        "nrw_river", "NRW River Levels API", "Open Government Licence",
        "Commercial reuse is permitted with Natural Resources Wales attribution. Wales only — not EA England or SEPA.",
        "NRW River Levels · OGL",
        ("api.naturalresources.wales",),
        licence_url="https://www.nationalarchives.gov.uk/doc/open-government-licence/version/3/",
    ),
    "ingv_fdsn": SourcePolicy(
        "ingv_fdsn", "INGV FDSN event service", "CC BY 4.0",
        "Commercial reuse is permitted with INGV attribution. Italian/regional catalog — not a USGS/EMSC replacement.",
        "INGV FDSN · CC BY 4.0",
        ("webservices.ingv.it",),
        licence_url="https://creativecommons.org/licenses/by/4.0/",
    ),
    "cdip_erddap": SourcePolicy(
        "cdip_erddap", "CDIP ERDDAP wave buoys", "Unrestricted (USACE-sponsored) + attribution",
        "ERDDAP license allows redistribution without restriction; CDIP requires acknowledgement and a link to cdip.ucsd.edu for web displays.",
        "CDIP / Scripps · USACE-sponsored · acknowledge CDIP",
        ("erddap.cdip.ucsd.edu",),
        licence_url="https://www.cdip.ucsd.edu/m/documents/data_access.html",
    ),
    "icos_atc": SourcePolicy(
        "icos_atc", "ICOS ATC NRT CO₂", "CC BY 4.0",
        "Commercial reuse is permitted with ICOS attribution and licence link; cite the object PID/DOI. "
        "Use GAIA_ICOS_EMAIL+GAIA_ICOS_PASSWORD (auto-refresh) or GAIA_ICOS_CPAUTH_TOKEN after licence accept.",
        "ICOS Carbon Portal ATC NRT · CC BY 4.0",
        ("meta.icos-cp.eu", "data.icos-cp.eu", "cpauth.icos-cp.eu"),
        licence_url="https://creativecommons.org/licenses/by/4.0/",
    ),
    "hubeau_hydro": SourcePolicy(
        "hubeau_hydro", "Hub'Eau hydrométrie", "Etalab Licence Ouverte 2.0",
        "Commercial reuse is permitted with attribution to the dataset authors. France only.",
        "Hub'Eau hydrométrie · Etalab Licence Ouverte 2.0",
        ("hubeau.eaufrance.fr",),
        licence_url="https://www.etalab.gouv.fr/licence-ouverte-open-licence/",
    ),
    "ehyd_austria": SourcePolicy(
        "ehyd_austria", "eHYD Hydrographie Österreich", "CC BY 4.0",
        "Commercial reuse is permitted with attribution «Datenquelle: ehyd.gv.at». Austria only.",
        "eHYD / BMLUK · CC BY 4.0 — Datenquelle: ehyd.gv.at",
        ("gis.lfrz.gv.at", "ehyd.gv.at"),
        licence_url="https://creativecommons.org/licenses/by/4.0/",
    ),
    "smhi_metobs": SourcePolicy(
        "smhi_metobs", "SMHI MetObs", "CC BY 4.0",
        "Commercial reuse is permitted with SMHI attribution. Sweden in-situ weather only — not hydroobs.",
        "SMHI MetObs · CC BY 4.0",
        ("opendata-download-metobs.smhi.se", "opendata.smhi.se"),
        licence_url="https://creativecommons.org/licenses/by/4.0/",
    ),
    "singapore_nea": SourcePolicy(
        "singapore_nea", "Singapore NEA / data.gov.sg", "Singapore Open Data Licence",
        "Commercial reuse is permitted with attribution under the Singapore Open Data Licence.",
        "Singapore NEA via data.gov.sg · Singapore Open Data Licence",
        ("api.data.gov.sg",),
        licence_url="https://data.gov.sg/open-data-licence",
    ),
    "hongkong_hko": SourcePolicy(
        "hongkong_hko", "Hong Kong Observatory", "DATA.GOV.HK terms",
        "Commercial and non-commercial reuse is free with attribution to the HKSAR Government / HKO.",
        "Hong Kong Observatory · DATA.GOV.HK (attribution required)",
        ("data.weather.gov.hk",),
        licence_url="https://data.gov.hk/en/terms-and-conditions",
    ),
    "irceline_be": SourcePolicy(
        "irceline_be", "IRCELINE Belgium air quality", "CC BY 4.0",
        "Commercial reuse is permitted with attribution to IRCELINE. Belgium in-situ AQ only.",
        "IRCELINE · CC BY 4.0",
        ("geo.irceline.be",),
        licence_url="https://creativecommons.org/licenses/by/4.0/",
    ),
    "aemet_es": SourcePolicy(
        "aemet_es", "AEMET OpenData", "Spanish PSI reuse (Fuente: AEMET)",
        "Commercial reuse is permitted with attribution «Fuente: AEMET». Spain in-situ weather only.",
        "AEMET OpenData · Spanish PSI reuse — Fuente: AEMET",
        ("opendata.aemet.es",),
        requires_operator_account=True,
        licence_url="https://www.aemet.es/en/datos_abiertos/Apertura_datos",
    ),
    "meteoswiss_ch": SourcePolicy(
        "meteoswiss_ch", "MeteoSwiss OGD", "CC BY 4.0",
        "Commercial reuse is permitted with attribution to MeteoSwiss. Switzerland in-situ weather only.",
        "MeteoSwiss OGD · CC BY 4.0",
        ("data.geo.admin.ch",),
        licence_url="https://creativecommons.org/licenses/by/4.0/",
    ),
    "bafu_hydro": SourcePolicy(
        "bafu_hydro", "BAFU/FOEN hydrology (LINDAS)", "OGD-CH Open-Use",
        "Commercial reuse is permitted with attribution under the Swiss Federal OGD Open-Use terms. Switzerland in-situ only.",
        "BAFU/FOEN hydrology · OGD-CH Open-Use",
        ("environment.ld.admin.ch",),
        licence_url="https://opendata.swiss/en/terms-of-use",
    ),
    "cwa_tw": SourcePolicy(
        "cwa_tw", "Taiwan CWA OpenData", "OGDL 1.0",
        "Commercial reuse is permitted under the Open Government Data License 1.0 (compatible with CC BY 4.0). Taiwan in-situ weather only.",
        "Taiwan CWA OpenData · OGDL 1.0 (≡ CC BY 4.0)",
        ("opendata.cwa.gov.tw",),
        requires_operator_account=True,
        licence_url="https://data.gov.tw/license",
    ),
    "meteofrance_dpobs": SourcePolicy(
        "meteofrance_dpobs", "Météo-France DPObs", "Etalab Licence Ouverte 2.0",
        "Commercial reuse is permitted with attribution under Etalab Licence Ouverte 2.0. France in-situ weather only.",
        "Météo-France DPObs · Etalab Licence Ouverte 2.0",
        ("portail-api.meteofrance.fr", "public-api.meteofrance.fr"),
        requires_operator_account=True,
        licence_url="https://www.etalab.gouv.fr/licence-ouverte-open-licence/",
    ),
    "estonia_ews": SourcePolicy(
        "estonia_ews", "Estonian Weather Service", "CC BY 4.0",
        "Commercial reuse is permitted with attribution to the Estonian Environment Agency. Estonia in-situ weather only.",
        "Estonian Weather Service · CC BY 4.0",
        ("www.ilmateenistus.ee",),
        licence_url="https://creativecommons.org/licenses/by/4.0/",
    ),
    "iceland_imo": SourcePolicy(
        "iceland_imo", "Icelandic Meteorological Office", "ODC-By",
        "Commercial reuse is permitted with attribution to the Icelandic Meteorological Office. Iceland in-situ weather only.",
        "Icelandic Meteorological Office · ODC-By",
        ("api.vedur.is",),
        licence_url="https://opendatacommons.org/licenses/by/1-0/",
    ),
    "hongkong_aqhi": SourcePolicy(
        "hongkong_aqhi", "Hong Kong EPD AQHI", "DATA.GOV.HK terms",
        "Commercial and non-commercial reuse is free with attribution to the HKSAR Government / EPD. AQHI is a health index, not PM2.5.",
        "Hong Kong EPD AQHI · DATA.GOV.HK (attribution required)",
        ("dashboard.data.gov.hk",),
        licence_url="https://data.gov.hk/en/terms-and-conditions",
    ),
    "geosphere_at": SourcePolicy(
        "geosphere_at", "GeoSphere Austria TAWES", "CC BY 4.0",
        "Commercial reuse is permitted with attribution to GeoSphere Austria. Austria in-situ weather only — not a paid forecast.",
        "GeoSphere Austria TAWES · CC BY 4.0",
        ("dataset.api.hub.geosphere.at",),
        licence_url="https://creativecommons.org/licenses/by/4.0/",
    ),
    "lhmt_lt": SourcePolicy(
        "lhmt_lt", "Lithuania LHMT Meteo.lt", "CC BY-SA 4.0",
        "Commercial reuse of live queries is permitted with attribution (SA applies to a derived database dump). Lithuania in-situ only — not /forecasts.",
        "Lithuania LHMT Meteo.lt · CC BY-SA 4.0",
        ("api.meteo.lt",),
        licence_url="https://creativecommons.org/licenses/by-sa/4.0/",
    ),
    "lvgmc_lv": SourcePolicy(
        "lvgmc_lv", "Latvia LVGMC HVD", "CC0-1.0",
        "Commercial reuse is permitted without restriction under CC0-1.0 (data.gov.lv HVD). Latvia in-situ only.",
        "Latvia LVGMC · CC0-1.0",
        ("data.gov.lv",),
        licence_url="https://creativecommons.org/publicdomain/zero/1.0/",
    ),
    "vigicrues_fr": SourcePolicy(
        "vigicrues_fr", "Vigicrues VIC", "Etalab Licence Ouverte 2.0",
        "Commercial reuse is permitted with attribution under Etalab Licence Ouverte 2.0. France flood WARNING only — not Hub'Eau gauges.",
        "Vigicrues VIC · Etalab Licence Ouverte 2.0",
        ("www.vigicrues.gouv.fr",),
        licence_url="https://www.etalab.gouv.fr/licence-ouverte-open-licence/",
    ),
    "rte_eco2mix": SourcePolicy(
        "rte_eco2mix", "RTE éCO2mix", "Etalab Licence Ouverte 2.0",
        "Commercial reuse is permitted with attribution under Etalab Licence Ouverte 2.0. France national production-only CO₂ — excludes imports/lifecycle.",
        "RTE éCO2mix · Etalab Licence Ouverte 2.0",
        ("odre.opendatasoft.com",),
        licence_url="https://www.etalab.gouv.fr/licence-ouverte-open-licence/",
    ),
    "opw_ie": SourcePolicy(
        "opw_ie", "Ireland OPW waterlevel.ie", "CC BY 4.0",
        "Commercial reuse is permitted with attribution to OPW. Ireland in-situ river gauges only — pre-generated GeoJSON; stations >41000 excluded.",
        "Ireland OPW waterlevel.ie · CC BY 4.0",
        ("waterlevel.ie",),
        licence_url="https://creativecommons.org/licenses/by/4.0/",
    ),
    "jma_amedas": SourcePolicy(
        "jma_amedas", "JMA AMeDAS", "Public Data License 1.0",
        "Commercial reuse is permitted under the Japan Public Data License 1.0 (compatible with CC BY 4.0). Japan in-situ observations only — not a forecast licence.",
        "JMA AMeDAS · Public Data License 1.0 (cite JMA)",
        ("www.jma.go.jp",),
        licence_url="https://www.jma.go.jp/jma/info/license.html",
    ),
    "jma_quake": SourcePolicy(
        "jma_quake", "JMA earthquake list", "Public Data License 1.0",
        "Commercial reuse is permitted under the Japan Public Data License 1.0 (compatible with CC BY 4.0). Official www.jma.go.jp only — not p2pquake.",
        "JMA quake · Public Data License 1.0 (cite JMA)",
        ("www.jma.go.jp",),
        licence_url="https://www.jma.go.jp/jma/info/license.html",
    ),
    "jma_typhoon": SourcePolicy(
        "jma_typhoon", "JMA typhoon", "Public Data License 1.0",
        "Commercial reuse is permitted under the Japan Public Data License 1.0 (compatible with CC BY 4.0). NW Pacific only — not NHC, not JTWC.",
        "JMA typhoon · Public Data License 1.0 (cite JMA)",
        ("www.jma.go.jp",),
        licence_url="https://www.jma.go.jp/jma/info/license.html",
    ),
    "inmet_br": SourcePolicy(
        "inmet_br", "INMET Brazil WIS2 SYNOP", "WMO core unrestricted",
        "Commercial reuse is permitted under WMO Unified Data Policy core data (unrestricted). HTTPS wis2bra.inmet.gov.br only. Brazil in-situ only.",
        "INMET WIS2 SYNOP · WMO core unrestricted (cite INMET)",
        ("wis2bra.inmet.gov.br",),
        licence_url="https://community.wmo.int/en/wmo-unified-data-policy",
    ),
    "chmu_cz": SourcePolicy(
        "chmu_cz", "CHMI / ČHMÚ climate now", "CC BY 4.0",
        "Commercial reuse is permitted with attribution to the Czech Hydrometeorological Institute. Czech in-situ weather only.",
        "CHMI climate now · CC BY 4.0",
        ("opendata.chmi.cz",),
        licence_url="https://creativecommons.org/licenses/by/4.0/",
    ),
    "kma_asos": SourcePolicy(
        "kma_asos", "KMA ASOS", "Public Nuri Type 1",
        "Commercial reuse is permitted under Korea Public Nuri Type 1 (attribution). Korea in-situ ASOS only — AirKorea Type 3 ND is not used.",
        "KMA ASOS · Public Nuri Type 1",
        ("apis.data.go.kr",),
        requires_operator_account=True,
        licence_url="https://www.data.go.kr/en/ugs/selectPublicDataUseGuideView.do",
    ),
    "gfm_observed": SourcePolicy(
        "gfm_observed", "Copernicus GFM observed flood", "CC BY 4.0",
        "Commercial reuse is permitted with attribution to Copernicus EMS / EODC. Observed flood product only — not GloFAS WMS.",
        "Copernicus GFM observed flood · CC BY 4.0",
        ("api.gfm.eodc.eu",),
        requires_operator_account=True,
        licence_url="https://creativecommons.org/licenses/by/4.0/",
    ),
    "smard": SourcePolicy(
        "smard", "SMARD.de (Bundesnetzagentur) day-ahead prices", "CC BY 4.0",
        "Commercial reuse is permitted with attribution to Bundesnetzagentur | SMARD.de. Fifteen bidding zones whose SMARD series are CC BY 4.0 (DE-LU and its coupled neighbours). Zones that Energy-Charts marks 'private and internal use only' (SE1-SE3, FI, EE, LT, LV, ES, IT-South) are deliberately not offered. Wholesale exchange price, not a retail tariff.",
        "Bundesnetzagentur | SMARD.de · CC BY 4.0",
        ("www.smard.de",),
        licence_url="https://www.smard.de/home/nutzungsbedingungen-231266",
    ),
    "copernicus_sentinel2": SourcePolicy(
        "copernicus_sentinel2", "Copernicus Sentinel-2 L2A (Earth Search STAC, AWS Open Data COGs)",
        "Copernicus Sentinel Data Terms (free, full and open)",
        "Commercial use, reproduction and modification are permitted under the Legal Notice on the use of Copernicus Sentinel Data, with the credit 'Contains modified Copernicus Sentinel data <year>'. Earth Search (Element84) indexes the public sentinel-cogs bucket of the AWS Open Data Registry; no key, no account.",
        "Contains modified Copernicus Sentinel data · processed via Element84 Earth Search",
        ("earth-search.aws.element84.com", "sentinel-cogs.s3.us-west-2.amazonaws.com"),
        licence_url="https://sentinels.copernicus.eu/documents/247904/690755/Sentinel_Data_Legal_Notice",
    ),
    "geonames": SourcePolicy(
        "geonames", "GeoNames gazetteer (cities1000, admin1, countryInfo)", "CC BY 4.0",
        "Commercial reuse is permitted with attribution to GeoNames. Used to turn place names into coordinates (gaia.place.resolve@v1 and the place field of weather/air reads); not a sensor and never a reading.",
        "GeoNames (geonames.org) · CC BY 4.0",
        ("download.geonames.org",),
        licence_url="https://creativecommons.org/licenses/by/4.0/",
    ),
}


def require_approved_source(source_id: str) -> SourcePolicy:
    """Return a reviewed policy or fail closed."""
    try:
        return APPROVED_SOURCES[source_id]
    except KeyError:
        raise ValueError(f"source is not approved for a commercial rail: {source_id}") from None


__all__ = ["SourcePolicy", "APPROVED_SOURCES", "require_approved_source"]
