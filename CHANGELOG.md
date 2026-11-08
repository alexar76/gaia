# Changelog

Notable changes to `aimarket-gaia-gateway`. Earlier releases: the git history.

## 0.3.0 — 2026-11-08

### Added
- Twenty-four live public sources (`gaia/devices/live_*.py`, 2026-11-03 and 2026-11-04):
  - **Rivers and groundwater:** Slovenian ARSO and Czech CHMI river levels; NOAA river forecasts and Environment
    Agency coastal water levels; USGS and Hub'Eau groundwater wells; USGS flood-impact thresholds checked against fresh
    gage data.
  - **Sea and coast:** NOAA AOML drifting buoys; FMI Baltic waves; GeoNet coastal water height; Kartverket observed sea
    level; SMHI coastal oceanography; NOAA bridge air gap; open wave-buoy, pollen and volcano stations.
  - **Air, snow and space weather:** USDA SNOTEL snow depth and water equivalent; Germany's BfS gamma dose; ECCC
    wildfire-smoke PM2.5; NOAA SWPC D-RAP HF absorption.
  - **Roads, rail and grid:** Entur live train positions; NDW road notices and closures; Fintraffic road notices and NOAA
    G-AIRMET forecasts; Toronto road restrictions and bike-share stations; UK Power Networks fault reports; DOT-NL
    charge points; British Columbia wildfire observations.
  - **Hazards:** NOAA storm reports, ash advisories and Geoscience Australia hotspots.
- Four new capabilities, and wider tide, river and rail products. All 8,100 signed D-RAP cells stay available next to
  a light map overview.

### Fixed
- **The wheel carries the city and extra-sensor catalogs.** `gaia.devices.om_mesh`, `sc_mesh` and `extra_sensors`
  read `config/*.yaml` from the repository layout only. In a pip install that path points beside `site-packages`,
  so the two mesh modules failed on import and the extra sensors were silently empty. This was the case in 0.2.2
  and earlier. The wheel now holds the three catalogs in `gaia/_config/`, taken from `gaia/config/` at build time
  (hatch `force-include`), and the modules read that copy when the repository's is absent.
