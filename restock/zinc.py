"""Zinc's fixed HTTPS API, with the official MPP parser and credential serializer."""

from __future__ import annotations

import asyncio
import hashlib
import math
import re
import time
from datetime import datetime
from typing import Any
from urllib.parse import urlparse

import httpx
from mpp import Credential, format_authorization, parse_www_authenticate

from restock.config import RestockError
from restock.costs import guidance
from restock.updates import public_delivery, public_failure_reasons

API = "https://api.zinc.com"
# Match Zinc's published Link example. Read repeated challenge headers separately
# and select Stripe after sending the actual cart, rather than filtering discovery.
ORDER_PATH = "/agent/orders"
FEE_CENTS = 100
MERCHANT_TERMINAL = {
    "order_placed",
    "order_failed",
    "failed",
    "cancelled",
    "canceled",
    "cancelled_by_retailer",
}

# Keep only known machine codes. Provider prose can contain private request data.
PAYMENT_ERROR_CODES = {
    "payment_method_required",
    "payment_required",
    "payment_verification_failed",
    "invalid_payment_credential",
    "unknown_payment_method",
    "invalid_request",
    "payment-required",
    "malformed-credential",
    "invalid-challenge",
    "verification-failed",
    "payment-expired",
    "invalid-payload",
    "payment-action-required",
    "method-unsupported",
}


def merchant_id(value):
    return (
        value if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,150}", value) else None
    )


class ZincSubmissionError(RestockError):
    """Public diagnostics only. Never retain a response, credential or private key."""

    def __init__(self, reason, *, status=None, data=None, order_id=None):
        super().__init__("merchant_outcome_unknown")
        self.diagnostic = {"stage": "merchant_submission", "reason": reason}
        if type(status) is int and 100 <= status <= 599:
            self.diagnostic["http_status"] = status
        if isinstance(data, dict):
            error = data.get("error")
            code = error.get("code") if isinstance(error, dict) else None
            problem = data.get("type")
            if isinstance(problem, str) and problem.startswith("https://paymentauth.org/problems/"):
                code = problem.removeprefix("https://paymentauth.org/problems/")
            if isinstance(code, str) and code in PAYMENT_ERROR_CODES:
                self.diagnostic["provider_code"] = code
        self.merchant_order_id = merchant_id(order_id)


def expiry(value: str | int | float | None) -> float | None:
    if value is None:
        return None
    try:
        if isinstance(value, bool):
            raise ValueError()
        if isinstance(value, (int, float)) or str(value).isdigit():
            number = float(value)
            if not math.isfinite(number):
                raise ValueError()
            return number
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError()
        return parsed.timestamp()
    except (ValueError, TypeError, OverflowError):
        raise RestockError("invalid_expiry") from None


def parse_challenge(headers: list[str], expected_amount: int | None) -> dict:
    matches = []
    for header in headers:
        try:
            challenge = parse_www_authenticate(header)
        except Exception:
            continue
        if challenge.method == "stripe" and challenge.intent == "charge":
            matches.append((header, challenge))
    if len(matches) != 1:
        raise RestockError("stripe_challenge_required")
    header, challenge = matches[0]
    request = challenge.request
    if not isinstance(request, dict):
        raise RestockError("invalid_payment_challenge")
    amount = request.get("amount")
    if (
        not isinstance(amount, str)
        or not re.fullmatch(r"[0-9]{1,9}", amount)
        or int(amount) <= 0
        or (expected_amount is not None and int(amount) != expected_amount)
    ):
        raise RestockError("payment_amount_changed")
    if str(request.get("currency", "")).lower() != "usd":
        raise RestockError("usd_payment_required")
    details = request.get("methodDetails") or {}
    network = details.get("networkId") or request.get("networkId")
    if not isinstance(network, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,150}", network):
        raise RestockError("invalid_payment_network")
    if challenge.header not in (None, "Authorization", "authorization"):
        raise RestockError("unsupported_payment_header")
    expires_at = expiry(challenge.expires)
    if expires_at is not None and expires_at <= time.time() + 15:
        raise RestockError("payment_challenge_expired")
    return {
        "header": header,
        "network_id": network,
        "amount": int(amount),
        "currency": "usd",
        "expires_at": expires_at,
        "id": challenge.id,
    }


def payment_header(challenge: dict, token: str) -> str:
    parsed = parse_www_authenticate(challenge["header"])
    echo = parsed.to_echo()
    # Match link-cli 0.22.0 mpp pay and the Stripe MPP verifier. The field
    # shared_payment_granted_token belongs to Zinc's subsequent Stripe API call,
    # not this MPP envelope (Zinc's prose example currently mixes them up).
    return format_authorization(
        Credential(
            challenge=echo,
            payload={"spt": token},
        )
    )


def public_product(raw: Any) -> dict | None:
    if not isinstance(raw, dict):
        return None
    url = raw.get("url")
    if not isinstance(url, str) or len(url) > 2000:
        return None
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        return None
    if parsed.hostname in {"localhost", "127.0.0.1", "::1"}:
        return None
    price = raw.get("price")
    if price is not None and (type(price) is not int or price < 0):
        price = None
    if raw.get("available") is False:
        return None
    return {
        "product_id": hashlib.sha256(url.encode()).hexdigest()[:16],
        "title": str(raw.get("title") or "Product")[:200],
        "retailer": str(raw.get("retailer") or parsed.hostname)[:80],
        "url": url,
        "price_cents": price,
    }


def public_order(raw: dict) -> dict:
    # An accepted API request is not yet a placed retailer order.
    status = raw.get("status", "unknown")
    if status not in {"pending", "in_progress", *MERCHANT_TERMINAL}:
        status = "unknown"
    return {
        "merchant_status": status,
        "failure_reasons": public_failure_reasons(raw),
        **public_delivery(raw),
    }


class Zinc:
    def __init__(self, client: httpx.AsyncClient | None = None):
        self.client = client

    async def request(self, method: str, path: str, **kwargs) -> httpx.Response:
        if self.client is not None:
            return await self.client.request(method, API + path, **kwargs)
        async with httpx.AsyncClient(timeout=45, follow_redirects=False) as client:
            return await client.request(method, API + path, **kwargs)

    async def search(self, query: str, key: str, maximum: int) -> list[dict]:
        response = await self.request(
            "GET",
            "/search",
            params={"q": query, "max_price": maximum, "limit": 6},
            headers={"Authorization": "Bearer " + key},
        )
        if response.status_code == 402:
            raise RestockError("zinc_search_balance_required")
        if response.status_code != 200:
            raise RestockError("zinc_search_failed")
        products = [public_product(item) for item in response.json().get("results", [])]
        return [item for item in products if item is not None][:6]

    async def shipping_guidance(self, urls: list[str]) -> list[dict]:
        async def read(path, **kwargs):
            try:
                response = await self.request("GET", path, **kwargs)
                value = response.json() if response.status_code == 200 else {}
                return value if isinstance(value, dict) else {}
            except (httpx.HTTPError, ValueError):
                return {}

        unique = list(dict.fromkeys(urls))
        catalog, *checks = await asyncio.gather(
            read("/retailers"),
            *(read("/retailers/check", params={"url": url, "country": "US"}) for url in unique),
        )
        return [guidance(url, check, catalog) for url, check in zip(unique, checks, strict=True)]

    async def webhook(self, key: str, url: str) -> str:
        """Reuse our endpoint; never replace another application's subscription."""
        headers = {"Authorization": "Bearer " + key}
        response = await self.request("GET", "/webhooks/endpoint", headers=headers)
        if response.status_code != 200:
            raise RestockError("order_updates_registration_unavailable")
        data = response.json()
        if not isinstance(data, dict):
            raise RestockError("invalid_webhook_configuration")
        existing = data.get("webhook_url")
        if existing and existing != url:
            raise RestockError("order_updates_endpoint_already_configured")
        if not existing:
            response = await self.request(
                "PUT", "/webhooks/endpoint", headers=headers, json={"url": url}
            )
            if response.status_code != 200:
                raise RestockError("order_updates_registration_unavailable")
            data = response.json()
        secret = data.get("webhook_secret") if isinstance(data, dict) else None
        if (
            not isinstance(secret, str)
            or not secret.startswith("zn_whsec_")
            or not 20 <= len(secret) <= 512
            or data.get("webhook_url") != url
        ):
            raise RestockError("invalid_webhook_configuration")
        return secret

    async def challenge(self, body: dict, amount: int | None) -> dict:
        # None is only for unpaid email-fee discovery. The service must obtain
        # an exact-amount challenge before review, Link access, or submission.
        response = await self.request("POST", ORDER_PATH, json=body)
        self.check_payment_availability(response)
        if response.status_code != 402:
            raise RestockError("zinc_challenge_failed")
        return parse_challenge(response.headers.get_list("www-authenticate"), amount)

    @staticmethod
    def check_payment_availability(response: httpx.Response) -> None:
        if response.status_code == 400:
            data = response.json()
            if data.get("error", {}).get("code") == "unknown_payment_method":
                raise RestockError("zinc_stripe_payments_unavailable_contact_zinc")

    async def availability(self) -> dict:
        """Check reachability only; bodyless discovery can omit a working Stripe route."""
        response = await self.request("POST", ORDER_PATH)
        cart_required = {
            "status": "cart_required",
            "notice": (
                "No payment requested. Bodyless discovery cannot establish Stripe support. "
                "Continue to product selection; the prepared cart must return a matching "
                "Stripe challenge before any Link payment request."
            ),
        }
        if response.status_code == 400:
            data = response.json()
            if data.get("error", {}).get("code") == "unknown_payment_method":
                return cart_required
        if response.status_code != 402:
            raise RestockError("zinc_payment_discovery_unavailable")
        for header in response.headers.get_list("www-authenticate"):
            try:
                parsed = parse_www_authenticate(header)
                if parsed.method == "stripe" and parsed.intent == "charge":
                    return {
                        "status": "stripe_challenge_advertised",
                        "notice": "No payment requested. A real cart and Link approval still need validation.",
                    }
            except (ValueError, TypeError):
                continue
        return cart_required

    async def submit(self, body: dict, challenge: dict, token: str) -> tuple[dict, str]:
        try:
            response = await self.request(
                "POST",
                ORDER_PATH,
                json=body,
                headers={"Authorization": payment_header(challenge, token)},
            )
        except httpx.TimeoutException:
            raise ZincSubmissionError("transport_timeout") from None
        except httpx.RequestError:
            raise ZincSubmissionError("transport_error") from None
        try:
            data = response.json()
        except ValueError:
            data = None
        if response.status_code not in {201, 409}:
            raise ZincSubmissionError("http_error", status=response.status_code, data=data)
        if not isinstance(data, dict):
            raise ZincSubmissionError("invalid_response", status=response.status_code)
        if response.status_code == 409:
            # Only the documented explicit order_id permits reconciliation.
            error = data.get("error")
            details = error.get("details") if isinstance(error, dict) else None
            order_id = merchant_id(details.get("order_id")) if isinstance(details, dict) else None
            key = response.headers.get("x-api-key")
            if not order_id or not key:
                raise ZincSubmissionError("missing_tracking_access", status=409, order_id=order_id)
            try:
                return await self.status(order_id, key), key
            except Exception:
                raise ZincSubmissionError(
                    "existing_order_lookup_failed", status=409, order_id=order_id
                ) from None
        key = response.headers.get("x-api-key")
        order_id = merchant_id(data.get("id"))
        if not key or not order_id:
            raise ZincSubmissionError("missing_tracking_access", status=201, order_id=order_id)
        return data, key

    async def status(self, order_id: str, key: str) -> dict:
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,150}", order_id):
            raise RestockError("invalid_merchant_order_id")
        response = await self.request(
            "GET", "/orders/" + order_id, headers={"Authorization": "Bearer " + key}
        )
        if response.status_code != 200:
            raise RestockError("merchant_status_unavailable")
        record = response.json()
        if not isinstance(record, dict) or record.get("id") != order_id:
            raise RestockError("merchant_order_mismatch")
        return record
