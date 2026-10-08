"""A fake Link API and token provider for offline tests.

The real SDK, tool code and (in compiled runs) the real MDA accessor execute;
only HTTP responses are replaced.
"""

from __future__ import annotations

import json
import time
from urllib.parse import parse_qs

import httpx


class FakeLink:
    """In-memory Link API served through an httpx.MockTransport."""

    def __init__(self, *, token: str = "fake-access-token", payment_methods: list | None = None):
        self.tokens = {token}
        self.expired_tokens: set[str] = set()
        self.calls: list[tuple[str, str, str]] = []
        self.requests: dict[str, dict] = {}
        self.counter = 0
        self.user = {"name": "Test User", "email": "test@example.invalid"}
        self.payment_methods = (
            payment_methods
            if payment_methods is not None
            else [
                {
                    "id": "pm_default",
                    "type": "card",
                    "is_default": True,
                    "name": "Visa",
                    "card_details": {
                        "brand": "visa",
                        "last4": "4242",
                        "exp_month": 12,
                        "exp_year": 2030,
                    },
                }
            ]
        )

    # Test controls -----------------------------------------------------
    def approve(self, request_id: str) -> None:
        self.requests[request_id]["status"] = "approved"

    def deny(self, request_id: str) -> None:
        self.requests[request_id]["status"] = "denied"

    def expire_token(self, token: str, replacement: str) -> None:
        self.expired_tokens.add(token)
        self.tokens.add(replacement)

    # HTTP handling -----------------------------------------------------
    def handler(self, request: httpx.Request) -> httpx.Response:
        token = request.headers.get("Authorization", "").removeprefix("Bearer ")
        self.calls.append((request.method, request.url.path, token))
        if token in self.expired_tokens or token not in self.tokens:
            return httpx.Response(
                401, json={"error": {"message": "invalid token"}}, request=request
            )
        path = request.url.path
        if request.method == "GET" and path == "/userinfo":
            return httpx.Response(200, json=self.user, request=request)
        if request.method == "GET" and path == "/payment-details":
            return httpx.Response(
                200, json={"payment_details": self.payment_methods}, request=request
            )
        if request.method == "POST" and path == "/spend_requests":
            body = json.loads(request.content)
            if len(body.get("context", "")) < 100:
                return httpx.Response(
                    400,
                    json={"error": {"code": "context_too_short", "message": "context too short"}},
                    request=request,
                )
            self.counter += 1
            request_id = f"lsrq_fake{self.counter}"
            record = {
                "id": request_id,
                "status": "pending_approval",
                "amount": body.get("amount"),
                "currency": body.get("currency"),
                "merchant_name": body.get("merchant_name"),
                "merchant_url": body.get("merchant_url"),
                "context": body["context"],
                "credential_type": body.get("credential_type"),
                "metadata": body.get("metadata", {}),
                "network_id": body.get("network_id"),
                "idempotency_key": body.get("idempotency_key"),
                "test": body.get("test"),
                "approval_url": f"https://app.link.com/approve/{request_id}",
                "expires_at": int(time.time()) + 900,
                "created_at": "2026-09-23T00:00:00Z",
                "updated_at": "2026-09-23T00:00:00Z",
            }
            self.requests[request_id] = record
            return httpx.Response(200, json=self._public(record), request=request)
        if request.method == "GET" and path == "/spend_requests":
            return httpx.Response(
                200,
                json={"data": [self._public(r) for r in self.requests.values()]},
                request=request,
            )
        if path.startswith("/spend_requests/"):
            parts = path.split("/")
            request_id = parts[2]
            record = self.requests.get(request_id)
            if record is None:
                return httpx.Response(
                    404, json={"error": {"message": "not found"}}, request=request
                )
            if request.method == "POST" and parts[-1] == "cancel":
                if record["status"] in {"pending_approval", "created", "approved"}:
                    record["status"] = "canceled"
                return httpx.Response(200, json=self._public(record), request=request)
            if request.method == "GET":
                include = parse_qs(request.url.query.decode()).get("include", [])
                include_card = any("card" in value for value in include)
                if (
                    "shared_payment_token" in include
                    and record["status"] == "approved"
                    and not record.get("test")
                ):
                    return httpx.Response(
                        200,
                        json={
                            **self._public(record),
                            "shared_payment_token": {"id": "spt_private_fixture"},
                        },
                        request=request,
                    )
                return httpx.Response(
                    200, json=self._public(record, include_card=include_card), request=request
                )
        return httpx.Response(404, json={"error": {"message": f"no route {path}"}}, request=request)

    def _public(self, record: dict, *, include_card: bool = False) -> dict:
        view = {k: v for k, v in record.items() if k != "test"}
        if record["status"] == "approved":
            view["card_brand"] = "visa"
            view["card_last4"] = "4242"
            if include_card and not record.get("test"):
                view["card"] = {
                    "id": "card_fake",
                    "brand": "visa",
                    "exp_month": 12,
                    "exp_year": 2030,
                    "number": "4242424242424242",
                    "cvc": "123",
                    "valid_until": time.strftime(
                        "%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + 600)
                    ),
                }
        return view

    def http_client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handler))


class FakeTokens:
    """A stand-in for MDA's connection lookup that records every call.

    ``sequence`` supplies successive tokens; the last one repeats.
    """

    def __init__(self, token: str = "fake-access-token", *, sequence: list[str] | None = None):
        self.sequence = list(sequence) if sequence else [token]
        self.calls = 0
        self.error: Exception | None = None

    @property
    def token(self) -> str:
        return self.sequence[-1]

    async def __call__(self) -> str:
        self.calls += 1
        if self.error is not None:
            raise self.error
        index = min(self.calls - 1, len(self.sequence) - 1)
        return self.sequence[index]


def serve_http(handler):
    """Serve an httpx-style handler on a loopback port; returns (server, base_url)."""
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def _handle(self):
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length else b""
            request = httpx.Request(
                self.command,
                f"https://fake.invalid{self.path}",
                headers=dict(self.headers),
                content=body,
            )
            response = handler(request)
            self.send_response(response.status_code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(response.content)))
            for name, value in response.headers.multi_items():
                if name.lower() not in {"content-type", "content-length", "transfer-encoding"}:
                    self.send_header(name, value)
            self.end_headers()
            self.wfile.write(response.content)

        do_GET = do_POST = do_PATCH = do_DELETE = _handle

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_port}"


class FakeLinkAuth:
    """Link's device-authorization endpoints (login.link.com) for offline CLI tests.

    ``approve()`` lets the pending device code succeed; ``expire_current()`` makes
    the fake API reject the current access token so the CLI refreshes and rotates.
    """

    def __init__(self, link: FakeLink):
        self.link = link
        self.approved = False
        self.sequence = 0
        self.calls: list[tuple[str, str]] = []
        self.current_access: str | None = None
        self.current_refresh: str | None = None

    def _issue(self) -> dict:
        self.sequence += 1
        if self.current_access:
            self.link.expired_tokens.add(self.current_access)
        self.current_access = f"liwltoken_fake{self.sequence}"
        self.current_refresh = f"liwlrefresh_fake{self.sequence}"
        self.link.tokens.add(self.current_access)
        return {
            "access_token": self.current_access,
            "refresh_token": self.current_refresh,
            "token_type": "Bearer",
            "expires_in": 3600,
            "scope": "userinfo:read payment_methods.agentic",
        }

    def approve(self) -> None:
        self.approved = True

    def expire_current(self) -> None:
        if self.current_access:
            self.link.expired_tokens.add(self.current_access)

    def handler(self, request: httpx.Request) -> httpx.Response:
        form = parse_qs(request.content.decode())
        grant = form.get("grant_type", [""])[0]
        self.calls.append((request.url.path, grant))
        if request.url.path == "/device/code":
            return httpx.Response(
                200,
                json={
                    "device_code": "device_fake",
                    "user_code": "FAKE-CODE",
                    "verification_uri": "https://app.link.com/device",
                    "verification_uri_complete": "https://app.link.com/device?code=FAKE-CODE",
                    "expires_in": 600,
                    "interval": 1,
                },
                request=request,
            )
        if request.url.path == "/device/token" and grant.endswith("device_code"):
            if not self.approved:
                return httpx.Response(400, json={"error": "authorization_pending"}, request=request)
            return httpx.Response(200, json=self._issue(), request=request)
        if request.url.path == "/device/token" and grant == "refresh_token":
            if form.get("refresh_token", [""])[0] != self.current_refresh:
                return httpx.Response(400, json={"error": "invalid_grant"}, request=request)
            return httpx.Response(200, json=self._issue(), request=request)
        if request.url.path == "/device/revoke":
            return httpx.Response(200, json={}, request=request)
        return httpx.Response(404, json={"error": "no route"}, request=request)


class FakeAgentAuthServer:
    """Agent Auth's connections/credentials endpoints for user-owned secrets, in memory.

    Drives MDA's real ``AgentAuth`` client in tests: pass ``request`` as its transport.
    """

    def __init__(self) -> None:
        self.connections: dict[str, dict] = {}
        self.calls: list[tuple[str, str]] = []
        self.counter = 0

    def read(self, slug: str, owner: str) -> str | None:
        credential = self.connections.get(slug, {}).get("credentials", {}).get(owner)
        return credential["value"] if credential else None

    def request(self, method: str, path: str, *, body: dict | None = None) -> tuple[int, dict]:
        from urllib.parse import parse_qs, urlsplit

        parts = urlsplit(path)
        query = {k: v[0] for k, v in parse_qs(parts.query).items()}
        route = parts.path
        self.calls.append((method, route))
        if method == "POST" and route == "/v1/agent-auth/connections":
            slug = body["slug"]
            credential = body["credential"]
            assert credential["kind"] == "secret" and credential["owner_type"] == "user"
            row = self.connections.setdefault(
                slug, {"id": f"conn-{slug}", "kind": "secret", "credentials": {}}
            )
            if row["kind"] != "secret":
                return 409, {"detail": "incompatible connection kind"}
            if credential["owner_id"] in row["credentials"]:
                return 409, {"detail": "credential exists"}
            self.counter += 1
            row["credentials"][credential["owner_id"]] = {
                "credential_id": f"cred-{self.counter}",
                "value": credential["value"],
            }
            return 201, {"id": row["id"], "slug": slug}
        if method == "GET" and route == "/v1/agent-auth/connections":
            return 200, {
                "items": [
                    {
                        "connection_id": row["id"],
                        "slug": slug,
                        "kind": row["kind"],
                        "owner_policy": "user",
                    }
                    for slug, row in self.connections.items()
                ]
            }
        if method == "GET" and route.startswith("/v1/agent-auth/connections/"):
            connection_id = route.rsplit("/", 1)[1]
            row = next((r for r in self.connections.values() if r["id"] == connection_id), None)
            if row is None:
                return 404, {}
            credential = row["credentials"].get(query.get("credential_owner_id", ""))
            view: dict = {"kind": row["kind"]}
            if credential:
                view["credential_id"] = credential["credential_id"]
            return 200, {"id": row["id"], "credential": view}
        if route.startswith("/v1/agent-auth/credentials/"):
            credential_id = route.split("/")[4]
            for row in self.connections.values():
                for credential in row["credentials"].values():
                    if credential["credential_id"] == credential_id:
                        if method == "GET" and route.endswith("/secret"):
                            return 200, {"secret": credential["value"]}
                        if method == "PATCH":
                            credential["value"] = body["secret"]
                            return 200, {}
            return 404, {}
        raise AssertionError(f"unexpected Agent Auth call {method} {route}")
