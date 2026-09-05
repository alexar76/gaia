"""Generic AIMarket hub federation client (shared by every oracle)."""

from __future__ import annotations

from typing import Any, Optional

import httpx

from oracle_core.protocol import Protocol


def announce_canonical(hub_url: str, well_known_url: str, capabilities_count: int) -> str:
    return f"hub_url:{hub_url}|well_known_url:{well_known_url}|capabilities_count:{capabilities_count}"


class HubClient:
    def __init__(self, proto: Protocol, hub_url: str):
        self.proto = proto
        self.hub_url = hub_url.rstrip("/")

    def self_verify_manifest(self) -> dict[str, Any]:
        man = self.proto.manifest()
        return {
            "ok": self.proto.signer.verify_manifest_signature(man),
            "capabilities": [t["capability_id"] for t in man["tools"]],
            "signer_public_key": self.proto.signer.public_key_b64,
        }

    def announce_body(self) -> dict[str, Any]:
        base = self.proto.spec.public_url.rstrip("/")
        wk = f"{base}/.well-known/ai-market.json"
        count = len(self.proto.spec.capabilities)
        canonical = announce_canonical(base, wk, count)
        return {
            "hub_url": base,
            "well_known_url": wk,
            "capabilities_count": count,
            "hub_name": self.proto.spec.name,
            "signer_public_key": self.proto.signer.public_key_b64,
            "signature": {"algorithm": "ed25519", "value": self.proto.signer.sign_canonical(canonical)},
        }

    async def hub_info(self) -> dict[str, Any]:
        try:
            async with httpx.AsyncClient(timeout=15.0) as c:
                r = await c.get(f"{self.hub_url}/.well-known/ai-market.json")
                if r.status_code == 200:
                    d = r.json()
                    return {"reachable": True, "name": d.get("name"), "hub_version": d.get("hub_version")}
                return {"reachable": False, "status": r.status_code}
        except httpx.HTTPError as exc:
            return {"reachable": False, "error": str(exc)}

    async def announce(self, admin_token: str = "") -> dict[str, Any]:
        params = {"authorization": admin_token} if admin_token else {}
        try:
            async with httpx.AsyncClient(timeout=20.0) as c:
                r = await c.post(f"{self.hub_url}/ai-market/v2/federation/announce", params=params, json=self.announce_body())
                body: Any
                try:
                    body = r.json()
                except ValueError:
                    body = r.text
                return {"registered": r.status_code == 200, "status": r.status_code, "response": body}
        except httpx.HTTPError as exc:
            return {"registered": False, "error": str(exc)}

    async def open_channel(self, deposit_usd: float = 1.0, token: str = "USDT", chain: str = "base") -> dict[str, Any]:
        try:
            async with httpx.AsyncClient(timeout=20.0) as c:
                r = await c.post(f"{self.hub_url}/ai-market/v2/channel/open", json={"deposit_usd": deposit_usd, "token": token, "chain": chain})
                return {"status": r.status_code, "response": _json_or_text(r)}
        except httpx.HTTPError as exc:
            return {"error": str(exc)}

    async def search(self, intent: str, budget: float = 0.05, limit: int = 10) -> dict[str, Any]:
        try:
            async with httpx.AsyncClient(timeout=20.0) as c:
                r = await c.get(f"{self.hub_url}/ai-market/v2/search", params={"intent": intent, "budget": budget, "limit": limit})
                return {"status": r.status_code, "response": _json_or_text(r)}
        except httpx.HTTPError as exc:
            return {"error": str(exc)}


def _json_or_text(r: "httpx.Response") -> Any:
    try:
        return r.json()
    except ValueError:
        return r.text
