# Troubleshooting

Start with the [README quickstart](../README.md#quickstart). Keep one order in
one conversation, and check its current status before attempting another purchase.

## Setup and Slack

| Problem | What to do |
| --- | --- |
| Model not found or access denied | Set `OPENAI_MODEL` in `.local/app/.env` to a model available to your account, then redeploy. |
| LangSmith authentication or workspace error | Check the API key, workspace ID, US region, and MDA access. |
| Sandbox setup fails | Check that the image has Node/npm and the organization permits Link API/login and npm registry access. |
| Zinc search returns insufficient balance | Fund the Zinc account associated with `restock-zinc`. Search fees are separate from Link payments. |
| No Slack authorization prompt | An existing authorization can be reused. Look for the Restock bot in the workspace. |
| Reply doesn't trigger Restock | Mention `@Restock` in the same thread. Avoid posting follow-ups as new channel messages. |
| One message produces two replies | Keep both trace IDs for the MDA team. Do not approve two copies of an order. Duplicate DM events remain unresolved. |
| Studio Resume returns 404 for a Slack conversation | Continue in the original Slack thread. Studio can inspect it, but its identity cannot resume that Slack-owned conversation. |
| Link asks for a default payment method | Select a default in Link, or use a wallet with one payment method. |
| `link_proxy_not_configured` | Run `scripts/configure_proxy.py` with this app's public HTTPS URL, then restart or redeploy. See [proxy setup](sandbox-proxy.md). |
| `link_proxy_identity_unavailable` | Check deployment/workspace configuration and access to the sandbox metadata API. A sandbox name is not its UUID. |
| `link_proxy_command_failed_check_existing_order` | Check callback reachability, signatures, and the saved session. Reconcile the same order; do not repeat a create command or switch transport mid-purchase. |
| `link_session_renewal_failed` | Renewal failed. Preserve the order, resolve the saved login, and check the same request. The helper does not restart consent or replay a payment automatically. |
| `link_check_failed_no_payment_requested` | The wallet check failed before a payment request was attempted. The cart remains prepared. Resolve the check, then review the same draft again. |
| A generic secret prompt appears instead of Link sign-in | Refresh and redeploy the current source. Never paste wallet session data into the prompt. |

For proxy configuration, follow [these steps](sandbox-proxy.md#configure-a-deployed-app).
To deploy source updates:

```bash
uv run --no-sync python scripts/setup.py --slack
uv run --no-sync python scripts/preflight.py
uv run mda deploy .local/app --name restock
```

If needed, `uv run --no-sync python scripts/check_zinc.py` checks reachability
without an API key or payment. `cart_required` is expected: a bodyless response
cannot prove which payment methods the actual cart supports.

## Payment failures

Link approval does not confirm retailer checkout. Zinc can accept a submission
and later fail while placing the retailer order. Ask in the original thread:

> @Restock Check this order and tell me why it failed. Do not place another order.

For `max_price_exceeded`, the retailer total exceeded the allowance left after
Zinc's fees. The listed product price may omit tax and shipping. A new order
with a different amount needs its own review and Link approval; the agent never
raises the amount automatically. Verify any refund with the provider.

| Status | Next step |
| --- | --- |
| `payment_amount_required` | Choose the payment amount separately from the shopping budget. |
| `prepared` | Review or cancel the unpaid draft. |
| `awaiting_link_approval` | Approve or cancel the existing Link request. If the wait ended, ask to check this same order. |
| `payment_unknown` | Check the same order. The helper looks for the existing request in Link history. |
| `submitting` or `submission_unknown` | Reconcile with Zinc before retrying. A payment or order may already have succeeded. |
| `merchant_pending` | Zinc is processing the existing order. Ask to check it again later. |
| `order_placed` | The retailer confirmed the order. Shipping and email can follow later. |

For an uncertain submission, preserve the Restock reference, Link request, and
Zinc order ID if available. The Restock reference is sent as the idempotency key.
Share those references with [Zinc support](mailto:support@zinc.com), not private
credentials or delivery details. Do not clear state, change mode, or start another
conversation to force a retry.

If `recovery_required` is true, an operator must investigate the returned
`submission_error`. When `merchant_status_check_available` is false, another check
only returns saved state until private tracking access is recovered.

## Tracking, email, and the Zinc dashboard

Ask **Did it ship?** in the original thread. Missing tracking alone does not mean
an order failed; it may not have shipped yet. Optional shipment emails are sent
by Zinc, not Restock, and depend on provider support. Test modes send no order email.
See [order updates](order-updates.md) for notification setup and timing.

Link/MPP orders use their returned tracking access. They may not appear in the
dashboard for the separate Zinc account used for product search.
