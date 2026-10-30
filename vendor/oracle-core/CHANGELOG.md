# Changelog

All notable changes to this project are documented in this file. Earlier releases: the git history.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.6.4] - 2026-10-29

### Changed

- The `price_gate` module docstring names SYMMARTYS (`symmartys.vat.check@v1`), formerly EMPORION. No behaviour change.

## [0.6.3] - 2026-10-28

### Security

- `X-Real-IP` / `X-Forwarded-For` were taken from any private, loopback or link-local peer, so a neighbour on the same docker network or LAN could name its own address and get a fresh rate-limit bucket per request. They now count only from `ORACLE_TRUSTED_PROXIES`: addresses, CIDRs, host names (looked up again every minute) and `gateway` (the container's default route, i.e. the host's nginx for a published port); the default is loopback only. Reported by Vulnetix (GCVE-110-VULN-PYPI-2026-000637). `peer_is_local_proxy` keeps its name. The oracles' compose file trusts `gateway`.
