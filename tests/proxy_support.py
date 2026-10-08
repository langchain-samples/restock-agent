"""Real CLI + application callback; fake cloud proxy, Link and Agent Auth.

The loopback proxy implements documented signed full_request snapshots. It does
not establish real cloud egress, callback routing, or hosted caller identity.
"""

import asyncio
import base64
import hashlib
import json
import time
from types import SimpleNamespace

import httpx
import jwt
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from managed_deepagents._agent_auth import AgentAuth, AgentAuthContext

from restock import link_session
from restock.link_proxy import Callback, Permissions

ORIGIN = "https://restock.example.invalid"
CALLBACK_URL = ORIGIN + "/channels/link_proxy/events"
ISSUER = "https://api.smith.langchain.com"
SANDBOX = "abc12345-0000-4000-8000-000000000000"
ENV = {
    "RESTOCK_LINK_TRANSPORT": "proxy",
    "RESTOCK_PROXY_URL": ORIGIN,
    "RESTOCK_PROXY_SIGNING_KEY": "fixture-key-" * 5,
    "LINK_SESSION_BACKEND": "connection",
}


class ProxyFixture:
    def __init__(self, link, auth, store):
        self.link, self.agent_auth, self.store = link, auth, store
        self.private_key = Ed25519PrivateKey.generate()
        self.permissions = Permissions(
            store,
            signing_key=ENV["RESTOCK_PROXY_SIGNING_KEY"],
            callback_url=CALLBACK_URL,
            workspace_id="fixture-tenant",
            deployment_id="fixture-agent",
        )
        self.callback = Callback(self.permissions, self.key, self.read_session, issuer=ISSUER)
        self.inbound_tokens, self.targets, self.forwarded = [], [], []
        self.failure, self.mutate, self.loop = None, None, None

    def client(self, runtime):
        return AgentAuth(
            AgentAuthContext(
                base_url="https://auth.example.invalid",
                principal_id=runtime.server_info.principal.id,
                agent_id="fixture-agent",
                api_key="fixture-platform-key",
                workspace_id="fixture-tenant",
            ),
            request=self.agent_auth.request,
        )

    async def key(self, kid):
        if kid != "fixture-key":
            raise ValueError()
        return self.private_key.public_key()

    async def read_session(self, grant):
        return self.agent_auth.read(grant["slug"], grant["principal_id"])

    async def context_for(self, runtime):
        self.loop = asyncio.get_running_loop()
        return SimpleNamespace(
            permissions=self.permissions,
            sandbox_id=SANDBOX,
            principal_id=runtime.server_info.principal.id,
            slug="link-session",
        )

    def envelope(self, request):
        query = request.url.query.decode()
        return {
            "host": "api.link.com",
            "port": 443,
            "identity": {
                "tenant_id": "fixture-tenant",
                "sandbox_id": SANDBOX,
                "ls_user_id": "creator-is-not-the-requester",
            },
            "request": {
                "method": request.method,
                "host": "api.link.com",
                "scheme": "https",
                "path": request.url.path,
                **({"query": query} if query else {}),
                "url": "https://api.link.com" + request.url.path + ("?" + query if query else ""),
                "headers": {k: request.headers.get_list(k) for k in request.headers},
                "body_truncated": False,
                "body_base64": base64.b64encode(request.content).decode(),
            },
        }

    def sign(self, raw, **override):
        claims = {
            "iss": ISSUER,
            "sub": "langsmith-sandbox-callback",
            # LangSmith's Go signer emits jwt.ClaimStrings as a JSON array,
            # including when there is exactly one callback audience.
            "aud": [CALLBACK_URL],
            "exp": int(time.time()) + 300,
            "body_sha256": hashlib.sha256(raw).hexdigest(),
        }
        claims.update(override)
        return jwt.encode(
            claims, self.private_key, algorithm="EdDSA", headers={"kid": "fixture-key"}
        )

    def handler(self, request):
        self.targets.append((request.method, request.url.path, request.url.query.decode()))
        self.inbound_tokens.append(request.headers.get("Authorization"))
        event = self.envelope(request)
        if self.mutate:
            self.mutate(event)
        raw = json.dumps(event).encode()
        try:
            if self.failure == "callback":
                raise ValueError()
            result = asyncio.run_coroutine_threadsafe(
                self.callback.resolve(raw, self.sign(raw)), self.loop
            ).result(timeout=15)
        except Exception:
            return httpx.Response(502, json={"error": {"message": "callback resolution failed"}})
        self.forwarded.append((request.method, request.url.path))
        if self.failure == "provider":
            return httpx.Response(502, json={"error": {"message": "fixture unavailable"}})
        headers = dict(request.headers)
        for name, value in result["headers"].items():
            headers.pop(name.lower(), None)
            headers[name] = value
        return self.link.handler(
            httpx.Request(request.method, request.url, headers=headers, content=request.content)
        )

    def session_store(self):
        return link_session.ConnectionSessions("link-session", agent_auth=self.client)
