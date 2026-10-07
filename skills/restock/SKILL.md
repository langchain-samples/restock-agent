---
name: restock
description: Find office supplies, packaged food, snacks, beverages, and pantry items with Zinc, separate the shopping budget from an explicitly chosen upfront payment, request the caller's Link approval, and track the same order.
---

# Office restocking

1. Ask for a product, quantity or pack preference, and maximum USD budget.
2. `search_restock_products` returns observed products and IDs. Present a few options.
3. Confirm the choice. Use those IDs in `prepare_restock_order`. It saves the cart
   and checks the caller's Link Connection. The office comes from a private
   Connection. The budget includes the $1 base Zinc order fee and any optional email fee.
4. If preparation returns `login_required`, show `verification_url`, tell the user
   their items are saved, and ask them to reply "Done" after connecting. End the
   turn so the link is visible. On their reply, use `link_finish_login`, then
   continue its returned `order_id`. Do not restart shopping or pending consent.
   Connected users skip this step. No separate "connect Link" request is needed.
5. A shopping budget is not a payment amount. Show the listed item subtotal and $1
   base fee; disclose a possible email surcharge when email_updates_requested is true.
   Explain tax/shipping are unknown and Zinc refunds unused prepaid money.
   Mention the selected retailer's shipping_guidance, including a published free
   shipping threshold when returned. It is not a checkout quote or guarantee.
   Ask the user to choose an upfront total within their budget, then call
   `set_restock_payment_amount`. Never automatically copy the budget or invent a
   final quote or a shipping/tax allowance. Exact-total-only requests must stop
   because this Zinc route does not quote the final total before payment.
6. Show the returned fee_cents and retailer_limit_cents. Fees stay within the chosen
   total. Show tax_shipping_allowance_cents as the remaining amount after listed
   items and fees, not an estimate of tax or shipping. Never increase the chosen
   total. `request_restock_payment` pauses for human review in Studio or Slack. Only that review
   can allow a Link request; do not invent an approval in tool arguments.
7. Show the returned Link URL and chosen amount, then call `wait_for_restock_approval`. It can finish
   after approval without another user message while the tool is still waiting.
   Slack posts the URL before waiting. If `approval_delivery=reply_required`, end
   the turn with the URL and ask for a reply after approval, then check the same order.
8. Report the exact returned status. `approved_test_mode` is an approved test with
   no merchant submission. `merchant_pending` is processing. `order_placed` is a
   retailer confirmation. Keep the normal Slack/Studio confirmation to order_update,
   items/quantities, and Zinc order ID. Use check_restock_order with detail="summary"
   after approval or for a short confirmation. Only an explicit order, shipping,
   or email status question uses detail="status" and includes tracking/email details.
   Never claim email delivery before Zinc reports it, or whole-order delivery from
   one package. Use detail="notifications" only for explicit questions about Slack
   notification setup. Do not show automatic-update settings in normal confirmations
   or status reports. Pre-payment fee disclosures still apply. Notification failure never
   changes the purchase result or permits a replacement order.
   The agent never needs a card number or the privately configured email address.
   For a failed order, explain the returned failure_reasons. Checking an existing
   failed order fetches fresh details from Zinc without resubmitting it. Missing
   reasons require investigation with Zinc, not a guessed cause or another payment.
   Failure alone does not confirm a refund.

If waiting ends, `check_restock_order` continues that same reviewed order. If the
outcome is unknown, preserve it for reconciliation. Never manufacture a new ID
or order to get past an error. `cancel_restock_order` handles drafts and pending
Link requests, not submitted merchant purchases.

`order_id` is a Restock reference. Only `merchant_order_id` is a Zinc order ID.
If `recovery_required` is true, report the safe `submission_error` and ask for
operator recovery. When `merchant_status_check_available` is false, another
status check only returns saved state. Do not promise that checking later will
resolve it or claim that no charge occurred. Never retry the payment.

Expired unpaid payment instructions are refreshed for the same cart automatically.
Do not ask the user to cancel and start again just because those instructions expired.

Rehearsal is visibly fictional. Its catalog is generated from the search phrase
only to demonstrate the conversation. Real modes search Zinc; they have no
hard-coded product catalog, merchant adapter, or browser checkout.
