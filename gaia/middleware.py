"""Provider response signature — the hub's supply-security handshake.

The AIMarket hub (supply_security.verify_provider_response) accepts a provider
response only when ``X-Provider-Signature`` verifies over the REQUEST-BOUND
canonical:

    {"capability_id": …, "product_id": …, "input_sha256": sha256(sorted-JSON of
     the request input), "result": <what the hub extracts>}   (sorted, compact)

The hub extracts ``result`` as ``payload.get("result", payload.get("output",
payload))`` — this middleware mirrors that exactly so the signed bytes match
what the hub verifies. Implemented as pure ASGI (BaseHTTPMiddleware cannot
safely read the request body and replay it downstream).
"""

from __future__ import annotations

import hashlib
import json
from typing import Any


def bound_response_canonical(capability_id: str, product_id: str,
                             input_payload: Any, result: Any) -> str:
    input_json = json.dumps(input_payload or {}, sort_keys=True,
                            separators=(",", ":"), ensure_ascii=False)
    return json.dumps(
        {
            "capability_id": capability_id or "",
            "product_id": product_id or "",
            "input_sha256": hashlib.sha256(input_json.encode("utf-8")).hexdigest(),
            "result": result,
        },
        sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    )


class ProviderSignatureMiddleware:
    """Sign successful /ai-market/v2/invoke responses with the gateway key."""

    def __init__(self, app, signer):
        self.app = app
        self.signer = signer

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope.get("path") != "/ai-market/v2/invoke":
            return await self.app(scope, receive, send)

        # Buffer the request body (needed for the bound canonical), then replay it.
        body = b""
        while True:
            message = await receive()
            body += message.get("body", b"")
            if not message.get("more_body", False):
                break

        async def replay():
            return {"type": "http.request", "body": body, "more_body": False}

        # Buffer the response so the signature can be computed over the final JSON.
        captured: dict[str, Any] = {"status": 500, "headers": []}
        chunks: list[bytes] = []

        async def capture(message):
            if message["type"] == "http.response.start":
                captured["status"] = message["status"]
                captured["headers"] = list(message.get("headers", []))
            elif message["type"] == "http.response.body":
                chunks.append(message.get("body", b""))

        await self.app(scope, replay, capture)
        payload = b"".join(chunks)
        headers = captured["headers"]

        if captured["status"] == 200:
            try:
                data = json.loads(payload)
                req = json.loads(body or b"{}")
                # Mirror the hub's extraction rule exactly.
                result = data.get("result", data.get("output", data))
                canonical = bound_response_canonical(
                    str(req.get("capability_id", "")),
                    str(req.get("product_id", "")),
                    req.get("input"),
                    result,
                )
                sig = self.signer.sign_canonical(canonical)
                headers = [
                    (k, v) for (k, v) in headers
                    if k.lower() != b"x-provider-signature"
                ] + [(b"x-provider-signature", sig.encode())]
            except (ValueError, TypeError):
                pass  # unsigned response — the hub decides per its own policy

        await send({"type": "http.response.start",
                    "status": captured["status"], "headers": headers})
        await send({"type": "http.response.body", "body": payload, "more_body": False})
