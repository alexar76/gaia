# GAIA backend — physical-oracle gateway (AIMarket v2 surface + verifier).
# Build from the MONOREPO ROOT so oracle-core is in context:
#     docker build -f gaia/Dockerfile -t gaia-backend .
# The GeoNames gazetteer behind gaia.place.resolve@v1 and "City, State" reads (CC BY 4.0).
# Its own stage, built from the two files it depends on, so an ordinary GAIA code change
# reuses the cached layer instead of fetching GeoNames again; raise GAZETTEER_EPOCH to
# refresh the data. A failed fetch fails the build: shipping a GAIA that silently knows
# only its relayed cities is worse than a red deploy.
FROM python:3.11-slim AS gazetteer
ARG GAZETTEER_EPOCH=2026-10
WORKDIR /g
COPY gaia/gaia/gazetteer.py gaia/gaia/country_names.json /g/
RUN pip install --no-cache-dir "httpx>=0.27" \
    && echo "gazetteer epoch ${GAZETTEER_EPOCH}" \
    && python /g/gazetteer.py build --out /g/gazetteer.sqlite --keep-dir /tmp/geonames \
    && rm -rf /tmp/geonames

FROM python:3.11-slim AS base
WORKDIR /app

COPY oracles/core /app/core
COPY gaia /app/gaia

RUN pip install --no-cache-dir -e "/app/core[pqc,postgres]" -e "/app/gaia[mqtt]"

COPY --from=gazetteer /g/gazetteer.sqlite /app/gazetteer/gazetteer.sqlite
ENV GAIA_GAZETTEER_PATH=/app/gazetteer/gazetteer.sqlite

# A stage the self-healing loop builds to run GAIA's OWN tests against a candidate image
# before anything is promoted. It is NOT the default target, so pytest never ships to
# production — `docker build .` still yields the runtime stage below.
#
# Why this exists: the loop's only gate used to be one MOMUS probe re-run. A probe asserts
# the single behaviour it was written for, so a patch could satisfy it, break the rest of
# the component, and pass. The probe is a regression test for one finding; it was never a
# test suite, and asking it to be both is how a green deploy ships a broken build.
FROM base AS test
RUN pip install --no-cache-dir pytest pytest-asyncio
CMD ["python", "-m", "pytest", "/app/gaia/tests", "-q"]

FROM base AS runtime
EXPOSE 9320
# Not root: the public GAIA backend (signing keys in /data). uid 10001 owns only what it writes (/data).
RUN mkdir -p /data && chown -R 10001:10001 /data
ENV HOME=/tmp \
    ORACLE_CALLS=1 \
    ORACLE_DATA_DIR=/data
USER 10001:10001

CMD ["python", "-m", "gaia.main"]
