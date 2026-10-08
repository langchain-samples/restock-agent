"""Verified Zinc events, durable routing, and fixed Slack order updates.

This receiver cannot create a payment or order. Routes are captured from MDA's
authenticated Slack context and signed before storage. Zinc's webhook secret
stays in its original caller's Connection, separate from the Link session.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import re
import time
import uuid
from datetime import datetime
from urllib.parse import urlsplit

from restock.config import RestockError
from restock.updates import order_update, public_delivery, public_failure_reasons

SAFE_ID = re.compile(r"[A-Za-z0-9_-]{1,150}")

NAMESPACE = ("restock-notifications-v1",)
EVENTS = {
    "order.placed": {"order_placed"},
    "order.failed": {"order_failed", "failed"},
    "order.tracking_received": {"shipped", "order_placed"},
    "order.delivered": {"delivered"},
    "order.cancelled": {"cancelled", "cancelled_by_retailer"},
}


def configuration(environment=None):
    environment = os.environ if environment is None else environment
    base = environment.get("RESTOCK_PUBLIC_URL", "").rstrip("/")
    key = environment.get("RESTOCK_UPDATES_SIGNING_KEY", "")
    try:
        parsed = urlsplit(base)
        port = parsed.port
    except ValueError:
        raise RestockError("order_updates_not_configured") from None
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.path
        or port not in (None, 443)
        or re.search(r"\s", base)
        or len(key) < 32
    ):
        raise RestockError("order_updates_not_configured")
    return base + "/channels/zinc/events", key


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def sealed(value, key):
    return {
        "value": value,
        "mac": hmac.new(key.encode(), encoded(value), hashlib.sha256).hexdigest(),
    }


def unseal(record, key):
    if not isinstance(record, dict) or not isinstance(record.get("mac"), str):
        raise RestockError("invalid_update_binding")
    value = record.get("value")
    if not isinstance(value, dict) or not hmac.compare_digest(
        record["mac"], sealed(value, key)["mac"]
    ):
        raise RestockError("invalid_update_binding")
    return value


def route_key(merchant_order_id):
    return "route:" + hashlib.sha256(merchant_order_id.encode()).hexdigest()


def callback_event(raw):
    if len(raw) > 131072:
        raise RestockError("invalid_order_event")
    try:
        event = json.loads(raw)
        if not isinstance(event, dict):
            raise ValueError()
        oid = event.get("order_id")
        if not isinstance(oid, str) or not SAFE_ID.fullmatch(oid):
            raise ValueError()
        stamp = datetime.fromisoformat(str(event.get("timestamp", "")).replace("Z", "+00:00"))
        if stamp.tzinfo is None or stamp.timestamp() > time.time() + 300:
            raise ValueError()
        if not isinstance(event.get("event"), str):
            raise ValueError()
        if event["event"] in EVENTS and event.get("status") not in EVENTS[event["event"]]:
            raise ValueError()
        return event, stamp.timestamp()
    except (ValueError, TypeError, OverflowError, UnicodeError):
        raise RestockError("invalid_order_event") from None


def public_event(event):
    """Signed provider text still never becomes agent instructions or Slack text."""
    name = event["event"]
    data = event.get("data")
    data = data if isinstance(data, dict) else {}
    status = (
        "order_failed"
        if name == "order.failed"
        else "cancelled_by_retailer"
        if name == "order.cancelled"
        else "order_placed"
    )
    return {
        "event": name,
        "mode": "live",
        "merchant_order_id": event["order_id"],
        "merchant_status": status,
        "failure_reasons": public_failure_reasons({"status": status, "job_result": data}),
        **public_delivery(data),
    }


def notification_text(public):
    detail = (
        "status" if public["event"] in {"order.tracking_received", "order.delivered"} else "summary"
    )
    message = order_update(public, detail=detail)
    if public["event"] == "order.tracking_received":
        message = "Zinc has added tracking to your order.\n" + message
    elif public["event"] == "order.delivered":
        message = "Zinc reports that all packages for this order were delivered.\n" + message
    return message


async def enable_updates(runtime, service, result):
    """Subscribe after confirmed submission. Any setup failure leaves payment state intact."""
    if result.get("mode") != "live" or not result.get("merchant_order_id"):
        return result
    if getattr(getattr(runtime, "channel", None), "provider", None) != "slack":
        return result
    from managed_deepagents._channels.binding import resolve_channel_binding

    from restock.link_session import ConnectionSessions
    from restock.service import view

    repo = service.repo
    async with repo.lock():
        order = await repo.order(result["order_id"])
        try:
            if order.get("tracking_access_status") == "unavailable":
                raise RestockError("order_updates_tracking_unavailable")
            url, signing_key = configuration()
            binding = resolve_channel_binding(runtime.context, runtime.config.get("configurable"))
            if (
                not binding
                or binding["provider"] != "slack"
                or binding["transport"] != "trigger_server"
            ):
                raise RestockError("order_updates_need_hosted_slack")
            secret_store = ConnectionSessions(
                "restock-updates-" + order["id"], display_name="Zinc order notifications"
            )
            client = await asyncio.to_thread(secret_store._client, runtime)
            owner = client.context.principal_id
            existing = await repo.store.aget(NAMESPACE, route_key(order["merchant_order_id"]))
            newly_enabled = existing is None
            if existing is not None:
                route = unseal(existing.value, signing_key)
                if (
                    route["order_reference"] != order["id"]
                    or route["merchant_order_id"] != order["merchant_order_id"]
                    or route["thread_id"] != repo.thread
                    or route["callback_url"] != url
                    or route["principal_id"] != owner
                    or route["caller"] != repo.caller
                    or route["target"] != binding["target"]
                ):
                    raise RestockError("invalid_update_binding")
                order["slack_updates_status"] = "enabled"
            else:
                key = await service.private.order_key(order["id"])
                secret = await service.zinc.webhook(key, url)
                await secret_store.save(runtime, secret)
                route = {
                    "order_reference": order["id"],
                    "merchant_order_id": order["merchant_order_id"],
                    "principal_id": owner,
                    "caller": repo.caller,
                    "thread_id": repo.thread,
                    "secret_slug": secret_store.slug,
                    "callback_url": url,
                    "channel_name": binding["name"],
                    "target": binding["target"],
                    "created_at": time.time(),
                }
                await repo.store.aput(
                    NAMESPACE, route_key(order["merchant_order_id"]), sealed(route, signing_key)
                )
                order["slack_updates_status"] = "enabled"
            # Registration starts after Zinc gives us the order's private key.
            # Re-read once to catch a placement/failure that happened before the
            # subscription existed. It returns through this tool's normal reply.
            if newly_enabled or order.get("updates_catchup_pending"):
                order["updates_catchup_pending"] = True
                from restock.zinc import MERCHANT_TERMINAL, public_order

                key = await service.private.order_key(order["id"])
                record = await service.zinc.status(order["merchant_order_id"], key)
                order.update(public_order(record))
                order["status"] = (
                    order["merchant_status"]
                    if order["merchant_status"] in MERCHANT_TERMINAL
                    else "merchant_pending"
                )
                order.pop("updates_catchup_pending", None)
        except RestockError as error:
            order["slack_updates_status"] = (
                str(error)
                if str(error)
                in {
                    "order_updates_not_configured",
                    "order_updates_need_hosted_slack",
                    "order_updates_endpoint_already_configured",
                }
                else "setup_failed"
            )
        except Exception:
            order["slack_updates_status"] = "setup_failed"
        try:
            await repo.save(order)
        except Exception:
            # Notification storage is separate from the already saved purchase.
            return {**result, "slack_updates_status": "setup_failed"}
        return view(order)


class Receiver:
    """Dependencies are replaced in tests; delivery never runs an agent or wallet."""

    def __init__(self, store, secrets, post, *, signing_key, callback_url):
        self.store, self.secrets, self.post = store, secrets, post
        self.signing_key, self.callback_url = signing_key, callback_url
        self.locks = {}

    async def authenticate(self, raw, signature):
        if not isinstance(signature, str) or not re.fullmatch(r"[0-9a-f]{64}", signature):
            raise RestockError("invalid_zinc_signature")
        event, stamp = callback_event(raw)
        record = await self.store.aget(NAMESPACE, route_key(event["order_id"]))
        if record is None:
            raise RestockError("unknown_order_event")
        route = unseal(record.value, self.signing_key)
        if (
            route.get("merchant_order_id") != event["order_id"]
            or route.get("callback_url") != self.callback_url
            or route.get("secret_slug") != "restock-updates-" + route.get("order_reference", "")
            or not isinstance(route.get("principal_id"), str)
            or not route["principal_id"]
        ):
            raise RestockError("invalid_update_binding")
        secret = await self.secrets(route)
        if not isinstance(secret, str) or not secret:
            raise RestockError("webhook_secret_unavailable")
        expected = hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
        if not isinstance(signature, str) or not hmac.compare_digest(signature, expected):
            raise RestockError("invalid_zinc_signature")
        return route, event, stamp

    async def receive(self, raw, signature):
        route, event, stamp = await self.authenticate(raw, signature)
        if event["event"] not in EVENTS:
            return "ignored"
        key = route_key(event["order_id"])
        async with self.locks.setdefault(key, asyncio.Lock()):
            cursor_key = "cursor:" + key
            cursor = await self.store.aget(NAMESPACE, cursor_key)
            previous = unseal(cursor.value, self.signing_key) if cursor else {}
            if previous and previous.get("merchant_order_id") != event["order_id"]:
                raise RestockError("invalid_update_binding")
            public = public_event(event)
            digest = hashlib.sha256(encoded(public)).hexdigest()
            if stamp < previous.get("timestamp", 0) or digest in previous.get("digests", []):
                return "duplicate_or_older"
            # Terminal and shipping events must not regress to a later delivery
            # of an earlier lifecycle stage, even with an inconsistent timestamp.
            prior = previous.get("event")
            if (
                prior in {"order.failed", "order.cancelled", "order.delivered"}
                and prior != event["event"]
            ) or (prior == "order.tracking_received" and event["event"] == "order.placed"):
                return "duplicate_or_older"
            # The same signed event keeps the same gateway id across retries/workers.
            action_id = str(uuid.uuid5(uuid.NAMESPACE_URL, self.callback_url + key + digest))
            await self.post(route, notification_text(public), action_id)
            await self.store.aput(
                NAMESPACE,
                cursor_key,
                sealed(
                    {
                        "merchant_order_id": event["order_id"],
                        "timestamp": stamp,
                        "digests": (previous.get("digests", []) + [digest])[-100:],
                        "event": event["event"],
                    },
                    self.signing_key,
                ),
            )
            return "posted"
