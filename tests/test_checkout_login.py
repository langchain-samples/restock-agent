"""Shopping prompts login and reuses Connections with the real CLI and fake services."""

import json
import os
import unittest
from unittest.mock import AsyncMock, patch

from restock.wallet import CliWallet
from tests import test_sessions as fixture
from tests.support import make_service
from tools import restock as tools

module = fixture.module


class CheckoutLoginTests(unittest.IsolatedAsyncioTestCase):
    setUp = fixture.LinkCliToolTests.setUp
    _restore_poll = fixture.LinkCliToolTests._restore_poll
    _restore_sessions = fixture.LinkCliToolTests._restore_sessions
    _client_for = fixture.LinkCliToolTests._client_for
    call = fixture.LinkCliToolTests.call
    saved = fixture.LinkCliToolTests.saved

    def service(self, runtime=None, thread="shopping", mode="link-test"):
        runtime = runtime or self.alice
        runtime.config["configurable"]["thread_id"] = thread
        return make_service(mode, runtime)

    async def prepare(self, service):
        results = await service.search("black pens", 2500)
        with (
            patch("tools.restock.service_for", return_value=service),
            patch("tools.restock.link_session", module),
        ):
            return await self.call(
                tools.prepare_restock_order,
                service.repo.runtime,
                selections=[{"product_id": results["products"][0]["product_id"], "quantity": 1}],
                budget_cents=2500,
            )

    async def test_shopping_prompts_once_and_resumes_same_order_after_login(self):
        service = self.service()
        first = await self.prepare(service)
        self.assertEqual(first["status"], "login_required")
        self.assertTrue(first["verification_url"].startswith("https://app.link.com/device"))
        order = await service.repo.order(first["order_id"])
        self.assertEqual(order["status"], "prepared")
        self.assertEqual(service.wallet.created, 0)

        # Repeating preparation must neither replace the cart nor restart consent.
        again = await self.prepare(service)
        self.assertEqual(again["order_id"], first["order_id"])
        self.assertEqual(again["verification_url"], first["verification_url"])
        self.assertEqual(self.auth.calls.count(("/device/code", "")), 1)
        self.auth.approve()
        with (
            patch.dict(os.environ, {"RESTOCK_MODE": "link-test"}),
            patch("tools.restock.link_session", module),
        ):
            finished = await self.call(tools.link_finish_login, self.alice)
        self.assertEqual(finished["status"], "connected")
        self.assertEqual(finished["order_id"], first["order_id"])
        self.assertEqual(finished["next_action"], "choose_payment_amount")
        self.assertEqual(service.wallet.created, 0, "Login cannot approve a purchase")
        await service.set_payment_amount(first["order_id"], 1200)
        with (
            patch("tools.restock.service_for", return_value=service),
            patch(
                "tools.restock.interrupt", return_value={"decisions": [{"type": "approve"}]}
            ) as review,
        ):
            result = await self.call(
                tools.request_restock_payment, self.alice, order_id=first["order_id"]
            )
        review.assert_called_once()
        self.assertEqual(result["order_id"], first["order_id"])
        self.assertEqual(result["status"], "awaiting_link_approval")
        self.assertEqual(service.wallet.created, 1)
        self.assertEqual(service.zinc.submissions, [])
        self.assertNotIn(self.auth.current_access, json.dumps([first, finished, result]))
        self.assertEqual(self.backend.files(), [])

    async def test_new_conversation_reuses_saved_connection_but_other_user_does_not(self):
        await self.prepare(self.service())
        self.auth.approve()
        await self.call(module.link_finish_login, self.alice)
        new_runtime = fixture.runtime_for("alice", self.store, self.backend)
        returning = await self.prepare(self.service(new_runtime, "new-conversation"))
        self.assertEqual(returning["status"], "payment_amount_required")
        self.assertEqual(returning["link_status"], "connected")
        self.assertNotIn("verification_url", returning)
        self.assertEqual(self.auth.calls.count(("/device/code", "")), 1)
        other = await self.prepare(self.service(self.bob, "bobs-conversation"))
        self.assertEqual(other["status"], "login_required")
        self.assertEqual(self.auth.calls.count(("/device/code", "")), 2)
        self.assertEqual(self.backend.files(), [])

    async def test_login_failure_preserves_cart_without_exposing_private_error(self):
        service = self.service()
        with patch.object(
            module.link_login,
            "coroutine",
            new=AsyncMock(return_value={"status": "error", "message": "private-fixture"}),
        ):
            result = await self.prepare(service)
        self.assertEqual(result["status"], "needs_attention")
        self.assertEqual(result["reason"], "link_connection_unavailable")
        self.assertEqual((await service.repo.order(result["order_id"]))["status"], "prepared")
        self.assertNotIn("private-fixture", json.dumps(result))
        self.assertEqual(service.wallet.created, 0)

    async def test_rehearsal_never_starts_login(self):
        service = self.service(mode="rehearsal")
        with patch.object(module.link_login, "coroutine", new=AsyncMock()) as login:
            result = await self.prepare(service)
        login.assert_not_awaited()
        self.assertEqual(result["status"], "payment_amount_required")
        self.assertEqual(self.auth.calls, [])

    async def test_missing_session_at_payment_reprompts_for_same_order(self):
        service = self.service()
        # Direct preparation simulates a session that disappeared after shopping.
        from tests.support import prepared

        order_id = await prepared(service)
        service.wallet = CliWallet(self.alice, module._caller(self.alice))
        with (
            patch("tools.restock.service_for", return_value=service),
            patch("tools.restock.link_session", module),
            patch("restock.wallet.Session", module.Session),
            patch("tools.restock.interrupt", return_value={"decisions": [{"type": "approve"}]}),
        ):
            result = await self.call(tools.request_restock_payment, self.alice, order_id=order_id)
        self.assertEqual(result["status"], "login_required")
        self.assertEqual(result["order_id"], order_id)
        self.assertEqual((await service.repo.order(order_id))["status"], "prepared")
        self.assertEqual(self.link.requests, {})
        self.assertEqual(self.backend.files(), [])
