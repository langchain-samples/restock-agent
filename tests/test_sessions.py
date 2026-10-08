"""The login and command tools, run with a real link-cli binary.

The CLI executes on this machine through a local stand-in for the sandbox backend and
talks to fake Link servers. Agent identity and the Store are real LangGraph objects.
"""

import asyncio
import json
import os
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import tests  # noqa: F401  Blocks the network (loopback fakes stay reachable).
from langchain.tools import ToolRuntime
from langgraph.store.memory import InMemoryStore

ROOT = Path(__file__).resolve().parents[1]
from managed_deepagents._agent_auth import AgentAuth, AgentAuthContext  # noqa: E402

from tests.fakes import FakeAgentAuthServer, FakeLink, FakeLinkAuth, serve_http  # noqa: E402
from tests.local_sandbox import LocalSandboxBackend  # noqa: E402


def load_blueprint_module():
    """Load tools/link_cli.py by path so the test never depends on import order."""
    import importlib.util

    path = ROOT / "restock/link_session.py"
    if not path.is_file():
        return None
    spec = importlib.util.spec_from_file_location("link_cli_sandbox_tools", path)
    loaded = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loaded)
    return loaded


module = load_blueprint_module()

CANDIDATES = (
    Path(os.environ.get("LINK_CLI", "")),
    ROOT / "node_modules/@stripe/link-cli/dist/cli.js",  # npm install @stripe/link-cli
    ROOT.parents[1] / "node_modules/@stripe/link-cli/dist/cli.js",
)
CLI = next((c for c in CANDIDATES if str(c) and c.is_file()), None)
if CLI is None:
    raise RuntimeError("Install the pinned test CLI with npm ci --ignore-scripts before testing")


def runtime_for(caller, store, backend):
    """The runtime as MDA's wrapper presents it: LangChain fields plus server_info and backend."""
    principal = SimpleNamespace(id=caller, kind="person") if caller else None
    inner = ToolRuntime(
        state={},
        context=None,
        config={"configurable": {}},
        stream_writer=lambda *_: None,
        tool_call_id="call",
        store=store,
        server_info=SimpleNamespace(principal=principal),
    )
    return SimpleNamespace(
        state=inner.state,
        context=inner.context,
        config=inner.config,
        store=inner.store,
        tool_call_id=inner.tool_call_id,
        server_info=inner.server_info,
        backend=backend,
    )


@unittest.skipIf(
    module is None or CLI is None, "link-cli not found; set LINK_CLI to its dist/cli.js"
)
class LinkCliToolTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        legacy = patch.dict(os.environ, RESTOCK_LINK_TRANSPORT="session")
        legacy.start()
        self.addCleanup(legacy.stop)
        self.link = FakeLink(token="unused")
        self.link.tokens.clear()
        self.auth = FakeLinkAuth(self.link)
        self.api_server, api_url = serve_http(self.link.handler)
        self.auth_server, auth_url = serve_http(self.auth.handler)
        for server in (self.api_server, self.auth_server):
            self.addCleanup(server.server_close)
            self.addCleanup(server.shutdown)
        self.backend = LocalSandboxBackend(
            CLI, api_url=api_url, auth_url=auth_url, auth_dir=module.AUTH_DIR
        )
        self.store = InMemoryStore()
        # Sessions live in a user-owned secret Connection: MDA's real AgentAuth client
        # runs over an in-memory Agent Auth for both optional reads and writes.
        self.agent_auth = FakeAgentAuthServer()
        self._sessions = module.sessions
        module.sessions = module.ConnectionSessions("link-session", agent_auth=self._client_for)
        self.addCleanup(self._restore_sessions)
        # Short polls keep the suite fast; production waits 15 x 3 seconds.
        self._poll = (module.FINISH_POLL_SECONDS, module.FINISH_POLL_ATTEMPTS)
        module.FINISH_POLL_SECONDS, module.FINISH_POLL_ATTEMPTS = 1, 2
        self.addCleanup(self._restore_poll)
        self.alice = runtime_for("alice", self.store, self.backend)
        self.bob = runtime_for("bob", self.store, self.backend)

    def _restore_poll(self):
        module.FINISH_POLL_SECONDS, module.FINISH_POLL_ATTEMPTS = self._poll

    def _restore_sessions(self):
        module.sessions = self._sessions

    def _client_for(self, runtime):
        principal = runtime.server_info.principal.id
        return AgentAuth(
            AgentAuthContext(
                base_url="https://auth.example.invalid",
                principal_id=principal,
                agent_id="synthetic-deployment",
                api_key="synthetic-platform-key",
                workspace_id="synthetic-workspace",
            ),
            request=self.agent_auth.request,
        )

    async def call(self, tool, runtime, **kwargs):
        return await tool.coroutine(runtime=runtime, **kwargs)

    async def saved(self, caller="alice"):
        value = self.agent_auth.read("link-session", caller)
        return json.loads(value) if value else None

    async def test_login_persists_per_user_and_leaves_no_file_behind(self):
        started = await self.call(module.link_login, self.alice)
        self.assertEqual(started["status"], "login_required")
        self.assertTrue(started["verification_url"].startswith("https://app.link.com/device"))
        pending = await self.saved()
        self.assertIsNotNone(
            pending["pendingDeviceAuth"], "device state must survive between calls"
        )
        self.assertEqual(self.backend.files(), [], "no auth file stays in the sandbox")
        waiting = await self.call(module.link_finish_login, self.alice)
        self.assertEqual(waiting["status"], "pending")
        self.auth.approve()
        done = await self.call(module.link_finish_login, self.alice)
        self.assertEqual(done["status"], "connected")
        self.assertIn("payment_methods.agentic", done["scope"])
        tokens = await self.saved()
        self.assertEqual(tokens["auth"]["access_token"], self.auth.current_access)
        self.assertEqual(self.backend.files(), [])
        again = await self.call(module.link_login, self.alice)
        self.assertEqual(again["status"], "connected")
        # Bob has his own, empty session.
        self.assertEqual(
            (await self.call(module.link_cli, self.bob, command="user-info retrieve"))["status"],
            "login_required",
        )
        self.assertIsNone(await self.saved("bob"))
        self.assertIn(("POST", "/v1/agent-auth/connections"), self.agent_auth.calls)

    async def test_commands_run_as_the_user_and_rotated_tokens_are_saved(self):
        await self.call(module.link_login, self.alice)
        self.auth.approve()
        await self.call(module.link_finish_login, self.alice)
        first = (await self.saved())["auth"]
        result = await self.call(module.link_cli, self.alice, command="user-info retrieve")
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["result"]["email"], "test@example.invalid")
        methods = await self.call(
            module.link_cli, self.alice, command="link-cli payment-methods list"
        )
        self.assertEqual(methods["result"][0]["card_details"]["last4"], "4242")
        # Expire the access token: the CLI refreshes, and the tool stores the rotated file.
        self.auth.expire_current()
        refreshed = await self.call(module.link_cli, self.alice, command="user-info retrieve")
        self.assertEqual(refreshed["status"], "ok")
        second = (await self.saved())["auth"]
        self.assertNotEqual(first["refresh_token"], second["refresh_token"])
        self.assertEqual(second["access_token"], self.auth.current_access)
        self.assertTrue(
            any(
                m == "PATCH" and p.startswith("/v1/agent-auth/credentials/")
                for m, p in self.agent_auth.calls
            ),
            "rotation must update the existing credential",
        )
        self.assertIn(("/device/token", "refresh_token"), self.auth.calls)
        created = await self.call(
            module.link_cli,
            self.alice,
            command='spend-request create --amount 525 --merchant-name "Demo Roasters" '
            '--merchant-url https://demo-roasters.example.com --test --context "'
            + "One medium oat latte for pickup at Demo Roasters, requested through the sandbox CLI tool test "
            "against a fake Link API with a fake device login." + '"',
        )
        self.assertEqual(created["status"], "ok")
        request_id = module.one(created["result"])["id"]
        self.assertTrue(self.link.requests[request_id]["test"])
        self.link.approve(request_id)
        approved = await self.call(
            module.link_cli, self.alice, command=f"spend-request retrieve {request_id}"
        )
        self.assertEqual(module.one(approved["result"])["status"], "approved")
        everything = json.dumps([result, methods, refreshed, created, approved])
        for secret in (first["access_token"], second["access_token"], second["refresh_token"]):
            self.assertNotIn(secret, everything)
        self.assertEqual(self.backend.files(), [])

    async def test_command_validation(self):
        create = (
            'spend-request create --amount 100 --merchant-name "Demo Roasters" '
            '--merchant-url https://demo-roasters.example.com --context "One drip coffee"'
        )
        cases = {
            "auth login": "handled by link_login",
            "onboard": "handled by link_login",
            "user-info retrieve --auth /tmp/x": "Do not pass",
            "user-info retrieve --auth=/tmp/x": "Do not pass",
            "user-info retrieve --format yaml": "Do not pass",
            "user-info retrieve --format=yaml": "Do not pass",
            "spend-request retrieve lsrq_1 --include card": "requires --output-file",
            "spend-request retrieve lsrq_1 --include=card": "requires --output-file",
            "spend-request retrieve lsrq_1 --include card --output-file /etc/card.json": "must point under",
            "spend-request retrieve lsrq_1 --include card --output-file=/etc/card.json": "must point under",
            "spend-request retrieve lsrq_1 --include card --output-file /workspace/../etc/card.json": "must point under",
            "spend-request retrieve lsrq_1 --include card --output-file /workspace/a.json --output-file /etc/b.json": "must point under",
            create: "Test mode is on",
            create + " --test=false": "Test mode is on",
            create + " --test --no-test": "Test mode is on",
            create + " --test --approve": "Do not pass",
            create + " --test --approval-detail {}": "Do not pass",
            "": "Give a link-cli command",
        }
        with patch.dict(os.environ, {"LINK_TEST_MODE": "1"}):
            for command, fragment in cases.items():
                result = await self.call(module.link_cli, self.alice, command=command)
                self.assertEqual(result["status"], "invalid", command)
                self.assertIn(fragment, result["message"], command)
            for allowed in (
                "auth status",
                "spend-request retrieve lsrq_1 --include card --output-file /workspace/card.json",
                "spend-request retrieve lsrq_1 --include=card --output-file=/workspace/card.json",
                create + " --test",
            ):
                args, error = module.validate_command(allowed)
                self.assertIsNone(error, allowed)
        # The operator, not the model, turns test mode off.
        with patch.dict(os.environ, {"LINK_TEST_MODE": "0"}):
            args, error = module.validate_command(create)
            self.assertIsNone(error)

    async def test_parallel_commands_for_one_user_keep_the_rotated_session(self):
        await self.call(module.link_login, self.alice)
        self.auth.approve()
        await self.call(module.link_finish_login, self.alice)
        # Both commands find the access token expired. Run at once, each would refresh with
        # the same refresh token and the fake Link would reject the second (invalid_grant).
        self.auth.expire_current()
        results = await asyncio.gather(
            self.call(module.link_cli, self.alice, command="user-info retrieve"),
            self.call(module.link_cli, self.alice, command="payment-methods list"),
        )
        self.assertEqual([r["status"] for r in results], ["ok", "ok"], results)
        saved = (await self.saved())["auth"]
        self.assertEqual(saved["refresh_token"], self.auth.current_refresh)
        again = await self.call(module.link_cli, self.alice, command="user-info retrieve")
        self.assertEqual(again["status"], "ok")
        self.assertEqual(self.backend.files(), [])

    async def test_logout_forgets_the_session(self):
        await self.call(module.link_login, self.alice)
        self.auth.approve()
        await self.call(module.link_finish_login, self.alice)
        gone = await self.call(module.link_logout, self.alice)
        self.assertEqual(gone["status"], "disconnected")
        self.assertEqual(await self.saved(), {"auth": None, "pendingDeviceAuth": None})
        self.assertIsNone(await module.sessions.load(self.alice))
        self.assertEqual(
            (await self.call(module.link_cli, self.alice, command="user-info retrieve"))["status"],
            "login_required",
        )

    async def test_store_fallback_keeps_sessions_per_user(self):
        module.sessions = module.StoreSessions()
        await self.call(module.link_login, self.alice)
        self.auth.approve()
        done = await self.call(module.link_finish_login, self.alice)
        self.assertEqual(done["status"], "connected")
        item = await self.store.aget((module.NAMESPACE, module._caller(self.alice)), "auth")
        self.assertTrue(module.has_session(item.value["auth"]))
        self.assertEqual(
            (await self.call(module.link_cli, self.bob, command="user-info retrieve"))["status"],
            "login_required",
        )

    async def test_missing_connection_slot_means_no_session(self):
        self.assertEqual(self.agent_auth.connections, {})
        self.assertIsNone(await module.sessions.load(self.alice))
        started = await self.call(module.link_login, self.alice)
        self.assertEqual(started["status"], "login_required")
        self.assertIn(("POST", "/v1/agent-auth/connections"), self.agent_auth.calls)

    async def test_existing_slot_for_another_user_starts_own_device_login(self):
        await self.call(module.link_login, self.bob)
        bobs_session = await self.saved("bob")
        self.assertIsNone(await module.sessions.load(self.alice))
        started = await self.call(module.link_login, self.alice)
        self.assertEqual(started["status"], "login_required")
        self.assertIn("verification_url", started)
        self.assertEqual(await self.saved("bob"), bobs_session)
        self.assertIsNotNone(await self.saved("alice"))
        self.assertEqual(self.link.requests, {})
        self.assertEqual(self.backend.files(), [])

    async def test_wrong_connection_kind_does_not_start_login_or_write(self):
        self.agent_auth.connections["link-session"] = {
            "id": "conn-wrong-kind",
            "kind": "oauth2",
            "credentials": {},
        }
        result = await self.call(module.link_login, self.alice)
        self.assertEqual(result["status"], "error")
        self.assertEqual(self.auth.calls, [])
        self.assertTrue(all(method == "GET" for method, _ in self.agent_auth.calls))

    async def test_owner_lookup_forbidden_does_not_start_login_or_write(self):
        await self.call(module.link_login, self.bob)
        self.auth.calls.clear()
        self.agent_auth.calls.clear()
        original = self.agent_auth.request

        def forbidden(method, path, *, body=None):
            if path.startswith("/v1/agent-auth/connections/conn-"):
                return 403, {"detail": "synthetic-private-denial"}
            return original(method, path, body=body)

        with patch.object(self.agent_auth, "request", side_effect=forbidden):
            result = await self.call(module.link_login, self.alice)
        self.assertEqual(result["status"], "error")
        self.assertNotIn("synthetic-private-denial", json.dumps(result))
        self.assertEqual(self.auth.calls, [])
        self.assertTrue(all(method == "GET" for method, _ in self.agent_auth.calls))

    async def test_storage_failures_become_tool_errors(self):
        def broken(method, path, *, body=None):
            return 500, {"detail": "synthetic outage"}

        def failing_client(runtime):
            return AgentAuth(
                AgentAuthContext(
                    base_url="https://auth.example.invalid",
                    principal_id="alice",
                    agent_id="d",
                    api_key="k",
                    workspace_id="w",
                ),
                request=broken,
            )

        module.sessions = module.ConnectionSessions("link-session", agent_auth=failing_client)
        result = await self.call(module.link_login, self.alice)
        self.assertEqual(result["status"], "error")
        self.assertIn("Connections failed", result["message"])
        self.assertEqual(self.auth.calls, [])

    async def test_preconditions(self):
        no_backend = runtime_for("alice", self.store, None)
        self.assertEqual(
            (await self.call(module.link_login, no_backend))["status"], "sandbox_required"
        )
        anonymous = runtime_for(None, self.store, self.backend)
        self.assertEqual(
            (await self.call(module.link_login, anonymous))["status"], "sign_in_required"
        )


if __name__ == "__main__":
    unittest.main()
