"""The purchase state machine. Only public summaries leave this service."""

from __future__ import annotations

import asyncio
import time
import uuid

from managed_deepagents import CredentialAuthorizationRequiredError

from restock.config import PaymentNotRequested, RestockError, Settings
from restock.storage import fingerprint
from restock.updates import order_update
from restock.zinc import FEE_CENTS, MERCHANT_TERMINAL, ZincSubmissionError, expiry, public_order

TERMINAL = {
    "approved_test_mode",
    "rehearsal_complete",
    "canceled",
    "denied",
    "expired",
    *MERCHANT_TERMINAL,
}


def payment_amount(order: dict) -> int:
    amount = order.get("payment_amount_cents")
    if type(amount) is int and amount > FEE_CENTS:
        return amount
    # Preserve previously created requests across a redeploy. Never apply the
    # old budget-as-payment behavior to an unpaid prepared draft.
    if order.get("request_id") or order["status"] in {
        "payment_creating",
        "payment_unknown",
        "submitting",
        "submission_unknown",
    }:
        return order["budget_cents"]
    raise RestockError("choose_payment_amount_first")


def order_fee(order: dict) -> int:
    fee = order.get("fee_cents", FEE_CENTS)
    if type(fee) is not int or fee < FEE_CENTS:
        raise RestockError("invalid_order_fee")
    return fee


def view(order: dict) -> dict:
    fields = (
        "mode",
        "status",
        "items",
        "budget_cents",
        "payment_amount_cents",
        "retailer_limit_cents",
        "fee_cents",
        "estimated_items_cents",
        "office_label",
        "approval_url",
        "link_status",
        "merchant_status",
        "failure_reasons",
        "shipping_status",
        "merchant_order_id",
        "shipments",
        "email_updates_requested",
        "submission_error",
        "tracking_access_status",
        "shipping_guidance",
        "slack_updates_status",
    )
    result = {key: order[key] for key in fields if key in order}
    result["order_id"] = order["id"]
    result["order_reference_notice"] = (
        "order_id is the internal Restock reference. Only merchant_order_id is a Zinc order ID. "
        "Never label a Restock reference as a Zinc order ID, including in test mode."
    )
    result["currency"] = "usd"
    result["amount_description"] = (
        "budget_cents is the shopping limit, not the payment amount. "
        "payment_amount_cents is the separately chosen upfront amount, including the Zinc fee. "
        "Tax and shipping are unknown; Zinc refunds any unused amount after checkout."
    )
    result["final_total_known"] = False
    if order.get("slack_updates_status"):
        result["slack_updates_notice"] = (
            "Zinc event notifications are enabled for the original Slack thread. "
            "They report placement, failure, tracking and delivery without another agent run. "
            "Manual status checks remain available if an update does not arrive."
            if order["slack_updates_status"] == "enabled"
            else "Automatic Slack updates are unavailable. Check this same order manually; "
            "this does not change its payment or submission status."
        )
    amount, items = order.get("retailer_limit_cents"), order.get("estimated_items_cents")
    if type(amount) is int and type(items) is int:
        result["tax_shipping_allowance_cents"] = max(0, amount - items)
        result["cost_notice"] = (
            "tax_shipping_allowance_cents is what remains for tax, shipping and price changes "
            "after the listed items and verified Zinc fees. It is not an estimate of those "
            "charges or a guarantee the order fits. Never increase the payment automatically."
        )
    if order.get("email_updates_requested"):
        result["email_updates_status"] = (
            "not_sent_test_mode"
            if order["mode"] != "live"
            else order.get("email_updates_status", "unconfirmed")
        )
        result["email_updates_notice"] = (
            "Email updates are requested for the privately configured recipient. "
            "Zinc must enable this feature. An extra fee may apply and must fit "
            "the chosen upfront total; never promise email delivery. "
            "Test modes do not send order emails."
        )
    else:
        result["email_updates_status"] = "not_requested"
    if order["status"] == "prepared" and order.get("payment_amount_cents") is None:
        result["status"] = "payment_amount_required"
        result["payment_notice"] = (
            "Show the listed item subtotal and $1 base Zinc fee separately. If email "
            "updates are requested, disclose the possible extra fee; the actual fee "
            "will be checked before review. Tax and shipping "
            "are not quoted. Ask what total amount the user wants to pay upfront, within "
            "their shopping budget; unused money is refunded. Do not copy the shopping "
            "budget automatically, invent a final total, or add an arbitrary allowance. "
            "Use set_restock_payment_amount after they choose, then purchase review. "
            "No payment has been requested."
        )
    if order["mode"] == "rehearsal":
        result["notice"] = (
            "REHEARSAL: fictional products and simulated approval. Nothing was purchased."
        )
    elif order["status"] == "approved_test_mode":
        result["notice"] = (
            "Approved in Link test mode. No payment token retrieved and no merchant order submitted."
        )
    elif order["status"] in {
        "payment_creating",
        "payment_unknown",
        "submitting",
        "submission_unknown",
    }:
        result["notice"] = (
            "Outcome needs reconciliation. Do not create a replacement request or order."
        )
    if order["status"] in {"submitting", "submission_unknown"}:
        can_check = (
            bool(order.get("merchant_order_id"))
            and order.get("tracking_access_status") != "unavailable"
        )
        result["merchant_status_check_available"] = can_check
        if not can_check:
            result["recovery_required"] = True
            result["notice"] = (
                "Operator recovery is required. The agent cannot query Zinc for this order "
                "without a confirmed Zinc ID and private tracking access. Repeated checks "
                "only return saved state. Do not retry payment, create a replacement, or "
                "claim no charge. Share the Restock reference and safe submission_error "
                "with the operator to reconcile the existing attempt."
            )
    if update := order_update(order):
        result["order_update"] = update
    return result


class Restock:
    def __init__(self, repository, private, zinc, wallet, settings: Settings):
        self.repo, self.private, self.zinc, self.wallet, self.settings = (
            repository,
            private,
            zinc,
            wallet,
            settings,
        )

    def mode_check(self, order):
        if order["mode"] != self.settings.mode:
            raise RestockError("mode_changed_start_no_new_payment")

    async def search(self, query: str, budget_cents: int) -> dict:
        if not 3 <= len(query.strip()) <= 200 or type(budget_cents) is not int:
            raise RestockError("invalid_search")
        if not 200 <= budget_cents <= self.settings.max_budget:
            raise RestockError("budget_out_of_range")
        key = await self.private.zinc_key(self.settings.zinc_connection)
        products = await self.zinc.search(query.strip(), key, budget_cents - FEE_CENTS)
        await self.repo.put("search:" + self.repo.thread, {"products": products, "at": time.time()})
        return {
            "mode": self.settings.mode,
            "products": products,
            "budget_cents": budget_cents,
            "notice": "Fictional rehearsal results."
            if self.settings.mode == "rehearsal"
            else "Live Zinc listings. Listed prices exclude any added tax, shipping, and the Zinc order fee.",
        }

    async def body(self, order):
        office = await self.private.office(self.settings.office_connection)
        body = {
            "products": [
                {"url": item["url"], "quantity": item["quantity"]} for item in order["items"]
            ],
            "shipping_address": office["shipping_address"],
            "max_price": payment_amount(order) - order_fee(order),
            "idempotency_key": order["id"],
            "metadata": {"restock_order": order["id"]},
        }
        # Existing orders never inherit an email opt-in added after their preparation.
        if order.get("email_updates_requested"):
            email = office.get("notification_email")
            if not email:
                raise RestockError("order_or_office_changed_review_again")
            body["customer_notifications"] = {"email": email}
        if order.get("fingerprint") and fingerprint(body) != order["fingerprint"]:
            raise RestockError("order_or_office_changed_review_again")
        return body, office["label"]

    async def prepare(self, selections: list[dict], budget_cents: int) -> dict:
        if type(budget_cents) is not int or not 200 <= budget_cents <= self.settings.max_budget:
            raise RestockError("budget_out_of_range")
        if not 1 <= len(selections) <= 5:
            raise RestockError("choose_one_to_five_products")
        async with self.repo.lock():
            active = await self.repo.get("active:" + self.repo.thread)
            if active:
                existing = await self.repo.order(active["id"])
                if existing["status"] not in TERMINAL:
                    return {
                        **view(existing),
                        "notice": "An order already exists. Review or cancel it before preparing another.",
                    }
            search = await self.repo.get("search:" + self.repo.thread)
            if not search or time.time() - search["at"] > 900:
                raise RestockError("search_again_for_current_products")
            indexed = {item["product_id"]: item for item in search["products"]}
            items, seen = [], set()
            for selection in selections:
                pid, quantity = selection.get("product_id"), selection.get("quantity")
                if (
                    pid not in indexed
                    or pid in seen
                    or type(quantity) is not int
                    or not 1 <= quantity <= 20
                ):
                    raise RestockError("invalid_product_selection")
                seen.add(pid)
                items.append({**indexed[pid], "quantity": quantity})
            known_items = sum((item["price_cents"] or 0) * item["quantity"] for item in items)
            if known_items > budget_cents - FEE_CENTS:
                raise RestockError("items_exceed_budget_before_tax_and_shipping")
            estimate = (
                known_items if all(item["price_cents"] is not None for item in items) else None
            )
            shipping = await self.zinc.shipping_guidance([item["url"] for item in items])
            if any(info.get("orderable") is False for info in shipping):
                raise RestockError("retailer_not_available_for_us_delivery")
            if any(info.get("customer_account_required") is True for info in shipping):
                raise RestockError("retailer_requires_linked_account_choose_another")
            order = {
                "id": str(uuid.uuid4()),
                "thread": self.repo.thread,
                "mode": self.settings.mode,
                "status": "prepared",
                "items": items,
                "budget_cents": budget_cents,
                "payment_amount_cents": None,
                "fee_cents": FEE_CENTS,
                "estimated_items_cents": estimate,
                "created_at": time.time(),
                "shipping_guidance": shipping,
            }
            office = await self.private.office(self.settings.office_connection)
            order["office_label"] = office["label"]
            order["email_updates_requested"] = bool(office.get("notification_email"))
            await self.repo.save(order)
            await self.repo.put("active:" + self.repo.thread, {"id": order["id"]})
            return view(order)

    async def set_payment_amount(self, order_id: str, amount_cents: int) -> dict:
        """Prepare an explicit upfront amount for human review, never infer it from budget."""
        async with self.repo.lock():
            order = await self.repo.order(order_id)
            self.mode_check(order)
            if order["status"] != "prepared" or order.get("request_id"):
                raise RestockError("payment_amount_cannot_change_after_request")
            if order.get("review_started") and order.get("payment_amount_cents") != amount_cents:
                raise RestockError("payment_amount_already_in_review_cancel_before_changing")
            if type(amount_cents) is not int or not FEE_CENTS < amount_cents <= min(
                order["budget_cents"], self.settings.max_budget
            ):
                raise RestockError("payment_amount_must_fit_shopping_budget")
            known_items = sum(
                (item["price_cents"] or 0) * item["quantity"] for item in order["items"]
            )
            if amount_cents < known_items + FEE_CENTS:
                raise RestockError("payment_amount_below_listed_items_and_fee")
            # Retrying the same amount keeps the cart and only refreshes expiry.
            if order.get("payment_amount_cents") == amount_cents:
                await self._review(order)
                return view(order)
            if order.get("fingerprint") and order.get("payment_amount_cents") is not None:
                await self.body(order)  # reject a changed office on an already bound cart
            updated = {
                **order,
                "payment_amount_cents": amount_cents,
                "fee_cents": FEE_CENTS,
                "retailer_limit_cents": amount_cents - FEE_CENTS,
            }
            updated.pop("fingerprint", None)
            body, label = await self.body(updated)
            if updated.get("email_updates_requested"):
                # Discover the opt-in fee with an unpaid request. Keep the user's
                # total fixed, subtract the fee from the retailer allowance, then
                # verify a fresh challenge for that exact body and chosen total.
                discovery = await self.zinc.challenge(body, None)
                fee = discovery["amount"] - body["max_price"]
                if type(fee) is not int or fee < FEE_CENTS:
                    raise RestockError("invalid_order_fee")
                if amount_cents <= fee or amount_cents < known_items + fee:
                    raise RestockError("payment_amount_below_listed_items_and_email_fee")
                updated["fee_cents"] = fee
                updated["retailer_limit_cents"] = amount_cents - fee
                body, label = await self.body(updated)
                challenge = await self.zinc.challenge(body, amount_cents)
                if challenge["network_id"] != discovery["network_id"]:
                    raise RestockError("payment_network_changed")
            else:
                challenge = await self.zinc.challenge(body, amount_cents)
            updated["office_label"] = label
            updated["fingerprint"] = fingerprint(body)
            updated["challenge"] = challenge
            await self.repo.save(updated)
            return view(updated)

    async def review(self, order_id: str) -> dict:
        async with self.repo.lock():
            order = await self.repo.order(order_id)
            await self._review(order)
            # The native review may resume by replaying the tool. Keep its amount
            # immutable so an old approve button cannot authorize a changed sum.
            order["review_started"] = True
            await self.repo.save(order)
            return view(order)

    async def _review(self, order: dict) -> None:
        self.mode_check(order)
        if order["status"] != "prepared":
            raise RestockError("order_is_not_awaiting_review")
        amount = payment_amount(order)
        body, _ = await self.body(order)
        expires_at = order["challenge"].get("expires_at")
        if expires_at is not None and expires_at <= time.time() + 15:
            # Login or human review can outlast Zinc's unpaid challenge. Refresh
            # only this prepared cart, with the same body and idempotency key.
            # This never creates a Link request, retrieves a token or submits.
            refreshed = await self.zinc.challenge(body, amount)
            for field in ("amount", "currency", "network_id"):
                if refreshed.get(field) != order["challenge"].get(field):
                    raise RestockError("payment_challenge_changed_review_required")
            fresh_expiry = refreshed.get("expires_at")
            if fresh_expiry is not None and fresh_expiry <= time.time() + 15:
                raise RestockError("payment_challenge_expired")
            order["challenge"] = refreshed
            await self.repo.save(order)

    async def request_payment(self, order_id: str) -> dict:
        """Called ONLY after the graph's human-review interrupt has been approved."""
        async with self.repo.lock():
            order = await self.repo.order(order_id)
            self.mode_check(order)
            if order["status"] != "prepared":
                return view(order)
            await self._review(order)
            # Persist intent before Link: an uncertain response is reconciled via
            # metadata, never retried by creating another spend request.
            order["status"] = "payment_creating"
            await self.repo.save(order)
            try:
                request = await self.wallet.create(order)
                self.bind_request(order, request)
            except CredentialAuthorizationRequiredError:
                order["status"] = "payment_unknown"
                await self.repo.save(order)
                raise
            except PaymentNotRequested:
                order["status"] = "prepared"
                await self.repo.save(order)
                raise
            except RestockError as error:
                if str(error) in {
                    "link_login_required",
                    "link_payment_method_required",
                    "choose_default_payment_method_in_link",
                }:
                    order["status"] = "prepared"
                    await self.repo.save(order)
                    raise
                order["status"] = "payment_unknown"
                await self.repo.save(order)
                raise RestockError("payment_outcome_unknown_check_existing_order") from None
            except Exception:
                order["status"] = "payment_unknown"
                await self.repo.save(order)
                raise RestockError("payment_outcome_unknown_check_existing_order") from None
            await self.repo.save(order)
            return view(order)

    @staticmethod
    def verify_request(order: dict, request: dict) -> None:
        if not isinstance(request, dict) or not isinstance(request.get("id"), str):
            raise RestockError("invalid_link_response")
        if (
            request.get("amount") != payment_amount(order)
            or request.get("currency", "").lower() != "usd"
        ):
            raise RestockError("payment_does_not_match_order")
        metadata = request.get("metadata") or {}
        if (
            metadata.get("restock_order") != order["id"]
            or metadata.get("restock_fingerprint") != order["fingerprint"]
        ):
            raise RestockError("payment_does_not_match_order")
        if order.get("request_id") and order["request_id"] != request["id"]:
            raise RestockError("payment_does_not_match_order")

    def bind_request(self, order, request):
        self.verify_request(order, request)
        order["request_id"] = request["id"]
        order["status"] = "awaiting_link_approval"
        order["link_status"] = request["status"]
        url = request.get("approval_url")
        if url:
            from urllib.parse import urlparse

            parsed = urlparse(url)
            if (
                parsed.scheme != "https"
                or parsed.hostname not in {"app.link.com", "link.com"}
                or parsed.username
            ):
                raise RestockError("invalid_link_approval_url")
            order["approval_url"] = url

    async def check(self, order_id: str, *, finish: bool = False) -> dict:
        async with self.repo.lock():
            order = await self.repo.order(order_id)
            self.mode_check(order)
            if order.get("merchant_order_id"):
                if order.get("tracking_access_status") == "unavailable":
                    return view(order)
                key = await self.private.order_key(order_id)
                record = await self.zinc.status(order["merchant_order_id"], key)
                order.update(public_order(record))
                order["status"] = (
                    order["merchant_status"]
                    if order["merchant_status"] in MERCHANT_TERMINAL
                    else "merchant_pending"
                )
                await self.repo.save(order)
                return view(order)
            if order["status"] in {"payment_creating", "payment_unknown"}:
                records = await self.wallet.history()
                matches = [
                    r for r in records if (r.get("metadata") or {}).get("restock_order") == order_id
                ]
                if len(matches) != 1:
                    return view(order)
                self.bind_request(order, matches[0])
                await self.repo.save(order)
            if order["status"] != "awaiting_link_approval":
                return view(order)
            request = await self.wallet.retrieve(order["request_id"])
            self.verify_request(order, request)
            order["link_status"] = request["status"]
            expires_at = expiry(request.get("expires_at"))
            if expires_at is not None and expires_at <= time.time():
                order["status"] = "expired"
            elif request["status"] in {"denied", "expired", "canceled"}:
                order["status"] = request["status"]
            elif request["status"] == "approved" and finish:
                if order["mode"] != "live":
                    order["status"] = (
                        "rehearsal_complete"
                        if order["mode"] == "rehearsal"
                        else "approved_test_mode"
                    )
                else:
                    await self._submit(order)
            await self.repo.save(order)
            return view(order)

    async def _submit(self, order):
        body, _ = await self.body(order)
        # Re-probe without payment. Bind the new challenge to the same body, amount,
        # and Stripe network. No credential is requested until these checks pass.
        current = await self.zinc.challenge(body, payment_amount(order))
        if current["network_id"] != order["challenge"]["network_id"]:
            raise RestockError("payment_network_changed")
        request, token = await self.wallet.token(order["request_id"])
        self.verify_request(order, request)
        if request.get("status") != "approved":
            raise RestockError("fresh_link_approval_required")
        expires_at = expiry(request.get("expires_at"))
        if expires_at is not None and expires_at <= time.time():
            raise RestockError("link_approval_expired")
        order["status"] = "submitting"
        await self.repo.save(order)
        stage = "merchant_submission"
        try:
            record, key = await self.zinc.submit(body, current, token)
            # Preserve the confirmed ID before attempting a separate private write.
            # A crash or rejected Connection write must not erase that evidence.
            order["merchant_order_id"] = record["id"]
            order["tracking_access_status"] = "unavailable"
            stage = "tracking_key_storage"
            await self.repo.save(order)
            # Saving the tracking key can fail AFTER the purchase. Keep the order
            # uncertain rather than tell the caller that nothing was charged.
            await self.private.save_order_key(order["id"], key)
            order["tracking_access_status"] = "available"
            stage = "merchant_response"
            order.update(public_order(record))
            order["status"] = (
                order["merchant_status"]
                if order["merchant_status"] in MERCHANT_TERMINAL
                else "merchant_pending"
            )
        except ZincSubmissionError as error:
            order["submission_error"] = error.diagnostic
            if error.merchant_order_id:
                order["merchant_order_id"] = error.merchant_order_id
                order["tracking_access_status"] = "unavailable"
            order["status"] = "submission_unknown"
        except Exception:
            order["submission_error"] = {"stage": stage, "reason": "operation_failed"}
            order["status"] = "submission_unknown"
        finally:
            token = None

    async def cancel(self, order_id: str) -> dict:
        async with self.repo.lock():
            order = await self.repo.order(order_id)
            self.mode_check(order)
            if order["status"] in TERMINAL:
                return view(order)
            if order["status"] not in {"prepared", "awaiting_link_approval"}:
                raise RestockError("cannot_cancel_unknown_or_submitted_order")
            if order.get("request_id"):
                request = await self.wallet.cancel(order["request_id"])
                self.verify_request(order, request)
                if request["status"] not in {"canceled", "expired", "denied"}:
                    raise RestockError("link_cancellation_not_confirmed")
                order["link_status"] = request["status"]
            order["status"] = "canceled"
            await self.repo.save(order)
            return view(order)

    async def wait(self, order_id: str, *, sleep=asyncio.sleep, clock=time.monotonic) -> dict:
        deadline = clock() + self.settings.wait_seconds
        while True:
            result = await self.check(order_id, finish=True)
            if result["status"] != "awaiting_link_approval":
                return result
            if result.get("link_status") not in {"created", "pending_approval", "approved"}:
                return {
                    **result,
                    "notice": "Link requires attention. Check this existing request in Link.",
                }
            if clock() >= deadline:
                return {
                    **result,
                    "notice": "Waiting ended. The request is still pending. Ask to check this same order after approving.",
                }
            await sleep(min(4, max(0, deadline - clock())))
