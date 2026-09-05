# GAIA backend — physical-oracle gateway (AIMarket v2 surface + verifier).
# Build from the MONOREPO ROOT so oracle-core is in context:
#     docker build -f gaia/Dockerfile -t gaia-backend .
FROM python:3.11-slim AS base
WORKDIR /app

COPY oracles/core /app/core
COPY gaia /app/gaia

RUN pip install --no-cache-dir -e /app/core -e /app/gaia

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
CMD ["python", "-m", "gaia.main"]
