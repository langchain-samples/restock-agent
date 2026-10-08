"""Run the generated graph through real MDA channel routing with fake external services.

Identity has already been resolved in this fixture. This does not test Slack
installation, signature verification, the rendered cards, or hosted identity.
"""

from __future__ import annotations

import asyncio
import importlib
import json
import os
import shutil
import sys
import time
import uuid
from contextlib import ExitStack, contextmanager, nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[1]
BUILD = Path(sys.argv[1]).resolve()
sys.path[:0] = [str(BUILD / "__runtime__"), str(BUILD), str(ROOT)]
import tests  # noqa: F401

from langchain_core.messages import ToolMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.store.memory import InMemoryStore
from langgraph.types import Command
from langsmith import tracing_context
from managed_deepagents._channel import collect_channels
from managed_deepagents._channels.messaging_registry import register_channel_messaging
from managed_deepagents._channels.trigger.http import _observe_accepted_run, _start_event_run
from managed_deepagents._channels.trigger.types import parse_trigger_channel_event

from restock import link_session
from restock.config import Settings
from restock.service import Restock
from restock.storage import Repository
from tests.run_compiled import ScriptedModel
from tests.support import Merchant, Private, Wallet


@contextmanager
def real_login_fixture():
    """Keep the real login, optional reader and principal resolver; replace HTTP/sandbox only."""
    from managed_deepagents import _managed_tools
    from managed_deepagents._agent_auth import AgentAuth

    from tests.fakes import FakeAgentAuthServer, FakeLink, FakeLinkAuth, serve_http
    from tests.local_sandbox import LocalSandboxBackend

    link = FakeLink(token="unused")
    auth, agent_auth = FakeLinkAuth(link), FakeAgentAuthServer()
    other_secret = json.dumps({"auth": {"access_token": "other-caller-fixture"}})
    agent_auth.request(
        "POST",
        "/v1/agent-auth/connections",
        body={
            "slug": "link-session",
            "credential": {
                "kind": "secret",
                "owner_type": "user",
                "owner_id": "bob",
                "value": other_secret,
            },
        },
    )
    with ExitStack() as stack:
        api_server, api_url = serve_http(link.handler)
        auth_server, auth_url = serve_http(auth.handler)
        for server in (api_server, auth_server):
            stack.callback(server.server_close)
            stack.callback(server.shutdown)
        backend = LocalSandboxBackend(
            ROOT / "node_modules/@stripe/link-cli/dist/cli.js",
            api_url=api_url,
            auth_url=auth_url,
            auth_dir=link_session.AUTH_DIR,
        )
        stack.callback(shutil.rmtree, backend.root)
        original_overrides = _managed_tools.managed_runtime_overrides

        def runtime_overrides(runtime, backend_factory=None):
            return original_overrides(runtime, lambda _runtime: backend)

        def http(self, method, path, *, body=None):
            assert self.context.principal_id in {"alice", "bob"}
            return agent_auth.request(method, path, body=body)

        stack.enter_context(
            patch.object(_managed_tools, "managed_runtime_overrides", runtime_overrides)
        )
        stack.enter_context(patch.object(AgentAuth, "_urlopen_json", http))
        stack.enter_context(
            patch.object(link_session, "sessions", link_session.ConnectionSessions())
        )
        stack.enter_context(
            patch.dict(
                os.environ,
                {
                    "LANGSMITH_API_KEY": "synthetic-platform-key",
                    "LANGSMITH_WORKSPACE_ID": "synthetic-workspace",
                    "LANGSMITH_HOST_PROJECT_ID": "synthetic-deployment",
                    "MDA_LOCAL_DEV": "0",
                },
            )
        )
        yield SimpleNamespace(auth=auth, link=link)
        assert agent_auth.read("link-session", "bob") == other_secret
        assert link_session.has_session(agent_auth.read("link-session", "alice"))
        assert backend.files() == []
        assert auth.calls.count(("/device/code", "")) == 1
        assert link.requests == {}, "Login must not create a payment request"


async def scenario(
    entry,
    mode,
    decision="approve",
    *,
    failed_delivery=False,
    caller="alice",
    needs_login=False,
    real_login=False,
    recover_pause=False,
    email_updates=False,
    automatic_updates=False,
):
    private, merchant, wallet = Private(), Merchant(), Wallet()
    if automatic_updates:
        merchant.webhook = AsyncMock(return_value="zn_whsec_synthetic-notification-secret")
    if email_updates:
        private.value["notification_email"] = "office@example.invalid"
        merchant.email_fee = 50  # Fixture only; Zinc's actual fee is discovered.
        merchant.response_fields["customer_notifications"] = {
            "email": "office@example.invalid",
            "delivered": None,
        }
    merchant.challenge = AsyncMock(wraps=merchant.challenge)
    services = []
    merchant.next_status = "order_placed"
    timeline = []
    posted, events, states = [], [], {}
    connected = not needs_login
    saver, store = InMemorySaver(), InMemoryStore()
    source_thread = "fixture-slack:" + str(uuid.uuid4())
    channel_id = str(uuid.uuid4())
    address = {"channel": "fixture-channel", "thread_ts": "fixture-thread"}

    class Gateway:
        failed = False

        async def post_action(self, channel_id, action):
            assert channel_id == expected_channel_id
            assert action["address"] == address
            assert action["source_thread_key"] == source_thread
            text = action["message"]["text"]
            if text.startswith(("TEST approval:", "Payment approval:")):
                assert "https://app.link.com/approve/" in text
                assert wallet.created == 1
                assert wallet.tokens == 0 and not merchant.submissions
                if failed_delivery and not self.failed:
                    self.failed = True
                    raise RuntimeError("synthetic-private-transport-detail")
                timeline.append("link_url_visible")
                if not failed_delivery:
                    for request in wallet.requests.values():
                        request["status"] = "approved"
            posted.append(text)
            return {"id": "fixture-message"}

        async def post_runtime_event(self, channel_id, event):
            events.append(event)
            return {}

    expected_channel_id = channel_id
    gateway = Gateway()
    original_retrieve = wallet.retrieve

    async def retrieve(request_id):
        assert "link_url_visible" in timeline, "Polling began before the URL was visible"
        timeline.append("poll")
        return await original_retrieve(request_id)

    wallet.retrieve = retrieve

    def service_for(runtime):
        service = Restock(
            Repository(runtime), private, merchant, wallet, Settings(mode, wait_seconds=1)
        )
        services.append(service)
        return service

    async def login(**kwargs):
        if connected:
            return {"status": "connected"}
        return {
            "status": "login_required",
            "verification_url": "https://app.link.com/device?code=FIXTURE",
        }

    async def finish_login(**kwargs):
        nonlocal connected
        connected = True
        return {"status": "connected"}

    async def invoke(**kwargs):
        user = kwargs["user_id"]
        config = {
            "recursion_limit": 30,
            "configurable": {
                "thread_id": kwargs["thread_id"],
                "langgraph_auth_user": {
                    "identity": user,
                    "permissions": [],
                    "mda_user_id": user,
                    "mda_user_kind": "person",
                    "mda_agent_auth_principal_id": user,
                    "mda_source_provider": kwargs["source_provider"],
                    "mda_source_thread_id": kwargs["source_thread_key"],
                },
            },
        }
        data = (
            Command(resume=kwargs["resume"])
            if "resume" in kwargs
            else {"messages": kwargs["messages"]}
        )
        context = {"channel": kwargs["binding"]}
        # Match Agent Server's per-run factory, including the authenticated source
        # needed by MDA to select its channel context schema. Storage survives it.
        graph = await entry.agent(
            config, SimpleNamespace(execution_runtime=SimpleNamespace(context=context))
        )
        graph.checkpointer, graph.store = saver, store
        state = await graph.ainvoke(data, config, context=context)
        run_id = str(uuid.uuid4())
        states[run_id] = state
        return {"thread_id": kwargs["thread_id"], "run_id": run_id}

    async def observe(**kwargs):
        state = states[kwargs["run_id"]]
        pauses = state.get("__interrupt__", [])
        return {
            "status": "interrupted" if pauses else "success",
            "values": {
                "messages": [message.model_dump() for message in state["messages"]],
                "__interrupt__": [{"id": pause.id, "value": pause.value} for pause in pauses],
            },
        }

    async def deliver(content=None, resume=None, user="alice"):
        event = {
            "version": "1",
            "type": "message" if resume is None else "interrupt_resume",
            "event_id": str(uuid.uuid4()),
            "delivery_id": str(uuid.uuid4()),
            "created_at": "2026-10-06T12:00:00Z",
            "channel_id": channel_id,
            "source_thread_key": source_thread,
            "address": address,
        }
        if resume is None:
            event["input"] = {"messages": [{"role": "user", "content": content}]}
        else:
            event["interaction"] = {"type": "interrupt_resume", "resume": resume}
        event = parse_trigger_channel_event(event)
        started = await _start_event_run(
            event=event,
            assistant_id="restock",
            source_provider="slack",
            channel_name="slack",
            start_agent=invoke,
            resume_agent=invoke,
            user_id=user,
        )
        await _observe_accepted_run(
            event=event,
            started=started,
            client=gateway,
            observe_agent=observe,
            source_provider="slack",
            source_thread_key=source_thread,
            user_id=user,
        )
        return states[started["run_id"]]

    with (
        patch.dict(os.environ, {"RESTOCK_MODE": mode}),
        patch.dict(
            os.environ,
            {
                "RESTOCK_PUBLIC_URL": "https://restock.example.test" if automatic_updates else "",
                "RESTOCK_UPDATES_SIGNING_KEY": "synthetic-app-signing-key-for-offline-tests"
                if automatic_updates
                else "",
            },
        ),
        patch.object(
            link_session.ConnectionSessions,
            "_client",
            return_value=SimpleNamespace(context=SimpleNamespace(principal_id=caller)),
        )
        if automatic_updates
        else nullcontext(),
        patch.object(link_session.ConnectionSessions, "save", new_callable=AsyncMock)
        if automatic_updates
        else nullcontext(),
        patch("tools.restock.service_for", side_effect=service_for),
        real_login_fixture() if real_login else nullcontext() as login_fixture,
        nullcontext()
        if real_login
        else patch(
            "tools.restock.link_session.link_login.coroutine", new=AsyncMock(side_effect=login)
        ),
        nullcontext()
        if real_login
        else patch(
            "tools.restock.link_session.link_finish_login.coroutine",
            new=AsyncMock(side_effect=finish_login),
        ),
        patch(
            "managed_deepagents._channels.trigger.client.create_trigger_client",
            return_value=gateway,
        ),
    ):
        await deliver("Find black pens under $25")
        recovery_order = None
        if recover_pause:
            from managed_deepagents import connections

            async def old_load(self, runtime):
                return await connections.get(self.slug, {"type": "user"})

            with patch.object(link_session.ConnectionSessions, "load", old_load):
                old_state = await deliver("The first one looks good, one pack")
            assert old_state["__interrupt__"][0].value == {
                "type": "credential_authorization_required",
                "message": "Connect the following integrations to continue.",
                "credentials": [{"slug": "link-session", "kind": "secret"}],
            }
            repo = services[-1].repo
            recovery_order = (await repo.get("active:" + repo.thread))["id"]
            assert wallet.created == 0 and not login_fixture.auth.calls
            # A normal new Slack message starts a run, not a Studio Command(resume).
            # The next factory uses the fixed reader and the same checkpoint/store.
        state = await deliver("The first one looks good, one pack")
        if needs_login:
            assert not state.get("__interrupt__")
            result = json.loads(
                [m for m in state["messages"] if isinstance(m, ToolMessage)][-1].content
            )
            assert result["status"] == "login_required", result
            saved_order = result["order_id"]
            if recover_pause:
                assert saved_order == recovery_order
            assert result["verification_url"] in posted[-1]
            assert wallet.created == 0
            if real_login:
                repo = services[-1].repo
                order = await repo.order(saved_order)
                assert "challenge" not in order, (
                    "Obtain payment instructions after login and amount choice"
                )
                login_fixture.auth.approve()
            state = await deliver("Done, I'm connected")
            assert wallet.created == 0, "Login consent must not authorize payment"
            assert not state.get("__interrupt__"), "Login must not choose a payment amount"
            finished = json.loads(
                [m for m in state["messages"] if isinstance(m, ToolMessage)][-1].content
            )
            assert finished["order_id"] == saved_order
            assert sum(
                isinstance(m, ToolMessage) and m.name == "prepare_restock_order"
                for m in state["messages"]
            ) == (2 if recover_pause else 1)
        assert not state.get("__interrupt__"), "Product selection must not turn budget into payment"
        assert wallet.created == 0 and merchant.challenge.await_count == 0
        state = await deliver("Authorize $12 upfront")
        pause = state["__interrupt__"][0]
        assert pause.value["action_requests"][0]["args"]["payment_amount_cents"] == 1200
        assert pause.value["action_requests"][0]["args"]["budget_cents"] == 2500
        assert events[-1]["type"] == "run.interrupted"
        assert events[-1]["interrupts"][0]["id"] == pause.id
        assert wallet.created == 0
        if real_login:
            # It can expire while the native Slack review is displayed.
            order = await repo.order(saved_order)
            order["challenge"]["expires_at"] = time.time() - 1
            await repo.save(order)
        state = await deliver(resume={pause.id: {"decisions": [{"type": decision}]}}, user=caller)
        outputs, interrupted_results = [], []
        for message in state["messages"]:
            if not isinstance(message, ToolMessage):
                continue
            try:
                outputs.append(json.loads(message.content))
            except json.JSONDecodeError:
                # Deep Agents inserts a text result for the abandoned tool call
                # when a new user message replaces an interrupted run.
                assert recover_pause and message.name == "prepare_restock_order"
                interrupted_results.append(message.content)
        assert len(interrupted_results) == (1 if recover_pause else 0)
        if recover_pause:
            records = await repo.store.asearch(repo.namespace)
            assert sum(record.key.startswith("order:") for record in records) == 1
        result = outputs[-1]
        if real_login:
            assert merchant.challenge.await_count == 2, "Refresh the same cart after review expires"
            assert result["order_id"] == saved_order
        expected = (
            "needs_attention"
            if caller != "alice"
            else "canceled"
            if decision == "reject"
            else "awaiting_link_approval"
            if failed_delivery
            else "approved_test_mode"
            if mode == "link-test"
            else "order_placed"
        )
        assert result["status"] == expected, result
        if caller != "alice" or decision == "reject":
            assert wallet.created == 0 and not merchant.submissions
        elif failed_delivery:
            assert result["approval_delivery"] == "reply_required"
            assert "poll" not in timeline and not merchant.submissions and wallet.tokens == 0
        else:
            assert timeline[0] == "link_url_visible" and timeline[1] == "poll"
            assert wallet.created == 1
            assert next(iter(wallet.requests.values()))["amount"] == 1200
            assert len(merchant.submissions) == (1 if mode == "live" else 0)
            assert wallet.tokens == (1 if mode == "live" else 0)
        assert events[-1]["type"] == "run.completed"
        assert (result.get("order_update") or expected) in posted[-1], (
            "Final result did not return to the Slack conversation"
        )
        if email_updates:
            assert result["fee_cents"] == 150 and result["payment_amount_cents"] == 1200
            assert result["merchant_order_id"] in posted[-1]
            assert "retailer confirmed" in posted[-1]
            assert "Tracking is not available" not in posted[-1]
            assert "email_updates_status" not in result
            assert "slack_updates_status" not in result
            record = next(iter(merchant.orders.values()))
            record["tracking_numbers"] = [
                {
                    "carrier": "UPS",
                    "tracking_number": "1ZFIXTURE",
                    "status": "in_transit",
                    "zinc_tracking_url": "https://t.17track.net/en#nums=1ZFIXTURE",
                    "estimated_delivery_date": "2026-10-12",
                }
            ]
            record["customer_notifications"]["delivered"] = True
            await deliver("Did it ship?")
            assert "1ZFIXTURE" in posted[-1] and "2026-10-12" in posted[-1]
            assert "email was delivered" in posted[-1]
            assert result["merchant_order_id"] in posted[-1]
            assert wallet.created == 1 and wallet.tokens == 1 and len(merchant.submissions) == 1
        if automatic_updates:
            import hashlib
            import hmac
            from datetime import datetime, timezone
            from starlette.requests import Request
            from channels.zinc import channel as zinc_channel
            from restock.notification_runtime import post_update
            from restock.notifications import NAMESPACE, Receiver, route_key, unseal

            assert "slack_updates_status" not in result
            saved_order = await services[-1].repo.order(result["order_id"])
            assert saved_order["slack_updates_status"] == "enabled"
            merchant.webhook.assert_awaited_once()
            saved = await store.aget(NAMESPACE, route_key(result["merchant_order_id"]))
            route = unseal(saved.value, os.environ["RESTOCK_UPDATES_SIGNING_KEY"])
            assert route["principal_id"] == caller
            assert route["target"]["address"] == address
            assert route["target"]["source_thread_key"] == source_thread
            raw = json.dumps(
                {
                    "event": "order.tracking_received",
                    "status": "shipped",
                    "order_id": result["merchant_order_id"],
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "data": {
                        "tracking_numbers": [{"carrier": "UPS", "tracking_number": "1ZFIXTURE"}]
                    },
                }
            ).encode()
            secret = "zn_whsec_synthetic-notification-secret"
            signature = hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
            receiver = Receiver(
                store,
                AsyncMock(return_value=secret),
                post_update,
                signing_key=os.environ["RESTOCK_UPDATES_SIGNING_KEY"],
                callback_url="https://restock.example.test/channels/zinc/events",
            )
            request = Request(
                {"type": "http", "headers": [(b"x-webhook-signature", signature.encode())]}
            )
            before = len(posted)
            with patch("channels.zinc.receiver", return_value=receiver):
                for _ in range(2):
                    response = await zinc_channel.events(
                        {"name": "zinc", "request": request, "raw_body": raw}
                    )
                    assert response.status_code == 200
            assert len(posted) == before + 1
            assert "1ZFIXTURE" in posted[-1]
            assert wallet.tokens == 1 and len(merchant.submissions) == 1

        transcript = json.dumps([posted, events, outputs, interrupted_results])
        for private_value in (
            "spt_synthetic_private",
            "100 Example Street",
            "synthetic-private-transport-detail",
            "office@example.invalid",
        ):
            assert private_value not in transcript
        print(
            f"PASS compiled Slack flow: {mode}, {decision}, caller={caller}, delivery_failure={failed_delivery}, prompted_login={needs_login}, real_login={real_login}, recovered_pause={recover_pause}, email_updates={email_updates}"
        )


async def run():
    entry = importlib.import_module("_mda_entry")
    assert Path(entry.__file__).parent == BUILD
    entry._definition.config["model"] = ScriptedModel()
    entry._sandbox = None
    entry._context_root = None
    entry._has_skills = False
    register_channel_messaging(
        collect_channels([("channels/slack.py", importlib.import_module("channels.slack"))])
    )
    with tracing_context(enabled=False):
        await scenario(entry, "link-test")
        await scenario(entry, "live")
        await scenario(entry, "live", email_updates=True)
        await scenario(entry, "live", automatic_updates=True)
        await scenario(entry, "live", "reject")
        await scenario(entry, "live", failed_delivery=True)
        await scenario(entry, "live", caller="bob")
        await scenario(entry, "link-test", needs_login=True)
        await scenario(entry, "link-test", needs_login=True, real_login=True)
        await scenario(entry, "link-test", needs_login=True, real_login=True, recover_pause=True)


if __name__ == "__main__":
    asyncio.run(run())
