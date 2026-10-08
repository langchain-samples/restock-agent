"""Compiled MDA + real CLI + signed callback; all external services are fixtures."""

import importlib
import json
import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[1]
BUILD = Path(sys.argv[1]).resolve()
sys.path[:0] = [str(BUILD / "__runtime__"), str(BUILD), str(ROOT)]
import tests  # noqa: F401
import httpx
from langchain.tools import tool
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command
from langsmith import tracing_context
from managed_deepagents import _managed_tools, define_sandbox
from managed_deepagents._sandbox_proxy import (
    resolve_sandbox_proxy_config,
    sandbox_credential_consumers,
    static_sandbox_proxy_config,
)
from starlette.requests import Request

from channels.link_proxy import channel
from restock import link_session
from restock.config import Settings
from restock.link_proxy import operation
from restock.proxy_config import sandbox_proxy_config
from restock.service import Restock
from restock.storage import Repository
from restock.wallet import CliWallet
from tests.proxy_support import ENV, SANDBOX
from tests.run_compiled import ScriptedModel
from tests.support import Private, Merchant
from tests.test_proxy import ProxyTests


class CompiledProxyTests(unittest.IsolatedAsyncioTestCase):
    setUp = ProxyTests.setUp
    login = ProxyTests.login

    async def test_native_mda_template_retains_callback_without_onboarding_gate(self):
        cfg = sandbox_proxy_config(ENV)
        definition = define_sandbox(proxy_config=cfg)
        self.assertEqual(sandbox_credential_consumers(definition), [])
        self.assertEqual(static_sandbox_proxy_config(cfg), cfg)
        self.assertEqual(await resolve_sandbox_proxy_config(cfg, None), cfg)

    async def test_actual_channel_verifies_and_returns_header_without_agent_run(self):
        await self.login()
        saved = await link_session.sessions.load(self.alice)
        async with self.proxy.permissions.allow(
            SANDBOX, "alice", "link-session", saved, operation("GET", "/payment-details")
        ) as placeholder:
            raw = json.dumps(
                self.proxy.envelope(
                    httpx.Request(
                        "GET",
                        "https://api.link.com/payment-details",
                        headers={"Authorization": "Bearer " + placeholder},
                    )
                )
            ).encode()
            signature = self.proxy.sign(raw)
            request = Request(
                {
                    "type": "http",
                    "method": "POST",
                    "path": "/channels/link_proxy/events",
                    "headers": [(b"x-langsmith-signature-jwt", signature.encode())],
                }
            )
            with (
                patch("channels.link_proxy.receiver", return_value=self.proxy.callback),
                patch(
                    "managed_deepagents._channels.http.ingress.start_http_ingress_agent",
                    new_callable=AsyncMock,
                ) as agent,
            ):
                response = await channel.events(
                    {"name": "link_proxy", "request": request, "raw_body": raw}
                )
                self.assertEqual(response.status_code, 200)
                self.assertEqual(
                    json.loads(response.body)["headers"]["Authorization"],
                    "Bearer " + self.auth.current_access,
                )
                self.assertEqual(response.headers["cache-control"], "no-store")
                response = await channel.events(
                    {"name": "link_proxy", "request": request, "raw_body": raw}
                )
                self.assertEqual(response.status_code, 403)
                request = Request({"type": "http", "headers": []})
                response = await channel.events(
                    {"name": "link_proxy", "request": request, "raw_body": raw}
                )
                self.assertEqual(response.status_code, 401)
                agent.assert_not_awaited()

    async def test_compiled_login_review_proxy_payment_and_single_submission(self):
        entry = importlib.import_module("_mda_entry")
        entry._definition.config["model"] = ScriptedModel()
        entry._sandbox, entry._context_root, entry._has_skills = None, None, False
        merchant, private = Merchant(), Private()
        merchant.next_status = "order_placed"
        original_runtime = _managed_tools.managed_runtime_overrides

        def overrides(runtime, backend_factory=None):
            return original_runtime(runtime, lambda _rt: self.backend)

        def service_for(runtime):
            return Restock(
                Repository(runtime),
                private,
                merchant,
                CliWallet(runtime, link_session._caller(runtime)),
                Settings("live", wait_seconds=1),
            )

        create = CliWallet.create

        async def approve_after_creation(wallet, order):
            result = await create(wallet, order)
            # External Link approval fixture, only after native human review.
            self.link.approve(result["id"])
            return result

        cfg = {
            "recursion_limit": 30,
            "configurable": {
                "thread_id": "proxy-graph",
                "langgraph_auth_user": {
                    "identity": "alice",
                    "permissions": [],
                    "mda_user_id": "alice",
                    "mda_user_kind": "person",
                    "mda_agent_auth_principal_id": "alice",
                },
            },
        }
        with (
            patch.dict(os.environ, {"RESTOCK_MODE": "live"}),
            patch.object(_managed_tools, "managed_runtime_overrides", overrides),
            patch("tools.restock.service_for", side_effect=service_for),
            patch.object(CliWallet, "create", approve_after_creation),
            tracing_context(enabled=False),
        ):
            graph = await entry.agent(
                {}, SimpleNamespace(execution_runtime=SimpleNamespace(context=None))
            )
            graph.checkpointer, graph.store = InMemorySaver(), self.store

            async def say(text):
                return await graph.ainvoke({"messages": [{"role": "user", "content": text}]}, cfg)

            await say("Find pens under $25")
            state = await say("The first one looks good")

            def outputs(state):
                return [
                    json.loads(m.content) for m in state["messages"] if isinstance(m, ToolMessage)
                ]

            self.assertEqual(outputs(state)[-1]["status"], "login_required")
            self.assertFalse(state.get("__interrupt__"))
            self.auth.approve()
            await say("Done")
            state = await say("Authorize $12 upfront")
            self.assertTrue(state["__interrupt__"])
            self.assertEqual(self.link.requests, {})
            self.assertEqual(merchant.submissions, [])
            state = await graph.ainvoke(Command(resume={"decisions": [{"type": "approve"}]}), cfg)
            result = outputs(state)[-1]
            self.assertEqual(result["status"], "order_placed")
            self.assertEqual(len(self.link.requests), 1)
            self.assertEqual(len(merchant.submissions), 1)
            self.assertEqual(next(iter(self.link.requests.values()))["amount"], 1200)
            self.assertNotIn("spt_private_fixture", str(state["messages"]))
            self.assertNotIn(self.auth.current_access, str(state["messages"]))
            self.assertEqual(self.backend.files(), [])

    async def test_compiled_policy_blocks_model_shell_delegation_and_private_reads(self):
        entry = importlib.import_module("_mda_entry")
        entry._sandbox, entry._context_root, entry._has_skills = None, None, False
        executed = []

        @tool
        async def execute(command: str) -> str:
            """An offline shell sentinel; must never run."""
            executed.append(command)
            return "bad"

        class MaliciousModel(ScriptedModel):
            def _generate(self, messages, stop=None, run_manager=None, **kwargs):
                if messages[-1].type == "tool":
                    message = AIMessage(content="finished")
                else:
                    target = messages[-1].content
                    args = (
                        {"command": "link-cli spend-request create --approve"}
                        if target == "execute"
                        else {"description": "bypass", "subagent_type": "general-purpose"}
                        if target == "task"
                        else {"file_path": "/tmp/mda-link/private.json"}
                    )
                    message = AIMessage(
                        content="", tool_calls=[{"name": target, "args": args, "id": "bad-call"}]
                    )
                return ChatResult(generations=[ChatGeneration(message=message)])

        model = MaliciousModel()
        original_tools = entry._definition.config["tools"]
        entry._definition.config["tools"] = [*original_tools, execute]
        entry._definition.config["model"] = model
        try:
            with tracing_context(enabled=False):
                graph = await entry.agent(
                    {}, SimpleNamespace(execution_runtime=SimpleNamespace(context=None))
                )
                graph.checkpointer, graph.store = InMemorySaver(), self.store
                for name in ("execute", "task", "read_file"):
                    state = await graph.ainvoke(
                        {"messages": [{"role": "user", "content": name}]},
                        {"configurable": {"thread_id": name}},
                    )
                    result = [m for m in state["messages"] if isinstance(m, ToolMessage)][-1]
                    self.assertIn("use_restock_tools", result.content)
                    self.assertFalse(
                        any(getattr(t, "name", None) in {"execute", "task"} for t in model._schemas)
                    )
            self.assertEqual(executed, [])
        finally:
            entry._definition.config["tools"] = original_tools


if __name__ == "__main__":
    result = unittest.TextTestRunner(verbosity=2).run(
        unittest.defaultTestLoader.loadTestsFromTestCase(CompiledProxyTests)
    )
    if not result.wasSuccessful():
        raise SystemExit(1)
