# How Restock fits together

MDA runs the conversation and tools. Zinc searches for products and places retailer
orders. Link approves payment and supplies a token for Zinc's Stripe payment
route. The model chooses products and asks questions; it does not receive payment
tokens, wallet sessions, or the delivery address.

| Component | Responsibility |
| --- | --- |
| `agent.py`, instructions, skill | Natural conversation and use of the authored tools |
| `tools/restock.py` | Tool arguments, caller runtime, human-review interrupt |
| `channels/slack.py`, `restock/channel.py` | Optional native Slack channel and approval-link delivery before polling |
| `restock/service.py` | Saved order, shopping budget, chosen payment amount, cancellation and recovery |
| `restock/zinc.py` | Fixed Zinc API, unpaid payment challenge, MPP header and order status |
| `restock/updates.py` | Public order confirmation, shipment summaries, and email delivery status |
| `restock/costs.py` | Retailer shipping policies and the allowance remaining after listed items and verified fees |
| `channels/zinc.py`, `restock/notifications.py`, `restock/notification_runtime.py` | Signed Zinc callbacks, saved Slack destinations, and notification delivery |
| `restock/wallet.py` | Narrow Link CLI operations and private shared payment token retrieval |
| `restock/link_session.py` | Device login, user-owned Connection storage, refresh capture and sandbox cleanup |
| `restock/storage.py` | Caller/conversation-owned Store records and private Connections |
| `restock/rehearsal.py` | Fictional providers with no external payment behavior |
| `scripts/configure_office.py`, `scripts/office_connection.py` | Operator-only office setup and replacement of the selected deployment's saved delivery details |
| `scripts/configure_updates.py` | Private deployment URL and signing-key setup for optional Slack updates |

The shipping address, optional notification email, and Zinc search key are agent-owned Connections. The Link
session and the key returned by Zinc for order tracking are user-owned Connections.
The public order summary, its fingerprint, payment challenge, and provider IDs
live in MDA Store. Store records are separated by caller and conversation.

Email opt-in is captured for new orders; the recipient stays in the private
Connection and bound HTTP body, outside Store records, model inputs, and review
cards. When email is requested, unpaid challenge discovery derives the provider's
fee, reduces the retailer allowance to keep the chosen total fixed, then obtains
an exact-total challenge. Changed fees at review refresh or submission fail the
amount check; no extra charge is approved. Zinc controls whether its email feature
is enabled. A missing notification result is unconfirmed, not proof of delivery.
The final normal channel reply carries order_update. This avoids posting a second
duplicate confirmation through a separate Slack API call. Tracking and email
status update on explicit checks. With optional [Slack updates](order-updates.md),
signed Zinc events also post later changes to the original conversation. These
callbacks do not invoke the model or wallet. There is no periodic poller.

Subscriptions bind the order to MDA's verified Slack destination and resolved
Connection owner. Routes and event cursors are signed with an application key;
Zinc webhook secrets live in separate user-owned Connections. Setup preserves
another application's webhook endpoint instead of overwriting it. Registration
follows accepted submission, or a same-thread check of an existing order, with
one catch-up status read. Notification failures preserve the order result.

The shopping budget and payment amount are separate fields. Product selection
saves a cart with no payment amount. After the user chooses an upfront amount,
the helper binds Zinc's unpaid payment instructions to that amount and cart.
Human review fixes the amount before a Link request can be made. Expired unpaid
instructions refresh against the same cart; a changed amount, currency, network,
or private office prevents payment. Zinc currently supplies no exact final quote
through this route, so the chosen sum is explicitly described as upfront money
with an unused-balance refund, never as the price of the products.

Office, Zinc, and order-tracking reads use public `connections.get`. The CLI
session helper uses private MDA Agent Auth helpers for optional session reads
and writes, under MDA's resolved caller. A missing session starts CLI device
login. Using the public accessor there would instead pause for manual secret
entry before Link login could start. Permission and service errors still stop
the operation; the helper never uses another caller's session.

This is not an OAuth connector, an MCP server, or native sandbox proxy injection.
The CLI performs its device login and renewal, without requiring a new registered
OAuth application in the demo. The internal helper calls are covered against
the pinned MDA release and need review when upgrading.

An order uses one UUID as its Zinc idempotency key and Link request key. Before
submission, the code checks that the private order body still matches the reviewed
items, address, quantity, and chosen payment amount. It checks fresh Link approval again before
handing over the payment token. The token is read from a private sandbox file
inside the tool, then deleted. Only a public summary returns to the model.

MPP is the HTTP payment exchange. Zinc first returns a 402 payment challenge.
The tool asks Link for a token approved for that Stripe network and amount, then
returns it to Zinc in an encoded authorization header. The official `pympp`
library parses and formats these headers. The credential uses `payload.spt`,
matching the executable Link CLI and Stripe MPP specification. Bodyless discovery
can omit Stripe, so only the actual cart's matching challenge can authorize
continuing to the payment-review stage. See [payment failures](troubleshooting.md#payment-failures)
for recovery guidance.

Human approval is an actual LangGraph interrupt. It accepts only an explicit
approval decision from the client resume path. A model tool argument cannot
approve it. The demo assumes the caller is also the person authorizing
and paying. It does not implement a different manager's approval rights.

For a production workplace agent, replace the process-local locks with database
claims/leases, add a transactional notification queue and durable recovery, and define employee/manager
identity. The optional native Slack channel uses the current caller's wallet and
posts the Link URL to MDA's bound conversation before the wait starts. It does not
let the model choose a destination. A failed post returns the existing URL for a
normal reply instead of starting a wait the user cannot act on.

References:

- [MDA tools](https://docs.langchain.com/langsmith/python/managed-deep-agents-tools)
- [MDA Connections](https://docs.langchain.com/langsmith/python/managed-deep-agents-connections)
- [MDA sandboxes](https://docs.langchain.com/langsmith/python/managed-deep-agents-sandboxes)
- [Zinc MPP](https://www.zinc.com/docs/v2/mpp)
- [Zinc idempotency](https://www.zinc.com/docs/v2/api-reference/introduction/idempotency)
