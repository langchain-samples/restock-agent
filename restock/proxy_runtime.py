"""Pinned MDA 0.8.1 adapters for proxy identity, private storage and JWKS.

The callback does not execute an agent or infer a user from the sandbox creator.
Only a signed operation record created by an authored tool selects the principal.
"""

import asyncio
import os
import time
from types import SimpleNamespace
from urllib.parse import quote
from uuid import UUID

import httpx
import jwt

from restock import link_session
from restock.config import RestockError
from restock.link_proxy import Callback, Permissions
from restock.notification_runtime import ServerStore
from restock.proxy_config import configuration, public_origin


def issuer():
    return public_origin(
        os.environ.get("LANGSMITH_ENDPOINT")
        or os.environ.get("LANGCHAIN_ENDPOINT")
        or "https://api.smith.langchain.com"
    )


class SigningKeys:
    def __init__(self, origin):
        self.url = origin + "/.well-known/jwks.json"
        self.keys, self.expires = {}, 0
        self.lock = asyncio.Lock()

    async def __call__(self, kid):
        if not kid or len(kid) > 128:
            raise RestockError("link_proxy_callback_denied")
        async with self.lock:
            if time.monotonic() >= self.expires:
                async with httpx.AsyncClient(timeout=10, follow_redirects=False) as client:
                    response = await client.get(self.url)
                    response.raise_for_status()
                    if len(response.content) > 65536:
                        raise ValueError()
                    data = response.json()
                keys = {}
                for value in data["keys"]:
                    if (
                        value.get("kty") == "OKP"
                        and value.get("crv") == "Ed25519"
                        and value.get("use", "sig") == "sig"
                        and value.get("alg", "EdDSA") == "EdDSA"
                    ):
                        if (
                            not isinstance(value.get("kid"), str)
                            or value["kid"] in keys
                            or "d" in value
                        ):
                            raise ValueError()
                        keys[value["kid"]] = jwt.PyJWK.from_dict(value, algorithm="EdDSA").key
                self.keys, self.expires = keys, time.monotonic() + 60
            # A new unknown kid is denied until the short cache expires. It cannot
            # force an unbounded JWKS fetch or choose another key origin.
            if kid not in self.keys:
                raise RestockError("link_proxy_callback_denied")
            return self.keys[kid]


async def context_for(runtime):
    url, key = configuration()
    if os.environ.get("LINK_SESSION_BACKEND", "connection") != "connection":
        raise RestockError("link_proxy_requires_connections")
    sessions = link_session.sessions
    client = sessions._client(runtime)
    store = getattr(runtime, "store", None)
    backend = getattr(runtime, "backend", None)
    if backend is None or store is None:
        raise RestockError("link_proxy_identity_unavailable")
    try:
        # Forces lazy provisioning and MDA's own template synchronization first.
        result = await backend.aexecute("true", timeout=20)
        if result.exit_code != 0:
            raise ValueError()
        name = backend.id  # MDA exposes a name here, not a sandbox UUID.
        if not name or name.startswith("pending:"):
            raise ValueError()
        headers = {"x-api-key": client.context.api_key, "x-tenant-id": client.context.workspace_id}
        async with httpx.AsyncClient(timeout=20, follow_redirects=False) as http:
            response = await http.get(
                issuer() + "/v2/sandboxes/boxes/" + quote(name, safe=""), headers=headers
            )
            response.raise_for_status()
            box = response.json()
        if box["name"] != name:
            raise ValueError()
        sandbox_id = str(UUID(box["id"]))
    except Exception:
        raise RestockError("link_proxy_identity_unavailable") from None
    permissions = Permissions(
        store,
        signing_key=key,
        callback_url=url,
        deployment_id=client.context.agent_id,
        workspace_id=client.context.workspace_id,
    )
    return SimpleNamespace(
        permissions=permissions,
        sandbox_id=sandbox_id,
        principal_id=client.context.principal_id,
        slug=sessions.slug,
    )


async def read_session(grant):
    from managed_deepagents import _connections
    from managed_deepagents._agent_auth import AgentAuth, AgentAuthContext

    config = _connections._resolve_agent_auth_config(None)
    if (
        config.deployment_id != grant["deployment_id"]
        or config.workspace_id != grant["workspace_id"]
    ):
        raise RestockError("link_proxy_identity_unavailable")
    client = AgentAuth(
        AgentAuthContext(
            base_url=config.base_url,
            principal_id=grant["principal_id"],
            agent_id=config.deployment_id,
            api_key=config.api_key,
            workspace_id=config.workspace_id,
        )
    )
    return await asyncio.to_thread(client.read_user_secret, grant["slug"])


_receiver = None


def receiver():
    global _receiver
    if _receiver is None:
        from langgraph_sdk import get_client
        from managed_deepagents import _connections
        from managed_deepagents._loopback import resolve_loopback_api_key, resolve_loopback_api_url

        url, key = configuration()
        config = _connections._resolve_agent_auth_config(None)
        client = get_client(url=resolve_loopback_api_url(), api_key=resolve_loopback_api_key())
        permissions = Permissions(
            ServerStore(client.store),
            signing_key=key,
            callback_url=url,
            deployment_id=config.deployment_id,
            workspace_id=config.workspace_id,
        )
        _receiver = Callback(permissions, SigningKeys(issuer()), read_session, issuer=issuer())
    return _receiver
