"""Pinned MDA 0.8.1 adapters for order notifications outside a model run.

Only app-signed routes authorize a lookup of the original user's notification
secret. Neither this module nor the receiver accesses a wallet or submits orders.
The MDA internal adapters below need revalidation when upgrading the SDK.
"""

import asyncio
from types import SimpleNamespace

import httpx

from restock.config import RestockError
from restock.notifications import Receiver, configuration


class ServerStore:
    def __init__(self, client):
        self.client = client

    async def aget(self, namespace, key):
        try:
            item = await self.client.get_item(namespace, key)
        except httpx.HTTPStatusError as error:
            if error.response.status_code == 404:
                return None
            raise
        return SimpleNamespace(value=item["value"]) if item else None

    async def aput(self, namespace, key, value):
        await self.client.put_item(namespace, key, value)


async def notification_secret(route):
    from managed_deepagents import _connections
    from managed_deepagents._agent_auth import AgentAuth, AgentAuthContext

    config = _connections._resolve_agent_auth_config(None)
    client = AgentAuth(
        AgentAuthContext(
            base_url=config.base_url,
            principal_id=route["principal_id"],
            agent_id=config.deployment_id,
            api_key=config.api_key,
            workspace_id=config.workspace_id,
        )
    )
    # The caller of this function has verified the entire route's MAC.
    return await asyncio.to_thread(client.read_user_secret, route["secret_slug"])


async def post_update(route, text, action_id):
    from managed_deepagents._channels.trigger.client import create_trigger_client
    from managed_deepagents._channels.trigger.transport import TriggerTransport

    client = create_trigger_client()
    if client is None:
        raise RestockError("order_updates_need_hosted_slack")
    await TriggerTransport(client, action_id=action_id).post(
        message={"type": "content", "content": text}, target=route["target"]
    )


_receiver = None


def receiver():
    global _receiver
    if _receiver is None:
        from langgraph_sdk import get_client
        from managed_deepagents._loopback import resolve_loopback_api_key, resolve_loopback_api_url

        url, key = configuration()
        store = get_client(url=resolve_loopback_api_url(), api_key=resolve_loopback_api_key()).store
        _receiver = Receiver(
            ServerStore(store),
            notification_secret,
            post_update,
            signing_key=key,
            callback_url=url,
        )
    return _receiver
