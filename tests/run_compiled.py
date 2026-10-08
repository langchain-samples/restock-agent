"""Exercise the generated MDA graph and real interrupt/resume without external services."""

from __future__ import annotations

import asyncio
import importlib
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
BUILD = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else ROOT / ".mda/build"
sys.path[:0] = [str(BUILD / "__runtime__"), str(BUILD), str(ROOT)]
import tests  # noqa: F401

os.environ["RESTOCK_MODE"] = "rehearsal"

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.store.memory import InMemoryStore
from langgraph.types import Command
from langsmith import tracing_context
from pydantic import PrivateAttr


class ScriptedModel(BaseChatModel):
    """Deterministic tool choices test wiring, not real-model shopping judgment."""

    _schemas: list = PrivateAttr(default_factory=list)

    @property
    def _llm_type(self):
        return "restock-offline-fixture"

    def bind_tools(self, tools, **kwargs):
        self._schemas = tools
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        last = messages[-1]
        name, args = None, {}
        content = ""
        if last.type == "human":
            if "ship" in str(last.content).lower():
                last_result = json.loads(
                    [m for m in messages if isinstance(m, ToolMessage)][-1].content
                )
                name, args = (
                    "check_restock_order",
                    {
                        "order_id": last_result["order_id"],
                        "detail": "status",
                    },
                )
            elif "authorize" in str(last.content).lower():
                last_result = json.loads(
                    [m for m in messages if isinstance(m, ToolMessage)][-1].content
                )
                name, args = (
                    "set_restock_payment_amount",
                    {"order_id": last_result["order_id"], "amount_cents": 1200},
                )
            elif "done" in str(last.content).lower():
                name = "link_finish_login"
            elif "first" in str(last.content).lower():
                searches = [
                    json.loads(m.content)
                    for m in messages
                    if isinstance(m, ToolMessage) and m.name == "search_restock_products"
                ]
                name = "prepare_restock_order"
                args = {
                    "selections": [
                        {"product_id": searches[-1]["products"][0]["product_id"], "quantity": 1}
                    ],
                    "budget_cents": 2500,
                }
            else:
                name, args = (
                    "search_restock_products",
                    {"query": "black ballpoint pens", "budget_cents": 2500},
                )
        elif isinstance(last, ToolMessage):
            if not str(last.content).startswith("{"):
                raise AssertionError(
                    f"Expected a public JSON tool result from {last.name}: {last.content}"
                )
            value = json.loads(last.content)
            if last.name == "set_restock_payment_amount" and value.get("status") == "prepared":
                name, args = "request_restock_payment", {"order_id": value["order_id"]}
            elif (
                last.name == "link_finish_login"
                and value.get("next_action") == "request_restock_payment"
            ):
                name, args = "request_restock_payment", {"order_id": value["order_id"]}
            elif (
                last.name == "request_restock_payment"
                and value.get("status") == "awaiting_link_approval"
            ):
                name, args = "wait_for_restock_approval", {"order_id": value["order_id"]}
            else:
                content = value.get("order_update") or json.dumps(value)
        message = AIMessage(
            content=content,
            tool_calls=[{"name": name, "args": args, "id": "call-" + name}] if name else [],
        )
        return ChatResult(generations=[ChatGeneration(message=message)])


async def run():
    entry = importlib.import_module("_mda_entry")
    assert Path(entry.__file__).parent == BUILD
    entry._definition.config["model"] = ScriptedModel()
    # No cloud sandbox/context in this offline graph test. Real CLI/sandbox calls
    # are covered separately against local fake servers, never a live wallet.
    entry._sandbox = None
    entry._context_root = None
    entry._has_skills = False
    runtime = SimpleNamespace(execution_runtime=SimpleNamespace(context=None))
    with tracing_context(enabled=False):
        graph = await entry.agent({}, runtime)
        graph.checkpointer, graph.store = InMemorySaver(), InMemoryStore()

        def config(thread, caller="alice"):
            return {
                "recursion_limit": 30,
                "configurable": {
                    "thread_id": thread,
                    "langgraph_auth_user": {
                        "identity": caller,
                        "permissions": [],
                        "mda_user_id": caller,
                        "mda_user_kind": "person",
                        "mda_agent_auth_principal_id": caller,
                    },
                },
            }

        async def start(thread):
            cfg = config(thread)
            await graph.ainvoke(
                {"messages": [{"role": "user", "content": "Find some pens under $25"}]}, cfg
            )
            state = await graph.ainvoke(
                {"messages": [{"role": "user", "content": "The first one looks good"}]}, cfg
            )
            assert not state.get("__interrupt__"), "Shopping budget must not open payment review"
            state = await graph.ainvoke(
                {"messages": [{"role": "user", "content": "Authorize $12 upfront"}]}, cfg
            )
            pause = state["__interrupt__"][0].value
            assert pause["action_requests"][0]["args"]["budget_cents"] == 2500
            assert pause["action_requests"][0]["args"]["payment_amount_cents"] == 1200
            assert pause["review_configs"][0]["allowed_decisions"] == ["approve", "reject"]
            assert "shipping_address" not in json.dumps(pause)
            return cfg

        cfg = await start("approve")
        # Recreate compiled graph: persisted interrupt resumes after process restart.
        saver, store = graph.checkpointer, graph.store
        graph = await entry.agent({}, runtime)
        graph.checkpointer, graph.store = saver, store
        done = await graph.ainvoke(Command(resume={"decisions": [{"type": "approve"}]}), cfg)
        assert not done.get("__interrupt__")
        outputs = [json.loads(m.content) for m in done["messages"] if isinstance(m, ToolMessage)]
        assert outputs[-1]["status"] == "rehearsal_complete", outputs[-1]
        print("PASS generated MDA graph: review, restart, approve, continue automatically")
        cfg = await start("reject")
        done = await graph.ainvoke(Command(resume={"decisions": [{"type": "reject"}]}), cfg)
        result = json.loads([m for m in done["messages"] if isinstance(m, ToolMessage)][-1].content)
        assert result["status"] == "canceled", result
        print("PASS generated MDA graph: reject cancels without a payment request")
        for index, decision in enumerate(
            (
                {"approved": True},
                {"decisions": None},
                {"decisions": [None]},
                {
                    "decisions": [
                        {"type": "edit", "edited_action": {"args": {"budget_cents": 999999}}}
                    ]
                },
            )
        ):
            cfg = await start(f"malformed-{index}")
            done = await graph.ainvoke(Command(resume=decision), cfg)
            result = json.loads(
                [m for m in done["messages"] if isinstance(m, ToolMessage)][-1].content
            )
            assert result["status"] == "canceled"
        print("PASS generated MDA graph: unrecognized approval fails closed")


if __name__ == "__main__":
    asyncio.run(run())
