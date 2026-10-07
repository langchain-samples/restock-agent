"""Explicit fictional providers for the rehearsal mode. No network or real wallet."""

from __future__ import annotations

import base64
import json
import time

from restock.zinc import parse_challenge, public_product

OFFICE = {
    "label": "Fictional demo office",
    "shipping_address": {
        "first_name": "Demo",
        "last_name": "Recipient",
        "address_line1": "100 Example Street",
        "city": "Example City",
        "state": "NY",
        "postal_code": "10001",
        "country": "US",
        "phone_number": "2125550100",
    },
}


class RehearsalZinc:
    async def shipping_guidance(self, urls):
        from restock.costs import guidance

        return [guidance(url, {}, {}) for url in dict.fromkeys(urls)]

    async def search(self, query, key, maximum):
        # Query-shaped fixtures make it explicit that these are not live listings.
        return [
            public_product(
                {
                    "url": f"https://shop.example.com/{index}",
                    "title": f"Demo {query} option {index}",
                    "retailer": "Fictional demo shop",
                    "price": amount,
                }
            )
            for index, amount in [(1, 850), (2, 1200)]
            if amount <= maximum
        ]

    async def challenge(self, body, amount):
        quoted_amount = body["max_price"] + 100
        request = {
            "amount": str(quoted_amount),
            "currency": "usd",
            "methodDetails": {"networkId": "demo_network"},
        }
        encoded = base64.urlsafe_b64encode(json.dumps(request).encode()).decode().rstrip("=")
        header = f'Payment id="demo_challenge", realm="zinc", method="stripe", intent="charge", request="{encoded}"'
        return parse_challenge([header], amount)

    async def submit(self, *args):
        raise AssertionError("Rehearsal cannot submit a merchant order")


class RehearsalWallet:
    def __init__(self, repository):
        self.repository = repository

    async def create(self, order):
        from restock.service import payment_amount

        value = {
            "id": "demo_" + order["id"],
            "amount": payment_amount(order),
            "currency": "usd",
            "status": "approved",
            "credential_type": "shared_payment_token",
            "metadata": {"restock_order": order["id"], "restock_fingerprint": order["fingerprint"]},
            "expires_at": time.time() + 600,
        }
        await self.repository.put("rehearsal:" + value["id"], value)
        return value

    async def retrieve(self, request_id):
        return await self.repository.get("rehearsal:" + request_id)

    async def cancel(self, request_id):
        value = await self.retrieve(request_id)
        value["status"] = "canceled"
        await self.repository.put("rehearsal:" + request_id, value)
        return value

    async def history(self):
        return []

    async def token(self, *args):
        raise AssertionError("Rehearsal never retrieves a payment token")


class RehearsalPrivate:
    async def office(self, slug):
        return json.loads(json.dumps(OFFICE))

    async def zinc_key(self, slug):
        return "synthetic-rehearsal"
