import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
from mpp import parse_authorization, parse_www_authenticate
from mpp.methods.stripe.intents import ChargeIntent
from mpp.methods.stripe.schemas import StripeCredentialPayload

from restock.wallet import CliWallet, create_args
from restock.zinc import Zinc, parse_challenge, payment_header
from tests.fakes import serve_http
from tests.support import make_service, prepared
from tests.test_zinc import header
from tests import test_sessions as fixture

module = fixture.module


class WalletTests(unittest.IsolatedAsyncioTestCase):
    setUp = fixture.LinkCliToolTests.setUp
    _restore_poll = fixture.LinkCliToolTests._restore_poll
    _restore_sessions = fixture.LinkCliToolTests._restore_sessions
    _client_for = fixture.LinkCliToolTests._client_for
    call = fixture.LinkCliToolTests.call
    saved = fixture.LinkCliToolTests.saved

    async def test_returned_delete_error_uses_cleanup_fallback(self):
        async def failed_delete(path):
            return SimpleNamespace(error="synthetic_backend_error")

        with patch.object(self.backend, "adelete", failed_delete):
            async with module.Session(self.alice, module._caller(self.alice)) as session:
                await self.backend.awrite(session.path, json.dumps({"auth": None}))
        self.assertEqual(self.backend.files(), [])
        self.assertTrue(any(command.startswith("rm -f -- ") for command in self.backend.commands))
        self.assertFalse(module._user_lock(module._caller(self.alice)).locked())

    async def test_failed_cleanup_is_reported_and_releases_lock(self):
        async def failed_delete(path):
            return SimpleNamespace(error="synthetic_backend_error")

        original = self.backend.aexecute

        async def execute(command, **kwargs):
            if command.startswith("rm -f -- "):
                return SimpleNamespace(exit_code=1, output="synthetic private failure detail")
            return await original(command, **kwargs)

        with (
            patch.object(self.backend, "adelete", failed_delete),
            patch.object(self.backend, "aexecute", execute),
        ):
            with self.assertRaisesRegex(
                module.CredentialStoreError, "Private sandbox file cleanup failed"
            ):
                async with module.Session(self.alice, module._caller(self.alice)) as session:
                    await self.backend.awrite(session.path, json.dumps({"auth": None}))
        self.assertFalse(module._user_lock(module._caller(self.alice)).locked())
        # Only a synthetic empty auth file exists; remove it with the restored backend.
        await self.backend.adelete(session.path)

    # Reuse the real CLI, fake HTTP servers and caller-owned Connection fixture.
    async def test_real_cli_spt_request_retrieval_and_cleanup(self):
        self.auth.approve()
        await self.call(module.link_login, self.alice)
        await self.call(module.link_finish_login, self.alice)
        service = make_service()
        oid = await prepared(service)
        await service.set_payment_amount(oid, 1200)
        order = await service.repo.order(oid)
        with patch("restock.wallet.Session", module.Session):
            wallet = CliWallet(self.alice, module._caller(self.alice))
            created = await wallet.create(order)
            rid = created["id"]
            actual = self.link.requests[rid]
            self.assertEqual(actual["idempotency_key"], oid)
            self.assertEqual(actual["metadata"]["restock_fingerprint"], order["fingerprint"])
            self.assertEqual(actual["credential_type"], "shared_payment_token")
            self.assertEqual(actual["network_id"], order["challenge"]["network_id"])
            self.assertEqual(actual["amount"], 1200)
            self.assertEqual(order["budget_cents"], 2500)
            self.assertIn("chosen upfront amount of 1200", actual["context"])
            self.assertIsNone(actual["merchant_name"])
            self.link.approve(rid)
            request, token = await wallet.token(rid)
            self.assertEqual(token, "spt_private_fixture")
            self.assertNotIn("shared_payment_token", request)
            self.assertEqual((await wallet.history())[0]["id"], rid)
            self.assertEqual((await wallet.cancel(rid))["status"], "canceled")
        self.assertEqual(self.backend.files(), [])
        self.assertNotIn("spt_private_fixture", json.dumps(self.backend.commands))

    async def test_cli_test_flag_does_not_auto_approve(self):
        self.auth.approve()
        await self.call(module.link_login, self.alice)
        await self.call(module.link_finish_login, self.alice)
        service = make_service("link-test")
        oid = await prepared(service)
        with patch("restock.wallet.Session", module.Session):
            request = await CliWallet(self.alice, module._caller(self.alice)).create(
                await service.repo.order(oid)
            )
        self.assertTrue(self.link.requests[request["id"]]["test"])
        self.assertEqual(request["status"], "pending_approval")
        self.assertNotIn("--approve", " ".join(self.backend.commands))

    async def test_link_summary_uses_verified_email_fee_within_chosen_total(self):
        service = make_service()
        service.private.value["notification_email"] = "fixture@example.invalid"
        service.zinc.email_fee = 25
        oid = await prepared(service)
        await service.set_payment_amount(oid, 1200)
        args = create_args(await service.repo.order(oid), "pm_fixture")
        self.assertIn(
            "name:Retailer allowance (final total unknown),quantity:1,unit_amount:1075", args
        )
        self.assertIn("type:fee,display_text:Zinc fees,amount:125", args)
        self.assertIn("type:total,display_text:Chosen upfront amount,amount:1200", args)

    async def test_payment_header_matches_pinned_cli_mpp_pay(self):
        self.auth.approve()
        await self.call(module.link_login, self.alice)
        await self.call(module.link_finish_login, self.alice)
        service = make_service()
        oid = await prepared(service)
        raw = header()
        received = []

        def merchant(req):
            if "authorization" not in req.headers:
                return httpx.Response(402, headers={"WWW-Authenticate": raw})
            credential = parse_authorization(req.headers["authorization"])
            StripeCredentialPayload.model_validate(credential.payload)
            received.append(credential)
            return httpx.Response(
                201,
                json={"id": "zinc_cli_fixture", "status": "pending"},
                headers={"X-Api-Key": "fixture-tracking-key"},
            )

        server, url = serve_http(merchant)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        with patch("restock.wallet.Session", module.Session):
            wallet = CliWallet(self.alice, module._caller(self.alice))
            request = await wallet.create(await service.repo.order(oid))
            self.link.approve(request["id"])
            async with module.Session(self.alice, module._caller(self.alice)) as session:
                code, _, _ = await session.cli(
                    "mpp",
                    "pay",
                    url + "/agent/orders",
                    "--spend-request-id",
                    request["id"],
                    "--method",
                    "POST",
                    "--data",
                    '{"fixture":true}',
                )
        self.assertEqual(code, 0)
        self.assertEqual(len(received), 1)
        ours = parse_authorization(
            payment_header(parse_challenge([raw], 2500), "spt_private_fixture")
        )
        self.assertEqual(ours.payload, received[0].payload)
        self.assertEqual(ours.challenge.request, received[0].challenge.request)
        self.assertEqual(ours.challenge.id, received[0].challenge.id)
        self.assertEqual(self.backend.files(), [])

    async def test_complete_pipeline_with_real_cli_and_mock_zinc_http(self):
        self.auth.approve()
        await self.call(module.link_login, self.alice)
        await self.call(module.link_finish_login, self.alice)
        service = make_service()
        posts = []
        create_payment = AsyncMock(
            return_value=SimpleNamespace(id="pi_fixture", status="succeeded")
        )
        verifier = ChargeIntent(
            client=SimpleNamespace(
                v1=SimpleNamespace(payment_intents=SimpleNamespace(create_async=create_payment))
            )
        )
        payment_challenge = header()

        async def merchant(req):
            if req.url.path == "/search":
                return httpx.Response(
                    200,
                    json={
                        "results": [
                            {
                                "url": "https://shop.example.com/pens",
                                "title": "Black pens",
                                "price": 850,
                            }
                        ]
                    },
                )
            if req.method == "GET":
                return httpx.Response(200, json={"id": "zinc_fixture", "status": "order_placed"})
            if "authorization" not in req.headers:
                return httpx.Response(402, headers={"WWW-Authenticate": payment_challenge})
            await verifier.verify(
                parse_authorization(req.headers["authorization"]),
                parse_www_authenticate(payment_challenge).request,
            )
            posts.append(req)
            return httpx.Response(
                201,
                json={"id": "zinc_fixture", "status": "pending"},
                headers={"X-Api-Key": "private-tracking-fixture"},
            )

        with patch("restock.wallet.Session", module.Session):
            async with httpx.AsyncClient(transport=httpx.MockTransport(merchant)) as client:
                service.wallet = CliWallet(self.alice, module._caller(self.alice))
                service.zinc = Zinc(client)
                oid = await prepared(service)
                pending = await service.request_payment(oid)
                self.assertEqual(pending["status"], "awaiting_link_approval")
                self.assertEqual(posts, [])
                self.link.approve(next(iter(self.link.requests)))
                done = await service.wait(oid)
                self.assertEqual(done["status"], "merchant_pending")
                placed = await service.check(oid, finish=True)
                self.assertEqual(placed["status"], "order_placed")
                self.assertEqual(len(posts), 1)
                self.assertEqual(json.loads(posts[0].content)["idempotency_key"], oid)
                self.assertNotIn("spt_private_fixture", json.dumps(done))
                self.assertNotIn("private-tracking-fixture", json.dumps(placed))
        create_payment.assert_awaited_once()
        self.assertEqual(self.backend.files(), [])

    async def test_auth_file_removed_even_when_saving_rotation_fails(self):
        async def fail_save(*args):
            raise RuntimeError("synthetic save failure")

        with patch.object(module.sessions, "save", fail_save):
            with self.assertRaises(RuntimeError):
                async with module.Session(self.alice, module._caller(self.alice)) as session:
                    await self.backend.awrite(
                        session.path, json.dumps({"auth": {"access_token": "fixture"}})
                    )
        self.assertEqual(self.backend.files(), [])
        self.assertFalse(module._user_lock(module._caller(self.alice)).locked())
