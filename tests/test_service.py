import asyncio
import json
import time
import unittest
from unittest.mock import AsyncMock, patch

import tests  # noqa: F401
from restock.config import RestockError, Settings
from restock.rehearsal import RehearsalPrivate, RehearsalWallet, RehearsalZinc
from restock.service import Restock
from restock.storage import Repository
from restock.link_session import _caller
from tests.support import approved, make_service, prepared, runtime


class ServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_shopping_budget_does_not_automatically_become_payment_amount(self):
        service = make_service()
        search = await service.search("pens", 2500)
        with patch.object(service.zinc, "challenge", wraps=service.zinc.challenge) as challenge:
            draft = await service.prepare(
                [{"product_id": search["products"][0]["product_id"], "quantity": 1}], 2500
            )
            self.assertEqual(draft["status"], "payment_amount_required")
            self.assertEqual(draft["estimated_items_cents"], 850)
            self.assertFalse(draft["final_total_known"])
            self.assertIsNone(draft["payment_amount_cents"])
            self.assertEqual(challenge.await_count, 0)
            with self.assertRaisesRegex(RestockError, "choose_payment_amount_first"):
                await service.request_payment(draft["order_id"])
            self.assertEqual(service.wallet.created, 0)
            chosen = await service.set_payment_amount(draft["order_id"], 1200)
            self.assertEqual(chosen["budget_cents"], 2500)
            self.assertEqual(chosen["payment_amount_cents"], 1200)
            self.assertEqual(chosen["retailer_limit_cents"], 1100)
            self.assertEqual(challenge.await_args.args[0]["max_price"], 1100)
            self.assertEqual(challenge.await_args.args[1], 1200)
        await service.review(draft["order_id"])
        await service.request_payment(draft["order_id"])
        self.assertEqual(service.wallet.requests["lsrq_1"]["amount"], 1200)
        service.wallet.requests["lsrq_1"]["status"] = "approved"
        await service.check(draft["order_id"], finish=True)
        self.assertEqual(service.zinc.submissions[0]["max_price"], 1100)

    async def test_amount_choice_is_bounded_and_cannot_change_after_review_or_request(self):
        service = make_service()
        oid = await prepared(service)
        for value in (True, 99, 500, 2501, "1200"):
            with self.subTest(value=value), self.assertRaises(RestockError):
                await service.set_payment_amount(oid, value)
        await service.set_payment_amount(oid, 1200)
        await service.review(oid)
        with self.assertRaisesRegex(RestockError, "already_in_review"):
            await service.set_payment_amount(oid, 1400)
        await service.request_payment(oid)
        with self.assertRaisesRegex(RestockError, "cannot_change_after_request"):
            await service.set_payment_amount(oid, 1200)
        self.assertEqual(service.wallet.requests["lsrq_1"]["amount"], 1200)

    async def test_legacy_pending_amount_is_preserved_but_unpaid_draft_requires_choice(self):
        service = make_service()
        oid = await prepared(service)
        order = await service.repo.order(oid)
        order.pop("payment_amount_cents")
        await service.repo.save(order)
        with self.assertRaisesRegex(RestockError, "choose_payment_amount_first"):
            await service.request_payment(oid)
        await service.set_payment_amount(oid, 1200)
        await service.request_payment(oid)
        order = await service.repo.order(oid)
        # A historic pending request stored its payment sum in budget_cents.
        order["budget_cents"] = order.pop("payment_amount_cents")
        await service.repo.save(order)
        self.assertEqual((await service.check(oid))["status"], "awaiting_link_approval")
        self.assertEqual(service.wallet.created, 1)

    async def test_unknown_item_price_is_not_presented_as_zero_or_exact_total(self):
        service = make_service()
        search = await service.search("pens", 2500)
        data = await service.repo.get("search:" + service.repo.thread)
        data["products"][0]["price_cents"] = None
        await service.repo.put("search:" + service.repo.thread, data)
        draft = await service.prepare(
            [{"product_id": search["products"][0]["product_id"], "quantity": 1}], 2500
        )
        self.assertIsNone(draft["estimated_items_cents"])
        self.assertFalse(draft["final_total_known"])
        self.assertEqual(service.wallet.created, 0)

    async def test_legacy_uncertain_payment_reconciles_without_new_request(self):
        for status in ("payment_creating", "payment_unknown"):
            with self.subTest(status=status):
                service = make_service()
                oid = await prepared(service)
                await service.request_payment(oid)
                order = await service.repo.order(oid)
                order.pop("payment_amount_cents")
                order.pop("request_id")
                order["status"] = status
                await service.repo.save(order)
                result = await service.check(oid)
                self.assertEqual(result["status"], "awaiting_link_approval")
                self.assertEqual((await service.repo.order(oid))["request_id"], "lsrq_1")
                self.assertEqual(service.wallet.created, 1)
                self.assertEqual(service.wallet.tokens, 0)
                self.assertEqual(service.zinc.submissions, [])

    async def expire_challenge(self, service, oid):
        order = await service.repo.order(oid)
        order["challenge"]["expires_at"] = time.time() - 1
        await service.repo.save(order)
        return order

    async def test_expired_unpaid_challenge_refreshes_same_cart_before_review(self):
        service = make_service()
        oid = await prepared(service)
        old = await self.expire_challenge(service, oid)
        with patch.object(service.zinc, "challenge", wraps=service.zinc.challenge) as refresh:
            summary = await service.review(oid)
        self.assertEqual(summary["order_id"], oid)
        current = await service.repo.order(oid)
        self.assertEqual(current["fingerprint"], old["fingerprint"])
        self.assertEqual(current["status"], "prepared")
        self.assertEqual(current["items"], old["items"])
        self.assertEqual(refresh.await_args.args[0]["idempotency_key"], oid)
        self.assertEqual(refresh.await_count, 1)
        self.assertEqual(service.wallet.created, 0)
        self.assertEqual(service.wallet.tokens, 0)
        self.assertEqual(service.zinc.submissions, [])

    async def test_challenge_expiring_during_review_refreshes_before_single_request(self):
        service = make_service()
        oid = await prepared(service)
        await service.review(oid)
        await self.expire_challenge(service, oid)
        with patch.object(service.zinc, "challenge", wraps=service.zinc.challenge) as refresh:
            result = await service.request_payment(oid)
            await service.request_payment(oid)
        self.assertEqual(result["status"], "awaiting_link_approval")
        self.assertEqual(refresh.await_count, 1)
        self.assertEqual(service.wallet.created, 1)
        self.assertEqual(service.zinc.submissions, [])

    async def test_changed_or_stale_refresh_never_creates_payment(self):
        for change in (
            {"amount": 2501},
            {"currency": "eur"},
            {"network_id": "different-network"},
            {"expires_at": time.time() - 1},
        ):
            with self.subTest(change=change):
                service = make_service()
                oid = await prepared(service)
                old = await self.expire_challenge(service, oid)
                fresh = {**old["challenge"], "expires_at": time.time() + 600, **change}
                with patch.object(service.zinc, "challenge", new=AsyncMock(return_value=fresh)):
                    with self.assertRaises(RestockError):
                        await service.request_payment(oid)
                self.assertEqual((await service.repo.order(oid))["challenge"], old["challenge"])
                self.assertEqual(service.wallet.created, 0)
                self.assertEqual(service.zinc.submissions, [])

    async def test_refresh_failure_keeps_prepared_order_for_retry(self):
        service = make_service()
        oid = await prepared(service)
        old = await self.expire_challenge(service, oid)
        with patch.object(service.zinc, "challenge", new=AsyncMock(side_effect=TimeoutError)):
            with self.assertRaises(TimeoutError):
                await service.review(oid)
        self.assertEqual(await service.repo.order(oid), old)
        self.assertEqual(service.wallet.created, 0)

    async def test_retailer_cancellation_is_terminal_not_processing(self):
        service = make_service()
        service.zinc.next_status = "cancelled_by_retailer"
        oid = await approved(service)
        result = await service.check(oid, finish=True)
        self.assertEqual(result["status"], "cancelled_by_retailer")
        self.assertEqual(result["merchant_status"], "cancelled_by_retailer")
        self.assertEqual(len(service.zinc.submissions), 1)
        self.assertNotEqual(await prepared(service), oid)

    async def test_distinct_caller_ids_cannot_collide_after_normalization(self):
        self.assertNotEqual(_caller(runtime("a/b")), _caller(runtime("a_b")))
        self.assertNotEqual(_caller(runtime("x" * 150 + "a")), _caller(runtime("x" * 150 + "b")))

    async def test_live_order_requires_approval_and_reports_processing(self):
        service = make_service()
        oid = await prepared(service)
        await service.request_payment(oid)
        pending = await service.check(oid, finish=True)
        self.assertEqual(pending["status"], "awaiting_link_approval")
        self.assertEqual(service.zinc.submissions, [])
        service.wallet.requests["lsrq_1"]["status"] = "approved"
        result = await service.check(oid, finish=True)
        self.assertEqual(result["status"], "merchant_pending")
        self.assertEqual(len(service.zinc.submissions), 1)
        service.zinc.orders[oid]["status"] = "order_placed"
        result = await service.check(oid, finish=True)
        self.assertEqual(result["status"], "order_placed")
        self.assertEqual(len(service.zinc.submissions), 1)

    async def test_test_approval_never_retrieves_token_or_submits(self):
        service = make_service("link-test")
        oid = await approved(service)
        result = await service.check(oid, finish=True)
        self.assertEqual(result["status"], "approved_test_mode")
        self.assertEqual(service.wallet.tokens, 0)
        self.assertEqual(service.zinc.submissions, [])

    async def test_test_order_cannot_become_live_on_redeploy(self):
        service = make_service("link-test")
        oid = await approved(service)
        service.settings = Settings("live")
        with self.assertRaisesRegex(RestockError, "mode_changed"):
            await service.check(oid, finish=True)
        self.assertEqual(service.wallet.tokens, 0)

    async def test_rehearsal_is_labeled_and_never_touches_wallet(self):
        repo = Repository(runtime())
        service = Restock(
            repo, RehearsalPrivate(), RehearsalZinc(), RehearsalWallet(repo), Settings()
        )
        oid = await prepared(service)
        await service.request_payment(oid)
        result = await service.wait(oid)
        self.assertEqual(result["status"], "rehearsal_complete")
        self.assertIn("REHEARSAL", result["notice"])

    async def test_changed_office_blocks_payment(self):
        service = make_service()
        oid = await approved(service)
        service.private.value["shipping_address"]["postal_code"] = "99999"
        with self.assertRaisesRegex(RestockError, "office_changed"):
            await service.check(oid, finish=True)
        self.assertEqual(service.wallet.tokens, 0)

    async def test_amount_currency_and_metadata_are_checked(self):
        for field, value in [("amount", 2501), ("currency", "eur"), ("metadata", {})]:
            service = make_service()
            oid = await approved(service)
            service.wallet.requests["lsrq_1"][field] = value
            with self.assertRaisesRegex(RestockError, "does_not_match"):
                await service.check(oid, finish=True)
            self.assertEqual(service.zinc.submissions, [])

    async def test_expired_and_denied_requests_do_not_submit(self):
        for status in ("expired", "denied", "canceled"):
            service = make_service()
            oid = await approved(service)
            service.wallet.requests["lsrq_1"]["status"] = status
            self.assertEqual((await service.check(oid, finish=True))["status"], status)
            self.assertEqual(service.wallet.tokens, 0)

    async def test_expiry_checked_even_when_link_says_approved(self):
        service = make_service()
        oid = await approved(service)
        service.wallet.requests["lsrq_1"]["expires_at"] = time.time() - 10
        self.assertEqual((await service.check(oid, finish=True))["status"], "expired")

    async def test_final_retrieval_must_still_be_approved(self):
        service = make_service()
        oid = await approved(service)
        service.wallet.on_token = lambda record: record.update(status="canceled")
        with self.assertRaisesRegex(RestockError, "fresh_link_approval"):
            await service.check(oid, finish=True)
        self.assertEqual(service.zinc.submissions, [])

    async def test_missing_create_response_reconciles_same_request(self):
        service = make_service()
        oid = await prepared(service)
        service.wallet.fail_create = True
        with self.assertRaisesRegex(RestockError, "payment_outcome_unknown"):
            await service.request_payment(oid)
        result = await service.check(oid)
        self.assertEqual(result["status"], "awaiting_link_approval")
        await service.request_payment(oid)
        self.assertEqual(service.wallet.created, 1)

    async def test_uncertain_submit_never_replayed_even_after_restart(self):
        service = make_service()
        oid = await approved(service)
        service.zinc.lose_response = True
        self.assertEqual((await service.check(oid, finish=True))["status"], "submission_unknown")
        restarted = Restock(
            Repository(service.repo.runtime),
            service.private,
            service.zinc,
            service.wallet,
            service.settings,
        )
        self.assertEqual((await restarted.check(oid, finish=True))["status"], "submission_unknown")
        self.assertEqual(len(service.zinc.submissions), 1)
        self.assertEqual(service.zinc.submissions[0]["idempotency_key"], oid)
        self.assertEqual((await prepared(restarted)), oid)

    async def test_tracking_key_failure_does_not_claim_no_charge(self):
        service = make_service()
        oid = await approved(service)
        service.private.fail_save = True
        result = await service.check(oid, finish=True)
        self.assertEqual(result["status"], "submission_unknown")
        self.assertNotIn("Nothing", result["notice"])
        self.assertEqual(result["merchant_order_id"], service.zinc.orders[oid]["id"])
        self.assertEqual(result["submission_error"]["stage"], "tracking_key_storage")
        self.assertTrue(result["recovery_required"])
        self.assertFalse(result["merchant_status_check_available"])
        self.assertNotIn("Ask me to check", result["order_update"])
        self.assertIn("recover private tracking access", result["order_update"])
        self.assertEqual(
            (await service.check(oid, finish=True))["merchant_order_id"],
            result["merchant_order_id"],
        )
        self.assertEqual(len(service.zinc.submissions), 1)

    async def test_payment_rejection_is_diagnosable_and_not_replayed_after_restart(self):
        from restock.zinc import ZincSubmissionError

        service = make_service()
        oid = await approved(service)
        failure = ZincSubmissionError(
            "http_error",
            status=402,
            data={
                "error": {
                    "code": "payment_verification_failed",
                    "message": "spt_private_do_not_disclose",
                }
            },
        )
        with patch.object(service.zinc, "submit", new=AsyncMock(side_effect=failure)) as submit:
            result = await service.check(oid, finish=True)
            restarted = Restock(
                Repository(service.repo.runtime),
                service.private,
                service.zinc,
                service.wallet,
                service.settings,
            )
            again = await restarted.check(oid, finish=True)
        self.assertEqual(result["submission_error"]["http_status"], 402)
        self.assertEqual(again["submission_error"], result["submission_error"])
        self.assertTrue(again["recovery_required"])
        self.assertFalse(again["merchant_status_check_available"])
        self.assertIn("Repeated checks only return saved state", again["notice"])
        self.assertNotIn("spt_private_do_not_disclose", json.dumps([result, again]))
        self.assertNotIn("merchant_order_id", result)
        self.assertIn("internal Restock reference", result["order_reference_notice"])
        submit.assert_awaited_once()
        self.assertEqual(service.wallet.tokens, 1)

    async def test_returned_merchant_id_is_saved_when_response_lacks_tracking_access(self):
        from restock.zinc import ZincSubmissionError

        service = make_service()
        oid = await approved(service)
        failure = ZincSubmissionError("missing_tracking_access", status=201, order_id="known_order")
        with patch.object(service.zinc, "submit", new=AsyncMock(side_effect=failure)):
            result = await service.check(oid, finish=True)
        self.assertEqual(result["merchant_order_id"], "known_order")
        self.assertEqual(result["tracking_access_status"], "unavailable")
        self.assertTrue(result["recovery_required"])
        self.assertEqual((await service.repo.order(oid))["merchant_order_id"], "known_order")

    async def test_cancel_pending_and_draft(self):
        for request in [False, True]:
            service = make_service()
            oid = await prepared(service)
            if request:
                await service.request_payment(oid)
            result = await service.cancel(oid)
            self.assertEqual(result["status"], "canceled")
            self.assertEqual(service.zinc.submissions, [])

    async def test_cannot_cancel_submitted_order_as_link_request(self):
        service = make_service()
        oid = await approved(service)
        await service.check(oid, finish=True)
        with self.assertRaisesRegex(RestockError, "cannot_cancel"):
            await service.cancel(oid)

    async def test_cross_caller_and_cross_thread_ids_are_rejected(self):
        service = make_service()
        oid = await prepared(service)
        for rt in [
            runtime("bob", store=service.repo.store),
            runtime("alice", "other", service.repo.store),
        ]:
            other = make_service(rt=rt)
            with self.assertRaisesRegex(RestockError, "not_found"):
                await other.check(oid, finish=True)

    async def test_one_process_concurrent_completion_submits_once(self):
        service = make_service()
        oid = await approved(service)
        await asyncio.gather(*(service.check(oid, finish=True) for _ in range(5)))
        self.assertEqual(len(service.zinc.submissions), 1)

    async def test_public_state_does_not_contain_private_details(self):
        service = make_service()
        oid = await approved(service)
        result = await service.check(oid, finish=True)
        saved = await service.repo.order(oid)
        for sentinel in [
            "100 Example Street",
            "2125550100",
            "spt_synthetic_private",
            "synthetic-order-key",
        ]:
            self.assertNotIn(sentinel, json.dumps([result, saved]))

    async def test_budget_includes_fee_and_rejects_unknown_products(self):
        service = make_service()
        oid = await prepared(service)
        order = await service.repo.order(oid)
        self.assertEqual(order["retailer_limit_cents"], 2400)
        self.assertEqual(order["budget_cents"], 2500)
        await service.cancel(oid)
        with self.assertRaisesRegex(RestockError, "invalid_product"):
            await service.prepare([{"product_id": "invented", "quantity": 1}], 2500)

    async def test_wait_timeout_preserves_the_pending_request(self):
        service = make_service()
        oid = await prepared(service)
        await service.request_payment(oid)
        times = iter([0, 0, 2, 3, 4, 5])

        async def sleep(_):
            pass

        result = await service.wait(oid, sleep=sleep, clock=lambda: next(times))
        self.assertEqual(result["status"], "awaiting_link_approval")
        self.assertEqual(service.wallet.created, 1)

    async def test_anonymous_caller_rejected(self):
        with self.assertRaisesRegex(RestockError, "sign_in"):
            make_service(rt=runtime(None))
