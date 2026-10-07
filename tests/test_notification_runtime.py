import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import tests  # noqa: F401
import httpx

from restock.notification_runtime import ServerStore, notification_secret, post_update
from tests.test_notifications import NAMESPACE, ROUTE, SECRET, TARGET


class RuntimeAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_store_adapter_missing_item_and_exact_namespace(self):
        client = SimpleNamespace(
            get_item=AsyncMock(return_value={"value": {"test": 1}}), put_item=AsyncMock()
        )
        store = ServerStore(client)
        self.assertEqual((await store.aget(NAMESPACE, "route-test")).value, {"test": 1})
        client.get_item.assert_awaited_once_with(NAMESPACE, "route-test")
        await store.aput(NAMESPACE, "route-test", {"test": 2})
        client.put_item.assert_awaited_once_with(NAMESPACE, "route-test", {"test": 2})
        for status in (404, 403, 500):
            client.get_item.side_effect = httpx.HTTPStatusError(
                "fixture",
                request=httpx.Request("GET", "https://example.test"),
                response=httpx.Response(status),
            )
            if status == 404:
                self.assertIsNone(await store.aget(NAMESPACE, "missing"))
            else:
                with self.assertRaises(httpx.HTTPStatusError):
                    await store.aget(NAMESPACE, "missing")

    async def test_secret_adapter_reads_only_signed_owner_and_notification_slot(self):
        config = SimpleNamespace(
            base_url="https://auth.example.test",
            deployment_id="fixture-agent",
            api_key="fixture-key",
            workspace_id="fixture-workspace",
        )
        client = Mock()
        client.read_user_secret.return_value = SECRET
        with (
            patch(
                "managed_deepagents._connections._resolve_agent_auth_config", return_value=config
            ),
            patch("managed_deepagents._agent_auth.AgentAuth", return_value=client) as factory,
        ):
            self.assertEqual(await notification_secret(ROUTE), SECRET)
            context = factory.call_args.args[0]
            self.assertEqual(context.principal_id, "original-owner")
            self.assertEqual(context.agent_id, "fixture-agent")
        client.read_user_secret.assert_called_once_with("restock-updates-internal-fixture")

    async def test_slack_adapter_uses_bound_target_and_stable_action(self):
        gateway = SimpleNamespace(post_action=AsyncMock(return_value={"id": "fixture-post"}))
        with patch(
            "managed_deepagents._channels.trigger.client.create_trigger_client",
            return_value=gateway,
        ):
            await post_update(ROUTE, "Your order was placed.", "stable-action")
        destination, action = gateway.post_action.call_args.args
        self.assertEqual(destination, TARGET["channel_id"])
        self.assertEqual(action["address"], TARGET["address"])
        self.assertEqual(action["source_thread_key"], TARGET["source_thread_key"])
        self.assertEqual(action["action_id"], "stable-action")
