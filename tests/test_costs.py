import unittest
from unittest.mock import AsyncMock

import httpx
import tests  # noqa: F401

from restock.config import RestockError
from restock.costs import guidance
from restock.zinc import Zinc
from tests.support import make_service, prepared

URL = "https://www.example.com/product/1"
CATALOG = {
    "retailers": [
        {"base_url": "example.com", "free_shipping": True, "free_shipping_threshold_cents": 3500}
    ]
}
CHECK = {"domain": "example.com", "orderable": True, "checkout": {"guest_checkout": True}}


class CostTests(unittest.IsolatedAsyncioTestCase):
    def test_policy_is_not_a_quote(self):
        result = guidance(URL, CHECK, CATALOG)
        self.assertEqual(result["free_shipping_threshold_cents"], 3500)
        self.assertIn("$35.00", result["notice"])
        self.assertFalse(result["final_total_known"])
        self.assertFalse(result["customer_account_required"])

    def test_domain_mismatch_and_malformed_data_stay_unknown(self):
        for url in ("https://example.com.evil.test/item", "https://other.test/item"):
            result = guidance(url, CHECK, CATALOG)
            self.assertIsNone(result["orderable"])
            self.assertIsNone(result["free_shipping_offered"])
        for catalog in ({"retailers": "bad"}, [], None):
            self.assertIsNone(guidance(URL, {}, catalog)["free_shipping_threshold_cents"])
        bad = {
            "retailers": [
                {
                    "base_url": "example.com",
                    "free_shipping": "yes",
                    "free_shipping_threshold_cents": True,
                }
            ]
        }
        self.assertIsNone(guidance(URL, {}, bad)["free_shipping_threshold_cents"])

    async def test_free_reads_and_unavailable_provider(self):
        requests = []

        def handle(request):
            requests.append(request)
            self.assertNotIn("authorization", request.headers)
            return httpx.Response(200, json=CATALOG if request.url.path == "/retailers" else CHECK)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            result = await Zinc(client).shipping_guidance([URL, URL])
        self.assertEqual(len(requests), 2)
        self.assertEqual(len(result), 1)
        self.assertEqual(dict(requests[1].url.params), {"url": URL, "country": "US"})
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda r: httpx.Response(503, text="private provider prose")
            )
        ) as client:
            self.assertIsNone((await Zinc(client).shipping_guidance([URL]))[0]["orderable"])

    async def test_remaining_allowance_uses_actual_fees_and_no_new_payment(self):
        service = make_service()
        service.private.value["notification_email"] = "office@example.test"
        service.zinc.email_fee = 25
        oid = await prepared(service)
        result = await service.review(oid)
        self.assertEqual(result["retailer_limit_cents"], 2375)
        self.assertEqual(
            result["tax_shipping_allowance_cents"], 2375 - result["estimated_items_cents"]
        )
        self.assertFalse(result["final_total_known"])
        self.assertEqual(service.wallet.created, 0)
        self.assertEqual(service.zinc.submissions, [])

    async def test_only_explicit_unavailable_or_linked_account_blocks(self):
        for check, reason in (
            ({"orderable": False}, "retailer_not_available"),
            ({"customer_account_required": True}, "retailer_requires"),
        ):
            service = make_service()
            service.zinc.shipping_guidance = AsyncMock(return_value=[check])
            with self.assertRaisesRegex(RestockError, reason):
                await prepared(service)
            self.assertEqual(service.wallet.created, 0)
        service = make_service()
        service.zinc.shipping_guidance = AsyncMock(return_value=[{"orderable": None}])
        await prepared(service)
