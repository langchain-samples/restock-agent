# Restock

A Slack shopping agent built with [Managed Deep Agents](https://docs.langchain.com/langsmith/managed-deep-agents-quickstart),
[Zinc](https://www.zinc.com), and [Link](https://link.com).
Ask for office supplies or packaged food, choose a product, review the purchase
in Slack, and approve payment in Link. Zinc handles the retailer checkout.

> @Restock We're running low on pens. Can you find a 12-pack of blue ink pens
> for the office, under $25 altogether?

## Quickstart

This walkthrough uses **link-test**: real product search and Link approval, with
no purchase. Zinc search requests still use your Zinc account balance.
For a fictional run without Zinc or Link, see [rehearsal mode](#rehearsal-mode).

### 1. Get your accounts and keys

- **LangSmith:** a US-region workspace with Managed Deep Agents access, an API
  key, and the workspace ID. Start with the [MDA quickstart](https://docs.langchain.com/langsmith/managed-deep-agents-quickstart).
- **OpenAI:** an API key and access to the model you want to use.
- **Slack:** a workspace where you can install the agent. MDA handles the app setup.
- **Zinc:** create an account at [app.zinc.com](https://app.zinc.com), get an API
  key from the dashboard, and [fund its wallet](https://www.zinc.com/docs/v2/wallet)
  for product searches. Use a live key for real listings. This sample's
  [search endpoint](https://www.zinc.com/docs/v2/api-reference/search/cross-retailer)
  costs $0.01 per successful call and is billed separately from Link payments.
- **Link:** a US [Link account](https://app.link.com) with a saved payment method.
  If you have several, select a default. Restock will prompt you to connect it
  in chat. No Stripe developer account or separate OAuth app registration is
  required by this CLI login route.

### 2. Install

Install Python 3.11–3.14, [uv](https://docs.astral.sh/uv/getting-started/installation/),
and [Socket Firewall](https://github.com/SocketDev/sfw-free) (`sfw`), then:

```bash
git clone https://github.com/langchain-samples/restock-agent.git
cd restock-agent
sfw uv sync --frozen
uv run --no-sync python scripts/setup.py --slack
```

Run the remaining commands from the `restock-agent` directory. Setup creates a
runnable copy in `.local/app` and preserves existing settings and deployment
metadata when run again.

### 3. Configure and deploy

Open `.local/app/.env` in your editor and replace the placeholders below with
your own settings. Choose an OpenAI model your API key can access:

```dotenv
OPENAI_API_KEY=<your OpenAI API key>
OPENAI_MODEL=<an OpenAI model available to your account>
LANGSMITH_API_KEY=<your LangSmith API key>
LANGSMITH_WORKSPACE_ID=<your US workspace ID>
ZINC_API_KEY=<your Zinc API key>
RESTOCK_MODE=link-test
```

Keep this file private. Then deploy:

```bash
uv run --no-sync python scripts/preflight.py
sfw uv run mda deploy .local/app --name restock
```

Follow the Slack authorization link if one appears. An already-authorized
workspace may skip that step. MDA installs the bot; invite **Restock** to a
channel where you want to try it. Complete any MDA sign-in prompt.

### 4. Save the Zinc key and delivery details

Run these once, after deployment:

```bash
uv run --no-sync mda connections create restock-zinc --project .local/app --secret-from-env ZINC_API_KEY
uv run --no-sync python scripts/configure_office.py --project .local/app
```

The first command saves your Zinc key in an agent-owned Connection. The second
asks for the recipient's name, US delivery address, phone, and optional email,
and saves them in another Connection. Answers are visible as you type. Only the
office label appears in chat. Everyone using this deployment shares that destination.

You don't need to create a Link Connection manually. On first sign-in, the helper
creates a user-owned `link-session` Connection. Link CLI handles login and renewal;
the helper saves session updates for later conversations. Deployment installs Link
CLI in the sandbox, so you don't need it installed locally.

### 5. Try it in Slack

1. Send: **@Restock Find a 12-pack of blue ink pens for the office, under $25 altogether.**
2. Reply in the same thread: **The first one looks good. One pack.**
3. If asked to connect Link, open the sign-in URL, complete login, then reply **Done**.
4. When asked for a payment amount, choose it separately from the shopping budget.
   For this test, you can say **Let's approve $25 total.**
5. Click **Approve** on the Slack review, then open the Link URL and approve the
   **test** request.

Expected: a confirmation that approval completed **in test mode**, with nothing
purchased. The agent waits for
Link approval for up to two minutes. If that wait ends, reply **I approved it.
Can you check this order?** Keep replies in the same thread; add `@Restock` if a
reply doesn't trigger the agent.

## Make a real purchase

Set `RESTOCK_MODE=live` in `.local/app/.env` and redeploy:

```bash
sfw uv run mda deploy .local/app --name restock
```

Start a new Slack thread and repeat the flow for something you intend to buy.
The saved Link login is reused, but each order still needs Slack review and Link
payment approval. A test order cannot be converted into a live one.

Your shopping budget is a ceiling. You separately choose the payment amount,
which includes Zinc's fees and room for retailer tax and shipping. Listed prices
are not final checkout quotes. [Zinc documents refunds for unused amounts](https://www.zinc.com/docs/v2/mpp#automatic-refunds-stripe).

Restock reports **order_placed** only after Zinc confirms retailer checkout.
Processing can take time; ask to check the same order instead of placing another.
Tracking and shipment emails can arrive later. See [order updates](docs/order-updates.md)
for optional automatic Slack notifications and email behavior.

## Rehearsal mode

To try the conversation without Zinc or Link, follow steps 2–3 with only the
model and LangSmith settings, set `RESTOCK_MODE=rehearsal`, and skip step 4.
The same Slack prompts use fictional products and simulated payment approval.
Model calls still use your OpenAI account. For local Studio instead of Slack:

```bash
sfw uv run mda dev .local/app
```

## Change delivery details

Cancel any unpaid pending draft first, then run:

```bash
uv run --no-sync python scripts/configure_office.py --project .local/app --update
```

Re-enter all fields. Blank optional fields clear the old values. New orders use
the update immediately; no redeploy is needed. This does not reroute submitted orders.

## More details

- [Architecture and payment flow](docs/architecture.md)
- [Troubleshooting](docs/troubleshooting.md)
- [Shipping, email, and Slack updates](docs/order-updates.md)
- [Development and tests](CONTRIBUTING.md)

This sample supports US delivery, USD, one office per deployment, and the
requesting person's wallet. Food uses retailer shipping, not scheduled grocery
delivery. MDA 0.8.1 and Link CLI 0.22.0 are pinned. Concurrent production use
needs stronger coordination than the sample's process-local locks; see the architecture.

[MIT license](LICENSE). The Link session helper and CLI test fixtures are adapted
from [mda-link-integration](https://github.com/langchain-samples/mda-link-integration).
Dependencies retain their own licenses.
