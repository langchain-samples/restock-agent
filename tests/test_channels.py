import unittest
from unittest.mock import AsyncMock, patch

import tests  # noqa: F401
from managed_deepagents._channels.runtime import create_runtime_channel

from restock.channel import before_approval_wait
from tests.support import make_service, prepared, runtime
from tools.restock import call


class SlackTests(unittest.IsolatedAsyncioTestCase):
    async def pending(self, mode="link-test"):
        rt = runtime()
        transport = AsyncMock()
        transport.post.return_value = {"id": "fixture-message"}
        rt.channel = create_runtime_channel(
            name="slack",
            provider="slack",
            event={"type": "message"},
            target={"conversation": "trusted-current-thread"},
            transport=transport,
        )
        service = make_service(mode, rt)
        oid = await prepared(service)
        await service.request_payment(oid)
        return rt, transport, service, oid

    async def test_link_url_posts_to_bound_conversation_before_wait(self):
        rt, transport, service, oid = await self.pending()

        async def wait(order_id):
            transport.post.assert_awaited_once()
            self.assertEqual(order_id, oid)
            return {"status": "awaiting_link_approval"}

        with (
            patch("tools.restock.service_for", return_value=service),
            patch.object(service, "wait", side_effect=wait),
        ):
            await call(rt, "wait", oid)
        posted = transport.post.call_args.kwargs
        self.assertEqual(posted["target"], {"conversation": "trusted-current-thread"})
        text = posted["message"]["content"]
        self.assertIn("https://app.link.com/approve/1", text)
        self.assertIn("$25.00", text)
        self.assertIn("TEST", text)
        self.assertNotIn("shipping_address", text)
        self.assertEqual(service.wallet.created, 1)
        self.assertEqual(service.wallet.tokens, 0)

    async def test_repeated_wait_reuses_notification_and_payment_request(self):
        rt, transport, service, oid = await self.pending()
        await before_approval_wait(rt, service, oid)
        await before_approval_wait(rt, service, oid)
        transport.post.assert_awaited_once()
        self.assertEqual(service.wallet.created, 1)

    async def test_failed_delivery_returns_same_url_without_polling_or_leaking_errors(self):
        rt, transport, service, oid = await self.pending()
        transport.post.side_effect = RuntimeError("synthetic-private-provider-data")
        with (
            patch("tools.restock.service_for", return_value=service),
            patch.object(service, "wait", new_callable=AsyncMock) as wait,
        ):
            result = await call(rt, "wait", oid)
        wait.assert_not_awaited()
        self.assertEqual(result["approval_delivery"], "reply_required")
        self.assertEqual(result["approval_url"], "https://app.link.com/approve/1")
        self.assertNotIn("synthetic-private-provider-data", str(result))
        self.assertEqual(service.wallet.created, 1)

    async def test_studio_and_rehearsal_do_not_post_link_messages(self):
        rt, transport, service, oid = await self.pending()
        channel, rt.channel = rt.channel, None
        self.assertIsNone(await before_approval_wait(rt, service, oid))
        rt.channel = channel
        order = await service.repo.order(oid)
        order["mode"] = "rehearsal"
        await service.repo.save(order)
        from restock.config import Settings

        service.settings = Settings("rehearsal")
        self.assertIsNone(await before_approval_wait(rt, service, oid))
        transport.post.assert_not_awaited()
