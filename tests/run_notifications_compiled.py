"""Verify the exported HTTP callback with MDA's generated server dependencies."""

import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[1]
BUILD = Path(sys.argv[1]).resolve()
sys.path[:0] = [str(BUILD / "__runtime__"), str(BUILD), str(ROOT)]
import tests  # noqa: F401, E402
from starlette.requests import Request
from channels.zinc import channel
from restock.notifications import NAMESPACE, route_key
from tests.test_notifications import NotificationTests, event, ORDER


class ChannelTests(NotificationTests):
    async def test_real_mda_channel_handles_callback_without_agent_run(self):
        raw, signature = event()
        request = Request(
            {
                "type": "http",
                "method": "POST",
                "path": "/channels/zinc/events",
                "headers": [(b"x-webhook-signature", signature.encode())],
            }
        )
        with (
            patch("channels.zinc.receiver", return_value=self.receiver),
            patch(
                "managed_deepagents._channels.http.ingress.start_http_ingress_agent",
                new_callable=AsyncMock,
            ) as agent,
        ):
            response = await channel.events({"name": "zinc", "request": request, "raw_body": raw})
            self.assertEqual(response.status_code, 200)
            self.post.assert_awaited_once()
            agent.assert_not_awaited()
            self.post.side_effect = RuntimeError("private error")
            newer, signed = event("order.failed", "failed", seconds=1)
            request.scope["headers"] = [(b"x-webhook-signature", signed.encode())]
            response = await channel.events({"name": "zinc", "request": request, "raw_body": newer})
            self.assertEqual(response.status_code, 503)
            self.assertNotIn(b"private", response.body)

    async def test_unknown_route_is_temporary_failure_and_bad_signature_401(self):
        raw, signature = event()
        request = Request(
            {"type": "http", "headers": [(b"x-webhook-signature", signature.encode())]}
        )
        await self.store.adelete(NAMESPACE, route_key(ORDER))
        with patch("channels.zinc.receiver", return_value=self.receiver):
            response = await channel.events({"name": "zinc", "request": request, "raw_body": raw})
            self.assertEqual(response.status_code, 500)
            request.scope["headers"] = []
            response = await channel.events({"name": "zinc", "request": request, "raw_body": raw})
            self.assertEqual(response.status_code, 401)


if __name__ == "__main__":
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(ChannelTests)
    result = unittest.TextTestRunner(verbosity=1).run(suite)
    if not result.wasSuccessful():
        raise SystemExit(1)
    print(
        "PASS: exported Zinc HTTP channel verifies events, posts through fake Slack, and never runs an agent"
    )
