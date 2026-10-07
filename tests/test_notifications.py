import asyncio
import copy
import hashlib
import hmac
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import tests  # noqa: F401
import httpx
from langgraph.store.memory import InMemoryStore

from restock.config import RestockError
from restock.notifications import (
    NAMESPACE,
    Receiver,
    configuration,
    enable_updates,
    route_key,
    sealed,
    unseal,
)
from restock.zinc import Zinc
from scripts.configure_updates import configure
from scripts.preflight import settings_from_file
from tests.support import approved, make_service
from tools.restock import call

KEY = "synthetic-app-signing-key-for-offline-tests"
SECRET = "zn_whsec_synthetic-notification-secret"
ORIGIN = "https://restock.example.test"
CALLBACK = ORIGIN + "/channels/zinc/events"
ENV = {"RESTOCK_PUBLIC_URL": ORIGIN, "RESTOCK_UPDATES_SIGNING_KEY": KEY}
ORDER = "550e8400-e29b-41d4-a716-446655440000"
TARGET = {
    "channel_id": "trigger-channel",
    "address": {"channel": "Cfixture", "thread_ts": "123.1"},
    "source_thread_key": "slack-original-thread",
}
ROUTE = {
    "order_reference": "internal-fixture",
    "merchant_order_id": ORDER,
    "principal_id": "original-owner",
    "caller": "original-caller",
    "thread_id": "original-thread",
    "secret_slug": "restock-updates-internal-fixture",
    "callback_url": CALLBACK,
    "channel_name": "slack",
    "target": TARGET,
}


def event(name="order.placed", status="order_placed", seconds=0, data=None):
    raw = json.dumps(
        {
            "event": name,
            "status": status,
            "order_id": ORDER,
            "timestamp": (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat(),
            "data": data or {},
        }
    ).encode()
    return raw, hmac.new(SECRET.encode(), raw, hashlib.sha256).hexdigest()


class NotificationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.store = InMemoryStore()
        self.secret = AsyncMock(return_value=SECRET)
        self.post = AsyncMock()
        self.receiver = Receiver(
            self.store, self.secret, self.post, signing_key=KEY, callback_url=CALLBACK
        )
        await self.store.aput(NAMESPACE, route_key(ORDER), sealed(copy.deepcopy(ROUTE), KEY))

    async def test_placement_is_brief_but_later_tracking_is_still_sent(self):
        await self.receiver.receive(*event())
        message = self.post.call_args.args[1]
        self.assertIn("retailer confirmed", message)
        self.assertIn(ORDER, message)
        self.assertNotIn("Tracking", message)
        self.assertNotIn("email", message)
        self.assertNotIn("Enabled", message)
        await self.receiver.receive(
            *event(
                "order.tracking_received",
                "shipped",
                seconds=1,
                data={"tracking_numbers": [{"carrier": "UPS", "tracking_number": "1ZFIXTURE"}]},
            )
        )
        self.assertIn("UPS", self.post.call_args.args[1])
        self.assertIn("1ZFIXTURE", self.post.call_args.args[1])
        self.assertEqual(self.post.await_count, 2)

    async def test_duplicate_concurrent_and_restarted_events(self):
        raw, signature = event()
        await asyncio.gather(*(self.receiver.receive(raw, signature) for _ in range(4)))
        self.post.assert_awaited_once()
        self.assertEqual(self.post.call_args.args[0]["target"], TARGET)
        restarted = Receiver(
            self.store, self.secret, self.post, signing_key=KEY, callback_url=CALLBACK
        )
        self.assertEqual(await restarted.receive(*event()), "duplicate_or_older")
        self.post.assert_awaited_once()

    async def test_invalid_signature_or_forged_route_cannot_send(self):
        raw, signature = event()
        with self.assertRaises(RestockError):
            await self.receiver.receive(raw + b" ", signature)
        record = sealed(copy.deepcopy(ROUTE), KEY)
        record["value"]["principal_id"] = "someone-else"
        await self.store.aput(NAMESPACE, route_key(ORDER), record)
        self.secret.reset_mock()
        with self.assertRaises(RestockError):
            await self.receiver.receive(raw, signature)
        self.secret.assert_not_awaited()
        self.post.assert_not_awaited()

    async def test_signature_and_ids_validated_before_secret_lookup(self):
        for raw, signature in (
            (b"{}", None),
            (b"[]", "a" * 64),
            (b"x" * 131073, "a" * 64),
            (b"{}", "not-a-hmac"),
        ):
            with self.assertRaises(RestockError):
                await self.receiver.receive(raw, signature)
        self.secret.assert_not_awaited()
        self.post.assert_not_awaited()

    async def test_stale_and_regressive_notifications_are_suppressed(self):
        await self.receiver.receive(
            *event(
                "order.tracking_received",
                "shipped",
                data={"tracking_numbers": [{"carrier": "UPS", "tracking_number": "fixture123"}]},
            )
        )
        await self.receiver.receive(*event(seconds=-30))
        await self.receiver.receive(*event(seconds=30))
        self.post.assert_awaited_once()
        await self.receiver.receive(*event("order.delivered", "delivered", seconds=60))
        self.assertIn("all packages", self.post.call_args.args[1])
        await self.receiver.receive(*event("order.tracking_received", "shipped", seconds=90))
        self.assertEqual(self.post.await_count, 2)

    async def test_provider_prose_and_addresses_never_forwarded(self):
        await self.receiver.receive(
            *event(
                "order.failed",
                "failed",
                data={
                    "error_type": "max_price_exceeded",
                    "error": "private street and email",
                    "shipping_address": {"address_line1": "private street"},
                    "target": {"channel": "attacker"},
                },
            )
        )
        text = self.post.call_args.args[1]
        self.assertIn("max_price_exceeded", text)
        self.assertIn("refund status is unverified", text)
        self.assertNotIn("private street", text)
        self.assertNotIn("attacker", text)
        self.assertNotIn(SECRET, str(self.store._data))

    async def test_cursor_copied_from_another_order_is_rejected(self):
        await self.store.aput(
            NAMESPACE,
            "cursor:" + route_key(ORDER),
            sealed(
                {
                    "merchant_order_id": "another-order",
                    "timestamp": 9999999999,
                },
                KEY,
            ),
        )
        with self.assertRaisesRegex(RestockError, "invalid_update_binding"):
            await self.receiver.receive(*event())
        self.post.assert_not_awaited()

    async def test_later_failure_details_can_be_reported_without_replaying_purchase(self):
        await self.receiver.receive(*event("order.failed", "failed"))
        await self.receiver.receive(
            *event("order.failed", "failed", seconds=1, data={"error_type": "max_price_exceeded"})
        )
        self.assertEqual(self.post.await_count, 2)
        self.assertIn("max_price_exceeded", self.post.call_args.args[1])

    async def test_transient_send_failure_retries_same_action(self):
        self.post.side_effect = RuntimeError("private provider error")
        payload = event()
        with self.assertRaises(RuntimeError):
            await self.receiver.receive(*payload)
        action = self.post.call_args.args[2]
        self.post.side_effect = None
        await self.receiver.receive(*payload)
        self.assertEqual(self.post.call_args.args[2], action)


class SubscriptionTests(unittest.IsolatedAsyncioTestCase):
    async def test_existing_order_catches_up_and_keeps_original_owner_and_thread(self):
        service = make_service()
        oid = await approved(service)
        result = await service.check(oid, finish=True)
        rt = service.repo.runtime
        rt.channel = SimpleNamespace(provider="slack")
        rt.context = {}
        service.zinc.webhook = AsyncMock(return_value=SECRET)
        binding = {
            "name": "slack",
            "provider": "slack",
            "transport": "trigger_server",
            "target": TARGET,
        }
        for record in service.zinc.orders.values():
            record.update(status="order_failed", job_result={"error_type": "max_price_exceeded"})
        with (
            patch.dict("os.environ", ENV),
            patch(
                "managed_deepagents._channels.binding.resolve_channel_binding", return_value=binding
            ),
            patch(
                "restock.link_session.ConnectionSessions._client",
                return_value=SimpleNamespace(
                    context=SimpleNamespace(principal_id="original-owner")
                ),
            ),
            patch("restock.link_session.ConnectionSessions.save", new_callable=AsyncMock) as save,
        ):
            result = await enable_updates(rt, service, result)
            self.assertEqual(result["status"], "order_failed")
            self.assertEqual(result["slack_updates_status"], "enabled")
            self.assertEqual(result["failure_reasons"][0]["code"], "max_price_exceeded")
            save.assert_awaited_once()
            await enable_updates(rt, service, result)
            service.zinc.webhook.assert_awaited_once()
        self.assertEqual(len(service.zinc.submissions), 1)
        self.assertEqual(service.wallet.tokens, 1)
        record = await rt.store.aget(NAMESPACE, route_key(result["merchant_order_id"]))
        route = unseal(record.value, KEY)
        self.assertEqual(route["principal_id"], "original-owner")
        self.assertEqual(route["target"], TARGET)
        self.assertNotIn(SECRET, str(record.value))

    async def test_optional_configuration_and_failure_never_obscure_purchase(self):
        service = make_service()
        oid = await approved(service)
        rt = service.repo.runtime
        rt.channel = SimpleNamespace(provider="slack")
        with (
            patch("tools.restock.service_for", return_value=service),
            patch(
                "restock.notifications.enable_updates", side_effect=RuntimeError("private error")
            ),
        ):
            result = await call(rt, "check", oid, finish=True, response_detail="notifications")
        self.assertEqual(result["status"], "merchant_pending")
        self.assertEqual(result["slack_updates_status"], "setup_failed")
        self.assertNotIn("private error", str(result))
        self.assertEqual(len(service.zinc.submissions), 1)
        with patch.dict(
            "os.environ", {"RESTOCK_PUBLIC_URL": "", "RESTOCK_UPDATES_SIGNING_KEY": ""}
        ):
            result = await enable_updates(rt, service, result)
        self.assertEqual(result["slack_updates_status"], "order_updates_not_configured")
        self.assertEqual(result["status"], "merchant_pending")

    async def test_non_live_or_non_slack_has_no_subscription(self):
        service = make_service()
        rt = service.repo.runtime
        service.zinc.webhook = AsyncMock()
        result = {"mode": "link-test", "merchant_order_id": ORDER}
        self.assertEqual(await enable_updates(rt, service, result), result)
        result["mode"] = "live"
        self.assertEqual(await enable_updates(rt, service, result), result)
        service.zinc.webhook.assert_not_awaited()

    async def test_zinc_endpoint_registration_and_conflict(self):
        for existing in (None, CALLBACK, "https://another-app.example.test/events"):
            requests = []

            def handle(request):
                requests.append(request)
                self.assertEqual(request.headers["authorization"], "Bearer fixture-key")
                return httpx.Response(
                    200,
                    json={
                        "webhook_url": existing if request.method == "GET" else CALLBACK,
                        "webhook_secret": SECRET,
                    },
                )

            async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
                if existing and existing != CALLBACK:
                    with self.assertRaisesRegex(RestockError, "already_configured"):
                        await Zinc(client).webhook("fixture-key", CALLBACK)
                else:
                    self.assertEqual(await Zinc(client).webhook("fixture-key", CALLBACK), SECRET)
            self.assertEqual(len(requests), 2 if existing is None else 1)

    def test_configuration_keeps_private_settings_and_signing_key(self):
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            (project / "channels").mkdir()
            (project / "channels/slack.py").touch()
            (project / ".env").write_text("RESTOCK_MODE=live\nOPENAI_API_KEY=fixture\n")
            configure(project, ORIGIN)
            first = settings_from_file(
                project / ".env", {}, names={*ENV, "RESTOCK_MODE", "OPENAI_API_KEY"}
            )
            configure(project, ORIGIN)
            second = settings_from_file(project / ".env", {}, names=set(first))
            self.assertEqual(first, second)
            self.assertEqual(second["RESTOCK_MODE"], "live")
            self.assertEqual((project / ".env").stat().st_mode & 0o777, 0o600)
            self.assertEqual(configuration(second)[0], CALLBACK)
        for url in (
            "http://example.test",
            "https://user:pass@example.test",
            "https://example.test/path",
            "https://[bad",
        ):
            with self.assertRaises(RestockError):
                configuration({**ENV, "RESTOCK_PUBLIC_URL": url})
