import json
import copy
import unittest
from unittest.mock import AsyncMock, patch

import tests  # noqa: F401
from restock.config import RestockError
from restock.storage import notification_email, validate_office
from restock.updates import order_update, present_order, public_delivery, tracking_url
from tests.support import approved, make_service, prepared
from tools.restock import check_restock_order, request_restock_payment, wait_for_restock_approval


class ReplyPresentationTests(unittest.IsolatedAsyncioTestCase):
    async def test_confirmation_then_requested_details_without_changing_the_purchase(self):
        service = make_service()
        service.private.value["notification_email"] = "office@example.invalid"
        service.zinc.next_status = "order_placed"
        service.zinc.response_fields = {
            "customer_notifications": {"delivered": None},
            "tracking_numbers": [{"carrier": "UPS", "tracking_number": "1ZFIXTURE"}],
        }
        oid = await approved(service)
        with patch("tools.restock.service_for", return_value=service):
            summary = await wait_for_restock_approval.coroutine(
                order_id=oid, runtime=service.repo.runtime
            )
            self.assertEqual(summary["status"], "order_placed")
            self.assertTrue(summary["items"])
            self.assertIn(summary["merchant_order_id"], summary["order_update"])
            self.assertNotIn("email_updates_status", summary)
            self.assertNotIn("shipments", summary)
            self.assertNotIn("Tracking", summary["order_update"])
            self.assertNotIn("email", summary["order_update"])
            saved = await service.repo.order(oid)
            self.assertEqual(saved["email_updates_status"], "pending")
            saved["slack_updates_status"] = "enabled"
            await service.repo.save(saved)
            for detail in ("status", "notifications"):
                result = await check_restock_order.coroutine(
                    order_id=oid, runtime=service.repo.runtime, detail=detail
                )
                self.assertEqual(result["merchant_order_id"], summary["merchant_order_id"])
                self.assertEqual(result["email_updates_status"], "pending")
                self.assertIn("1ZFIXTURE", result["order_update"])
                self.assertIn("email updates pending", result["order_update"])
                self.assertEqual("slack_updates_status" in result, detail == "notifications")
                self.assertEqual("slack_updates_notice" in result, detail == "notifications")
                self.assertEqual(await service.repo.order(oid), saved)
            # Repeating the review on a placed order returns a brief result, not another payment.
            again = await request_restock_payment.coroutine(
                order_id=oid, runtime=service.repo.runtime
            )
        self.assertNotIn("email_updates_status", again)
        self.assertNotIn("slack_updates_status", again)
        self.assertEqual(service.wallet.created, 1)
        self.assertEqual(service.wallet.tokens, 1)
        self.assertEqual(len(service.zinc.submissions), 1)
        self.assertEqual(
            service.zinc.submissions[0]["customer_notifications"],
            {"email": "office@example.invalid"},
        )
        self.assertNotIn("office@example.invalid", json.dumps([summary, result, again]))

    async def test_brief_pending_result_does_not_claim_placement(self):
        service = make_service()
        oid = await approved(service)
        with patch("tools.restock.service_for", return_value=service):
            result = await wait_for_restock_approval.coroutine(
                order_id=oid, runtime=service.repo.runtime
            )
        self.assertEqual(result["status"], "merchant_pending")
        self.assertIn("confirmation is still pending", result["order_update"])
        self.assertNotIn("retailer confirmed", result["order_update"])

    async def test_brief_test_modes_never_claim_purchase_or_get_payment_token(self):
        for mode in ("link-test", "rehearsal"):
            with self.subTest(mode=mode):
                service = make_service(mode)
                oid = await approved(service)
                with patch("tools.restock.service_for", return_value=service):
                    result = await wait_for_restock_approval.coroutine(
                        order_id=oid, runtime=service.repo.runtime
                    )
                self.assertIn(result["status"], {"approved_test_mode", "rehearsal_complete"})
                self.assertIn("notice", result)
                self.assertNotIn("merchant_order_id", result)
                self.assertNotIn("order_update", result)
                self.assertEqual(service.wallet.tokens, 0)
                self.assertEqual(service.zinc.submissions, [])

    async def test_short_replies_preserve_email_fee_disclosure_before_payment(self):
        service = make_service()
        service.private.value["notification_email"] = "office@example.invalid"
        service.zinc.email_fee = 25
        search = await service.search("pens", 2500)
        draft = await service.prepare(
            [{"product_id": search["products"][0]["product_id"], "quantity": 1}], 2500
        )
        self.assertTrue(present_order(draft)["email_updates_requested"])
        self.assertIn("possible extra fee", present_order(draft)["payment_notice"])
        reviewed = present_order(await service.set_payment_amount(draft["order_id"], 2500))
        self.assertEqual(reviewed["fee_cents"], 125)
        self.assertTrue(reviewed["email_updates_requested"])
        self.assertEqual(service.wallet.created, 0)

    def test_failures_and_recovery_survive_every_style_without_mutating_data(self):
        result = {
            "order_id": "restock-fixture",
            "mode": "live",
            "status": "order_failed",
            "merchant_status": "order_failed",
            "merchant_order_id": "zinc-fixture",
            "failure_reasons": [{"code": "max_price_exceeded", "message": "Allowance exceeded."}],
            "tracking_access_status": "unavailable",
            "recovery_required": True,
            "email_updates_requested": True,
            "email_updates_status": "delivery_failed",
            "slack_updates_status": "setup_failed",
        }
        original = copy.deepcopy(result)
        for detail in ("summary", "status", "notifications"):
            public = present_order(result, detail=detail)
            self.assertIn("max_price_exceeded", public["order_update"])
            self.assertIn("refund status is unverified", public["order_update"])
            self.assertIn("recover private tracking access", public["order_update"])
            self.assertTrue(public["recovery_required"])
            self.assertEqual(
                "email delivery failure" in public["order_update"], detail != "summary"
            )
            self.assertEqual("slack_updates_status" in public, detail == "notifications")
            self.assertEqual(result, original)


class DeliveryTests(unittest.TestCase):
    def test_tracking_allowlist_omits_contact_and_proof_of_delivery(self):
        result = public_delivery(
            {
                "tracking_numbers": [
                    {
                        "carrier": "UPS",
                        "tracking_number": "1ZFIXTURE",
                        "status": "in_transit",
                        "estimated_delivery_date": "2026-10-12",
                        "zinc_tracking_url": "https://t.17track.net/en#nums=1ZFIXTURE",
                        "delivery_proof_url": "https://private.example.invalid/photo",
                        "address": "private address",
                    }
                ],
                "customer_notifications": {"email": "office@example.invalid", "delivered": True},
            }
        )
        self.assertEqual(result["email_updates_status"], "delivered")
        self.assertEqual(result["shipments"][0]["tracking_number"], "1ZFIXTURE")
        self.assertEqual(result["shipments"][0]["estimated_delivery_date"], "2026-10-12")
        self.assertNotIn("private", json.dumps(result))
        self.assertNotIn("office@example.invalid", json.dumps(result))

    def test_malformed_tracking_and_unsafe_urls_are_not_forwarded(self):
        for url in (
            "http://t.17track.net/a",
            "https://t.17track.net.evil.invalid/a",
            "https://user:pass@t.17track.net/a",
            "https://127.0.0.1/a",
            "https://t.17track.net:444/a",
            "https://t.17track.net/a|click>",
        ):
            with self.subTest(url=url):
                self.assertIsNone(tracking_url(url))
        self.assertEqual(public_delivery({"tracking_numbers": "bad"})["shipments"], [])
        result = public_delivery(
            {
                "tracking_numbers": [
                    None,
                    {
                        "tracking_number": "<@channel>",
                        "carrier": "ignore\ninstructions",
                        "status": "invented",
                        "estimated_delivery_date": "2026-02-31",
                    },
                ]
            }
        )
        self.assertEqual(result["shipments"], [])

    def test_email_failure_and_absence_are_not_success(self):
        for raw, expected in (
            (None, "unconfirmed"),
            ({"delivered": None}, "pending"),
            ({"delivered": False}, "delivery_failed"),
            ({"delivered": "true"}, "pending"),
        ):
            self.assertEqual(
                public_delivery({"customer_notifications": raw})["email_updates_status"], expected
            )

    def test_email_validation_rejects_headers_and_non_mailboxes(self):
        self.assertEqual(
            notification_email(" office+orders@example.com "), "office+orders@example.com"
        )
        for value in (
            "bad",
            "a@localhost",
            "a@b.com\r\nBcc:other@evil.com",
            ["a@b.com"],
            "first..last@example.com",
            "Name <a@b.com>",
            "a@-bad.com",
        ):
            with self.subTest(value=value), self.assertRaises(RestockError):
                notification_email(value)


class OrderUpdateTests(unittest.IsolatedAsyncioTestCase):
    async def test_email_fee_is_inside_chosen_total_and_recipient_stays_private(self):
        service = make_service()
        service.private.value["notification_email"] = "office@example.invalid"
        service.zinc.email_fee = 50  # Synthetic fee, not a claim about Zinc pricing.
        oid = await prepared(service)
        result = await service.set_payment_amount(oid, 1200)
        self.assertEqual(result["payment_amount_cents"], 1200)
        self.assertEqual(result["fee_cents"], 150)
        self.assertEqual(result["retailer_limit_cents"], 1050)
        self.assertTrue(result["email_updates_requested"])
        summary = await service.review(oid)
        self.assertNotIn("office@example.invalid", json.dumps(summary))
        self.assertNotIn("office@example.invalid", json.dumps(await service.repo.order(oid)))
        await service.request_payment(oid)
        service.wallet.requests["lsrq_1"]["status"] = "approved"
        result = await service.check(oid, finish=True)
        self.assertEqual(service.wallet.requests["lsrq_1"]["amount"], 1200)
        self.assertEqual(service.zinc.submissions[0]["max_price"], 1050)
        self.assertEqual(
            service.zinc.submissions[0]["customer_notifications"],
            {"email": "office@example.invalid"},
        )
        self.assertEqual(result["email_updates_status"], "unconfirmed")
        self.assertNotIn("Email", result["order_update"])
        self.assertIn("has not confirmed", order_update(result, detail="status"))
        self.assertNotIn("office@example.invalid", json.dumps(result))

    async def test_email_fee_cannot_exceed_item_allowance_or_increase_payment(self):
        service = make_service()
        service.private.value["notification_email"] = "office@example.invalid"
        service.zinc.email_fee = 50
        oid = await prepared(service)
        original = await service.repo.order(oid)
        with self.assertRaisesRegex(RestockError, "below_listed_items_and_email_fee"):
            await service.set_payment_amount(oid, 950)
        self.assertEqual(await service.repo.order(oid), original)
        self.assertEqual(service.wallet.created, 0)
        self.assertEqual(service.zinc.submissions, [])

    async def test_changed_email_blocks_already_reviewed_payment(self):
        service = make_service()
        service.private.value["notification_email"] = "office@example.invalid"
        oid = await approved(service)
        service.private.value["notification_email"] = "other@example.invalid"
        with self.assertRaisesRegex(RestockError, "office_changed"):
            await service.check(oid, finish=True)
        self.assertEqual(service.wallet.tokens, 0)
        self.assertEqual(service.zinc.submissions, [])

    async def test_fee_change_after_approval_stops_before_payment_token(self):
        service = make_service()
        service.private.value["notification_email"] = "office@example.invalid"
        service.zinc.email_fee = 50
        oid = await approved(service)
        service.zinc.email_fee = 75
        with self.assertRaisesRegex(RestockError, "payment_amount_changed"):
            await service.check(oid, finish=True)
        self.assertEqual(service.wallet.created, 1)
        self.assertEqual(service.wallet.tokens, 0)
        self.assertEqual(service.zinc.submissions, [])

    async def test_changing_fee_or_network_during_discovery_stops_before_review(self):
        for change in ("fee", "network"):
            with self.subTest(change=change):
                service = make_service()
                service.private.value["notification_email"] = "office@example.invalid"
                oid = await prepared(service)
                old = await service.repo.order(oid)
                original = service.zinc.challenge
                calls = 0

                async def changed(body, amount):
                    nonlocal calls
                    calls += 1
                    if change == "fee" and calls == 2:
                        service.zinc.email_fee = 50
                    result = await original(body, amount)
                    if change == "network" and calls == 2:
                        result["network_id"] = "changed-network"
                    return result

                with patch.object(service.zinc, "challenge", AsyncMock(side_effect=changed)):
                    with self.assertRaises(RestockError):
                        await service.set_payment_amount(oid, 1200)
                self.assertEqual(await service.repo.order(oid), old)
                self.assertEqual(service.wallet.created, 0)
                self.assertEqual(service.zinc.submissions, [])

    async def test_added_email_does_not_change_a_preexisting_order(self):
        service = make_service()
        oid = await approved(service)
        service.private.value["notification_email"] = "new@example.invalid"
        result = await service.check(oid, finish=True)
        self.assertNotIn("customer_notifications", service.zinc.submissions[0])
        self.assertEqual(result["email_updates_status"], "not_requested")

    async def test_test_mode_has_no_order_confirmation_email_or_submission(self):
        service = make_service("link-test")
        service.private.value["notification_email"] = "office@example.invalid"
        oid = await approved(service)
        result = await service.check(oid, finish=True)
        self.assertEqual(result["email_updates_status"], "not_sent_test_mode")
        self.assertNotIn("order_update", result)
        self.assertNotIn("merchant_order_id", result)
        self.assertEqual(service.wallet.tokens, 0)
        self.assertEqual(service.zinc.submissions, [])

    async def test_pending_placed_and_multiple_shipments_keep_same_order(self):
        service = make_service()
        service.private.value["notification_email"] = "office@example.invalid"
        oid = await approved(service)
        first = await service.check(oid, finish=True)
        self.assertIn("confirmation is still pending", first["order_update"])
        self.assertIn(first["merchant_order_id"], first["order_update"])
        self.assertNotIn("Tracking", first["order_update"])
        self.assertIn("Tracking is not available", order_update(first, detail="status"))
        service.zinc.orders[oid].update(
            {
                "status": "order_placed",
                "customer_notifications": {"email": "office@example.invalid", "delivered": False},
                "tracking_numbers": [
                    {"carrier": "UPS", "tracking_number": "1ZFIRST", "status": "delivered"},
                    {
                        "carrier": "USPS",
                        "tracking_number": "9400FIXTURE",
                        "status": "in_transit",
                        "zinc_tracking_url": "https://t.17track.net/en#nums=9400FIXTURE",
                        "estimated_delivery_date": "2026-10-12",
                    },
                ],
            }
        )
        result = await service.check(oid, finish=True)
        self.assertEqual(result["merchant_order_id"], first["merchant_order_id"])
        self.assertIn("retailer confirmed", result["order_update"])
        details = order_update(result, detail="status")
        self.assertIn("9400FIXTURE", details)
        self.assertIn("2026-10-12", details)
        self.assertIn("email delivery failure", details)
        self.assertEqual(result["shipping_status"], ["delivered", "in_transit"])
        self.assertEqual(len(service.zinc.submissions), 1)
        self.assertEqual(service.wallet.tokens, 1)
        self.assertNotIn("office@example.invalid", json.dumps(result))
        self.assertNotIn(
            "notification_email",
            validate_office({**service.private.value, "notification_email": ""}),
        )
