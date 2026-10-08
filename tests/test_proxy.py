import asyncio
import copy
import json
import os
import shutil
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import tests  # noqa: F401
import httpx
from langgraph.store.memory import InMemoryStore
from mpp import parse_authorization, parse_www_authenticate
from mpp.methods.stripe.intents import ChargeIntent

from restock import link_session
from restock.config import RestockError
from restock.link_proxy import NAMESPACE, operation, request_operation
from restock.proxy_wallet import ProxyRunner
from restock.wallet import CliWallet, create_args
from restock.zinc import Zinc
from tests.fakes import FakeAgentAuthServer, FakeLink, FakeLinkAuth, serve_http
from tests.local_sandbox import LocalSandboxBackend
from tests.proxy_support import CALLBACK_URL, ENV, SANDBOX, ProxyFixture
from tests.support import make_service, prepared
from tests.test_sessions import CLI, runtime_for
from tests.test_zinc import header


class ProxyTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.link = FakeLink(token="unused")
        self.link.tokens.clear()
        self.auth = FakeLinkAuth(self.link)
        self.agent_auth = FakeAgentAuthServer()
        self.store = InMemoryStore()
        self.proxy = ProxyFixture(self.link, self.agent_auth, self.store)
        proxy_server, api = serve_http(self.proxy.handler)
        auth_server, auth = serve_http(self.auth.handler)
        for server in (proxy_server, auth_server):
            self.addCleanup(server.server_close)
            self.addCleanup(server.shutdown)
        self.backend = LocalSandboxBackend(
            CLI, api_url=api, auth_url=auth, auth_dir=link_session.AUTH_DIR
        )
        self.addCleanup(shutil.rmtree, self.backend.root)
        self.alice = runtime_for("alice", self.store, self.backend)
        self.bob = runtime_for("bob", self.store, self.backend)
        for p in (
            patch.dict(os.environ, ENV),
            patch.object(link_session, "sessions", self.proxy.session_store()),
            patch("restock.proxy_wallet.context_for", self.proxy.context_for),
        ):
            p.start()
            self.addCleanup(p.stop)
        self.wallet = CliWallet(self.alice, link_session._caller(self.alice))

    async def login(self):
        start = await link_session.link_login.coroutine(runtime=self.alice)
        self.assertEqual(start["status"], "login_required")
        self.auth.approve()
        done = await link_session.link_finish_login.coroutine(runtime=self.alice)
        self.assertEqual(done["status"], "connected")

    async def test_first_login_reuse_and_real_cli_full_wallet_flow(self):
        with self.assertRaisesRegex(RestockError, "login_required"):
            await self.wallet.history()
        await self.login()
        self.assertEqual(
            (await link_session.link_login.coroutine(runtime=self.alice))["status"], "connected"
        )
        self.backend.commands.clear()
        service = make_service("live")
        oid = await prepared(service)
        order = await service.repo.order(oid)
        created = await self.wallet.create(order)
        rid = created["id"]
        self.assertEqual(created["status"], "pending_approval")
        self.assertFalse(self.link.requests[rid]["test"])
        self.assertEqual((await self.wallet.retrieve(rid))["id"], rid)
        self.link.approve(rid)
        request, token = await self.wallet.token(rid)
        self.assertEqual(token, "spt_private_fixture")
        self.assertNotIn("shared_payment_token", request)
        self.assertEqual((await self.wallet.history())[0]["id"], rid)
        self.assertEqual((await self.wallet.cancel(rid))["status"], "canceled")
        self.assertTrue(
            all(t.startswith("Bearer restock-proxy-") for t in self.proxy.inbound_tokens)
        )
        commands = json.dumps(self.backend.commands)
        self.assertNotIn(self.auth.current_access, commands)
        self.assertNotIn("spt_private_fixture", commands)
        self.assertFalse(any(c.startswith("cat ") for c in self.backend.commands))
        self.assertEqual(self.backend.files(), [])
        self.assertEqual((await self.store.aget(NAMESPACE, SANDBOX)).value, {})

    async def test_link_test_mode_approves_without_token_or_merchant_submission(self):
        await self.login()
        service = make_service("link-test")
        service.wallet = self.wallet
        oid = await prepared(service)
        await service.request_payment(oid)
        self.assertTrue(next(iter(self.link.requests.values()))["test"])
        self.link.approve(next(iter(self.link.requests)))
        result = await service.wait(oid)
        self.assertEqual(result["status"], "approved_test_mode")
        self.assertEqual(service.zinc.submissions, [])
        self.assertFalse(any("shared_payment_token" in query for _, _, query in self.proxy.targets))

    async def test_expired_session_refreshes_then_proxy_uses_rotation(self):
        await self.login()
        saved = json.loads(await link_session.sessions.load(self.alice))
        old = saved["auth"]["access_token"]
        saved["auth"]["expires_at"] = 1
        await link_session.sessions.save(self.alice, json.dumps(saved))
        await self.wallet.history()
        current = json.loads(await link_session.sessions.load(self.alice))["auth"]["access_token"]
        self.assertNotEqual(current, old)
        self.assertEqual(current, self.auth.current_access)
        self.assertEqual(self.proxy.targets[0][1], "/payment-details")
        self.assertEqual(self.proxy.inbound_tokens[0], "Bearer " + current)
        self.assertTrue(self.proxy.inbound_tokens[-1].startswith("Bearer restock-proxy-"))
        self.assertEqual(self.backend.files(), [])

    async def test_renewal_and_test_approval_with_optional_profile_field_missing(self):
        # The API can omit line2. CLI 0.22.0 rejects that profile response,
        # so an unrelated profile read must not gate payment-session renewal.
        self.link.user["address"] = {
            "line1": "123 Example Street",
            "city": "Boston",
            "state": "MA",
            "postal_code": "02108",
            "country": "US",
        }
        await self.login()
        saved = json.loads(await link_session.sessions.load(self.alice))
        old = saved["auth"]["access_token"]
        saved["auth"]["expires_at"] = 1
        await link_session.sessions.save(self.alice, json.dumps(saved))
        service = make_service("link-test")
        service.wallet = self.wallet
        oid = await prepared(service)
        created = await service.request_payment(oid)
        self.assertEqual(created["status"], "awaiting_link_approval")
        self.link.approve(next(iter(self.link.requests)))
        self.assertEqual((await service.wait(oid))["status"], "approved_test_mode")
        current = json.loads(await link_session.sessions.load(self.alice))["auth"]["access_token"]
        self.assertNotEqual(current, old)
        self.assertEqual(current, self.auth.current_access)
        self.assertFalse(any(path == "/userinfo" for _, path, _ in self.proxy.targets))
        self.assertEqual(len(self.link.requests), 1)
        self.assertEqual(service.zinc.submissions, [])
        self.assertEqual(self.backend.files(), [])

    async def test_read_only_failure_keeps_draft_and_does_not_claim_unknown_payment(self):
        await self.login()
        service = make_service("link-test")
        service.wallet = self.wallet
        oid = await prepared(service)
        self.proxy.failure = "provider"
        with self.assertRaisesRegex(RestockError, "no_payment_requested"):
            await service.request_payment(oid)
        self.assertEqual((await service.repo.order(oid))["status"], "prepared")
        self.assertFalse(any(method == "POST" for method, _, _ in self.proxy.targets))
        self.assertEqual(self.link.requests, {})
        self.proxy.failure = None
        self.assertEqual((await service.request_payment(oid))["status"], "awaiting_link_approval")
        self.assertEqual(len(self.link.requests), 1)

    async def test_missing_create_response_stays_unknown_and_reconciles_without_replay(self):
        await self.login()
        service = make_service("link-test")
        service.wallet = self.wallet
        oid = await prepared(service)
        original = self.link.handler

        def lost_response(request):
            result = original(request)
            if request.method == "POST" and request.url.path == "/spend_requests":
                return httpx.Response(502, json={"error": {"message": "fixture response lost"}})
            return result

        with patch.object(self.link, "handler", lost_response):
            with self.assertRaisesRegex(RestockError, "payment_outcome_unknown"):
                await service.request_payment(oid)
        self.assertEqual((await service.repo.order(oid))["status"], "payment_unknown")
        self.assertEqual((await service.request_payment(oid))["status"], "payment_unknown")
        self.assertEqual((await service.check(oid))["status"], "awaiting_link_approval")
        self.assertEqual(len(self.link.requests), 1)

    async def test_invalid_refresh_fails_without_new_consent_or_paid_command(self):
        await self.login()
        saved = json.loads(await link_session.sessions.load(self.alice))
        saved["auth"].update(expires_at=1, refresh_token="invalid-fixture")
        await link_session.sessions.save(self.alice, json.dumps(saved))
        self.backend.commands.clear()
        with self.assertRaisesRegex(RestockError, "renewal_failed"):
            await self.wallet.history()
        self.assertFalse(any("auth login" in c for c in self.backend.commands))
        self.assertFalse(any("spend-request" in c for c in self.backend.commands))
        self.assertEqual(self.backend.files(), [])

    async def test_callback_and_provider_failures_do_not_replay(self):
        await self.login()
        for failure in ("callback", "provider"):
            self.proxy.failure = failure
            before = len(self.proxy.targets)
            with self.assertRaisesRegex(RestockError, "check_existing_order"):
                await self.wallet.retrieve("lsrq_fixture")
            self.assertEqual(len(self.proxy.targets), before + 1)
            self.assertEqual((await self.store.aget(NAMESPACE, SANDBOX)).value, {})

    async def test_private_output_is_cleaned_on_failure(self):
        await self.login()
        self.proxy.failure = "provider"
        with self.assertRaises(RestockError):
            await self.wallet.token("lsrq_fixture")
        self.assertEqual(self.backend.files(), [])

    async def test_other_user_has_no_login_and_disconnect_denies_reuse(self):
        await self.login()
        bob = CliWallet(self.bob, link_session._caller(self.bob))
        with self.assertRaisesRegex(RestockError, "login_required"):
            await bob.history()
        await link_session.link_logout.coroutine(runtime=self.alice)
        with self.assertRaisesRegex(RestockError, "login_required"):
            await self.wallet.history()

    async def test_exact_request_signature_identity_and_replay(self):
        await self.login()
        saved = await link_session.sessions.load(self.alice)
        spec = operation("GET", "/payment-details")
        async with self.proxy.permissions.allow(
            SANDBOX, "alice", "link-session", saved, spec
        ) as placeholder:
            event = self.proxy.envelope(
                httpx.Request(
                    "GET",
                    "https://api.link.com/payment-details",
                    headers={"Authorization": "Bearer " + placeholder},
                )
            )
            mutations = [
                lambda e: e["identity"].update(tenant_id="other"),
                lambda e: e["identity"].update(sandbox_id="other"),
                lambda e: e.update(host="other.link.com"),
                lambda e: e["request"].update(method="POST"),
                lambda e: e["request"].update(body_truncated=True),
                lambda e: e["request"].update(body_base64="bnVsbA=="),
                lambda e: e["request"].update(query="include=shared_payment_token"),
                lambda e: e["request"]["headers"].update(authorization=["Bearer stolen"]),
            ]
            for mutate in mutations:
                altered = copy.deepcopy(event)
                mutate(altered)
                raw = json.dumps(altered).encode()
                with self.assertRaises(RestockError):
                    await self.proxy.callback.resolve(raw, self.proxy.sign(raw))
            raw = json.dumps(event).encode()
            for claims in (
                {"iss": "https://evil.invalid"},
                {"aud": "https://other.invalid"},
                {"exp": int(time.time()) - 1},
                {"body_sha256": "wrong"},
                {"sub": "other"},
            ):
                with self.assertRaises(RestockError):
                    await self.proxy.callback.resolve(raw, self.proxy.sign(raw, **claims))
            self.assertIn(
                self.auth.current_access,
                (await self.proxy.callback.resolve(raw, self.proxy.sign(raw)))["headers"][
                    "Authorization"
                ],
            )
            with self.assertRaises(RestockError):
                await self.proxy.callback.resolve(raw, self.proxy.sign(raw))

    async def test_callback_accepts_single_audience_in_go_and_string_formats(self):
        raw = b'{"fixture":true}'
        for audience in (CALLBACK_URL, [CALLBACK_URL]):
            result = await self.proxy.callback.verify(raw, self.proxy.sign(raw, aud=audience))
            self.assertEqual(result, {"fixture": True})
        for audience in (
            [],
            ["https://other.invalid"],
            [CALLBACK_URL, "https://other.invalid"],
            [CALLBACK_URL, CALLBACK_URL],
            "https://other.invalid",
        ):
            with self.assertRaises(RestockError):
                await self.proxy.callback.verify(raw, self.proxy.sign(raw, aud=audience))

    async def test_current_session_and_permission_required(self):
        await self.login()
        saved = await link_session.sessions.load(self.alice)
        async with self.proxy.permissions.allow(
            SANDBOX, "alice", "link-session", saved, operation("GET", "/payment-details")
        ) as placeholder:
            with self.assertRaisesRegex(RestockError, "busy"):
                async with self.proxy.permissions.allow(
                    SANDBOX, "bob", "link-session", saved, operation("GET", "/payment-details")
                ):
                    pass
            await link_session.sessions.clear(self.alice)
            raw = json.dumps(
                self.proxy.envelope(
                    httpx.Request(
                        "GET",
                        "https://api.link.com/payment-details",
                        headers={"Authorization": "Bearer " + placeholder},
                    )
                )
            ).encode()
            with self.assertRaises(RestockError):
                await self.proxy.callback.resolve(raw, self.proxy.sign(raw))
        self.assertEqual((await self.store.aget(NAMESPACE, SANDBOX)).value, {})

    async def test_body_change_blocks_real_cli_create(self):
        await self.login()
        service = make_service()
        oid = await prepared(service)

        def mutate(event):
            if event["request"]["method"] == "POST":
                event["request"]["body_base64"] = "e30="

        self.proxy.mutate = mutate
        with self.assertRaises(RestockError):
            await self.wallet.create(await service.repo.order(oid))
        self.assertEqual(self.link.requests, {})

    async def test_only_narrow_operations_and_no_approval_shortcuts(self):
        service = make_service()
        oid = await prepared(service)
        args = create_args(await service.repo.order(oid), "pm_fixture")
        for invalid in (
            ["user-info", "retrieve"],
            ["auth", "login"],
            ["spend-request", "retrieve", "../other"],
            args + ["--approve"],
            args + ["--amount", "9900"],
        ):
            with self.assertRaises(RestockError):
                request_operation(invalid)

    async def test_aborted_command_clears_permission(self):
        await self.login()
        with patch.object(ProxyRunner, "_execute", side_effect=asyncio.CancelledError()):
            with self.assertRaises(asyncio.CancelledError):
                await self.wallet.history()
        self.assertEqual((await self.store.aget(NAMESPACE, SANDBOX)).value, {})
        self.assertFalse(link_session._user_lock(link_session._caller(self.alice)).locked())

    async def test_proxy_payment_handoff_matches_zinc_mpp_contract(self):
        await self.login()
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

        async with httpx.AsyncClient(transport=httpx.MockTransport(merchant)) as client:
            service.wallet = self.wallet
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

    async def test_callback_connection_reader_uses_signed_principal_and_deployment(self):
        from managed_deepagents._agent_auth import AgentAuth
        from restock.proxy_runtime import read_session

        await self.login()
        env = {
            "LANGSMITH_API_KEY": "fixture-api",
            "LANGSMITH_WORKSPACE_ID": "fixture-tenant",
            "LANGSMITH_HOST_PROJECT_ID": "fixture-agent",
        }
        grant = {
            "workspace_id": "fixture-tenant",
            "deployment_id": "fixture-agent",
            "principal_id": "alice",
            "slug": "link-session",
        }
        principals = []
        auth = self.agent_auth

        def request(client, method, path, *, body=None):
            principals.append(client.context.principal_id)
            return auth.request(method, path, body=body)

        with patch.dict(os.environ, env), patch.object(AgentAuth, "_urlopen_json", request):
            self.assertEqual(
                await read_session(grant), await link_session.sessions.load(self.alice)
            )
            self.assertEqual(set(principals), {"alice"})
            with self.assertRaisesRegex(RestockError, "identity_unavailable"):
                await read_session({**grant, "deployment_id": "other"})
            with self.assertRaisesRegex(RestockError, "identity_unavailable"):
                await read_session({**grant, "workspace_id": "other"})
