# GAIA backend — physical-oracle gateway (AIMarket v2 surface + verifier).
# Build from the MONOREPO ROOT so oracle-core is in context:
#     docker build -f gaia/Dockerfile -t gaia-backend .
FROM python:3.11-slim
WORKDIR /app

COPY oracles/core /app/core
COPY gaia /app/gaia

RUN pip install --no-cache-dir -e /app/core -e /app/gaia

EXPOSE 9320
CMD ["python", "-m", "gaia.main"]
