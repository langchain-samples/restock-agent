"""Caller-scoped order state, and private Connection values."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import weakref
from contextlib import asynccontextmanager
from typing import Any

from managed_deepagents import connections

from restock.config import RestockError
from restock.link_session import ConnectionSessions, _caller

_locks: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()


def fingerprint(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def caller_for(runtime) -> str:
    caller = _caller(runtime)
    if not caller:
        raise RestockError("sign_in_required")
    return caller


class Repository:
    def __init__(self, runtime):
        self.runtime = runtime
        self.caller = caller_for(runtime)
        self.store = getattr(runtime, "store", None)
        self.thread = runtime.config.get("configurable", {}).get("thread_id")
        if self.store is None or not self.thread:
            raise RestockError("store_and_thread_required")
        self.namespace = ("restock-v1", fingerprint(self.caller))

    async def get(self, key: str) -> dict | None:
        item = await self.store.aget(self.namespace, key)
        return dict(item.value) if item else None

    async def put(self, key: str, value: dict) -> None:
        await self.store.aput(self.namespace, key, value)

    async def order(self, order_id: str) -> dict:
        order = await self.get("order:" + order_id)
        if order is None or order.get("thread") != self.thread:
            raise RestockError("order_not_found_in_this_conversation")
        return order

    async def save(self, order: dict) -> None:
        await self.put("order:" + order["id"], order)

    @asynccontextmanager
    async def lock(self):
        # This serializes one process only. Zinc's durable idempotency key also
        # protects merchant submission across processes. Link refresh still needs
        # a distributed lease before multi-worker production use.
        locks = _locks.setdefault(asyncio.get_running_loop(), {})
        lock = locks.setdefault(self.namespace, asyncio.Lock())
        async with lock:
            yield


class PrivateConnections:
    def __init__(self, runtime):
        self.runtime = runtime

    async def office(self, slug: str) -> dict:
        raw = await connections.get(slug, {"type": "agent"})
        try:
            value = json.loads(raw)
        except (TypeError, ValueError):
            raise RestockError("invalid_office_connection") from None
        return validate_office(value)

    async def zinc_key(self, slug: str) -> str:
        return await connections.get(slug, {"type": "agent"})

    async def save_order_key(self, order_id: str, key: str) -> None:
        # Order tracking keys are secrets and never go in graph state/tool output.
        await ConnectionSessions("restock-order-" + order_id).save(self.runtime, key)

    async def order_key(self, order_id: str) -> str:
        return await connections.get("restock-order-" + order_id, {"type": "user"})


def notification_email(value: Any) -> str:
    """Accept one ordinary mailbox, without display names or header characters."""
    if not isinstance(value, str):
        raise RestockError("invalid_notification_email")
    value = value.strip()
    if (
        len(value) > 254
        or not re.fullmatch(
            r"[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+(?:\.[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+)*"
            r"@[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
            r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+",
            value,
        )
        or len(value.split("@", 1)[0]) > 64
    ):
        raise RestockError("invalid_notification_email")
    return value


def validate_office(value: Any) -> dict:
    if not isinstance(value, dict) or not isinstance(value.get("shipping_address"), dict):
        raise RestockError("invalid_office_connection")
    address = value["shipping_address"]
    required = (
        "first_name",
        "last_name",
        "address_line1",
        "city",
        "state",
        "postal_code",
        "country",
        "phone_number",
    )
    if any(not isinstance(address.get(k), str) or not address[k].strip() for k in required):
        raise RestockError("invalid_office_connection")
    if address["country"] != "US":
        raise RestockError("us_shipping_required")
    allowed = (*required, "address_line2")
    if any(len(str(address.get(k, ""))) > 150 for k in allowed):
        raise RestockError("invalid_office_connection")
    label = value.get("label", "Office")
    if not isinstance(label, str) or not 1 <= len(label) <= 80:
        raise RestockError("invalid_office_connection")
    result = {"label": label, "shipping_address": {k: address[k] for k in allowed if k in address}}
    if value.get("notification_email") not in (None, ""):
        result["notification_email"] = notification_email(value["notification_email"])
    return result
