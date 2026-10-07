import json
import unittest

import tests  # noqa: F401
import httpx

from restock.config import RestockError
from restock.service import Restock
from restock.storage import Repository
from restock.zinc import Zinc, public_order
from tests.support import approved, make_service, runtime


class FailureProjectionTests(unittest.TestCase):
    def test_documented_job_and_item_codes_exclude_private_details(self):
        result = public_order(
            {
                "status": "order_failed",
                "job_result": {
                    "error_type": "shipping_address_invalid",
                    "error": "private-provider-prose",
                    "error_details": {
                        "code": "shipping_address_invalid",
                        "message": "private-provider-prose",
                        "field_errors": [{"received": "private-field-value"}],
                        "address_validation_reasons": ["private-address"],
                    },
                },
                "items": [
                    {"status": "failed", "error_type": "product_out_of_stock"},
                    {"status": "skipped", "error_type": "product_not_found"},
                ],
                "shipping_address": "private-address",
            }
        )
        self.assertEqual(
            [reason["code"] for reason in result["failure_reasons"]],
            ["shipping_address_invalid", "product_out_of_stock"],
        )
        self.assertNotIn("private-", json.dumps(result))

    def test_unrecognized_or_malformed_codes_are_not_forwarded(self):
        for value in (None, [], {}, "private_value", "someone@example.invalid", "<@channel>"):
            with self.subTest(value=value):
                result = public_order(
                    {
                        "status": "order_failed",
                        "error_type": value,
                        "job_result": {"error_type": value, "error_details": {"code": value}},
                        "items": [None, {"status": "failed", "error_type": value}],
                    }
                )
                self.assertEqual(result["failure_reasons"], [])
        for value in (None, [], "bad"):
            self.assertEqual(
                public_order({"status": "failed", "job_result": value, "items": value})[
                    "failure_reasons"
                ],
                [],
            )

    def test_nonfailed_orders_do_not_report_previous_or_best_effort_errors(self):
        for status in ("pending", "in_progress", "order_placed", "cancelled"):
            with self.subTest(status=status):
                self.assertEqual(
                    public_order(
                        {"status": status, "job_result": {"error_type": "max_price_exceeded"}}
                    )["failure_reasons"],
                    [],
                )

    def test_legacy_flat_error_type_is_supported(self):
        reasons = public_order({"status": "failed", "error_type": "checkout_blocked"})[
            "failure_reasons"
        ]
        self.assertEqual(reasons[0]["code"], "checkout_blocked")


class ExistingOrderTests(unittest.IsolatedAsyncioTestCase):
    async def test_restarted_service_reads_same_legacy_order_without_payment_or_submission(self):
        for saved_status in ("merchant_pending", "order_failed"):
            with self.subTest(saved_status=saved_status):
                previous = make_service()
                oid = await approved(previous)
                submitted = await previous.check(oid, finish=True)
                legacy = await previous.repo.order(oid)
                legacy.pop("failure_reasons")
                legacy["status"] = saved_status
                await previous.repo.save(legacy)
                calls = []

                def handle(request):
                    calls.append(request)
                    self.assertEqual(request.method, "GET")
                    self.assertEqual(request.url.path, "/orders/" + submitted["merchant_order_id"])
                    self.assertEqual(request.headers["Authorization"], "Bearer synthetic-order-key")
                    return httpx.Response(
                        200,
                        json={
                            "id": submitted["merchant_order_id"],
                            "status": "order_failed",
                            "job_result": {
                                "error_details": {
                                    "code": "max_price_exceeded",
                                    "message": "private-provider-prose",
                                }
                            },
                        },
                    )

                async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
                    service = Restock(
                        Repository(runtime(store=previous.repo.store)),
                        previous.private,
                        Zinc(client),
                        previous.wallet,
                        previous.settings,
                    )
                    for _ in range(2):
                        result = await service.check(oid, finish=True)
                        self.assertEqual(result["status"], "order_failed")
                        self.assertEqual(result["failure_reasons"][0]["code"], "max_price_exceeded")
                        self.assertIn("retailer allowance", result["order_update"])
                        self.assertIn("refund status is unverified", result["order_update"])
                        self.assertNotIn("private-provider-prose", json.dumps(result))
                        self.assertNotIn("synthetic-order-key", json.dumps(result))
                    # A fresh process still enforces the original caller and thread.
                    for rt in (
                        runtime(caller="bob", store=previous.repo.store),
                        runtime(thread="another-thread", store=previous.repo.store),
                    ):
                        service.repo = Repository(rt)
                        with self.assertRaises(RestockError):
                            await service.check(oid, finish=True)
                self.assertEqual(len(calls), 2)
                self.assertEqual(previous.wallet.created, 1)
                self.assertEqual(previous.wallet.tokens, 1)
                self.assertEqual(len(previous.zinc.submissions), 1)
                self.assertEqual(
                    (await previous.repo.order(oid))["failure_reasons"], result["failure_reasons"]
                )

    async def test_missing_reason_does_not_invent_cause_or_keep_stale_details(self):
        service = make_service()
        oid = await approved(service)
        await service.check(oid, finish=True)
        service.zinc.orders[oid].update(
            status="order_failed", job_result={"error_type": "max_price_exceeded"}
        )
        self.assertTrue((await service.check(oid))["failure_reasons"])
        service.zinc.orders[oid]["job_result"] = {"error_type": "unknown_new_provider_code"}
        result = await service.check(oid)
        self.assertEqual(result["failure_reasons"], [])
        self.assertIn("No recognized failure reason", result["order_update"])
        self.assertIn("refund status is unverified", result["order_update"])
        self.assertNotIn("max_price_exceeded", json.dumps(result))
        self.assertEqual(service.wallet.tokens, 1)
        self.assertEqual(len(service.zinc.submissions), 1)
