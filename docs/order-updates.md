# Shipping information and automatic Slack updates

Restock checks Zinc's retailer information when you select products. It shows a
published free-shipping threshold when available, then the amount remaining after
listed items and verified Zinc fees once you choose an upfront payment. Tax,
shipping, eligibility and price changes are still determined at checkout. Restock
does not invent a quote or increase your chosen amount.

The check uses Zinc's free `/retailers` and `/retailers/check` endpoints. An explicit
unsupported US destination or required linked retailer account stops preparation;
this sample does not link retailer accounts. Unavailable information stays unknown.
There are no product-specific or retailer-specific rules.

## Shipping and email timing

Purchase confirmations are brief. Email and tracking details appear when you
ask for a status update. Automatic placement messages are also brief; shipment
events still include available tracking. Slack notification settings appear only
when explicitly requested. Order/payment failures and tracking-access recovery
requirements are always surfaced. Pre-payment email-fee disclosures still apply.

An accepted submission, a placed retailer order, shipment, and delivery are
different milestones. Zinc's guide gives a typical 5–10 minutes for order
processing. It does not give a guaranteed shipping-update deadline for an
individual order. Tracking generally appears after the retailer ships and Zinc
processes its shipping notification. A carrier delivery estimate may remain
empty until the first in-transit scan.

Zinc sends optional shipment emails. Restock passes the saved office email with
the order and checks the provider's email fee against the chosen payment amount.
The feature depends on Zinc's enablement; an accepted email request does not prove
delivery. `pending` means delivery is not confirmed, and is distinct from
`delivery_failed`. An immediate placement email is not guaranteed.

If a placed order has no tracking, check the same order in its original thread.
If there is no useful update, ask [Zinc support](mailto:support@zinc.com) to check
the retailer's dispatch status and the order's email-send logs. Supply the Zinc
order ID, not private credentials or delivery details. A missing tracking number
alone is not evidence that the purchase failed.

## Enable Slack notifications

Use your existing HTTPS MDA deployment URL, without a path. From this repository:

```bash
uv run --no-sync python scripts/setup.py --slack
uv run --no-sync python scripts/configure_updates.py --url https://YOUR_DEPLOYMENT.us.langgraph.app
uv run --no-sync python scripts/preflight.py
uv run mda deploy .local/app --name restock
```

The helper saves `RESTOCK_PUBLIC_URL` and generates `RESTOCK_UPDATES_SIGNING_KEY`
privately in `.local/app/.env`. It preserves other settings and keeps the existing
signing key on later runs. Keep that key across redeployments so saved subscriptions
remain valid. This local setup does not contact Zinc or send a Slack message.

After deployment, new live orders subscribe automatically after Zinc accepts the
submission and returns private tracking access. For an existing submitted order,
ask in its original thread: “@Restock Can you check this order?” The check adds
the subscription without another payment. Restock rechecks status after setup to
catch an order that completed before the subscription existed.

When enabled, Zinc's signed events report order placement, failure with a recognized
reason, new tracking, delivery, or retailer cancellation. The original Slack thread
receives the update even after the agent run ends. No new model run starts.
Link approval and Zinc accepting a submission are still separate from retailer
confirmation. A failed order does not establish that a refund has completed.

`rehearsal` and `link-test` do not register subscriptions or send merchant emails.
Use the offline suite to test notifications without making another purchase:

```bash
uv run --no-sync python scripts/verify.py
```

## Delivery and recovery

Zinc supports one webhook URL per paying account, not one per order. Restock reuses
its own URL and refuses to replace another application's URL. If setup is missing,
conflicts, or fails, the order result remains intact. Ask for a status check and
resolve the notification setup separately. A later check retries setup.

The receiver verifies Zinc's HMAC signature over the raw request body. It uses a
signed saved route from MDA's authenticated Slack context, never a destination from
the event or model. The webhook secret stays in a separate user-owned Connection.
Addresses, email recipients, raw errors and payment credentials are excluded from
notifications. Application signing prevents other Store writers from changing the
saved owner or destination. The server adapters use internal APIs pinned to MDA
0.8.1 and need revalidation on upgrades.

Saved event fingerprints suppress duplicates and older events. Delivery failures
return a temporary HTTP error, and retries use the same Slack action ID. Zinc's
retry schedule and hosted delivery are not yet verified; this is not an exactly-once
delivery guarantee. There is no periodic poller. If an update does not arrive, ask
to check the same order. Multiple production workers need a transactional delivery
queue as well as the session/order concurrency work described in the
[architecture](architecture.md).

Accountless MPP orders use the tracking access returned with the order; the
product-search account's dashboard may not show them.

References: [retailers](https://www.zinc.com/docs/v2/api-reference/retailers/list-retailers),
[webhook endpoint](https://www.zinc.com/docs/v2/api-reference/webhooks/set-webhook-endpoint),
[webhooks](https://www.zinc.com/docs/v2/api-reference/introduction/webhooks),
[tracking](https://www.zinc.com/docs/v2/api-reference/orders/tracking),
[email status](https://www.zinc.com/docs/v2/api-reference/orders/get-order), and
[agent usage](https://www.zinc.com/docs/v2/agent-skills/usage).
